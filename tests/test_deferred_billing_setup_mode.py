"""
Task 7 — Test 1: checkout creates a SetupIntent (setup mode) and the webhook
stores the payment method + sets trial_ends_at.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import api.onboarding as onboarding
from api.db import SessionLocal
from api.models import Tenant


# ─── mocks ───────────────────────────────────────────────────────────────

@pytest.fixture()
def mocks(monkeypatch):
    calls = {"internal_alert": [], "welcome": []}

    def fake_session_create(**kwargs):
        assert kwargs.get("mode") == "setup", "checkout must use setup mode"
        assert "line_items" not in kwargs, "setup mode must not have line_items"
        assert "subscription_data" not in kwargs, "setup mode must not have subscription_data"
        si_data = kwargs.get("setup_intent_data") or {}
        assert si_data.get("metadata", {}).get("onboarding_token"), "setup_intent_data.metadata.onboarding_token required"
        return SimpleNamespace(
            url="https://checkout.stripe.test/cs_setup_123",
            metadata=kwargs.get("metadata", {}),
        )

    monkeypatch.setattr(onboarding.stripe.checkout.Session, "create", fake_session_create)
    monkeypatch.setattr(onboarding, "STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(
        onboarding, "send_internal_alert",
        lambda *a, **k: calls["internal_alert"].append((a, k)) or True,
    )
    monkeypatch.setattr(
        onboarding, "send_welcome_email",
        lambda **kw: calls["welcome"].append(kw) or True,
    )
    return calls


def _do_checkout(client, email="setup@example.com"):
    resp = client.post("/v1/onboarding/checkout", json={
        "email": email,
        "full_name": "Sam Setup",
        "company": "Setup Solar",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _fire_setup_webhook(client, onboarding_token, monkeypatch,
                        pm_id="pm_test_abc", event_id="evt_setup_1"):
    """Simulate Stripe POSTing checkout.session.completed for a setup-mode session."""
    import api.stripe_webhook as wh

    def fake_si_retrieve(si_id):
        return {"id": si_id, "payment_method": pm_id}

    monkeypatch.setattr(wh.stripe.SetupIntent, "retrieve", fake_si_retrieve)
    monkeypatch.setattr(
        wh, "send_internal_alert",
        lambda *a, **k: None,
    )

    event = {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "mode": "setup",
            "metadata": {"onboarding_token": onboarding_token},
            "customer": "cus_setup_123",
            "setup_intent": "seti_test_123",
            "customer_email": "setup@example.com",
        }},
    }
    resp = client.post(
        "/v1/stripe/webhook",
        content=json.dumps(event),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── tests ────────────────────────────────────────────────────────────────

def test_checkout_uses_setup_mode(client, mocks):
    """checkout endpoint must use mode='setup' and not pass line_items."""
    body = _do_checkout(client, email="setup1@example.com")
    assert body["checkout_url"].startswith("https://checkout.stripe.test/")
    token = body["onboarding_token"]

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.onboarding_token == token)
        ).scalar_one()
        assert t.active is False
        assert t.subscription_status == "pending"
        assert t.trial_ends_at is None  # not set until webhook fires


def test_webhook_setup_mode_stores_pm_and_trial(client, mocks, monkeypatch):
    """Webhook for setup mode must store payment_method_id and set trial_ends_at."""
    token = _do_checkout(client, email="setup2@example.com")["onboarding_token"]
    result = _fire_setup_webhook(client, token, monkeypatch,
                                 pm_id="pm_test_xyz", event_id="evt_setup_2")
    assert result.get("tenant_activated")

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.onboarding_token == token)
        ).scalar_one()
        assert t.active is True
        assert t.subscription_status == "trialing"
        assert t.stripe_customer_id == "cus_setup_123"
        assert t.stripe_payment_method_id == "pm_test_xyz"
        assert t.trial_ends_at is not None
        # trial_ends_at should be ~14 days from now
        expected = datetime.utcnow() + timedelta(days=14)
        diff = abs((t.trial_ends_at - expected).total_seconds())
        assert diff < 60, f"trial_ends_at too far off: {t.trial_ends_at}"
        # No subscription created yet
        assert t.stripe_subscription_id is None


def test_webhook_setup_mode_no_welcome_email(client, mocks, monkeypatch):
    """Welcome email must NOT fire at webhook time (deferred to /complete)."""
    token = _do_checkout(client, email="setup3@example.com")["onboarding_token"]
    _fire_setup_webhook(client, token, monkeypatch, event_id="evt_setup_3")
    assert mocks["welcome"] == []


def test_checkout_missing_subscription_data(client, mocks):
    """Stripe session must not carry subscription_data in setup mode."""
    # The fake_session_create fixture already asserts this — just verify the
    # checkout call doesn't crash and returns a URL.
    body = _do_checkout(client, email="setup4@example.com")
    assert "checkout_url" in body
