"""
Notification helpers — welcome emails to customers + internal alerts to us.

For MVP: uses simple SMTP via Resend.com (free tier 100 emails/day) OR falls back
to logging if no email credentials are configured. Wire up RESEND_API_KEY later.

Resend is chosen because: 1 API call, no OAuth dance, no Gmail rate limits,
free for our volume.
"""
from __future__ import annotations
import os
import logging
import json
import urllib.request
import urllib.error

from .email_skin import render_email_skin, render_email_skin_text, link_color
from . import branding

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_ADDRESS = os.getenv("MAIL_FROM", "Solar Operator <admin@solaroperator.org>")
INTERNAL_ALERT_TO = os.getenv("INTERNAL_ALERT_TO", "ford.genereaux@dysonswarmtechnologies.com")
EXTENSION_INSTALL_URL = os.getenv(
    "EXTENSION_INSTALL_URL",
    "https://chromewebstore.google.com/detail/solar-operator-sync"
)


def _send_via_resend(to: str, subject: str, html: str, text: str | None = None,
                     attachments: list[dict] | None = None,
                     from_addr: str | None = None,
                     reply_to: str | None = None,
                     product: str = "nepool",
                     log_failures: bool = True) -> bool:
    """Returns True on success, False otherwise. Uses the official Resend
    SDK so we play nice with their Cloudflare bot rules.

    from_addr overrides the platform default From header (V2 "send as me").
    reply_to sets a Reply-To so replies still reach a tenant even when we fell
    back to the platform From for an unverified domain."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — logging email instead of sending.")
        logger.info("EMAIL → to=%s subject=%s\n%s", to, subject, text or html[:500])
        return False

    try:
        import resend
        resend.api_key = RESEND_API_KEY
    except ImportError as e:
        logger.error("resend SDK not installed: %s", e)
        _send_via_resend._last_error = f"ImportError: {e}"
        return False

    params: dict = {
        "from": from_addr or branding.from_address(product),
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "html": html,
    }
    if text:
        params["text"] = text
    # Always set a Reply-To to the monitored support inbox so a brand-domain
    # From (whose mailbox may not receive) never strands a customer reply.
    # An explicit reply_to (e.g. report "send as me") is respected as-is.
    params["reply_to"] = reply_to or branding.reply_to_address(product)
    if attachments:
        params["attachments"] = attachments

    try:
        result = resend.Emails.send(params)
        # result is a dict like {"id": "xxx"}
        if result and result.get("id"):
            _send_via_resend._last_error = None
            return True
        (logger.error if log_failures else logger.warning)(
            "Resend returned unexpected response: %s", result)
        _send_via_resend._last_error = f"Unexpected response: {result}"
        return False
    except Exception as e:
        (logger.error if log_failures else logger.warning)(
            "Resend send failed: %s: %s", type(e).__name__, e)
        _send_via_resend._last_error = f"{type(e).__name__}: {e}"
        return False


def send_workbook_email(to: str, subject: str, html: str, text: str,
                        workbook_path: str, filename: str | None = None,
                        from_addr: str | None = None,
                        reply_to: str | None = None) -> bool:
    """Send a workbook as a base64-encoded attachment via Resend.

    When from_addr is given (V2 "send as me") and the send fails — the usual
    cause is Resend refusing an unverified sender domain — we retry ONCE from
    the platform default address, preserving the tenant address as Reply-To so
    the client's reply still reaches them. Delivery beats vanity."""
    import base64, pathlib as _p
    p = _p.Path(workbook_path)
    if not p.exists():
        logger.error("Workbook missing: %s", workbook_path)
        return False
    encoded = base64.b64encode(p.read_bytes()).decode()
    attachments = [{"filename": filename or p.name, "content": encoded}]

    # Wrap the operator's rendered body in the skin. The skin provides the brand
    # header/footer + a visible attachment chip; the operator's template HTML
    # is the entire body content.  Do NOT pass the subject as intro_line — the
    # subject is already visible in the recipient's inbox row and repeating it
    # as a subhead reads as a glitch (Ford Jun 8'26 fix).
    wrapped_html = render_email_skin(
        preheader="Your quarterly solar generation report is attached.",
        headline="NEPOOL Operator",
        intro_line="",  # falls back to brand tagline
        body_html=html,
        attachment_label=filename or p.name,
        attachment_size_bytes=p.stat().st_size,
    )
    wrapped_text = render_email_skin_text(
        headline="NEPOOL Operator",
        intro_line="",
        body_text=text,
        attachment_label=filename or p.name,
    )

    # No "send as me" identity — use the platform brand From directly.
    if not from_addr:
        return _send_via_resend(
            to=to, subject=subject, html=wrapped_html, text=wrapped_text,
            attachments=attachments, from_addr=None, reply_to=reply_to,
        )

    # A real custom domain MIGHT be a verified sender — try it once. Its failure
    # is expected/recoverable (we retry below), so don't log it as an error. A
    # free-mail From (gmail/yahoo/...) can NEVER be verified, so we skip straight
    # to the platform send rather than fail + spam Sentry on every report.
    if not _is_free_mail(from_addr):
        ok = _send_via_resend(
            to=to, subject=subject, html=wrapped_html, text=wrapped_text,
            attachments=attachments, from_addr=from_addr, reply_to=reply_to,
            log_failures=False,
        )
        if ok:
            return True
        logger.warning(
            "Custom From %r failed (unverified domain?) — retrying from platform "
            "default with Reply-To preserved.", from_addr)

    # Platform send: keep the operator's NAME ("Op via NEPOOL Operator") and their
    # address as Reply-To so the client's reply still reaches them. Delivery beats
    # vanity. A failure HERE is a real outage on a verified domain — log it.
    op_name = _name_part(from_addr)
    fallback_from = (
        f'"{op_name} via NEPOOL Operator" <{_addr_only(FROM_ADDRESS)}>'
        if op_name else FROM_ADDRESS
    )
    fallback_reply = reply_to or _addr_only(from_addr)
    return _send_via_resend(
        to=to, subject=subject, html=wrapped_html, text=wrapped_text,
        attachments=attachments, from_addr=fallback_from, reply_to=fallback_reply,
    )


