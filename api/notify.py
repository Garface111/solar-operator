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
                     reply_to: str | None = None) -> bool:
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
        "from": from_addr or FROM_ADDRESS,
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "html": html,
    }
    if text:
        params["text"] = text
    if reply_to:
        params["reply_to"] = reply_to
    if attachments:
        params["attachments"] = attachments

    try:
        result = resend.Emails.send(params)
        # result is a dict like {"id": "xxx"}
        if result and result.get("id"):
            _send_via_resend._last_error = None
            return True
        logger.error("Resend returned unexpected response: %s", result)
        _send_via_resend._last_error = f"Unexpected response: {result}"
        return False
    except Exception as e:
        logger.error("Resend send failed: %s: %s", type(e).__name__, e)
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
    ok = _send_via_resend(
        to=to, subject=subject, html=html, text=text,
        attachments=attachments, from_addr=from_addr, reply_to=reply_to,
    )
    if not ok and from_addr:
        logger.warning(
            "Custom From %r failed (unverified domain?) — retrying from platform "
            "default with Reply-To preserved.", from_addr)
        fallback_reply = reply_to or _addr_only(from_addr)
        # Build "Operator Name via Solar Operator <admin@solaroperator.org>"
        # so the recipient sees the operator's name even when we can't send as them.
        op_name = _name_part(from_addr)
        fallback_from = (
            f'"{op_name} via Solar Operator" <{_addr_only(FROM_ADDRESS)}>'
            if op_name else FROM_ADDRESS
        )
        ok = _send_via_resend(
            to=to, subject=subject, html=html, text=text,
            attachments=attachments, from_addr=fallback_from, reply_to=fallback_reply,
        )
    return ok


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


# ─── customer-facing ─────────────────────────────────────────────────────

WELCOME_HTML = """\
<!DOCTYPE html><html><body style="margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f4;padding:30px 0;color:#1a2a1f;">
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="560" style="max-width:560px;background:white;border-radius:12px;overflow:hidden;">
<tr><td style="background:#2e6b3a;padding:28px 32px;color:white;">
  <div style="font-size:22px;font-weight:700;">Solar Operator</div>
  <div style="font-size:14px;color:#cfe4d3;margin-top:4px;">Welcome aboard, {name}.</div>
</td></tr>
<tr><td style="padding:32px;font-size:15px;line-height:1.6;">
<p>Your Solar Operator account is live on the <strong>{plan_label}</strong> plan.
You're a few minutes from automatic quarterly reporting.</p>

<p><strong>Setup, in three steps:</strong></p>

<ol style="padding-left:20px;margin:14px 0;">
  <li style="margin:10px 0;"><strong>Install the Chrome extension</strong> —
      <a href="{install_url}" style="color:#2e6b3a;">Add Solar Operator Sync to Chrome</a></li>
  <li style="margin:10px 0;"><strong>The extension auto-pairs with your account</strong> —
      open your <a href="https://solaroperator.org/accounts" style="color:#2e6b3a;">Solar Operator dashboard</a>
      once after installing and the extension links itself automatically. No codes to copy.</li>
  <li style="margin:10px 0;"><strong>Sign into your utility portal once</strong> — visit
      <a href="https://greenmountainpower.com" style="color:#2e6b3a;">greenmountainpower.com</a>
      (or Vermont Electric Co-op), log in with the extension active. We capture the rest.</li>
</ol>

<p style="margin-top:24px;background:#f4f9f5;border-radius:8px;padding:16px 20px;font-size:14px;color:#3a5a42;">
  <strong>What happens next:</strong><br>
  Once you finish setup, the extension will pull your utility bills automatically
  every 6 hours in the background. Your first quarterly NEPOOL-GIS report goes
  out on <strong>{next_quarter_date}</strong> — you don't need to do anything
  between now and then. If you ever want to trigger a report early, there's a
  "Send a report now" button in your dashboard.
</p>

<p style="margin-top:24px;background:#fffbea;border-radius:8px;padding:14px 18px;font-size:13px;color:#6b5a1f;border:1px solid #f0e68c;">
  <strong>Setting up on a new device later?</strong> If auto-pairing doesn't trigger,
  you can pair manually: open the extension, click "Enter code manually," and paste
  your activation code: <span style="font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:700;">{tenant_key}</span>
</p>

<p style="margin-top:24px;color:#667;font-size:14px;">
Questions? Just reply — we read every email and respond same business day.
</p>

<p style="margin-top:32px;">— The Solar Operator team</p>

</td></tr>
<tr><td style="background:#1f4e2a;padding:14px 32px;font-size:11px;color:#cfe4d3;text-align:center;">
Solar Operator · solaroperator.org · You're receiving this because you signed up for service.
</td></tr>
</table>
</td></tr></table>
</body></html>
"""

