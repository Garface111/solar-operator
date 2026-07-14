"""Array Operator redesign + Energy Agent announcement.

Sends once to every Array Operator tenant with a deliverable email (testers
included), same recipient rules as cloud_capture_announce.

Scheduling: ``maybe_send_scheduled()`` on the backend cron. Fires ONCE at/after
``ENERGY_AGENT_ANNOUNCE_AT`` (ISO datetime), guarded by a KVFlag so a restart or
second tick never re-sends.
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

log = logging.getLogger("energy_agent_announce")

DASH_URL = "https://arrayoperator.com/"
ACCOUNT_URL = "https://arrayoperator.com/#account"
REPLY_TO = "admin@solaroperator.org"
SUBJECT = "Array Operator just got a major upgrade — new design + Energy Agent"
SENT_FLAG = "energy_agent_announce_sent"

# Comped design partner — usually held out of mass mail. Ford 2026-07-14: include
# Paul on THIS Energy Agent / redesign announce. Env ANNOUNCE_DENYLIST still works
# for others; ANNOUNCE_ALLOWLIST always wins (comma emails forced in if a tenant).
_DENY_EXACT = {e.strip().lower() for e in
               (os.environ.get("ANNOUNCE_DENYLIST") or "").split(",") if e.strip()}
# Always include Paul (and any extra allowlist) even if they appear on a denylist.
_ALLOW_EXACT = {e.strip().lower() for e in
                (os.environ.get("ANNOUNCE_ALLOWLIST") or "pbozuwa@gmail.com").split(",")
                if e.strip()}
# Undeliverable placeholder domains (demo/system rows), never real inboxes.
_PLACEHOLDER = re.compile(r"@(example\.com|energyagent-demo\.com|test\.com)$", re.I)


def _deliverable(t: Tenant) -> bool:
    email = (t.contact_email or "").strip().lower()
    if not email or "@" not in email or " " in email:
        return False
    if email in _ALLOW_EXACT:
        return True
    if email in _DENY_EXACT or _PLACEHOLDER.search(email):
        return False
    return True


def recipients() -> list[Tenant]:
    """Every Array Operator tenant with a deliverable email (testers + Paul)."""
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
    # If Paul has no AO tenant row but is allowlisted, still send a bare recipient.
    # (Tenant.contact_email is preferred when a real row exists.)
    for extra in sorted(_ALLOW_EXACT):
        if extra in seen:
            continue
        # Synthetic stand-in so send loop has an address (no name personalization).
        class _Extra:
            contact_email = extra
            name = "Paul"
            operator_name = "Paul"
            id = "allowlist:" + extra
        out.append(_Extra())  # type: ignore[arg-type]
        seen.add(extra)
    return out


def _first_name(t: Tenant) -> str:
    return ((getattr(t, "operator_name", None) or t.name or "").split(" ")[0]).strip()


def email_html(name: str) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    body = f"""
    <p>{greeting}</p>
    <p>We've shipped a big upgrade to <strong>Array Operator</strong> — a cleaner
    design, clearer offtaker invoicing, and something new we think you'll use every day:
    <strong>Energy Agent</strong>, a live voice + chat assistant built right into your
    dashboard.</p>

    <p style="margin:18px 0 8px;font-size:15px;font-weight:700;color:#0f172a;">
      What's new in the product
    </p>
    <ul>
      <li><strong>Fresh sky design</strong> — lighter, calmer, and easier to scan across
      Fleet Triage, Inverters, Analysis, Invoices, Resources, and Account.</li>
      <li><strong>Smarter offtaker invoicing</strong> — clearer bill sources, master solar
      credit rate, auto-refresh that respects cloud vs device capture, and a tighter review
      &amp; send pipeline.</li>
      <li><strong>Auto-refresh you can trust</strong> — keep passwords on your computer
      with the extension, or choose <em>Store it with us</em> for 24/7 cloud capture.
      Both paths feed the same live data and utility bills.</li>
    </ul>

    <p style="margin:22px 0 8px;font-size:15px;font-weight:700;color:#0f172a;">
      Meet Energy Agent
    </p>
    <p>Open the glowing orb on your dashboard (or the left dock) and talk or type.
    Energy Agent is scoped to <em>your</em> account only — it can help you operate the
    product without hunting through tabs.</p>
    <p><strong>What it can do today:</strong></p>
    <ul>
      <li><strong>Fleet health</strong> — what needs attention, why, and what to do next
      (using the same data as your live Inverters / Fleet Triage views).</li>
      <li><strong>Guided tours</strong> — walk Account, Invoices, Analysis, and more
      top-to-bottom while it highlights the real UI.</li>
      <li><strong>Offtaker help</strong> — list offtakers, explain share % and bill
      sources, and (with your confirmation) update share, email, auto-send, or rebind
      master / utility source without a hard page refresh.</li>
      <li><strong>Product knowledge</strong> — plain-English how Array Operator works:
      cloud vs device auto-refresh, how offtaker invoices are generated from utility
      bills, tabs and workflows.</li>
      <li><strong>Voice + text</strong> — GPT natural voice when you enable the mic, or
      type anytime. Weekly usage is metered (thinking + voice) with a simple fill bar;
      default allowance is $5/week so costs stay predictable.</li>
      <li><strong>Improve this site</strong> — mark up a change and send it through our
      judge pipeline for small UI fixes on your account.</li>
    </ul>
    <p><strong>What it will not do:</strong> change your Stripe plan, charge cards, or
    touch operator billing. Money-moving account billing stays with you.</p>

    <p>Open your dashboard, click the Energy Agent orb, and try something like:
    <em>&ldquo;What needs attention in my fleet?&rdquo;</em> or
    <em>&ldquo;Show me the Invoices tab.&rdquo;</em></p>

    <p>If anything feels off or you want a feature prioritized, just reply to this email
    or tell Energy Agent to escalate to Ford.</p>
    <p>&mdash; Ford, Array Operator</p>
    """
    return render_email_skin(
        preheader="New design + Energy Agent live chat/voice — your operator inside the dashboard.",
        headline="Array Operator just leveled up",
        intro_line="New design, clearer invoicing, and Energy Agent on your side.",
        body_html=body,
        cta={"label": "Open Array Operator", "url": DASH_URL},
        product="array_operator",
    )


def email_text(name: str) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    return render_email_skin_text(
        headline="Array Operator just leveled up",
        intro_line="New design, clearer invoicing, and Energy Agent on your side.",
        body_text=(
            f"{greeting}\n\n"
            "We've shipped a big upgrade to Array Operator — a cleaner design, clearer "
            "offtaker invoicing, and Energy Agent: a live voice + chat assistant built into "
            "your dashboard.\n\n"
            "WHAT'S NEW\n"
            "- Fresh sky design across Fleet Triage, Inverters, Analysis, Invoices, Resources, Account.\n"
            "- Smarter offtaker invoicing (bill sources, master rate, cloud/device auto-refresh).\n"
            "- Auto-refresh: keep passwords on your computer OR Store it with us for 24/7 cloud capture.\n\n"
            "ENERGY AGENT\n"
            "Open the glowing orb on your dashboard (or left dock). Talk or type — it only sees YOUR account.\n\n"
            "It can:\n"
            "- Explain fleet health and what needs attention\n"
            "- Give guided tours of the real UI\n"
            "- Help with offtakers (list, share %, bill source rebind) after you confirm\n"
            "- Explain how the product works (auto-refresh, invoice pipeline)\n"
            "- Voice + text; weekly usage metered (default $5 thinking+voice)\n"
            "- Propose small UI improvements via Improve\n\n"
            "It will NOT change Stripe plans, charge cards, or touch operator billing.\n\n"
            "Try: \"What needs attention in my fleet?\" or \"Show me the Invoices tab.\"\n\n"
            f"Open Array Operator: {DASH_URL}\n"
            f"Account / Auto-refresh: {ACCOUNT_URL}\n\n"
            "Reply to this email anytime, or ask Energy Agent to escalate to Ford.\n\n"
            "-- Ford, Array Operator"
        ),
        cta={"label": "Open Array Operator", "url": DASH_URL},
        product="array_operator",
    )


def send_announcement(send: bool = False) -> dict:
    """Render + (optionally) send to every recipient. send=False → dry run only."""
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
    log.info("energy agent announcement sent: %d/%d", ok, len(tos))
    return {"sent": True, "count": len(tos), "ok": ok, "recipients": emails}


def _announce_at() -> datetime | None:
    raw = (os.environ.get("ENERGY_AGENT_ANNOUNCE_AT") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("ENERGY_AGENT_ANNOUNCE_AT is not ISO-8601: %r", raw)
        return None


def maybe_send_scheduled() -> None:
    """Backend cron entrypoint. Sends ONCE at/after ENERGY_AGENT_ANNOUNCE_AT."""
    target = _announce_at()
    if target is None:
        return
    if datetime.now(timezone.utc) < target:
        return
    with SessionLocal() as db:
        if db.get(KVFlag, SENT_FLAG) is not None:
            return
        db.add(KVFlag(key=SENT_FLAG, value="claimed"))
        try:
            db.commit()
        except Exception:
            db.rollback()
            return
    log.info("firing scheduled Energy Agent announcement (target=%s)", target.isoformat())
    result = send_announcement(send=True)
    with SessionLocal() as db:
        row = db.get(KVFlag, SENT_FLAG)
        if row is not None:
            row.value = f"sent {result.get('ok', 0)}/{result.get('count', 0)} at {now().isoformat()}"
            db.commit()