def _addr_only(from_header: str) -> str:
    """Extract the bare address from a 'Name <addr@x>' header."""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<", 1)[1].split(">", 1)[0].strip()
    return from_header.strip()


def _name_part(from_header: str) -> str:
    """Extract the display name from a 'Name <addr@x>' header, or empty string."""
    if "<" in from_header:
        return from_header.split("<", 1)[0].strip().strip('"').strip("'")
    return ""


# Free-mail providers can never be verified as a Resend *sending* domain, so a
# "send as me" From on one of these is a guaranteed rejection. We skip the doomed
# attempt and send from the platform default instead (operator kept as Reply-To)
# rather than fail + log an error on every report.
_FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "yahoo.co.uk",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "zoho.com",
}


def _is_free_mail(from_header: str | None) -> bool:
    if not from_header:
        return False
    addr = _addr_only(from_header).lower()
    return "@" in addr and addr.rsplit("@", 1)[1] in _FREE_MAIL_DOMAINS


# ─── customer-facing ─────────────────────────────────────────────────────

PLAN_LABELS = {"standard": "NEPOOL Operator", "comped": "NEPOOL Operator (comped)",
               "solo": "Solo", "manager": "Manager", "operator": "Operator"}


def _next_quarterly_date() -> str:
    """Return the next Jan 1/Apr 1/Jul 1/Oct 1 as a human date, e.g. 'Oct 1, 2026'."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    year = now.year
    candidates = [(year, 1), (year, 4), (year, 7), (year, 10),
                  (year + 1, 1)]
    for y, m in candidates:
        d = datetime(y, m, 1, 9, 0, tzinfo=timezone.utc)
        if d > now:
            return d.strftime(f"%b {d.day}, {d.year}")
    return "the next quarter"


def send_welcome_email(to: str, name: str, tenant_key: str, plan: str) -> bool:
    import html as _html
    plan_label = PLAN_LABELS.get(plan, "NEPOOL Operator")
    first = _html.escape(name.split()[0] if name else "there")
    install_url = EXTENSION_INSTALL_URL
    next_q = _next_quarterly_date()

    body_html = (
        f"<p>Your NEPOOL Operator account is live on the <strong>{_html.escape(plan_label)}</strong> plan. "
        f"You're a few minutes from automatic quarterly reporting.</p>"
        f"<p><strong>Setup, in three steps:</strong></p>"
        f'<ol style="padding-left:20px;margin:14px 0;">'
        f'<li style="margin:10px 0;"><strong>Install the Chrome extension</strong> — '
        f'<a href="{install_url}" style="color:#047857;">Add NEPOOL Operator Sync to Chrome</a></li>'
        f'<li style="margin:10px 0;"><strong>The extension auto-pairs with your account</strong> — '
        f'open your <a href="https://nepooloperator.com/accounts" style="color:#047857;">NEPOOL Operator dashboard</a> '
        f"once after installing and the extension links itself automatically. No codes to copy.</li>"
        f'<li style="margin:10px 0;"><strong>Sign into your utility portal once</strong> — visit '
        f'<a href="https://greenmountainpower.com" style="color:#047857;">greenmountainpower.com</a> '
        f"(or Vermont Electric Co-op), log in with the extension active. We capture the rest.</li>"
        f"</ol>"
        f'<p style="margin-top:24px;background:#ecfdf5;border-radius:4px;padding:16px 20px;font-size:14px;color:#3a5a42;">'
        f"<strong>What happens next:</strong><br>"
        f"Once you finish setup, the extension will pull your utility bills automatically "
        f"every 6 hours in the background. Your first quarterly NEPOOL-GIS report goes "
        f"out on <strong>{next_q}</strong> — you don't need to do anything between now and then. "
        f"If you ever want to trigger a report early, there's a \"Send a report now\" button in your dashboard."
        f"</p>"
        f'<p style="margin-top:24px;background:#faedd8;border-radius:4px;padding:14px 18px;font-size:13px;color:#6b4423;border:1px solid #e6d4bd;">'
        f"<strong>Setting up on a new device later?</strong> If auto-pairing doesn't trigger, "
        f"open the extension, click \"Enter code manually,\" and paste your activation code: "
        f'<span style="font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:700;">{tenant_key}</span>'
        f"</p>"
        f'<p style="margin-top:24px;color:#3a5a42;font-size:14px;">'
        f"Questions? Just reply — we read every email and respond same business day."
        f"</p>"
        f"<p style=\"margin-top:24px;\">— The NEPOOL Operator team</p>"
    )
    body_text = (
        f"Your account is live on the {plan_label} plan.\n\n"
        f"Setup (3 steps):\n\n"
        f"  1. Install the Chrome extension: {install_url}\n"
        f"  2. Open your NEPOOL Operator dashboard (nepooloperator.com/accounts) — the\n"
        f"     extension auto-pairs with your account. No codes to copy.\n"
        f"  3. Sign into your utility portal once (greenmountainpower.com or Vermont\n"
        f"     Electric Co-op). The extension captures the rest.\n\n"
        f"What happens next:\n"
        f"  Once set up, the extension pulls your utility bills every 6 hours in the\n"
        f"  background. Your first quarterly report goes out on {next_q}. You\n"
        f"  don't need to do anything between now and then.\n\n"
        f"Setting up on a new device later? If auto-pairing doesn't trigger, open the\n"
        f"extension, click \"Enter code manually,\" and paste your activation code:\n"
        f"  {tenant_key}\n\n"
        f"Questions? Just reply.\n\n"
        f"— The NEPOOL Operator team"
    )
    html = render_email_skin(
        preheader="Your account is ready — 3 steps to go live.",
        headline="NEPOOL Operator",
        intro_line=f"Welcome aboard, {first}.",
        body_html=body_html,
        cta={"label": "Install the Chrome extension", "url": install_url},
    )
    text = render_email_skin_text(
        headline="NEPOOL Operator",
        intro_line=f"Welcome aboard, {name.split()[0] if name else 'there'}.",
        body_text=body_text,
        cta={"label": "Install the Chrome extension", "url": install_url},
    )
    return _send_via_resend(
        to=to,
        subject="Welcome to NEPOOL Operator — your activation code",
        html=html,
        text=text,
    )


def send_sample_workbook_email(to: str, name: str,
                               dashboard_url: str = "https://nepooloperator.com/accounts") -> bool:
    """Email the generic demo workbook so a new operator sees what their
    quarterly reports will look like. Generates a fresh sample to a temp file
    and attaches it. Best-effort: returns False (and logs) on any failure."""
    import tempfile, pathlib as _p
    import html as _html
    # Deferred import keeps notify.py import-light and avoids any writer/db
    # import cost for the (common) code paths that never send this email.
    from .writers.demo_writer import build_demo_workbook

    first = _html.escape(name.split()[0] if name else "there")

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>Welcome aboard! Attached is a <strong>sample report</strong> so you can see "
        f"exactly what we'll send your clients each quarter — a pixel-perfect NEPOOL-GIS "
        f"generation workbook, one sheet per array, covering the last six complete "
        f"quarters with monthly MWh and REC counts.</p>"
        f"<p>This particular file uses made-up \"Demo Array\" data, but once your Green "
        f"Mountain Power bills sync through the Chrome extension, your real arrays will "
        f"appear automatically and go out on the schedule you choose. You can manage "
        f"everything — clients, schedule, and recipients — from your "
        f'<a href="{dashboard_url}" style="color:#047857;">dashboard</a>.</p>'
        f'<p style="margin-top:24px;color:#3a5a42;font-size:14px;">Questions? Just reply — we read every email.</p>'
        f"<p style=\"margin-top:24px;\">— The NEPOOL Operator team</p>"
    )
    body_text = (
        f"Hi {name.split()[0] if name else 'there'},\n\n"
        f"Welcome aboard! Attached is a sample report so you can see exactly what we'll\n"
        f"send your clients each quarter — a pixel-perfect NEPOOL-GIS generation workbook,\n"
        f"one sheet per array, covering the last six complete quarters with monthly MWh\n"
        f"and REC counts.\n\n"
        f"This file uses made-up \"Demo Array\" data, but once your Green Mountain Power\n"
        f"bills sync through the Chrome extension, your real arrays appear automatically\n"
        f"and go out on the schedule you choose.\n\n"
        f"Manage everything at {dashboard_url}\n\n"
        f"Questions? Just reply.\n\n"
        f"— The NEPOOL Operator team"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="so-sample-") as tmp:
            path = build_demo_workbook(_p.Path(tmp) / "sample.xlsx")
            return send_workbook_email(
                to=to,
                subject="Sample NEPOOL Operator report — what to expect",
                html=body_html,
                text=body_text,
                workbook_path=str(path),
                filename="sample.xlsx",
            )
    except Exception as e:  # noqa: BLE001 — never block onboarding completion
        logger.error("send_sample_workbook_email failed: %s: %s",
                     type(e).__name__, e)
        return False


def send_payment_failed_email(to: str, name: str, amount_dollars: float,
                              next_attempt_unix: int | None,
                              product: str = "nepool") -> bool:
    """Warn the customer their card was declined. Stripe will retry; we just
    want them to update the card before the retries run out. Brand-aware."""
    import html as _html
    first = _html.escape((name or "there").split()[0])
    _brand = branding.brand_name(product)
    _dash = branding.dashboard_url(product)
    _link = link_color(product)
    from datetime import datetime as _dt
    retry_line = ""
    if next_attempt_unix:
        try:
            retry_line = (
                f" Our next retry runs around "
                f"{_dt.utcfromtimestamp(next_attempt_unix):%B %d, %Y}."
            )
        except Exception:
            pass

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>We tried to charge your card <strong>${amount_dollars:.2f}</strong> for your "
        f"{_brand} subscription, but it was declined.{retry_line}</p>"
        f"<p>To keep things running, please update your card at "
        f'<a href="{_dash}/" style="color:{_link};">your {_brand} dashboard</a> — '
        f"sign in, click <strong>Manage billing</strong>, update your payment method.</p>"
        f"<p>If you don't update before our retries run out, your subscription will be "
        f"canceled.</p>"
        f"<p>Questions or need help? Just reply.</p>"
        f"<p style=\"margin-top:24px;\">— {_brand}</p>"
    )
    body_text = (
        f"Hi {(name or 'there').split()[0]},\n\n"
        f"We tried to charge your card ${amount_dollars:.2f} for "
        f"{_brand}, but it was declined.{retry_line}\n\n"
        f"Update your card at {_dash}/ — sign in, click Manage billing.\n\n"
        f"Questions? Just reply.\n\n— {_brand}"
    )
    html = render_email_skin(
        preheader="Your subscription payment was declined — please update your card.",
        headline="Payment issue on your account",
        intro_line=f"We were unable to charge ${amount_dollars:.2f} for your subscription.",
        body_html=body_html,
        cta={"label": "Update payment method", "url": f"{_dash}/"},
        product=product,
    )
    text = render_email_skin_text(
        headline="Payment issue on your account",
        intro_line=f"We were unable to charge ${amount_dollars:.2f} for your subscription.",
        body_text=body_text,
        cta={"label": "Update payment method", "url": f"{_dash}/"},
        product=product,
    )
    return _send_via_resend(
        to=to,
        subject=f"Your {_brand} payment was declined",
        html=html,
        text=text,
        product=product,
    )


def send_trial_charge_failed_email(
    to: str,
    name: str,
    dashboard_url: str = "https://nepooloperator.com/accounts",
    product: str = "nepool",
) -> bool:
    """Notify operator their card was declined when we tried to activate their
    subscription at trial end. Different from send_payment_failed_email —
    no retry timeline, and the context is the trial-end charge specifically."""
    import html as _html
    first = _html.escape((name or "there").split()[0])
    _brand = branding.brand_name(product)
    _link = link_color(product)
    _what = "watching your arrays stays paused" if product == "array_operator" else "reports stay paused"

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>Your 14-day free trial just ended, and we tried to charge the card you "
        f"saved at signup — but it was declined.</p>"
        f"<p>Your account is paused until your card is updated.</p>"
        f"<p>To get back up and running, please update your payment method at "
        f'<a href="{dashboard_url}" style="color:{_link};">your {_brand} dashboard</a> — '
        f"sign in, click <strong>Manage billing</strong>, update your payment method.</p>"
        f"<p>Questions or need help? Just reply.</p>"
        f"<p style=\"margin-top:24px;\">— {_brand}</p>"
    )
    body_text = (
        f"Hi {(name or 'there').split()[0]},\n\n"
        f"Your 14-day free trial just ended, and we tried to charge the card you saved at signup "
        f"— but it was declined.\n\n"
        f"Your account is paused until your card is updated.\n\n"
        f"Update your payment method at {dashboard_url} — sign in, click Manage billing.\n\n"
        f"Questions or need help? Just reply.\n\n— {_brand}"
    )
    html = render_email_skin(
        preheader="Your card was declined at trial end — update to stay on.",
        headline="Card declined at trial end",
        intro_line="Update your payment method to activate your subscription.",
        body_html=body_html,
        cta={"label": "Update payment method", "url": dashboard_url},
        product=product,
    )
    text = render_email_skin_text(
        headline="Card declined at trial end",
        intro_line="Update your payment method to activate your subscription.",
        body_text=body_text,
        cta={"label": "Update payment method", "url": dashboard_url},
        product=product,
    )
    return _send_via_resend(
        to=to,
        subject=f"Card declined when activating your {_brand} subscription",
        html=html,
        text=text,
        product=product,
    )


def send_trial_welcome_email(
    to: str,
    name: str,
    trial_end_iso_date: str,
    dashboard_url: str = "https://nepooloperator.com/accounts",
    product: str = "nepool",
) -> bool:
    """Welcome email sent immediately after onboarding completes. No-card reality:
    the trial is live with no payment method on file. Product-aware — Array
    Operator owners get owner-side copy (per-kWh, connect arrays, no extension),
    NEPOOL operators get verifier-side copy (clients, GMP, $250+$15/array)."""
    import html as _html
    first = _html.escape((name or "there").split()[0])
    trial_end_escaped = _html.escape(trial_end_iso_date)
    _brand = branding.brand_name(product)
    _link = link_color(product)
    _pricing = branding.pricing_blurb(product)

    if product == "array_operator":
        # ── Array Operator (owner-side) ──────────────────────────────────────
        body_html = (
            f"<p>Hi {first},</p>"
            f"<p>Your Array Operator account is live, and your <strong>14-day trial "
            f"has started — no card required.</strong> Your agent is already watching "
            f"every panel: weather-normalized, per-inverter, in dollars.</p>"
            f"<p>From here on, we'll catch a quiet underperformer the day it starts, "
            f"draft the warranty claim when a panel dies, and show you what your array "
            f"is really worth — in plain language, not installer jargon.</p>"
            f"<p>One thing worth doing while the trial runs:</p>"
            f'<ul style="padding-left:20px;">'
            f'<li style="margin-bottom:10px;"><strong>Connect the rest of your fleet</strong> — '
            f'<a href="{dashboard_url}" style="color:{_link};">open your dashboard</a> '
            f"and add any other arrays or inverter brands so your whole portfolio is watched.</li>"
            f"</ul>"
            f"<p>When your trial ends on <strong>{trial_end_escaped}</strong>, add a card "
            f"to stay on — Array Operator is {_pricing}. No card, no charge: we'll just "
            f"pause and hold all your data until you're ready.</p>"
            f'<p style="margin-top:24px;font-size:14px;opacity:.8;">Questions? Just reply — a real person reads every email.</p>'
            f"<p style=\"margin-top:24px;\">— Array Operator</p>"
        )
        body_text = (
            f"Hi {(name or 'there').split()[0]},\n\n"
            f"Your Array Operator account is live, and your 14-day trial has started — "
            f"no card required. Your agent is already watching every panel: "
            f"weather-normalized, per-inverter, in dollars.\n\n"
            f"We'll catch a quiet underperformer the day it starts, draft the warranty "
            f"claim when a panel dies, and show you what your array is really worth.\n\n"
            f"While the trial runs, connect the rest of your fleet: open your dashboard "
            f"at {dashboard_url} and add any other arrays or inverter brands.\n\n"
            f"When your trial ends on {trial_end_iso_date}, add a card to stay on — "
            f"Array Operator is {_pricing}. No card, no charge: we'll pause and hold "
            f"all your data until you're ready.\n\n"
            f"Questions? Just reply.\n\n— Array Operator"
        )
        preheader = "Your Array Operator trial has started — no card needed today."
        subject = "Welcome to Array Operator — your 14-day trial has started"
    else:
        # ── NEPOOL Operator (verifier-side) ──────────────────────────────────
        body_html = (
            f"<p>Hi {first},</p>"
            f"<p>Your 14-day trial is live — <strong>no card required.</strong> "
            f"Add a payment method whenever you're ready, and we'll remind you a few "
            f"days before the trial ends so reports keep flowing.</p>"
            f"<p>Two things to do while the trial runs so you're ready when reports start going out:</p>"
            f'<ol style="padding-left:20px;">'
            f'<li style="margin-bottom:12px;"><strong>Add your clients</strong> — '
            f'<a href="{dashboard_url}" style="color:{_link};">open your dashboard</a> '
            f"and create a client for each solar subscriber you manage.</li>"
            f'<li style="margin-bottom:12px;"><strong>Add each client\'s NEPOOL arrays</strong> — '
            f"or sign into Green Mountain Power once and we'll auto-detect them.</li>"
            f"</ol>"
            f"<p>When your trial ends on <strong>{trial_end_escaped}</strong>, add a card "
            f"from the Accounts tab to keep your reports going — it's {_pricing}. No card, "
            f"no charge: we'll just pause reports and hold all your data until you're ready.</p>"
            f'<p style="margin-top:24px;font-size:14px;opacity:.8;">Questions? Just reply — a real person reads every email.</p>'
            f"<p style=\"margin-top:24px;\">— NEPOOL Operator</p>"
        )
        body_text = (
            f"Hi {(name or 'there').split()[0]},\n\n"
            f"Your 14-day trial is live — no card required. Add a payment method "
            f"whenever you're ready, and we'll remind you a few days before the trial "
            f"ends so reports keep flowing.\n\n"
            f"Two things to do while the trial runs so you're ready when reports start going out:\n\n"
            f"1. Add your clients — open your dashboard at {dashboard_url} and create a client "
            f"for each solar subscriber you manage.\n\n"
            f"2. Add each client's NEPOOL arrays — or sign into Green Mountain Power once "
            f"and we'll auto-detect them.\n\n"
            f"When your trial ends on {trial_end_iso_date}, add a card from the Accounts tab "
            f"to keep your reports going — {_pricing}. No card, no charge: we'll just pause "
            f"reports and hold all your data until you're ready.\n\n"
            f"Questions? Just reply — a real person reads every email.\n\n— NEPOOL Operator"
        )
        preheader = "Your NEPOOL Operator trial has started — no card needed today."
        subject = "Welcome to NEPOOL Operator — your 14-day trial has started"

    html = render_email_skin(
        preheader=preheader,
        intro_line="Welcome — your 14-day trial has started.",
        body_html=body_html,
        cta={"label": "Open your dashboard", "url": dashboard_url},
        product=product,
    )
    text = render_email_skin_text(
        intro_line="Welcome — your 14-day trial has started.",
        body_text=body_text,
        cta={"label": "Open your dashboard", "url": dashboard_url},
        product=product,
    )
    return _send_via_resend(to=to, subject=subject, html=html, text=text, product=product)


def send_trial_paused_no_card_email(
    to: str,
    name: str,
    dashboard_url: str = "https://nepooloperator.com/accounts",
    product: str = "nepool",
) -> bool:
    """Trial ended with no card on file — the account is paused (read-only).
    Tell the operator nothing was deleted and how to resume."""
    import html as _html
    first = _html.escape((name or "there").split()[0])
    _brand = branding.brand_name(product)
    _link = link_color(product)
    _pricing = branding.pricing_blurb(product)
    _ao = product == "array_operator"
    _resume = "we resume watching your arrays" if _ao else "your reports pick right back up"
    _paused_what = ("you can still see your whole fleet and its history, but we've paused "
                    "the live alerts and reports") if _ao else \
                   ("you can still see all your clients, arrays, and past reports, but "
                    "we've paused sending new ones")

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>Your trial ended. Add a payment method from your dashboard to resume — "
        f"we've held all your data, and <strong>nothing is deleted.</strong></p>"
        f"<p>Until you add a card, your account is read-only: {_paused_what}.</p>"
        f'<p>Add a card from the <a href="{dashboard_url}" style="color:{_link};">Accounts '
        f"tab</a> and {_resume} — {_pricing}.</p>"
        f"<p>Questions? Just reply — a real person reads every email.</p>"
        f"<p style=\"margin-top:24px;\">— {_brand}</p>"
    )
    body_text = (
        f"Hi {(name or 'there').split()[0]},\n\n"
        f"Your trial ended. Add a payment method from your dashboard to resume. "
        f"We've held all your data — nothing is deleted.\n\n"
        f"Until you add a card, your account is read-only.\n\n"
        f"Add a card from the Accounts tab at {dashboard_url} — {_pricing} — "
        f"and {_resume}.\n\n"
        f"Questions? Just reply.\n\n— {_brand}"
    )
    html = render_email_skin(
        preheader="Your trial ended — add a card to stay on. Nothing was deleted.",
        headline="Add a card to stay on",
        intro_line="Your trial ended — we've held all your data.",
        body_html=body_html,
        cta={"label": "Add a payment method", "url": dashboard_url},
        product=product,
    )
    text = render_email_skin_text(
        headline="Add a card to stay on",
        intro_line="Your trial ended — we've held all your data.",
        body_text=body_text,
        cta={"label": "Add a payment method", "url": dashboard_url},
        product=product,
    )
    return _send_via_resend(
        to=to,
        subject=f"Add a card to stay on {_brand}",
        html=html,
        text=text,
        product=product,
    )


def send_trial_ending_no_card_reminder_email(
    to: str,
    name: str,
    trial_end_date: str,
    dashboard_url: str = "https://nepooloperator.com/accounts",
    product: str = "nepool",
) -> bool:
    """Sent ~3 days before a no-card trial ends. Nudge the operator to add a card
    so they don't get paused when the trial expires."""
    import html as _html
    first = _html.escape((name or "there").split()[0])
    end_escaped = _html.escape(trial_end_date)
    _brand = branding.brand_name(product)
    _link = link_color(product)
    _pricing = branding.pricing_blurb(product)
    _keep = "keep your fleet watched" if product == "array_operator" else "keep your reports flowing"
    _pause = "we'll pause the live alerts and reports" if product == "array_operator" else "we'll pause reports"

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>Your free trial ends on <strong>{end_escaped}</strong>. Add a card to "
        f"{_keep} — without one, {_pause} when the trial ends (your data stays safe, "
        f"nothing is deleted).</p>"
        f'<p>It takes a minute from the <a href="{dashboard_url}" style="color:{_link};">'
        f"Accounts tab</a> — {_pricing}, charged after the trial ends.</p>"
        f"<p>Questions? Just reply.</p>"
        f"<p style=\"margin-top:24px;\">— {_brand}</p>"
    )
    body_text = (
        f"Hi {(name or 'there').split()[0]},\n\n"
        f"Your free trial ends on {trial_end_date}. Add a card to {_keep} — without "
        f"one, {_pause} when the trial ends (your data stays safe, nothing is deleted).\n\n"
        f"Add a card from the Accounts tab at {dashboard_url} — {_pricing}, charged "
        f"after the trial ends.\n\n"
        f"Questions? Just reply.\n\n— {_brand}"
    )
    html = render_email_skin(
        preheader=f"Your trial ends {trial_end_date} — add a card to stay on.",
        headline="Add a card to stay on",
        intro_line=f"Your free trial ends on {trial_end_date}.",
        body_html=body_html,
        cta={"label": "Add a payment method", "url": dashboard_url},
        product=product,
    )
    text = render_email_skin_text(
        headline="Add a card to stay on",
        intro_line=f"Your free trial ends on {trial_end_date}.",
        body_text=body_text,
        cta={"label": "Add a payment method", "url": dashboard_url},
        product=product,
    )
    return _send_via_resend(
        to=to,
        subject=f"Add a card to stay on {_brand}",
        html=html,
        text=text,
        product=product,
    )


