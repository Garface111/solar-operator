"""WhatsApp group chat as a channel into the household's one shared thread.

The copilot holds a real seat in the family group — its own WhatsApp account on
its own number, driven by the Baileys sidecar in whatsapp-bridge/. The bridge
only moves bytes; everything that requires judgment happens here:

* WHO counts: only the household. Senders are matched by phone number
  (HOUSEHOLD_PHONES) or privacy LID (WHATSAPP_HOUSEHOLD_LIDS); everyone else in
  a group is ignored in silence, exactly like the email allowlist. Push names
  are display text anyone can set — never identity.
* WHEN to speak: mostly never. The group is two spouses living their life; the
  copilot is there to LISTEN for the money that flows through conversation
  ("just venmo'd the sitter $80") and log it, speaking only when addressed or
  when it has something material. GROUP_DYNAMICS + SILENCE do the restraint;
  log_expense does the ledger.

A poll cycle ingests every new spool line, stores household messages in the
shared ChatMessage history (same thread as web/SMS/email), and runs ONE agent
turn over the batch — people text in bursts, and five replies to five fragments
is how a bot gets removed from a group chat.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from .. import config
from ..agent import chat as agent_chat
from ..models import ChatMessage
from . import sms
from . import thread as shared_thread

log = logging.getLogger("bankai.whatsapp")

MAX_BATCH = 25
#: The bridge heartbeats status.json every ~2s; older than this = bridge down.
STATUS_STALE_SECONDS = 120


def data_dir() -> Path | None:
    return Path(config.WHATSAPP_DATA_DIR) if config.WHATSAPP_DATA_DIR else None


def configured() -> bool:
    return bool(
        config.WHATSAPP_ENABLED
        and data_dir()
        and (sms.household_phones() or household_lids())
    )


def household_lids() -> dict[str, str]:
    """Parse WHATSAPP_HOUSEHOLD_LIDS ('Ford:12309...,Partner:456...') -> {lid: name}."""
    out: dict[str, str] = {}
    for part in config.WHATSAPP_HOUSEHOLD_LIDS.split(","):
        if ":" in part:
            name, lid = part.split(":", 1)
            lid = "".join(ch for ch in lid if ch.isdigit())
            if name.strip() and lid:
                out[lid] = name.strip()
    return out


def _household_name_for_jid(jid: str, number_hint: str = "") -> str | None:
    """Household member behind a JID (phone form or privacy LID), or None."""
    number = sms.normalize_phone(number_hint)
    if not number and jid.endswith("@s.whatsapp.net"):
        number = sms.normalize_phone("+" + jid.split("@")[0].split(":")[0])
    if number:
        for name, known in sms.household_phones().items():
            if known == number:
                return name
    if jid.endswith("@lid"):
        lid = jid.split("@")[0].split(":")[0]
        return household_lids().get(lid)
    return None


def identify_sender(message: dict) -> str | None:
    """Map a spool line to a household member's name, or None if unknown.

    A from_me message was typed by whoever owns the paired account — that is
    WHATSAPP_ACCOUNT_OWNER in watch-only mode, and nobody (our own echo) when
    the copilot holds its own seat. For everyone else: phone number first
    (stable, shared with the SMS channel), then privacy LID. The push name is
    deliberately never consulted: it is whatever the sender typed into their
    own profile.
    """
    if message.get("from_me"):
        owner = config.WHATSAPP_ACCOUNT_OWNER.strip()
        return owner or None
    return _household_name_for_jid(
        str(message.get("sender_jid") or ""),
        message.get("sender_number") or "",
    )


def bridge_status() -> dict:
    """The bridge's own report of itself, with staleness made explicit."""
    base = data_dir()
    if base is None:
        return {"running": False, "detail": "WHATSAPP_DATA_DIR not set"}
    status_file = base / "status.json"
    if not status_file.exists():
        return {"running": False, "detail": "bridge has never started"}
    try:
        status = json.loads(status_file.read_text())
    except Exception:
        return {"running": False, "detail": "status.json unreadable"}
    import time

    age = time.time() - status_file.stat().st_mtime
    status["running"] = age < STATUS_STALE_SECONDS
    if not status["running"]:
        status["detail"] = f"bridge heartbeat is {int(age)}s old — process down?"
    return status


def _cursor_file(base: Path) -> Path:
    return base / "inbound.cursor"


def _read_new_lines(base: Path) -> list[dict]:
    """Spool lines past the cursor, advancing it only after a successful parse
    pass. The spool is append-only, so a byte offset is the whole cursor."""
    spool = base / "inbound.jsonl"
    if not spool.exists():
        return []
    try:
        offset = int(_cursor_file(base).read_text().strip() or 0)
    except FileNotFoundError:
        offset = 0
    except ValueError:
        offset = 0
    size = spool.stat().st_size
    if size < offset:  # spool was rotated/truncated — start over, do not crash
        offset = 0
    if size == offset:
        return []
    with spool.open("rb") as f:
        f.seek(offset)
        chunk = f.read()
    # Only complete lines move the cursor; a partially-flushed last line waits.
    end = chunk.rfind(b"\n")
    if end < 0:
        return []
    consumed = chunk[: end + 1]
    messages: list[dict] = []
    for raw in consumed.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            messages.append(json.loads(raw))
        except Exception:
            log.warning("unparseable spool line skipped: %.120s", raw)
    _cursor_file(base).write_text(str(offset + len(consumed)))
    return messages


