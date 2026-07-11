"""Cloud Capture launch announcement — the opt-in email to Array Operator account
holders, plus the fire-once scheduler job that sends it at a set time.

Ford: "send it to everyone who has an account, testers as well." So recipients =
every Array Operator tenant with a deliverable email (testers INCLUDED); only
undeliverable placeholder domains and the comped design partner are held out.

Scheduling: ``maybe_send_scheduled()`` runs on the backend cron. It sends ONCE,
at/after ``CLOUD_CAPTURE_ANNOUNCE_AT`` (ISO datetime), guarded by a KVFlag row so
a restart or a second cron tick can never re-send.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import KVFlag, Tenant, now
from ..email_skin import render_email_skin, render_email_skin_text
from ..notify import _send_via_resend

log = logging.getLogger("cloud_capture_announce")

ACCOUNT_URL = "https://arrayoperator.com/#account"
REPLY_TO = "admin@solaroperator.org"
SUBJECT = "A new way to keep your solar data fresh — Cloud Capture (opt-in)"
SENT_FLAG = "cloud_capture_announce_sent"

# Comped design partner — do not mass-mail (standing rule). Override by clearing.
_DENY_EXACT = {e.strip().lower() for e in
               (os.environ.get("ANNOUNCE_DENYLIST") or "pbozuwa@gmail.com").split(",") if e.strip()}
# Undeliverable placeholder domains (demo/system rows), never real inboxes.
_PLACEHOLDER = re.compile(r"@(example\.com|energyagent-demo\.com|test\.com)$", re.I)


def _deliverable(t: Tenant) -> bool:
    email = (t.contact_email or "").strip().lower()
    if not email or "@" not in email or " " in email:
        return False
    if email in _DENY_EXACT or _PLACEHOLDER.search(email):
        return False
    return True


def recipients() -> list[Tenant]:
    """Every Array Operator tenant with a deliverable email (testers included)."""
    with SessionLocal() as db:
        rows = db.execute(
            select(Tenant).where(Tenant.product == "array_operator")
        ).scalars().all()
    seen, out = set(), []
    for t in rows:
        if not _deliverable(t):
            continue
        email = t.contact_email.strip().lower()
        if email in seen:
            continue
        seen.add(email)
        out.append(t)
    return out


def _first_name(t: Tenant) -> str:
    return ((getattr(t, "operator_name", None) or t.name or "").split(" ")[0]).strip()


def email_html(name: str) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    body = f"""
    <p>{greeting}</p>
    <p>We built a new, optional way to keep your Array Operator data fresh:
    <strong>Cloud Capture</strong>.</p>
    <p>Until now, keeping your live production and utility bills up to date meant running our
    browser extension on your own computer. That still works — and your passwords stay on your
    device — but it only refreshes while a browser tab is open.</p>
    <p><strong>With Cloud Capture, you give us your portal logins once and we do the rest, on our
    servers, around the clock.</strong> Your live inverter data stays under five minutes old and
    your utility bills refresh on their own. No tab to keep open, no computer to leave running.</p>
    <p>It is entirely <strong>opt-in</strong>. If you would rather keep your passwords on your own
    device, just keep using the extension — nothing changes for you.</p>
    <p>If you do turn it on:</p>
    <ul>
      <li>Your passwords are <strong>encrypted</strong> and used only to sign in on your behalf to
      the portals you connect.</li>
      <li>You can turn any login off or delete it at any time.</li>
      <li>Automated sign-in stops itself if a password looks wrong, so we never lock you out.</li>
    </ul>
    <p>To try it, open <strong>Master Account &rarr; Auto-refresh</strong> and choose
    &ldquo;Store it with us.&rdquo;</p>
    <p>&mdash; Ford, Array Operator</p>
    """
    return render_email_skin(
        preheader="A new, hands-off way to keep your solar data fresh — opt-in.",
        headline="Introducing Cloud Capture",
        intro_line="Live data, no browser tab required.",
        body_html=body,
        cta={"label": "Open Master Account", "url": ACCOUNT_URL},
        product="array_operator",
    )


def email_text(name: str) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    return render_email_skin_text(
        headline="Introducing Cloud Capture",
        intro_line="Live data, no browser tab required.",
        body_text=(
            f"{greeting}\n\n"
            "We built a new, optional way to keep your Array Operator data fresh: Cloud Capture.\n\n"
            "Until now, keeping your data up to date meant running our browser extension on your own "
            "computer (your passwords stay on your device), and it only refreshes while a tab is "
            "open.\n\nWith Cloud Capture, you give us your portal logins once and we do the rest, on "
            "our servers, around the clock — live inverter data under five minutes old, utility "
            "bills refreshed on their own. No tab, no computer to leave running.\n\n"
            "It is entirely opt-in. Prefer to keep your passwords on your own device? Keep using the "
            "extension — nothing changes.\n\nIf you turn it on: your passwords are encrypted and used "
            "only to sign in on your behalf; you can remove any login anytime; and automated sign-in "
            "stops itself if a password looks wrong, so we never lock you out.\n\n"
            "Turn it on in Master Account -> Auto-refresh -> \"Store it with us\": " + ACCOUNT_URL +
            "\n\n-- Ford, Array Operator"
        ),
        cta={"label": "Open Master Account", "url": ACCOUNT_URL},
        product="array_operator",
    )


def send_announcement(send: bool = False) -> dict:
    """Render + (optionally) send to every recipient. Returns a summary. When
    send=False this is a dry run — it returns the recipient list and sends nothing."""
    tos = recipients()
    emails = [t.contact_email for t in tos]
    if not send:
        return {"sent": False, "count": len(tos), "recipients": emails}
    ok = 0
    for t in tos:
        if _send_via_resend(
            to=t.contact_email, subject=SUBJECT,
            html=email_html(_first_name(t)), text=email_text(_first_name(t)),
            reply_to=REPLY_TO, product="array_operator",
        ):
            ok += 1
        time.sleep(0.5)
    log.info("cloud capture announcement sent: %d/%d", ok, len(tos))
    return {"sent": True, "count": len(tos), "ok": ok, "recipients": emails}


def _announce_at() -> datetime | None:
    raw = (os.environ.get("CLOUD_CAPTURE_ANNOUNCE_AT") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("CLOUD_CAPTURE_ANNOUNCE_AT is not ISO-8601: %r", raw)
        return None


def maybe_send_scheduled() -> None:
    """Backend cron entrypoint. Sends ONCE, at/after CLOUD_CAPTURE_ANNOUNCE_AT,
    guarded by a KVFlag so a restart or a second tick never re-sends."""
    target = _announce_at()
    if target is None:
        return                                        # not scheduled
    if datetime.now(timezone.utc) < target:
        return                                        # not yet
    # Claim the fire-once flag transactionally BEFORE sending (so two overlapping
    # ticks can't both send). If the row already exists, someone sent it.
    with SessionLocal() as db:
        if db.get(KVFlag, SENT_FLAG) is not None:
            return
        db.add(KVFlag(key=SENT_FLAG, value="claimed"))
        try:
            db.commit()
        except Exception:                             # unique-violation race → someone else claimed
            db.rollback()
            return
    log.info("firing scheduled Cloud Capture announcement (target=%s)", target.isoformat())
    result = send_announcement(send=True)
    with SessionLocal() as db:
        row = db.get(KVFlag, SENT_FLAG)
        if row is not None:
            row.value = f"sent {result.get('ok', 0)}/{result.get('count', 0)} at {now().isoformat()}"
            db.commit()
