"""Email as a first-class channel into the household's one shared thread.

Either spouse emails the copilot's address and gets a reply addressed to BOTH of
them — a genuine three-way conversation where the copilot is a participant, not a
bot each person talks to alone. Replies carry In-Reply-To/References so Gmail
threads them, and everything lands in the same `ChatMessage` history as the
dashboard and SMS, so context is continuous across all three.

Security: ONLY addresses listed in HOUSEHOLD_EMAILS are answered. The inbox is a
public address — anyone can write to it — and the copilot can read this
household's entire financial picture, so unknown senders are ignored in silence
(never bounced, which would confirm the address is live). Note honestly that a
From header can be forged; this allowlist is the practical bar for a household
tool, not cryptographic proof of identity. Turn on DMARC-aware filtering or
require SMS confirmation before treating email as an authorization channel for
anything consequential.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

from sqlalchemy.orm import Session

from .. import config
from ..agent import chat as agent_chat
from ..connectors.email_harvest import connect_writable, send_email_message
from ..models import ChatMessage
from . import thread as shared_thread

log = logging.getLogger("bankai.email")

MAX_BODY_CHARS = 8000
MAX_MESSAGES_PER_POLL = 10

# "On Tue, Jul 28, 2026 at 9:41 AM Ford <f@x.com> wrote:" and friends — everything
# from here down is quoted history the copilot already has in its own thread.
_QUOTE_MARKERS = [
    re.compile(r"^\s*On .{5,120}\bwrote:\s*$", re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.M | re.I),
    re.compile(r"^\s*From:\s.+\bSent:\s", re.M | re.I),
    re.compile(r"^\s*_{10,}\s*$", re.M),
]


def household_emails() -> dict[str, str]:
    """Parse HOUSEHOLD_EMAILS ('Ford:ford@x.com,Sam:sam@x.com') -> {name: address}."""
    out: dict[str, str] = {}
    for part in config.HOUSEHOLD_EMAILS.split(","):
        if ":" in part:
            name, address = part.split(":", 1)
            address = address.strip().lower()
            if name.strip() and "@" in address:
                out[name.strip()] = address
    return out


def configured() -> bool:
    from ..connectors import email_harvest

    return email_harvest.configured() and bool(household_emails())


def identify_sender(from_header: str) -> str | None:
    """Map a From header to a household member's name, or None if unknown."""
    address = parseaddr(from_header or "")[1].strip().lower()
    if not address:
        return None
    for name, known in household_emails().items():
        if known == address:
            return name
    return None


def strip_quoted(text: str) -> str:
    """Keep only what this person just wrote, not the thread they replied to."""
    earliest = len(text)
    for pattern in _QUOTE_MARKERS:
        match = pattern.search(text)
        if match:
            earliest = min(earliest, match.start())
    body = text[:earliest]
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()[:MAX_BODY_CHARS]


def plain_body(msg) -> str:
    """Best-effort plain text from a message (prefers text/plain over HTML)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_content()
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                html = part.get_content()
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    try:
        return msg.get_content()
    except Exception:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _reply_subject(subject: str) -> str:
    subject = (subject or "").strip() or "your household copilot"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def build_reply(
    *, reply_text: str, subject: str, message_id: str, references: str,
    recipients: list[str],
) -> EmailMessage:
    """A reply addressed to the whole household, threaded under the original."""
    msg = EmailMessage()
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = _reply_subject(subject)
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = (references + " " + message_id).strip() if references else message_id
    msg.set_content(reply_text)
    return msg


def household_recipients() -> list[str]:
    """Every household member, so a reply is a three-way conversation rather than
    a private answer to whoever happened to write. Deliberately NOT derived from
    the original To/Cc: an outsider copied on the thread must never be mailed the
    household's financial picture."""
    return sorted(set(household_emails().values()))


def process_message(session: Session, parsed: dict) -> str | None:
    """Store one inbound household email, run a turn, and reply to everyone.
    Returns the reply text, or None when the sender is not household."""
    sender = identify_sender(parsed["from"])
    if not sender:
        log.info("ignoring email from unknown sender: %s", parsed["from"][:80])
        return None
    body = strip_quoted(parsed["body"])
    if not body:
        return None

    session.add(
        ChatMessage(channel="email", role="user", speaker=sender, content=body)
    )
    # Commit before the turn: durable even if the model call fails, and it frees
    # the SQLite write lock for the MCP server process mid-turn.
    session.commit()

    history = shared_thread.build_history(session)
    reply = agent_chat.run_turn(session, history, channel="email")

    session.add(
        ChatMessage(channel="email", role="assistant", speaker="copilot", content=reply)
    )
    session.flush()

    recipients = household_recipients()
    message = build_reply(
        reply_text=reply,
        subject=parsed.get("subject", ""),
        message_id=parsed.get("message_id", ""),
        references=parsed.get("references", ""),
        recipients=recipients,
    )
    send_email_message(message)
    return reply


def _parse(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    to_cc = [addr for _, addr in getaddresses(
        [str(msg.get("To", "")), str(msg.get("Cc", ""))]
    )]
    return {
        "from": str(msg.get("From", "")),
        "subject": str(msg.get("Subject", "")),
        "message_id": str(msg.get("Message-ID", "")),
        "references": str(msg.get("References", "")),
        "to_cc": to_cc,
        "body": plain_body(msg),
    }


def poll_once(session: Session) -> dict:
    """Read unseen mail, answer household senders, mark handled messages read."""
    if not configured():
        return {"status": "skipped", "detail": "email chat not configured"}
    conn = connect_writable()
    answered, ignored = 0, 0
    try:
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return {"status": "error", "detail": f"IMAP search: {status}"}
        ids = (data[0].split() if data and data[0] else [])[:MAX_MESSAGES_PER_POLL]
        for msg_id in ids:
            fetch_status, fetched = conn.fetch(msg_id, "(RFC822)")
            if fetch_status != "OK" or not fetched or fetched[0] is None:
                continue
            parsed = _parse(fetched[0][1])
            try:
                reply = process_message(session, parsed)
            except Exception:
                # Leave it UNSEEN so the next poll retries rather than losing it.
                log.exception("failed handling email from %s", parsed["from"][:80])
                continue
            if reply is None:
                ignored += 1
                continue
            answered += 1
            conn.store(msg_id, "+FLAGS", "\\Seen")
        return {"status": "ok", "answered": answered, "ignored": ignored}
    finally:
        try:
            conn.logout()
        except Exception:
            pass
