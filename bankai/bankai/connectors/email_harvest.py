"""Email document harvester — the copilot's reach into the household inbox.

Connects to Gmail (or any IMAP host) with an app password and hunts for the
documents that complete the household picture: trust and estate papers, deeds,
mortgage/closing documents, insurance policies, tax records, statements.
Matching attachments (PDF/Word/text) are pulled into the vault with full
provenance (sender, subject, date) and sha-deduped, so repeated sweeps are
idempotent. Search uses Gmail's own query syntax via the X-GM-RAW IMAP
extension, falling back to IMAP TEXT search on non-Gmail hosts.

Setup: Google account → 2-step verification on → myaccount.google.com/apppasswords
→ GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env. Read-only by usage: we never send,
delete, or mark mail.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from .. import config, vault
from ..models import SyncLog

# The standing sweep: financial/legal documents likely to arrive by mail.
DEFAULT_QUERY = (
    "has:attachment (filename:pdf OR filename:docx) "
    "(trust OR will OR estate OR deed OR escrow OR closing OR mortgage OR "
    "insurance OR policy OR 1099 OR w-2 OR w2 OR k-1 OR \"tax return\" OR "
    "statement OR appraisal OR title)"
)

DOC_EXTENSIONS = (".pdf", ".docx", ".txt", ".rtf", ".csv")
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_MESSAGES_PER_SWEEP = 60

_CATEGORY_HINTS = [
    (re.compile(r"trust|will|estate|executor|beneficiar", re.I), "estate"),
    (re.compile(r"deed|escrow|closing|mortgage|title|appraisal|hoa", re.I), "home"),
    (re.compile(r"insurance|policy|coverage|premium", re.I), "insurance"),
    (re.compile(r"1099|w-?2|k-?1|tax|irs|1040|return", re.I), "tax"),
    (re.compile(r"statement|account|brokerage", re.I), "financial"),
    (re.compile(r"agreement|contract|lease", re.I), "contract"),
]


def configured() -> bool:
    return bool(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)


def send_email(to: str, subject: str, body: str) -> str:
    """Send plain-text mail AS the household address (approved actions only —
    the approval gate lives in the portal, not here). Returns a receipt line."""
    if not configured():
        raise RuntimeError(
            "email is not connected — set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env"
        )
    msg = EmailMessage()
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    smtp_host = config.IMAP_HOST.replace("imap.", "smtp.", 1)
    with smtplib.SMTP(smtp_host, 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    return f"sent to {to} from {config.GMAIL_ADDRESS}"


def guess_category(text: str) -> str:
    for pattern, category in _CATEGORY_HINTS:
        if pattern.search(text):
            return category
    return "other"


def _connect() -> imaplib.IMAP4_SSL:
    if not configured():
        raise RuntimeError(
            "email is not connected — set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in "
            ".env (Google account → 2-step verification → App passwords)"
        )
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST)
    conn.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
    conn.select("INBOX", readonly=True)
    return conn


def _search_ids(conn: imaplib.IMAP4_SSL, query: str) -> list[bytes]:
    try:
        status, data = conn.search(None, "X-GM-RAW", f'"{query}"')
    except imaplib.IMAP4.error:
        # Non-Gmail host: crude fallback — full-text on the first quoted-out term.
        plain = re.sub(r'[()"]|filename:\S+|has:attachment', " ", query)
        first = next((w for w in plain.split() if w.upper() != "OR"), "document")
        status, data = conn.search(None, "TEXT", f'"{first}"')
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status}")
    ids = data[0].split() if data and data[0] else []
    return ids[-MAX_MESSAGES_PER_SWEEP:]


def _parse_message(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    attachments = []
    for part in msg.walk():
        filename = part.get_filename() or ""
        if not filename.lower().endswith(DOC_EXTENSIONS):
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload or len(payload) > MAX_ATTACHMENT_BYTES:
            continue
        attachments.append({"filename": filename, "data": payload})
    return {
        "from": str(msg.get("From", "")),
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "")),
        "attachments": attachments,
    }


def fetch_messages(query: str) -> list[dict]:
    """All matching messages (newest window), parsed with attachments."""
    conn = _connect()
    try:
        out = []
        for msg_id in _search_ids(conn, query):
            status, data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not data or data[0] is None:
                continue
            out.append(_parse_message(data[0][1]))
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def search_email(query: str, limit: int = 20) -> list[dict]:
    """Metadata-only recon: subject / sender / date / attachment names."""
    messages = fetch_messages(query)
    return [
        {
            "from": m["from"], "subject": m["subject"], "date": m["date"],
            "attachments": [a["filename"] for a in m["attachments"]],
        }
        for m in messages[-limit:]
    ]


def harvest(session: Session, query: str | None = None) -> dict:
    """Sweep the inbox and file matching attachments into the vault."""
    query = (query or "").strip() or DEFAULT_QUERY
    filed, skipped, results = 0, 0, []
    messages = fetch_messages(query)
    for m in messages:
        for att in m["attachments"]:
            hint_text = f"{att['filename']} {m['subject']}"
            doc, created = vault.add_document(
                session,
                filename=att["filename"],
                data=att["data"],
                title=None,
                category=guess_category(hint_text),
            )
            if created:
                filed += 1
                provenance = (
                    f"Harvested from email {m['date']}: \"{m['subject']}\" "
                    f"from {m['from']}. Not yet reviewed in detail."
                )
                doc.summary = provenance
                results.append(
                    {"title": doc.title, "category": doc.category,
                     "from": m["from"], "subject": m["subject"]}
                )
            else:
                skipped += 1
    session.add(
        SyncLog(
            source="email",
            status="ok",
            detail=f"messages={len(messages)} filed={filed} dupes={skipped}",
        )
    )
    session.flush()
    return {
        "messages_scanned": len(messages),
        "documents_filed": filed,
        "duplicates_skipped": skipped,
        "filed": results,
    }