def send_cancellation_email(to: str, name: str,
                             cancel_date: "datetime | None" = None,
                             product: str = "nepool") -> bool:
    import html as _html
    from datetime import datetime, timedelta
    first = _html.escape((name or "there").split()[0])
    _brand = branding.brand_name(product)
    _link = link_color(product)
    _signup = f"{branding.app_url(product)}/onboarding"
    base = cancel_date if cancel_date is not None else datetime.utcnow()
    purge_date = base + timedelta(days=30)
    purge_str = purge_date.strftime(f"%B {purge_date.day}, {purge_date.year}")

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>Your {_brand} subscription has been canceled. "
        f"You won't be charged again, and we'll stop sending automatic reports.</p>"
        f"<p>Your historical data is still in our system. "
        f"You'll have access to your account for 30 days — download "
        f"anything you need before <strong>{purge_str}</strong>. After that, your "
        f"data is permanently deleted.</p>"
        f"<p>If you change your mind, sign up again any time at "
        f'<a href="{_signup}" style="color:{_link};">{_brand}</a> — '
        f"we'll restore your account automatically.</p>"
        f"<p>If this cancellation was a mistake, or you'd like to share why "
        f"you're leaving, just reply. We read every email.</p>"
        f"<p>Thank you for being a customer.</p>"
        f"<p style=\"margin-top:24px;\">— {_brand}</p>"
    )
    body_text = (
        f"Hi {(name or 'there').split()[0]},\n\n"
        f"Your {_brand} subscription is canceled. "
        f"We'll stop sending reports and won't charge you again.\n\n"
        f"Your historical data is still in our system. You'll have access to your "
        f"account for 30 days — download anything you need before "
        f"{purge_str}. After that, your data is permanently deleted.\n\n"
        f"If you change your mind, sign up at {_signup} — "
        f"we'll restore your account automatically.\n\n"
        f"Questions or feedback? Just reply.\n\n— {_brand}"
    )
    html = render_email_skin(
        preheader=f"Your subscription is canceled — download your data before {purge_str}.",
        headline="Subscription canceled",
        intro_line="You won't be charged again. Reports have stopped.",
        body_html=body_html,
        cta={"label": "Sign up again", "url": _signup},
        product=product,
    )
    text = render_email_skin_text(
        headline="Subscription canceled",
        intro_line="You won't be charged again. Reports have stopped.",
        body_text=body_text,
        cta={"label": "Sign up again", "url": _signup},
        product=product,
    )
    return _send_via_resend(
        to=to,
        subject=f"Your {_brand} subscription is canceled",
        html=html,
        text=text,
        product=product,
    )


