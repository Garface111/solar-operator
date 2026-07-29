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
from ..connectors import attachments, email_harvest, resend_inbound
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
    """Parse HOUSEHOLD_EMAILS ('Ford:ford@x.com,Partner:partner@example.com') -> {name: address}."""
    out: dict[str, str] = {}
    for part in config.HOUSEHOLD_EMAILS.split(","):
        if ":" in part:
            name, address = part.split(":", 1)
            address = address.strip().lower()
            if name.strip() and "@" in address:
                out[name.strip()] = address
    return out


def configured() -> bool:
    """Ready when it can receive, send, and knows who the household is.

    Receiving works two ways: Resend's pollable inbound list (preferred — no
    mailbox credential at all) or IMAP against a real mailbox.
    """
    can_receive = resend_inbound.configured() or email_harvest.configured()
    return can_receive and email_harvest.can_send() and bool(household_emails())


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


def threading_headers(message_id: str, references: str) -> dict[str, str]:
    """Headers that keep the reply inside the same Gmail conversation, so the
    household sees one running thread rather than a pile of separate emails."""
    if not message_id:
        return {}
    return {
        "In-Reply-To": message_id,
        "References": f"{references} {message_id}".strip() if references else message_id,
    }


def send_reply(
    *, reply_text: str, subject: str, message_id: str, references: str,
    recipients: list[str],
) -> str:
    """One reply to the whole household, threaded under the original message."""
    return email_harvest.send_message(
        to=recipients,
        subject=_reply_subject(subject),
        text=reply_text,
        headers=threading_headers(message_id, references),
    )


def household_recipients() -> list[str]:
    """Every household member, so a reply is a three-way conversation rather than
    a private answer to whoever happened to write. Deliberately NOT derived from
    the original To/Cc: an outsider copied on the thread must never be mailed the
    household's financial picture."""
    return sorted(set(household_emails().values()))


def start_thread(session: Session, subject: str, body: str) -> dict:
    """Open a new email thread addressed to the whole household.

    Only ever to the household itself — this is the copilot talking to the people
    it works for, the same audience as the SMS broadcast and the rules engine.
    Anything aimed at an outside party goes through propose_action and the
    dashboard's approval gate instead.

    The message is recorded in the shared thread first, so what the copilot said
    by email is part of the same conversation as the dashboard and SMS, and a
    reply lands with its own words already in context.
    """
    recipients = household_recipients()
    if not recipients:
        raise RuntimeError("HOUSEHOLD_EMAILS is not configured — nobody to write to")

    session.add(
        ChatMessage(
            channel="email", role="assistant", speaker="copilot",
            content=f"[email to the household — {subject}]\n\n{body}",
        )
    )
    session.commit()

    receipt = email_harvest.send_message(to=recipients, subject=subject, text=body)
    return {"sent": True, "to": recipients, "subject": subject, "receipt": receipt}


def _describe_attachments(results: list[dict]) -> str:
    """Tell the turn what actually became of each file, in its own words.

    Stating the outcome plainly is what stops the copilot from thanking someone
    for data it failed to read, or claiming an import that never happened.
    """
    lines = ["[Files arrived with this email:]"]
    for r in results:
        name = r.get("filename", "a file")
        if r.get("imported"):
            line = (
                f"- {name}: IMPORTED into '{r['account']}' — {r['added']} new "
                f"transactions, {r['duplicates_skipped']} already known."
            )
            if r.get("balance_unknown"):
                line += f" BUT: {r['note']}"
            else:
                line += " The balances now reflect it; confirm what you can see."
            lines.append(line)
        elif r.get("needs_account"):
            lines.append(
                f"- {name}: saved to the vault, but it looks like a statement and "
                f"nothing says which account it belongs to. ASK which account, "
                f"then import it. Do not guess."
            )
        elif r.get("import_error"):
            lines.append(
                f"- {name}: saved to the vault, but its transactions could NOT be "
                f"read ({r['import_error']}). Say so plainly and ask for another "
                f"export rather than implying the data is in."
            )
        else:
            lines.append(
                f"- {name}: filed in the vault (document_id {r.get('document_id')}). "
                f"Read it and annotate_document with what it says."
            )
    return "\n".join(lines)