WELCOME_TEXT = """\
Welcome aboard, {name}.

Your Solar Operator account is live on the {plan_label} plan.

Setup (3 steps):

  1. Install the Chrome extension: {install_url}
  2. Open your Solar Operator dashboard (solaroperator.org/accounts) — the
     extension auto-pairs with your account. No codes to copy.
  3. Sign into your utility portal once (greenmountainpower.com or Vermont
     Electric Co-op). The extension captures the rest.

What happens next:
  Once set up, the extension pulls your utility bills every 6 hours in the
  background. Your first quarterly report goes out on {next_quarter_date}. You
  don't need to do anything between now and then. There's a "Send a report now"
  button on your dashboard if you ever want to trigger one early.

Setting up on a new device later? If auto-pairing doesn't trigger, open the
extension, click "Enter code manually," and paste your activation code:
  {tenant_key}

Questions? Just reply.

— The Solar Operator team
solaroperator.org
"""

PLAN_LABELS = {"standard": "Solar Operator", "comped": "Solar Operator (comped)",
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
    plan_label = PLAN_LABELS.get(plan, "Solar Operator")
    first = name.split()[0] if name else "there"
    fmt = dict(
        name=_html.escape(first),
        plan_label=_html.escape(plan_label),
        tenant_key=tenant_key,  # random token — safe
        install_url=EXTENSION_INSTALL_URL,
        next_quarter_date=_next_quarterly_date(),
    )
    return _send_via_resend(
        to=to,
        subject="Welcome to Solar Operator — your activation code",
        html=WELCOME_HTML.format(**fmt),
        text=WELCOME_TEXT.format(**fmt),
    )


SAMPLE_REPORT_HTML = """\
<!DOCTYPE html><html><body style="margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f4;padding:30px 0;color:#1a2a1f;">
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="560" style="max-width:560px;background:white;border-radius:12px;overflow:hidden;">
<tr><td style="background:#2e6b3a;padding:28px 32px;color:white;">
  <div style="font-size:22px;font-weight:700;">Solar Operator</div>
  <div style="font-size:14px;color:#cfe4d3;margin-top:4px;">Here's what your reports will look like</div>
</td></tr>
<tr><td style="padding:32px;font-size:15px;line-height:1.6;">
<p>Hi {name},</p>
<p>Welcome aboard! Attached is a <strong>sample report</strong> so you can see
exactly what we'll send your clients each quarter — a pixel-perfect NEPOOL-GIS
generation workbook, one sheet per array, covering the last six complete
quarters with monthly MWh and REC counts.</p>
<p>This particular file uses made-up "Demo Array" data, but once your Green
Mountain Power bills sync through the Chrome extension, your real arrays will
appear automatically and go out on the schedule you choose. You can manage
everything — clients, schedule, and recipients — from your
<a href="{dashboard_url}" style="color:#2e6b3a;">dashboard</a>.</p>
<p style="margin-top:24px;color:#667;font-size:14px;">Questions? Just reply — we read every email.</p>
<p style="margin-top:24px;">— The Solar Operator team</p>
</td></tr>
<tr><td style="background:#1f4e2a;padding:14px 32px;font-size:11px;color:#cfe4d3;text-align:center;">
Solar Operator · solaroperator.org
</td></tr>
</table>
</td></tr></table></body></html>
"""

SAMPLE_REPORT_TEXT = """\
Hi {name},

Welcome aboard! Attached is a sample report so you can see exactly what we'll
send your clients each quarter — a pixel-perfect NEPOOL-GIS generation workbook,
one sheet per array, covering the last six complete quarters with monthly MWh
and REC counts.

This file uses made-up "Demo Array" data, but once your Green Mountain Power
bills sync through the Chrome extension, your real arrays appear automatically
and go out on the schedule you choose.

Manage everything at {dashboard_url}

Questions? Just reply.

— The Solar Operator team
"""


def send_sample_workbook_email(to: str, name: str,
                               dashboard_url: str = "https://solaroperator.org/accounts") -> bool:
    """Email the generic demo workbook so a new operator sees what their
    quarterly reports will look like. Generates a fresh sample to a temp file
    and attaches it. Best-effort: returns False (and logs) on any failure."""
    import tempfile, pathlib as _p
    # Deferred import keeps notify.py import-light and avoids any writer/db
    # import cost for the (common) code paths that never send this email.
    from .writers.demo_writer import build_demo_workbook

    first = name.split()[0] if name else "there"
    fmt = dict(name=first, dashboard_url=dashboard_url)
    try:
        with tempfile.TemporaryDirectory(prefix="so-sample-") as tmp:
            path = build_demo_workbook(_p.Path(tmp) / "sample.xlsx")
            return send_workbook_email(
                to=to,
                subject="Sample Solar Operator report — what to expect",
                html=SAMPLE_REPORT_HTML.format(**fmt),
                text=SAMPLE_REPORT_TEXT.format(**fmt),
                workbook_path=str(path),
                filename="sample.xlsx",
            )
    except Exception as e:  # noqa: BLE001 — never block onboarding completion
        logger.error("send_sample_workbook_email failed: %s: %s",
                     type(e).__name__, e)
        return False


def send_payment_failed_email(to: str, name: str, amount_dollars: float,
                              next_attempt_unix: int | None) -> bool:
    """Warn the customer their card was declined. Stripe will retry; we just
    want them to update the card before the retries run out."""
    first = (name or "there").split()[0]
    from datetime import datetime as _dt
    when = ""
    if next_attempt_unix:
        try:
            when = f" Our next retry runs around {_dt.utcfromtimestamp(next_attempt_unix):%B %d, %Y}."
        except Exception:
            pass
    html = (f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;max-width:560px;margin:30px auto;padding:0 20px;color:#1a2a1f;'>"
            f"<h2 style='color:#a64a1f;'>Payment issue on your Solar Operator account</h2>"
            f"<p>Hi {first},</p>"
            f"<p>We tried to charge your card ${amount_dollars:.2f} for your Solar Operator subscription, but it was declined.{when}</p>"
            f"<p>To keep your reports flowing, please update your card at "
            f"<a href='https://solaroperator.org/accounts/'>your Solar Operator dashboard</a> — "
            f"sign in, click <strong>Manage billing</strong>, update your payment method.</p>"
            f"<p>If you don't update before our retries run out, your subscription will be canceled and reports will stop.</p>"
            f"<p>Questions or need help? Just reply.</p>"
            f"<p>— Solar Operator</p></body></html>")
    text = (f"Hi {first},\n\nWe tried to charge your card ${amount_dollars:.2f} for "
            f"Solar Operator, but it was declined.{when}\n\n"
            f"Update your card at https://solaroperator.org/accounts/ — "
            f"sign in, click Manage billing.\n\nQuestions? Just reply.\n\n— Solar Operator")
    return _send_via_resend(
        to=to,
        subject="Your Solar Operator payment was declined",
        html=html, text=text,
    )


def send_trial_charge_failed_email(
    to: str,
    name: str,
    dashboard_url: str = "https://solaroperator.org/accounts",
) -> bool:
    """Notify operator their card was declined when we tried to activate their
    subscription at trial end. Different from send_payment_failed_email —
    no retry timeline, and the context is the trial-end charge specifically."""
    first = (name or "there").split()[0]
    html = (
        f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;"
        f"max-width:560px;margin:30px auto;padding:0 20px;color:#1a2a1f;'>"
        f"<h2 style='color:#a64a1f;'>Card declined when activating your Solar Operator subscription</h2>"
        f"<p>Hi {first},</p>"
        f"<p>Your 14-day free trial just ended, and we tried to charge the card you saved at signup "
        f"— but it was declined.</p>"
        f"<p>Reports stay paused until your card is updated.</p>"
        f"<p>To get back up and running, please update your payment method at "
        f"<a href='{dashboard_url}'>your Solar Operator dashboard</a> — "
        f"sign in, click <strong>Manage billing</strong>, update your payment method.</p>"
        f"<p>Questions or need help? Just reply.</p>"
        f"<p>— Solar Operator</p></body></html>"
    )
    text = (
        f"Hi {first},\n\n"
        f"Your 14-day free trial just ended, and we tried to charge the card you saved at signup "
        f"— but it was declined.\n\n"
        f"Reports stay paused until your card is updated.\n\n"
        f"Update your payment method at {dashboard_url} — sign in, click Manage billing.\n\n"
        f"Questions or need help? Just reply.\n\n— Solar Operator"
    )
    return _send_via_resend(
        to=to,
        subject="Card declined when activating your Solar Operator subscription",
        html=html, text=text,
    )


def send_trial_welcome_email(
    to: str,
    name: str,
    trial_end_iso_date: str,
    dashboard_url: str = "https://solaroperator.org/accounts",
) -> bool:
    """Welcome email sent immediately after onboarding completes. Explains the
    14-day trial and primes the operator to add clients and arrays."""
    first = (name or "there").split()[0]
    html = (
        f"<!DOCTYPE html><html><body style='margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        f"background:#f4f6f4;padding:30px 0;color:#1a2a1f;'>"
        f"<table cellpadding='0' cellspacing='0' border='0' width='100%'><tr><td align='center'>"
        f"<table cellpadding='0' cellspacing='0' border='0' width='560' "
        f"style='max-width:560px;background:white;border-radius:12px;overflow:hidden;'>"
        f"<tr><td style='background:#2e6b3a;padding:28px 32px;color:white;'>"
        f"<div style='font-size:22px;font-weight:700;'>Solar Operator</div>"
        f"<div style='font-size:14px;color:#cfe4d3;margin-top:4px;'>Welcome — your 14-day trial has started</div>"
        f"</td></tr>"
        f"<tr><td style='padding:32px;font-size:15px;line-height:1.6;'>"
        f"<p>Hi {first},</p>"
        f"<p>You're all signed up. Your card won't be charged until <strong>{trial_end_iso_date}</strong> "
        f"— that's 14 days from today.</p>"
        f"<p>Two things to do while the trial runs so you're ready when reports start going out:</p>"
        f"<ol style='padding-left:20px;'>"
        f"<li style='margin-bottom:12px;'><strong>Add your clients</strong> — "
        f"<a href='{dashboard_url}' style='color:#2e6b3a;'>open your dashboard</a> "
        f"and create a client for each solar subscriber you manage.</li>"
        f"<li style='margin-bottom:12px;'><strong>Add each client's NEPOOL arrays</strong> — "
        f"or sign into Green Mountain Power once and we'll auto-detect them.</li>"
        f"</ol>"
        f"<p>If you haven't added any arrays by trial end, we'll extend your trial 3 more "
        f"days. After that, if you still have zero arrays, we apply a one-array minimum "
        f"($15/month) plus the $250 setup fee. Cancel anytime before then to avoid any "
        f"charge — your card stays on file until you say otherwise.</p>"
        f"<p style='margin-top:24px;color:#667;font-size:14px;'>Questions? Just reply — a real person reads every email.</p>"
        f"<p style='margin-top:24px;'>— Solar Operator</p>"
        f"</td></tr>"
        f"<tr><td style='background:#1f4e2a;padding:14px 32px;font-size:11px;color:#cfe4d3;text-align:center;'>"
        f"Solar Operator · solaroperator.org"
        f"</td></tr>"
        f"</table></td></tr></table></body></html>"
    )
    text = (
        f"Hi {first},\n\n"
        f"You're all signed up. Your card won't be charged until {trial_end_iso_date} "
        f"— that's 14 days from today.\n\n"
        f"Two things to do while the trial runs so you're ready when reports start going out:\n\n"
        f"1. Add your clients — open your dashboard at {dashboard_url} and create a client "
        f"for each solar subscriber you manage.\n\n"
        f"2. Add each client's NEPOOL arrays — or sign into Green Mountain Power once "
        f"and we'll auto-detect them.\n\n"
        f"If you haven't added any arrays by trial end, we'll extend your trial 3 more "
        f"days. After that, if you still have zero arrays, we apply a one-array minimum "
        f"($15/month) plus the $250 setup fee. Cancel anytime before then to avoid any "
        f"charge — your card stays on file until you say otherwise.\n\n"
        f"Questions? Just reply — a real person reads every email.\n\n— Solar Operator"
    )
    return _send_via_resend(
        to=to,
        subject="Welcome to Solar Operator — your 14-day trial has started",
        html=html, text=text,
    )


def send_cancellation_email(to: str, name: str,
                             cancel_date: "datetime | None" = None) -> bool:
    from datetime import datetime, timedelta
    first = (name or "there").split()[0]
    base = cancel_date if cancel_date is not None else datetime.utcnow()
    purge_date = base + timedelta(days=30)
    purge_str = purge_date.strftime(f"%B {purge_date.day}, {purge_date.year}")
    html = (f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;max-width:560px;margin:30px auto;padding:0 20px;color:#1a2a1f;'>"
            f"<h2 style='color:#2e6b3a;'>Your Solar Operator subscription is canceled</h2>"
            f"<p>Hi {first},</p>"
            f"<p>Your Solar Operator subscription has been canceled. "
            f"You won't be charged again, and we'll stop sending automatic reports.</p>"
            f"<p>Your historical data is still in our system. "
            f"You'll have access to your account and reports for 30 days — download "
            f"anything you need before <strong>{purge_str}</strong>. After that, your "
            f"data is permanently deleted.</p>"
            f"<p>If you change your mind, sign up again any time at "
            f"<a href='https://solaroperator.org/signup.html'>solaroperator.org/signup</a> — "
            f"we'll restore your existing meters automatically.</p>"
            f"<p>If this cancellation was a mistake, or you'd like to share why "
            f"you're leaving, just reply. We read every email.</p>"
            f"<p>Thank you for being a customer.</p>"
            f"<p>— Solar Operator</p></body></html>")
    text = (f"Hi {first},\n\nYour Solar Operator subscription is canceled. "
            f"We'll stop sending reports and won't charge you again.\n\n"
            f"Your historical data is still in our system. You'll have access to your "
            f"account and reports for 30 days — download anything you need before "
            f"{purge_str}. After that, your data is permanently deleted.\n\n"
            f"If you change your mind, sign up at https://solaroperator.org/signup.html — "
            f"we'll restore your meters automatically.\n\n"
            f"Questions or feedback? Just reply.\n\n— Solar Operator")
    return _send_via_resend(
        to=to,
        subject="Your Solar Operator subscription is canceled",
        html=html, text=text,
    )


def send_add_first_array_email(to: str, name: str, dashboard_url: str = "https://solaroperator.org/accounts") -> bool:
    """Trial extended 3 more days — operator has no arrays yet."""
    first = (name or "there").split()[0]
    html = (
        f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;max-width:560px;margin:30px auto;padding:0 20px;color:#1a2a1f;'>"
        f"<h2 style='color:#2e6b3a;'>Add your first array before your trial ends</h2>"
        f"<p>Hi {first},</p>"
        f"<p>You signed up for Solar Operator but haven't added any arrays yet. "
        f"We've extended your trial by 3 more days so you have time to finish setup.</p>"
        f"<p>Head to your <a href='{dashboard_url}'>dashboard</a>, install the Chrome extension, "
        f"and log into your utility portal to pull your arrays automatically.</p>"
        f"<p>Once your trial ends, we'll bill you based on the arrays that are there. "
        f"If you still have zero, we'll charge the 1-array minimum.</p>"
        f"<p>Questions? Just reply — we read every email.</p>"
        f"<p>— Solar Operator</p></body></html>"
    )
    text = (
        f"Hi {first},\n\n"
        f"You signed up for Solar Operator but haven't added any arrays yet. "
        f"We've extended your trial by 3 more days.\n\n"
        f"Head to {dashboard_url} to finish setup.\n\n"
        f"Questions? Just reply.\n\n— Solar Operator"
    )
    return _send_via_resend(
        to=to,
        subject="Add your first array — trial extended 3 days",
        html=html, text=text,
    )


def send_trial_charged_email(to: str, name: str, array_count: int,
                              amount_dollars: float) -> bool:
    """Trial ended and subscription created — confirm what was charged."""
    first = (name or "there").split()[0]
    plural = "array" if array_count == 1 else "arrays"
    html = (
        f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;max-width:560px;margin:30px auto;padding:0 20px;color:#1a2a1f;'>"
        f"<h2 style='color:#2e6b3a;'>Your Solar Operator subscription is active</h2>"
        f"<p>Hi {first},</p>"
        f"<p>Your 14-day trial just ended and your card was charged "
        f"<strong>${amount_dollars:.2f}</strong> for {array_count} {plural}.</p>"
        f"<p>You're all set — reports will continue running automatically on your schedule. "
        f"As you add or remove arrays, your next invoice will update to match.</p>"
        f"<p>Manage your account at <a href='https://solaroperator.org/accounts'>solaroperator.org/accounts</a>.</p>"
        f"<p>Questions? Just reply.</p>"
        f"<p>— Solar Operator</p></body></html>"
    )
    text = (
        f"Hi {first},\n\n"
        f"Your trial ended and your card was charged ${amount_dollars:.2f} "
        f"for {array_count} {plural}.\n\n"
        f"Manage your account at https://solaroperator.org/accounts\n\n"
        f"Questions? Just reply.\n\n— Solar Operator"
    )
    return _send_via_resend(
        to=to,
        subject=f"Charged ${amount_dollars:.2f} — Solar Operator subscription active",
        html=html, text=text,
    )


def send_gmp_reauth_needed_email(to: str, name: str) -> bool:
    """Notify an operator that we can't auto-refresh their GMP session and
    they need to log in once to reconnect."""
    first = (name or "there").split()[0]
    html = (
        f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;"
        f"max-width:560px;margin:30px auto;padding:0 20px;color:#1a2a1f;'>"
        f"<h2 style='color:#a64a1f;'>GMP session needs reconnecting</h2>"
        f"<p>Hi {first},</p>"
        f"<p>We're having trouble automatically refreshing your Green Mountain Power "
        f"session. This usually means the session was revoked (e.g. a password change).</p>"
        f"<p>Please log into "
        f"<a href='https://mypower.greenmountainpower.com/'>greenmountainpower.com</a> "
        f"once — the extension will capture a fresh session and automatic bill pulls "
        f"will resume immediately.</p>"
        f"<p>Questions? Just reply.</p>"
        f"<p>— Solar Operator</p></body></html>"
    )
    text = (
        f"Hi {first},\n\n"
        f"We're having trouble automatically refreshing your Green Mountain Power session. "
        f"This usually means the session was revoked (e.g. a password change).\n\n"
        f"Please log into https://mypower.greenmountainpower.com/ once — the extension "
        f"will capture a fresh session and automatic bill pulls will resume.\n\n"
        f"Questions? Just reply.\n\n— Solar Operator"
    )
    return _send_via_resend(
        to=to,
        subject="Action needed: reconnect your Green Mountain Power account",
        html=html,
        text=text,
    )


# ─── internal ───────────────────────────────────────────────────────────

def send_internal_alert(subject: str, body: str) -> bool:
    """Plain-text notification to ourselves. Used for new signups + errors."""
    html = "<pre style='font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;'>" \
           f"{_escape(body)}</pre>"
    return _send_via_resend(
        to=INTERNAL_ALERT_TO,
        subject=f"[Solar Operator] {subject}",
        html=html,
        text=body,
    )


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
