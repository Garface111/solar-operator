"""
no-upfront-payment — resume a 'paused_no_card' tenant once a card lands.

Two paths converge on stripe_helpers.create_subscription_for_tenant:
  - the setup_intent.succeeded webhook (auto-resume, no clicks), and
  - the POST /v1/account/resume-from-pause endpoint (manual fallback).

Both create the subscription (setup fee + per-array × current count), set
active=True, subscription_status='active'. Stripe is mocked.
"""
from __future__ import annotations

import json
import secrets
from unittest.mock import patch

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, Client, Array, now


def _make_paused_tenant(n_arrays: int = 3, pm_id: str | None = None) -> str:
    tid = "ten_pause_" + secrets.token_hex(4)
    with SessionLocal() as db:
        t = Tenant(
            id=tid,
            name="Paused Solar",
            contact_email=f"{tid}@pause.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard",
            active=False,
            subscription_status="paused_no_card",
            stripe_customer_id="cus_paused",
            stripe_payment_method_id=pm_id,
            trial_ends_at=None,
            created_at=now(),
        )
        db.add(t)
        db.flush()
        c = Client(tenant_id=tid, name="Paused Client", active=True)
        db.add(c)
        db.flush()
        for i in range(n_arrays):
            db.add(Array(tenant_id=tid, client_id=c.id, name=f"Array {i}"))
        db.commit()
    return tid


def _fire_setup_intent_webhook(client, tenant_id, *, pm="pm_resume",
                               customer="cus_paused", event_id="evt_si_1"):
    event = {
        "id": event_id,
        "type": "setup_intent.succeeded",
        "data": {"object": {
            "id": "seti_resume_1",
            "metadata": {"tenant_id": tenant_id},
            "customer": customer,
            "payment_method": pm,
        }},
    }
    return client.post(
        "/v1/stripe/webhook",
        content=json.dumps(event),
        headers={"content-type": "application/json"},
    )


def test_setup_intent_succeeded_resumes_paused_tenant(client, monkeypatch):
    """Webhook stores the card AND auto-resumes a paused_no_card tenant."""
    tid = _make_paused_tenant(n_arrays=3)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_SETUP_PRICE_ID", "price_setup")
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_array")

    with patch("api.stripe_helpers.stripe") as mock_stripe:
        mock_stripe.Subscription.create.return_value = {"id": "sub_resumed"}
        resp = _fire_setup_intent_webhook(client, tid)
        assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.stripe_payment_method_id == "pm_resume"
        assert t.subscription_status == "active"
        assert t.active is True
        assert t.stripe_subscription_id == "sub_resumed"
        assert t.trial_ends_at is None

    call = mock_stripe.Subscription.create.call_args[1]
    assert call["customer"] == "cus_paused"
    assert call["default_payment_method"] == "pm_resume"
    # Recurring per-array line on `items`; the $250 ONE-TIME setup fee on
    # `add_invoice_items` (Stripe rejects a one_time price in subscription items).
    qmap = {item["price"]: item["quantity"] for item in call["items"]}
    assert qmap["price_array"] == 3
    assert "price_setup" not in qmap
    inv_items = {i["price"]: i.get("quantity") for i in (call.get("add_invoice_items") or [])}
    assert inv_items["price_setup"] == 1


def test_setup_intent_succeeded_stores_card_without_resume_when_not_paused(client, monkeypatch):
    """For a trialing (not paused) tenant, the webhook just stores the card —
    no subscription is created early."""
    tid = "ten_trial_" + secrets.token_hex(4)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Trial Solar", contact_email=f"{tid}@trial.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12), plan="standard",
            active=True, subscription_status="trialing",
            stripe_customer_id="cus_trial", created_at=now(),
        ))
        db.commit()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")

    with patch("api.stripe_helpers.stripe") as mock_stripe:
        resp = _fire_setup_intent_webhook(client, tid, pm="pm_trial",
                                          customer="cus_trial", event_id="evt_si_2")
        assert resp.status_code == 200, resp.text
        mock_stripe.Subscription.create.assert_not_called()

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.stripe_payment_method_id == "pm_trial"
        assert t.subscription_status == "trialing"  # unchanged
        assert t.stripe_subscription_id is None


def test_resume_from_pause_endpoint(client, monkeypatch):
    """Manual fallback endpoint resumes a paused tenant that already has a card."""
    tid = _make_paused_tenant(n_arrays=2, pm_id="pm_already")
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_SETUP_PRICE_ID", "price_setup")
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_array")

    with patch("api.stripe_helpers.stripe") as mock_stripe:
        mock_stripe.Subscription.create.return_value = {"id": "sub_manual"}
        resp = client.post("/v1/account/resume-from-pause",
                           headers={"Authorization": auth})
        assert resp.status_code == 200, resp.text
        assert resp.json()["subscription_status"] == "active"
        assert resp.json()["active"] is True

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.subscription_status == "active"
        assert t.stripe_subscription_id == "sub_manual"


def test_resume_from_pause_requires_card(client):
    """No card on file → 400, don't try to create a subscription."""
    tid = _make_paused_tenant(n_arrays=1, pm_id=None)
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    resp = client.post("/v1/account/resume-from-pause",
                       headers={"Authorization": auth})
    assert resp.status_code == 400