def send_add_first_array_email(to: str, name: str, dashboard_url: str = "https://nepooloperator.com/accounts") -> bool:
    """Trial extended 3 more days — operator has no arrays yet."""
    import html as _html
    first = _html.escape((name or "there").split()[0])

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>You signed up for NEPOOL Operator but haven't added any arrays yet. "
        f"We've extended your trial by 3 more days so you have time to finish setup.</p>"
        f'<p>Head to your <a href="{dashboard_url}" style="color:#047857;">dashboard</a>, '
        f"install the Chrome extension, and log into your utility portal to pull your "
        f"arrays automatically.</p>"
        f"<p>Once your trial ends, we'll bill you based on the arrays that are there. "
        f"If you still have zero, we'll charge the 1-array minimum.</p>"
        f"<p>Questions? Just reply — we read every email.</p>"
        f"<p style=\"margin-top:24px;\">— NEPOOL Operator</p>"
    )
    body_text = (
        f"Hi {(name or 'there').split()[0]},\n\n"
        f"You signed up for NEPOOL Operator but haven't added any arrays yet. "
        f"We've extended your trial by 3 more days.\n\n"
        f"Head to {dashboard_url} to finish setup.\n\n"
        f"Questions? Just reply.\n\n— NEPOOL Operator"
    )
    html = render_email_skin(
        preheader="Trial extended 3 days — add your first array before it ends.",
        headline="Add your first array",
        intro_line="Your trial has been extended by 3 days.",
        body_html=body_html,
        cta={"label": "Open dashboard", "url": dashboard_url},
    )
    text = render_email_skin_text(
        headline="Add your first array",
        intro_line="Your trial has been extended by 3 days.",
        body_text=body_text,
        cta={"label": "Open dashboard", "url": dashboard_url},
    )
    return _send_via_resend(
        to=to,
        subject="Add your first array — trial extended 3 days",
        html=html,
        text=text,
    )