def process_message(session: Session, parsed: dict) -> str | None:
    """Store one inbound household email, run a turn, and reply to everyone.
    Returns the reply text, or None when the sender is not household."""
    sender = identify_sender(parsed["from"])
    if not sender:
        log.info("ignoring email from unknown sender: %s", parsed["from"][:80])
        return None
    body = strip_quoted(parsed["body"])
    files = parsed.get("attachments") or []
    # Someone forwarding a statement often writes nothing at all. Silence plus an
    # attachment is still a message, and answering nothing would look broken.
    if not body and not files:
        return None
    if files:
        body = (body + "\n\n" + _describe_attachments(files)).strip()

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

    send_reply(
        reply_text=reply,
        subject=parsed.get("subject", ""),
        message_id=parsed.get("message_id", ""),
        references=parsed.get("references", ""),
        recipients=household_recipients(),
    )
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


def poll_resend(session: Session) -> dict:
    """Answer new household mail from Resend's inbound list.

    Attachments ride along into the vault — emailing the copilot a document is
    the least-friction way to hand it something.
    """

    if not resend_inbound.seen_ids(session):
        adopted = resend_inbound.adopt_backlog(session)
        return {"status": "initialized", "adopted": adopted}

    answered, ignored, filed, imported = 0, 0, 0, 0
    for summary in resend_inbound.new_messages(session):
        resend_id = summary["id"]
        # Claim first: the reply must go out exactly once even if two workers
        # are polling. A lost claim means someone else already has this one.
        if not resend_inbound.claim(session, resend_id):
            continue
        try:
            message = resend_inbound.fetch_inbound(resend_id)
        except Exception:
            log.exception("could not fetch inbound %s", resend_id)
            resend_inbound.release(session, resend_id)  # retry on the next poll
            continue
        sender = identify_sender(message.get("from", ""))
        if not sender:
            log.info("ignoring email from unknown sender: %s", str(message.get("from"))[:80])
            resend_inbound.settle(session, resend_id, "ignored")
            ignored += 1
            continue

        attachment_results = []
        for attachment in message.get("attachments") or []:
            data = resend_inbound.attachment_bytes(attachment)
            filename = attachment.get("filename") or attachment.get("name") or ""
            if not data or not filename:
                continue
            try:
                outcome = attachments.handle(
                    session,
                    filename=filename,
                    data=data,
                    sender=sender,
                    subject=message.get("subject", ""),
                    category=email_harvest.guess_category(
                        f"{filename} {message.get('subject', '')}"
                    ),
                )
                attachment_results.append(outcome)
                filed += int(bool(outcome.get("filed")))
                imported += int(bool(outcome.get("imported")))
            except Exception:
                log.exception("could not handle attachment %s", filename)

        parsed = {
            "from": message.get("from", ""),
            "subject": message.get("subject", ""),
            "message_id": message.get("message_id", ""),
            "references": message.get("references", "") or "",
            "body": resend_inbound.body_text(message),
            # The turn is told what actually happened to the files, so it can
            # confirm an import, ask which account an ambiguous export belongs
            # to, or admit a file could not be read — rather than assuming.
            "attachments": attachment_results,
        }
        try:
            reply = process_message(session, parsed)
        except Exception:
            log.exception("failed handling email %s", resend_id)
            resend_inbound.release(session, resend_id)  # retried on the next poll
            continue
        resend_inbound.settle(session, resend_id, "answered" if reply else "ignored")
        answered += int(bool(reply))
    return {"status": "ok", "answered": answered, "ignored": ignored,
            "documents_filed": filed, "statements_imported": imported}


def poll_once(session: Session) -> dict:
    """Check for new household mail. Resend inbound when available, else IMAP."""
    if not configured():
        return {"status": "skipped", "detail": "email chat not configured"}
    if resend_inbound.configured():
        return poll_resend(session)
    conn = email_harvest.connect_writable()
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
