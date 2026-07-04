"""Watch Ford's Gmail for the Fronius / SMA inverter-API enrollment replies and
carry the linking process as far as it can safely go automatically.

Context: on 2026-07-04 we emailed Fronius (pv-support-usa@fronius.com) and
submitted the SMA developer-portal form asking for Query-API pricing/enablement
and SMA sandbox credentials (see HANDOFF_API_VERIFICATION.md). This job removes
the "watch the inbox" chore: every run it looks for a reply from either vendor
and does the next safe step.

What it does per vendor when a reply lands:
  • SMA — tries to extract sandbox client credentials from the reply, and if it
    has enough (client_id + client_secret [+ system_id]) it RUNS the real
    verification harness (scripts.verify_inverter_apis --vendor sma) and emails
    Ford the PASS/FAIL verdict. The adapter is then verified end-to-end; the only
    thing left is Ford's production-registration decision (terms), which this job
    deliberately does NOT take.
  • Fronius — emails Ford a heads-up with the reply excerpt and a best-effort read
    of whether US Query-API access is available + any pricing lines. Signing the
    order form is a paid commitment and stays Ford's/Bruce's call.

Hard limits (do not cross): no purchases, no terms acceptance, no credentials in
git. It only reads mail from the two vendor senders, only emails Ford himself,
and never sends anything outward.

Credential: a Gmail App Password at ~/.hermes/secrets/gmail_app_password (or
env GMAIL_APP_PASSWORD). Absent → the job logs "no credential, skipping" and
exits 0, so it's safe to schedule before the password exists.

    python -m scripts.watch_inverter_api_replies              # real run
    python -m scripts.watch_inverter_api_replies --dry-run    # detect + print, don't send/act
    python -m scripts.watch_inverter_api_replies --self-test  # prove parse+harness wiring offline
"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path

GMAIL_USER = os.environ.get("GMAIL_WATCH_USER", "ford.genereaux@gmail.com")
NOTIFY_TO = os.environ.get("GMAIL_WATCH_NOTIFY_TO", GMAIL_USER)
SECRET_FILE = Path(os.path.expanduser("~/.hermes/secrets/gmail_app_password"))
STATE_FILE = Path(os.path.expanduser("~/.hermes/state/inverter_api_watch.json"))
REPO_ROOT = Path(__file__).resolve().parent.parent

# Purpose-scoped sender matching. FROM is matched as a substring by IMAP; we keep
# it narrow to these vendors so the job never reads unrelated mail. When in doubt
# we'd rather notify Ford (cheap) than miss a reply (costly), so SMA also matches
# any sma.de sender and a subject/body sandbox hint.
FRONIUS_SENDERS = ["pv-support-usa@fronius.com", "@fronius.com"]
SMA_SENDERS = ["@sma.de", "@sma-service.com", "sunnyportal"]
SEARCH_SINCE_DAYS = 45


# ── credential + state ────────────────────────────────────────────────────────

def _app_password() -> str | None:
    if os.environ.get("GMAIL_APP_PASSWORD"):
        return os.environ["GMAIL_APP_PASSWORD"].strip()
    if SECRET_FILE.exists():
        pw = SECRET_FILE.read_text(encoding="utf-8").strip()
        return pw or None
    return None


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"processed_ids": [], "last_run": None}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── email parsing ─────────────────────────────────────────────────────────────

def _hdr(msg, name: str) -> str:
    raw = msg.get(name, "")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 — a malformed header must not crash the run
        return raw or ""


def _body_text(msg) -> str:
    """Best plain-text body; fall back to a crude HTML strip."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                    part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True)
        text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
        if msg.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        return text
    except Exception:  # noqa: BLE001
        return str(msg.get_payload())


def _classify(sender: str) -> str | None:
    s = sender.lower()
    if any(tok in s for tok in FRONIUS_SENDERS):
        return "fronius"
    if any(tok in s for tok in SMA_SENDERS):
        return "sma"
    return None


_AUTO_SUBJECT_HINTS = (
    "automatische antwort", "automatic reply", "auto-reply", "autoreply",
    "auto reply", "automatic response", "out of office", "abwesenheit",
    "delivery status notification", "undeliverable", "read receipt",
)
_AUTO_BODY_HINTS = (
    "we have received your email", "will respond as quickly as possible",
    "due to the high volume", "do not send a follow up", "this is an automated",
    "automatic reply", "out of office", "we will get back to you as soon as possible",
)


def _is_auto_reply(msg, subject: str, body: str) -> bool:
    """Filter vendor auto-acknowledgments / out-of-office / bounces so we only
    notify Ford on a REAL reply. The first Fronius contact returns an
    "Automatische Antwort — we'll respond in 2-3 days" that must not page him."""
    autosub = (msg.get("Auto-Submitted", "") or "").lower()
    if "auto" in autosub and autosub != "no":
        return True
    for h in ("X-Autoreply", "X-Autorespond"):
        if (msg.get(h, "") or "").lower() in ("yes", "true"):
            return True
    if (msg.get("Precedence", "") or "").lower() == "auto_reply":
        return True
    subj = subject.lower()
    if any(h in subj for h in _AUTO_SUBJECT_HINTS):
        return True
    low = body.lower()
    # body language alone is a weak signal — require two independent hints
    if sum(1 for h in _AUTO_BODY_HINTS if h in low) >= 2:
        return True
    return False