def send_trial_charged_email(to: str, name: str, array_count: int,
                              amount_dollars: float,
                              product: str = "nepool") -> bool:
    """Trial ended and subscription created — confirm what was charged."""
    import html as _html
    first = _html.escape((name or "there").split()[0])
    _brand = branding.brand_name(product)
    _dash = branding.dashboard_url(product)
    _link = link_color(product)
    if product == "array_operator":
        _for = "for the energy your arrays generated this period"
        _continue = ("You're all set — your agent keeps watching every panel, and your "
                     "next invoice tracks the kWh your fleet actually makes.")
    else:
        plural = "array" if array_count == 1 else "arrays"
        _for = f"for {array_count} {plural}"
        _continue = ("You're all set — reports will continue running automatically on "
                     "your schedule. As you add or remove arrays, your next invoice "
                     "will update to match.")

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>Your 14-day trial just ended and your card was charged "
        f"<strong>${amount_dollars:.2f}</strong> {_for}.</p>"
        f"<p>{_continue}</p>"
        f'<p>Manage your account at <a href="{_dash}" style="color:{_link};">your {_brand} dashboard</a>.</p>'
        f"<p>Questions? Just reply.</p>"
        f"<p style=\"margin-top:24px;\">— {_brand}</p>"
    )
    body_text = (
        f"Hi {(name or 'there').split()[0]},\n\n"
        f"Your trial ended and your card was charged ${amount_dollars:.2f} {_for}.\n\n"
        f"Manage your account at {_dash}\n\n"
        f"Questions? Just reply.\n\n— {_brand}"
    )
    html = render_email_skin(
        preheader=f"Your ${amount_dollars:.2f} charge was successful — subscription active.",
        headline="Subscription active",
        intro_line=f"Your trial ended and your card was charged ${amount_dollars:.2f}.",
        body_html=body_html,
        cta={"label": "Manage your account", "url": _dash},
        product=product,
    )
    text = render_email_skin_text(
        headline="Subscription active",
        intro_line=f"Your trial ended and your card was charged ${amount_dollars:.2f}.",
        body_text=body_text,
        cta={"label": "Manage your account", "url": _dash},
        product=product,
    )
    return _send_via_resend(
        to=to,
        subject=f"Charged ${amount_dollars:.2f} — {_brand} subscription active",
        html=html,
        text=text,
        product=product,
    )


