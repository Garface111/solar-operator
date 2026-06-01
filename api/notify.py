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

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_ADDRESS = os.getenv("MAIL_FROM", "Solar Operator <hello@solaroperator.org>")
INTERNAL_ALERT_TO = os.getenv("INTERNAL_ALERT_TO", "ford.genereaux@dysonswarmtechnologies.com")
EXTENSION_INSTALL_URL = os.getenv(
    "EXTENSION_INSTALL_URL",
    "https://chromewebstore.google.com/detail/solar-operator-sync"
)


def _send_via_resend(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Returns True on success, False otherwise. Never raises — caller can
    decide whether to bubble up."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — logging email instead of sending.")
        logger.info("EMAIL → to=%s subject=%s\n%s", to, subject, text or html[:500])
        return False

    body = json.dumps({
        "from": FROM_ADDRESS,
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "html": html,
        **({"text": text} if text else {}),
    }).encode()

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=body,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.error("Resend HTTP %s: %s", resp.status, resp.read()[:300])
            return ok
    except Exception as e:
        logger.error("Resend send failed: %s", e)
        return False


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
You're 5 minutes from automatic monthly reporting.</p>

<div style="background:#eef3ec;border-radius:8px;padding:18px 22px;margin:24px 0;">
  <div style="font-size:12px;letter-spacing:0.5px;text-transform:uppercase;color:#557060;font-weight:600;">YOUR ACTIVATION CODE</div>
  <div style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:15px;font-weight:700;color:#1f4e2a;margin-top:8px;word-break:break-all;">{tenant_key}</div>
</div>

<p><strong>Setup, in three steps:</strong></p>

<ol style="padding-left:20px;margin:14px 0;">
  <li style="margin:10px 0;"><strong>Install the Chrome extension</strong> —
      <a href="{install_url}" style="color:#2e6b3a;">Add Solar Operator Sync to Chrome</a></li>
  <li style="margin:10px 0;"><strong>Paste your activation code</strong> — click the
      Solar Operator icon in Chrome's toolbar, paste the code above, hit Save.</li>
  <li style="margin:10px 0;"><strong>Sign into Green Mountain Power</strong> — visit
      <a href="https://greenmountainpower.com" style="color:#2e6b3a;">greenmountainpower.com</a>,
      log in, check "Stay signed in". Close the tab. You're done.</li>
</ol>

<p style="margin-top:24px;background:#fff4d6;padding:14px 18px;border-radius:6px;font-size:14px;">
<strong>One more thing:</strong> reply to this email with your current reporting
spreadsheet attached so we can match your exact format. Your first updated
workbook lands within 1 business day after that.
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

YOUR ACTIVATION CODE:
  {tenant_key}

Setup (3 steps):

  1. Install the Chrome extension: {install_url}
  2. Click the Solar Operator icon in Chrome's toolbar, paste the activation
     code above, hit Save.
  3. Visit greenmountainpower.com, log in, check "Stay signed in." Close
     the tab. That's it.

One more thing: reply to this email with your current reporting spreadsheet
attached so we can match your exact format. Your first updated workbook
lands within 1 business day after that.

Questions? Just reply.

— The Solar Operator team
solaroperator.org
"""

PLAN_LABELS = {"solo": "Solo", "manager": "Manager", "operator": "Operator"}


def send_welcome_email(to: str, name: str, tenant_key: str, plan: str) -> bool:
    plan_label = PLAN_LABELS.get(plan, plan.title())
    fmt = dict(
        name=name.split()[0] if name else "there",
        plan_label=plan_label,
        tenant_key=tenant_key,
        install_url=EXTENSION_INSTALL_URL,
    )
    return _send_via_resend(
        to=to,
        subject="Welcome to Solar Operator — your activation code",
        html=WELCOME_HTML.format(**fmt),
        text=WELCOME_TEXT.format(**fmt),
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