# ── SMA credential extraction ────────────────────────────────────────────────

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _grab(patterns: list[str], text: str) -> str | None:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def extract_sma_creds(text: str) -> dict:
    """Best-effort pull of sandbox credentials from an SMA reply. Returns a dict
    with any of client_id / client_secret / system_id it can confidently find."""
    out: dict[str, str] = {}
    cid = _grab([
        r"client[\s_-]?id[\"'\s:=]+([A-Za-z0-9._-]{8,})",
        r"clientId[\"'\s:=]+([A-Za-z0-9._-]{8,})",
    ], text)
    sec = _grab([
        r"client[\s_-]?secret[\"'\s:=]+([A-Za-z0-9._~-]{8,})",
        r"clientSecret[\"'\s:=]+([A-Za-z0-9._~-]{8,})",
    ], text)
    sysid = _grab([
        r"system[\s_-]?id[\"'\s:=]+(" + _UUID + r")",
        r"system[\s_-]?id[\"'\s:=]+([A-Za-z0-9._-]{6,})",
        r"plant[\s_-]?id[\"'\s:=]+([A-Za-z0-9._-]{6,})",
    ], text)
    if cid:
        out["client_id"] = cid
    if sec:
        out["client_secret"] = sec
    if sysid:
        out["system_id"] = sysid
    return out