def send_gmp_reauth_needed_email(to: str, name: str) -> bool:
    """Notify an operator that we can't auto-refresh their GMP session and
    they need to log in once to reconnect."""
    import html as _html
    first = _html.escape((name or "there").split()[0])
    gmp_url = "https://greenmountainpower.com/account/login/"

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>We're having trouble automatically refreshing your Green Mountain Power "
        f"session. This usually means the session was revoked (e.g. a password change).</p>"
        f"<p>Please log into "
        f'<a href="{gmp_url}" style="color:#047857;">greenmountainpower.com</a> '
        f"once — the extension will capture a fresh session and automatic bill pulls "
        f"will resume immediately.</p>"
        f"<p>Questions? Just reply.</p>"
        f"<p style=\"margin-top:24px;\">— NEPOOL Operator</p>"
    )
    body_text = (
        f"Hi {(name or 'there').split()[0]},\n\n"
        f"We're having trouble automatically refreshing your Green Mountain Power session. "
        f"This usually means the session was revoked (e.g. a password change).\n\n"
        f"Please log into {gmp_url} once — the extension "
        f"will capture a fresh session and automatic bill pulls will resume.\n\n"
        f"Questions? Just reply.\n\n— NEPOOL Operator"
    )
    html = render_email_skin(
        preheader="Your Green Mountain Power session needs reconnecting.",
        headline="Action needed: reconnect your GMP account",
        intro_line="Log in once to restore automatic bill pulls.",
        body_html=body_html,
        cta={"label": "Log in to greenmountainpower.com", "url": gmp_url},
    )
    text = render_email_skin_text(
        headline="Action needed: reconnect your GMP account",
        intro_line="Log in once to restore automatic bill pulls.",
        body_text=body_text,
        cta={"label": "Log in to greenmountainpower.com", "url": gmp_url},
    )
    return _send_via_resend(
        to=to,
        subject="Action needed: reconnect your Green Mountain Power account",
        html=html,
        text=text,
    )


