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


def identify_sender(message: dict) -> str | None:
    """Map a spool line to a household member's name, or None if unknown.

    Phone number first (stable, shared with the SMS channel), then privacy LID.
    The push name is deliberately never consulted: it is whatever the sender
    typed into their own profile.
    """
    number = sms.normalize_phone(message.get("sender_number") or "")
    if number:
        for name, known in sms.household_phones().items():
            if known == number:
                return name
    sender_jid = str(message.get("sender_jid") or "")
    if sender_jid.endswith("@lid"):
        lid = sender_jid.split("@")[0].split(":")[0]
        return household_lids().get(lid)
    return None


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
    if config.WHATSAPP_GROUP_JID:
        return message.get("group_jid") == config.WHATSAPP_GROUP_JID
    return True


def send_group_message(group_jid: str, text: str) -> None:
    """Queue one message for the bridge to deliver (atomic rename so the bridge
    never reads a half-written file)."""
    base = data_dir()
    if base is None:
        raise RuntimeError("WHATSAPP_DATA_DIR is not configured")
    outbox = base / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    name = f"msg-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}.json"
    tmp = outbox / (name + ".tmp")
    tmp.write_text(json.dumps({"to": group_jid, "text": text}))
    tmp.rename(outbox / name)


def poll_once(session: Session) -> dict:
    """Ingest new group messages, then run ONE turn over the whole batch."""
    if not configured():
        return {"status": "skipped", "detail": "whatsapp not configured"}
    base = data_dir()
    stored, ignored = 0, 0
    last_group = ""
    for message in _read_new_lines(base)[:MAX_BATCH]:
        if not _wanted(message):
            continue
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
        last_group = message.get("group_jid") or last_group
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
    send_group_message(last_group, reply)
    return {"status": "ok", "stored": stored, "ignored": ignored, "answered": 1}
