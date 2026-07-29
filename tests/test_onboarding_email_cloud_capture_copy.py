"""
Cloud Capture is the real onboarding path now (zero active tenants run
capture_mode='device' — see commits 2053a05b / e3193a31). These three emails
used to teach new signups the extension-first flow nobody actually uses:

  - send_welcome_email
  - send_sample_workbook_email
  - send_add_first_array_email

Pin that Cloud Capture (Account -> Cloud Capture, save a username/password,
no install) is now the step-one instruction, with the extension mentioned
only as an opt-out for keeping a utility password off our servers.
"""
from __future__ import annotations


def _capture_resend(monkeypatch):
    sent: list[dict] = []

    def fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    return sent


# ── send_welcome_email ────────────────────────────────────────────────────

def test_welcome_email_does_not_lead_with_extension_install(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_welcome_email
    send_welcome_email(to="op@example.com", name="Alice Operator",
                       tenant_key="sol_live_abc123", plan="standard")
    html = sent[0]["html"].lower()
    text = sent[0]["text"].lower()
    assert "install the chrome extension" not in html
    assert "install the chrome extension" not in text


def test_welcome_email_leads_with_cloud_capture(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_welcome_email
    send_welcome_email(to="op@example.com", name="Alice Operator",
                       tenant_key="sol_live_abc123", plan="standard")
    html = sent[0]["html"]
    # Cloud Capture instruction must appear before any extension mention —
    # it's step one, the extension is the fallback further down.
    cc_pos = html.lower().find("cloud capture")
    ext_pos = html.lower().find("chrome extension")
    assert cc_pos != -1, "Cloud Capture not mentioned at all"
    assert ext_pos != -1, "extension fallback should still be offered"
    assert cc_pos < ext_pos, "Cloud Capture must come before the extension fallback"


def test_welcome_email_cta_is_dashboard_not_extension(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_welcome_email
    send_welcome_email(to="op@example.com", name="Alice Operator",
                       tenant_key="sol_live_abc123", plan="standard")
    html = sent[0]["html"]
    assert "Open your dashboard" in html
    assert 'cta={"label": "Install the Chrome extension"' not in html


def test_welcome_email_extension_fallback_keeps_activation_code(monkeypatch):
    """Extension users on a second device still need the manual pairing code —
    that instruction must survive the rewrite, just repositioned as fallback."""
    sent = _capture_resend(monkeypatch)
    from api.notify import send_welcome_email
    send_welcome_email(to="op@example.com", name="Alice Operator",
                       tenant_key="sol_live_abc123", plan="standard")
    html = sent[0]["html"]
    text = sent[0]["text"]
    assert "sol_live_abc123" in html
    assert "sol_live_abc123" in text
    assert "Enter code manually" in html


def test_welcome_email_no_browser_extension_needed_copy(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_welcome_email
    send_welcome_email(to="op@example.com", name="Alice Operator",
                       tenant_key="sol_live_abc123", plan="standard")
    combined = (sent[0]["html"] + sent[0]["text"]).lower()
    assert "no browser extension" in combined


# ── send_sample_workbook_email ────────────────────────────────────────────

def test_sample_workbook_email_does_not_lead_with_extension(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_sample_workbook_email
    send_sample_workbook_email(to="op@example.com", name="Bob Operator")
    assert sent, "No email sent"
    combined = (sent[0]["html"] + sent[0]["text"]).lower()
    assert "install the chrome extension" not in combined
    assert "sync through the chrome extension" not in combined
    assert "cloud capture" in combined


# ── send_add_first_array_email ────────────────────────────────────────────

def test_add_first_array_email_nepool_leads_with_cloud_capture(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_add_first_array_email
    send_add_first_array_email(to="op@example.com", name="Carol Operator",
                               product="nepool")
    html = sent[0]["html"].lower()
    assert "install the chrome extension" not in html
    assert "cloud capture" in html


def test_add_first_array_email_array_operator_leads_with_cloud_capture(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_add_first_array_email
    send_add_first_array_email(to="op@example.com", name="Dave Owner",
                               product="array_operator")
    html = sent[0]["html"].lower()
    assert "install the chrome extension" not in html
    assert "cloud capture" in html
    # product-aware billing line must still be intact
    assert "nothing connected, nothing charged" in html