def _wanted(message: dict) -> bool:
    """Is this chat the copilot's business at all?

    Groups: only the one pinned via WHATSAPP_GROUP_JID — on a person's own
    account every other group is their life, not the copilot's. Unpinned groups
    leave a discovery line in the log so pinning is a copy-paste, not a hunt.

    DMs: only conversations between household members — the DM partner must map
    to a household name. That single rule keeps the account owner's messages to
    anyone else (from_me in some other chat) out of the spool's reach.
    """
    chat_jid = str(message.get("chat_jid") or message.get("group_jid") or "")
    kind = message.get("chat_kind") or ("group" if chat_jid.endswith("@g.us") else "dm")
    if kind == "group":
        if not config.WHATSAPP_GROUP_JID:
            log.info(
                "group %s (%r) seen but not pinned — set WHATSAPP_GROUP_JID=%s to watch it",
                chat_jid, str(message.get("chat_subject") or "")[:40], chat_jid,
            )
            return False
        return chat_jid == config.WHATSAPP_GROUP_JID
    if message.get("from_me"):
        # The owner talking — but to whom? Only a household partner counts.
        partner = _household_name_for_jid(chat_jid)
        owner = config.WHATSAPP_ACCOUNT_OWNER.strip()
        return partner is not None and partner != owner
    return _household_name_for_jid(
        chat_jid, message.get("sender_number") or ""
    ) is not None


def _attributed(text: str) -> str:
    """Stamp the copilot's marker so a message on a spouse's account is never
    mistaken for the spouse. Idempotent — never double-stamps. Spacing is
    normalized here because config values are stripped on load."""
    marker = config.WHATSAPP_SEND_PREFIX.strip()
    text = (text or "").strip()
    if marker and not text.startswith(marker):
        return f"{marker} {text}"
    return text


def send_group_message(group_jid: str, text: str) -> None:
    """Queue one message for the bridge to deliver (atomic rename so the bridge
    never reads a half-written file). The copilot's attribution marker is added
    here, so every outbound path is stamped."""
    base = data_dir()
    if base is None:
        raise RuntimeError("WHATSAPP_DATA_DIR is not configured")
    outbox = base / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    name = f"msg-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}.json"
    tmp = outbox / (name + ".tmp")
    tmp.write_text(json.dumps({"to": group_jid, "text": _attributed(text)}))
    tmp.rename(outbox / name)


def poll_once(session: Session) -> dict:
    """Ingest new group messages, then run ONE turn over the whole batch."""
    if not configured():
        return {"status": "skipped", "detail": "whatsapp not configured"}
    base = data_dir()
    stored, ignored = 0, 0
    last_chat = ""
    for message in _read_new_lines(base)[:MAX_BATCH]:
        if not _wanted(message):
            continue
        if message.get("from_me") and not config.WHATSAPP_ACCOUNT_OWNER.strip():
            continue  # our own seat's echo — not a stranger, just us
        sender = identify_sender(message)
        if sender is None:
            # Silence for strangers, but leave the operator a trail: an
            # unmapped LID looks exactly like this, and the fix is one env var.
            log.info(
                "ignoring whatsapp message from unknown sender jid=%s number=%s push_name=%r",
                message.get("sender_jid"), message.get("sender_number"),
                str(message.get("push_name"))[:40],
            )
            ignored += 1
            continue
        text = (message.get("text") or "").strip()
        if not text:
            continue
        session.add(
            ChatMessage(channel="whatsapp", role="user", speaker=sender, content=text)
        )
        stored += 1
        last_chat = message.get("chat_jid") or message.get("group_jid") or last_chat
    if not stored:
        return {"status": "ok", "stored": 0, "ignored": ignored, "answered": 0}

    # Commit before the turn (durability + releases the SQLite write lock for
    # the MCP server process mid-turn — same discipline as every other channel).
    session.commit()

    history = shared_thread.build_history(session)
    reply = agent_chat.run_turn(session, history, channel="whatsapp")

    if agent_chat.is_silence(reply):
        # Heard, possibly logged an expense via tools, said nothing. Correct.
        return {"status": "ok", "stored": stored, "ignored": ignored,
                "answered": 0, "silent": 1}

    session.add(
        ChatMessage(channel="whatsapp", role="assistant", speaker="copilot", content=reply)
    )
    session.flush()
    if config.WHATSAPP_WATCH_ONLY:
        # A borrowed account never speaks. The reply still lives in the shared
        # thread (dashboard), and anything urgent goes out via email_household —
        # the addendum tells the turn so, this is just the enforcement.
        log.info("watch-only: reply kept in the thread, not sent to WhatsApp")
        return {"status": "ok", "stored": stored, "ignored": ignored,
                "answered": 0, "noted": 1}
    send_group_message(last_chat, reply)
    return {"status": "ok", "stored": stored, "ignored": ignored, "answered": 1}