# ─── warranty claims (Array Operator) ────────────────────────────────────

def send_warranty_claim_email(
    to: str,
    subject: str,
    body_text: str,
    *,
    reply_to: str | None = None,
    from_name: str | None = None,
) -> bool:
    """Send one auto-drafted warranty/service claim.

    `body_text` is the plain-text claim the engine composed (manufacturer
    address, evidence, peer-index, lost-value). We wrap it in a light monospace
    HTML shell so it's readable in any client but stays copy-paste faithful, and
    set Reply-To to the owner so the manufacturer's reply reaches THEM, not us,
    even when we send from the platform From address.
    """
    html = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='color-scheme' content='light only'>"
        "<meta name='supported-color-schemes' content='light'>"
        "<style>:root{color-scheme:light only;supported-color-schemes:light;}</style></head>"
        "<body bgcolor='#ffffff' style='margin:0;padding:0;background:#ffffff;color-scheme:light only;'>"
        "<div style='font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;"
        "background:#f8fafc;padding:18px 22px;border-left:3px solid #2563eb;max-width:680px;'>"
        f"<pre style='margin:0;white-space:pre-wrap;color:#0f172a;'>{_escape(body_text)}</pre>"
        "</div></body></html>"
    )
    from_addr = None
    if from_name:
        from_addr = f"{from_name} <{_addr_only(FROM_ADDRESS)}>"
    return _send_via_resend(
        to=to,
        subject=subject,
        html=html,
        text=body_text,
        from_addr=from_addr,
        reply_to=reply_to,
    )


# ─── internal ───────────────────────────────────────────────────────────

def send_internal_alert(subject: str, body: str) -> bool:
    """Plain-text notification to ourselves. Used for new signups + errors.
    Kept intentionally simple — Ford reads these on his phone at 2am."""
    html = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='color-scheme' content='light only'>"
        "<meta name='supported-color-schemes' content='light'>"
        "<style>:root{color-scheme:light only;supported-color-schemes:light;}</style></head>"
        "<body bgcolor='#faf8f5' style='margin:0;padding:0;background:#faf8f5;color-scheme:light only;'>"
        "<div style='font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;"
        "background:#faf8f5;padding:16px 20px;border-left:3px solid #e6b470;'>"
        f"<pre style='margin:0;white-space:pre-wrap;color:#1a2a1f;'>{_escape(body)}</pre>"
        "</div></body></html>"
    )
    return _send_via_resend(
        to=INTERNAL_ALERT_TO,
        subject=f"[NEPOOL Operator] {subject}",
        html=html,
        text=body,
    )


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
