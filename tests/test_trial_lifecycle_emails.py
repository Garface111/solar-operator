"""
Tests for the two trial lifecycle emails:
  - send_trial_charge_failed_email  (Task A)
  - send_trial_welcome_email        (Task B)
"""
from __future__ import annotations

from unittest.mock import patch, call


def _capture_resend(monkeypatch):
    """Patch _send_via_resend and return a list that records each call's kwargs."""
    sent: list[dict] = []

    def fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    return sent


# ── send_trial_charge_failed_email ────────────────────────────────────────────

def test_trial_charge_failed_subject_contains_declined(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_charge_failed_email
    send_trial_charge_failed_email(to="op@example.com", name="Alice Operator")
    assert sent, "No email sent"
    assert "declined" in sent[0]["subject"].lower()


def test_trial_charge_failed_body_contains_trial_copy(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_charge_failed_email
    send_trial_charge_failed_email(to="op@example.com", name="Alice Operator")
    html = sent[0]["html"]
    text = sent[0]["text"]
    assert "14-day free trial" in html
    assert "14-day free trial" in text


def test_trial_charge_failed_body_contains_dashboard_link(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_charge_failed_email
    dashboard = "https://solaroperator.org/accounts"
    send_trial_charge_failed_email(to="op@example.com", name="Alice Operator",
                                   dashboard_url=dashboard)
    assert dashboard in sent[0]["html"]
    assert dashboard in sent[0]["text"]


def test_trial_charge_failed_custom_dashboard_url(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_charge_failed_email
    custom = "https://example.com/portal"
    send_trial_charge_failed_email(to="op@example.com", name="Tester",
                                   dashboard_url=custom)
    assert custom in sent[0]["html"]
    assert custom in sent[0]["text"]


def test_trial_charge_failed_uses_first_name(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_charge_failed_email
    send_trial_charge_failed_email(to="op@example.com", name="Alice Operator")
    assert "Alice" in sent[0]["html"]
    assert "Operator" not in sent[0]["html"].split("Hi ")[1].split(",")[0]


def test_trial_charge_failed_returns_true_on_success(monkeypatch):
    _capture_resend(monkeypatch)
    from api.notify import send_trial_charge_failed_email
    result = send_trial_charge_failed_email(to="op@example.com", name="Tester")
    assert result is True


# ── send_trial_welcome_email ──────────────────────────────────────────────────

def test_trial_welcome_subject_contains_welcome(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_welcome_email
    send_trial_welcome_email(to="op@example.com", name="Bob Operator",
                             trial_end_iso_date="June 20, 2026")
    assert sent, "No email sent"
    assert "Welcome" in sent[0]["subject"]


def test_trial_welcome_body_contains_trial_end_date(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_welcome_email
    send_trial_welcome_email(to="op@example.com", name="Bob Operator",
                             trial_end_iso_date="June 20, 2026")
    assert "June 20, 2026" in sent[0]["html"]
    assert "June 20, 2026" in sent[0]["text"]


def test_trial_welcome_body_contains_clients_cta(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_welcome_email
    dashboard = "https://solaroperator.org/accounts"
    send_trial_welcome_email(to="op@example.com", name="Bob Operator",
                             trial_end_iso_date="June 20, 2026",
                             dashboard_url=dashboard)
    html = sent[0]["html"]
    text = sent[0]["text"]
    # CTA 1: add clients
    assert "clients" in html.lower()
    assert dashboard in html
    # CTA 2: GMP / auto-detect
    assert "green mountain power" in html.lower() or "gmp" in html.lower() or \
           "green mountain power" in text.lower()


def test_trial_welcome_body_contains_two_ctas(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_welcome_email
    send_trial_welcome_email(to="op@example.com", name="Bob Operator",
                             trial_end_iso_date="June 20, 2026")
    html = sent[0]["html"]
    # Both numbered list items present
    assert "Add your clients" in html
    assert "NEPOOL" in html or "Green Mountain Power" in html


def test_trial_welcome_body_no_charge_if_no_arrays(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_trial_welcome_email
    send_trial_welcome_email(to="op@example.com", name="Bob Operator",
                             trial_end_iso_date="June 20, 2026")
    combined = sent[0]["html"] + sent[0]["text"]
    assert "won't be charged" in combined


def test_trial_welcome_returns_true_on_success(monkeypatch):
    _capture_resend(monkeypatch)
    from api.notify import send_trial_welcome_email
    result = send_trial_welcome_email(to="op@example.com", name="Tester",
                                      trial_end_iso_date="July 1, 2026")
    assert result is True


# ── scheduler wires send_trial_charge_failed_email ────────────────────────────

def test_scheduler_calls_trial_charge_failed_on_stripe_error(monkeypatch):
    """When Stripe raises during trial-end charge, scheduler must call
    send_trial_charge_failed_email (not send_payment_failed_email)."""
    import secrets
    from datetime import datetime, timedelta
    from api.db import SessionLocal
    from api.models import Tenant, now as model_now

    tid = "ten_schfail_" + secrets.token_hex(4)
    with SessionLocal() as db:
        from api.models import Client, Array
        t = Tenant(
            id=tid,
            name="Charge Fail Solar",
            contact_email="failtest@example.com",
            tenant_key="sol_live_schfail_" + secrets.token_hex(8),
            plan="standard",
            active=True,
            subscription_status="trialing",
            stripe_customer_id="cus_schfail",
            stripe_payment_method_id="pm_schfail",
            trial_ends_at=datetime.utcnow() - timedelta(hours=1),
            trial_extended=False,
            created_at=model_now(),
            onboarding_stage="done",
        )
        db.add(t)
        db.flush()
        c = Client(tenant_id=tid, name="Fail Client", active=True, created_at=model_now())
        db.add(c)
        db.flush()
        db.add(Array(tenant_id=tid, client_id=c.id, name="Fail Array 1"))
        db.commit()

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_SETUP_PRICE_ID", "price_setup")
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_array")

    with patch("api.scheduler.stripe") as mock_stripe, \
         patch("api.scheduler.send_trial_charge_failed_email") as mock_failed, \
         patch("api.scheduler.send_internal_alert"):
        mock_stripe.Subscription.create.side_effect = Exception("card_declined")

        import api.scheduler as sched
        sched.finalize_expired_trials()

    assert mock_failed.called, "send_trial_charge_failed_email was not called"
    kwargs = mock_failed.call_args[1] if mock_failed.call_args[1] else {}
    args = mock_failed.call_args[0] if mock_failed.call_args[0] else ()
    # Accept both positional and keyword call styles
    to_val = kwargs.get("to") or (args[0] if args else None)
    name_val = kwargs.get("name") or (args[1] if len(args) > 1 else None)
    assert to_val == "failtest@example.com"
    assert name_val == "Charge Fail Solar"

    # Clean up: remove test tenant so it doesn't leak into subsequent finalize tests.
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        if t:
            t.subscription_status = "canceled"
            db.commit()
