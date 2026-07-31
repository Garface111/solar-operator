"""Inbound email over Resend — the receiving half of the household thread.

Resend exposes received mail as a pollable list (`GET /emails/inbound`), so the
copilot can be emailed without a mailbox, an app password, or a public webhook
URL. That matters here: this server listens on localhost, so a webhook was never
an option.

Two filters keep this correct and safe:

* BY ADDRESS — the same Resend account receives mail for the household's other
  agents (repairs@, sovereign@ ...). Only mail addressed to this copilot is ours;
  everything else belongs to another system and is left completely alone.
* BY SENDER — enforced upstream in messaging.email_thread against the household
  allowlist, because this inbox can be written to by anyone.

Processed ids live in their own table, so a restart never re-answers an email and
the first poll after setup adopts the existing backlog silently instead of
replying to months-old mail.
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime

import httpx
from sqlalchemy import String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .. import config
from ..models import Base, _uid

log = logging.getLogger("bankai.resend_inbound")

INBOUND_URL = "https://api.resend.com/emails/inbound"
MAX_PER_POLL = 25


class InboundEmail(Base):
    """One row per Resend inbound id we have already dealt with.

    Its own table rather than a column on an existing one: `create_all` adds new
    tables to a live database but never new columns, and this shipped after the
    household's database already held their real data.
    """

    __tablename__ = "inbound_emails"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("inb"))
    resend_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    #: answered | adopted (pre-existing at setup) | ignored (not for us / not household)
    outcome: Mapped[str] = mapped_column(String(20), default="answered")
    processed_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


def configured() -> bool:
    return bool(config.RESEND_API_KEY and inbound_address())


def inbound_address() -> str:
    """The address this copilot receives at (defaults to whatever it sends as)."""
    return (config.EMAIL_INBOUND_ADDRESS or config.EMAIL_FROM or "").strip().lower()


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.RESEND_API_KEY}"}


def list_inbound() -> list[dict]:
    resp = httpx.get(INBOUND_URL, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("data") or []


def fetch_inbound(resend_id: str) -> dict:
    resp = httpx.get(f"{INBOUND_URL}/{resend_id}", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def addressed_to_us(message: dict) -> bool:
    """Is this message for THIS copilot, or for one of the household's other
    agents sharing the same Resend account?"""
    ours = inbound_address()
    if not ours:
        return False
    everyone = [
        *(message.get("to") or []),
        *(message.get("cc") or []),
        *(message.get("bcc") or []),
    ]
    return any(ours in str(addr).lower() for addr in everyone)


def html_to_text(html: str) -> str:
    """Readable text from an HTML body, keeping paragraph breaks."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def body_text(message: dict) -> str:
    return (message.get("text") or "").strip() or html_to_text(message.get("html") or "")


def attachment_bytes(attachment: dict) -> bytes | None:
    """Attachment payloads arrive inline (base64) or as a link — support both,
    and never let one malformed attachment sink the whole message."""
    content = attachment.get("content")
    if isinstance(content, str) and content:
        try:
            return base64.b64decode(content)
        except Exception:
            return None
    if isinstance(content, dict) and content.get("type") == "Buffer":
        try:
            return bytes(content.get("data") or [])
        except Exception:
            return None
    url = attachment.get("url") or attachment.get("download_url")
    if url:
        try:
            resp = httpx.get(url, headers=_headers(), timeout=60)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            log.warning("attachment download failed: %s", exc)
    return None


def seen_ids(session: Session) -> set[str]:
    return set(session.execute(select(InboundEmail.resend_id)).scalars())


def mark(session: Session, resend_id: str, outcome: str) -> None:
    session.add(InboundEmail(resend_id=resend_id, outcome=outcome))
    session.flush()


def claim(session: Session, resend_id: str) -> bool:
    """Take exclusive ownership of an inbound email before answering it.

    `resend_id` is UNIQUE, so the insert itself is the lock: if a second worker
    (a stray duplicate server process, a manual poll racing the scheduler) tries
    to claim the same message, its insert fails and it backs off. Without this,
    two processes both answer and the household gets the reply twice.

    Committed immediately so the claim is visible to everyone at once.
    """
    try:
        session.add(InboundEmail(resend_id=resend_id, outcome="claimed"))
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False


def settle(session: Session, resend_id: str, outcome: str) -> None:
    """Record what actually happened to a message we claimed."""
    row = session.execute(
        select(InboundEmail).where(InboundEmail.resend_id == resend_id)
    ).scalar_one_or_none()
    if row:
        row.outcome = outcome
        session.flush()


def release(session: Session, resend_id: str) -> None:
    """Give a claim back after a failure, so the next poll can retry it."""
    row = session.execute(
        select(InboundEmail).where(InboundEmail.resend_id == resend_id)
    ).scalar_one_or_none()
    if row and row.outcome == "claimed":
        session.delete(row)
        session.commit()


def adopt_backlog(session: Session) -> int:
    """First run: record everything already in the inbox as handled, without
    answering. Nobody wants the copilot replying to a month of old mail the
    moment it is switched on."""
    count = 0
    for message in list_inbound():
        resend_id = message.get("id")
        if resend_id:
            mark(session, resend_id, "adopted")
            count += 1
    return count


def new_messages(session: Session) -> list[dict]:
    """Unprocessed inbound mail addressed to this copilot, oldest first."""
    already = seen_ids(session)
    fresh: list[dict] = []
    for message in list_inbound():
        resend_id = message.get("id")
        if not resend_id or resend_id in already:
            continue
        if not addressed_to_us(message):
            mark(session, resend_id, "ignored")  # another agent's mail
            continue
        fresh.append(message)
    fresh.sort(key=lambda m: str(m.get("created_at") or ""))
    return fresh[:MAX_PER_POLL]