def run_sma_harness(creds: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Run the real verification harness against the SMA sandbox with the parsed
    creds. Returns (passed, output). Requires client_id+client_secret+system_id."""
    need = ("client_id", "client_secret", "system_id")
    if not all(creds.get(k) for k in need):
        missing = [k for k in need if not creds.get(k)]
        return False, f"cannot run harness — missing {', '.join(missing)}"
    env = dict(os.environ, SMA_SANDBOX="1",
               SMA_CLIENT_ID=creds["client_id"],
               SMA_CLIENT_SECRET=creds["client_secret"],
               SMA_SYSTEM_ID=creds["system_id"])
    cmd = [str(REPO_ROOT / ".venv/bin/python"), "-m",
           "scripts.verify_inverter_apis", "--vendor", "sma"]
    if dry_run:
        return False, f"[dry-run] would run: {' '.join(cmd)} (creds redacted)"
    try:
        p = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env,
                           capture_output=True, text=True, timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        passed = "sma: PASS" in out
        return passed, out
    except Exception as exc:  # noqa: BLE001
        return False, f"harness run error: {exc}"


def _redact(text: str, creds: dict) -> str:
    for v in creds.values():
        if v and len(v) >= 6:
            text = text.replace(v, v[:3] + "…redacted")
    return text


# ── Fronius reply read ────────────────────────────────────────────────────────

def summarize_fronius(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ["not available", "not offered", "cannot offer", "unavailable in the us",
                              "not possible in the us", "no us"]):
        avail = "Reads as: US access likely NOT available (verify by reading the full reply)."
    elif any(k in low for k in ["available", "we can enable", "order form", "activate", "pricing", "per data point"]):
        avail = "Reads as: US access may be available / pricing provided (read the full reply)."
    else:
        avail = "Availability unclear from the text — read the full reply."
    price_lines = [ln.strip() for ln in text.splitlines()
                   if re.search(r"(price|pricing|per|€|\$|eur|usd|fee|cost|tier|package)", ln, re.IGNORECASE)]
    price = "\n".join(price_lines[:8]) if price_lines else "(no obvious price lines detected)"
    return f"{avail}\n\nPossible pricing lines:\n{price}"


# ── notification ──────────────────────────────────────────────────────────────

def send_notification(subject: str, body: str, app_pw: str, dry_run: bool = False) -> None:
    if dry_run or not app_pw:
        print(f"\n----- NOTIFICATION ({'dry-run' if dry_run else 'no-cred'}) -----")
        print("Subject:", subject)
        print(body)
        print("----- end -----\n")
        return
    msg = EmailMessage()
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_TO
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(GMAIL_USER, app_pw)
        s.send_message(msg)
    print(f"notified {NOTIFY_TO}: {subject}")


# ── main loop ─────────────────────────────────────────────────────────────────

def process_message(vendor: str, subject: str, body: str, when: str,
                    app_pw: str, dry_run: bool) -> None:
    if vendor == "sma":
        creds = extract_sma_creds(body)
        if creds.get("client_id") and creds.get("client_secret"):
            passed, out = run_sma_harness(creds, dry_run=dry_run)
            verdict = "✅ VERIFIED (sandbox PASS)" if passed else "⚠ ran but did NOT fully pass"
            note = (f"SMA replied ({when}) and I extracted sandbox credentials.\n\n"
                    f"Harness result: {verdict}\n\n"
                    f"{_redact(out, creds)[:2500]}\n\n"
                    "Next (yours): the SMA production app registration accepts commercial "
                    "terms — that's your call. Reply 'register SMA' and I'll walk it. "
                    "Nothing paid was done.")
            send_notification(f"[EnergyAgent] SMA sandbox creds arrived — {verdict}",
                             note, app_pw, dry_run)
        else:
            note = (f"SMA replied ({when}) but I couldn't confidently extract sandbox "
                    "credentials from it (they may be in an attachment or a portal link).\n\n"
                    f"Reply excerpt:\n{body[:2000]}\n\n"
                    "Paste the client_id / client_secret / system_id (or forward the mail) "
                    "and I'll run the verification.")
            send_notification("[EnergyAgent] SMA replied — creds need a human look",
                             note, app_pw, dry_run)
    elif vendor == "fronius":
        note = (f"Fronius replied ({when}) about the Solar.web Query API.\n\n"
                f"{summarize_fronius(body)}\n\n"
                f"Full reply:\n{body[:2500]}\n\n"
                "Next (yours + Bruce's): if US access is offered, signing the order form "
                "is a paid commitment on Bruce's Solar.web account — your decision. Once "
                "it's enabled and an AccessKey is generated, reply with it and I'll verify "
                "the adapter and wire the server-side pull.")
        send_notification("[EnergyAgent] Fronius replied re: Query API pricing/access",
                         note, app_pw, dry_run)


def run(dry_run: bool = False) -> int:
    app_pw = _app_password()
    if not app_pw:
        print("no Gmail app password (~/.hermes/secrets/gmail_app_password or "
              "$GMAIL_APP_PASSWORD) — skipping. Set it to activate the watcher.")
        return 0
    state = _load_state()
    processed = set(state.get("processed_ids", []))

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(GMAIL_USER, app_pw)
    except Exception as exc:  # noqa: BLE001
        print(f"IMAP login failed: {exc}")
        return 1

    since = (datetime.now(timezone.utc) - timedelta(days=SEARCH_SINCE_DAYS)).strftime("%d-%b-%Y")
    seen_uids: set[bytes] = set()
    found = 0
    try:
        imap.select("INBOX", readonly=True)
        for sender in FRONIUS_SENDERS[:1] + SMA_SENDERS:  # narrow, per-sender searches
            typ, data = imap.search(None, "SINCE", since, "FROM", f'"{sender}"')
            if typ != "OK" or not data or not data[0]:
                continue
            for uid in data[0].split():
                if uid in seen_uids:
                    continue
                seen_uids.add(uid)
                typ, raw = imap.fetch(uid, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                mid = _hdr(msg, "Message-ID") or f"uid-{uid.decode()}"
                if mid in processed:
                    continue
                frm = _hdr(msg, "From")
                vendor = _classify(frm)
                if not vendor:
                    continue
                subject = _hdr(msg, "Subject")
                when = _hdr(msg, "Date")
                body = _body_text(msg)
                if _is_auto_reply(msg, subject, body):
                    print(f"[{vendor}] skipped auto-reply — {subject!r}")
                    if not dry_run:
                        processed.add(mid)  # mark handled so we don't re-check it
                    continue
                print(f"[{vendor}] {when} — {subject!r}")
                process_message(vendor, subject, body, when, app_pw, dry_run)
                found += 1
                if not dry_run:
                    processed.add(mid)
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass

    if not found:
        print("no new vendor replies.")
    if not dry_run:
        state["processed_ids"] = sorted(processed)
        _save_state(state)
    return 0


def self_test() -> int:
    """Prove the parse + harness-invocation wiring offline (no Gmail, no send)."""
    print("== self-test: SMA credential extraction ==")
    sample = (
        "Hello, thank you for your interest. Here are your sandbox credentials:\n"
        "client_id: sandbox-abc123DEF456\n"
        "client_secret: s3cr3t~Value_9988\n"
        "system_id: 11111111-2222-3333-4444-555555555555\n"
        "Best regards, SMA Developer Support")
    creds = extract_sma_creds(sample)
    print("extracted:", {k: (v[:4] + "…") for k, v in creds.items()})
    assert creds.get("client_id") and creds.get("client_secret") and creds.get("system_id"), \
        "extraction failed"
    passed, out = run_sma_harness(creds, dry_run=True)
    print("harness (dry):", out)
    print("\n== self-test: Fronius summary ==")
    print(summarize_fronius("The Query API is available. Pricing is per data point, "
                            "tier 1 covers up to 50 systems at EUR X/month. Order form attached."))
    print("\n== self-test: notification render (dry) ==")
    process_message("sma", "Re: sandbox", sample, "today", app_pw="", dry_run=True)
    print("self-test OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                   help="detect + print, but don't send email, run the harness, or write state")
    ap.add_argument("--self-test", action="store_true",
                   help="offline wiring test (no Gmail, no send)")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
