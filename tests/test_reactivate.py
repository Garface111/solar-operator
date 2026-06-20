"""
Reactivation — a CANCELLED account starts a fresh PAID subscription with NO trial.

When an operator cancels (cancel-trial detaches the card → active=False,
subscription_status='cancelled'; or a Stripe webhook cancel → 'canceled'), the
cancelled-account gate prompts them to begin their subscription again. They add a
card via POST /v1/account/reactivate (Stripe Checkout, mode=setup). The
setup_intent.succeeded webhook then stores the card and, seeing the tenant is
cancelled, calls create_subscription_for_tenant — which creates a no-trial paid
subscription and flips the tenant back to active. Stripe is mocked.
"""
from __future__ import annotations

import json
import secrets
from unittest.mock import patch

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, Client, Array, now


def _make_cancelled_tenant(
    *, product: str = "nepool", status: str = "cancelled",
    n_arrays: int = 2, sub_id: str | None = None,
) -> str:
    tid = "ten_cxl_" + secrets.token_hex(4)
    with SessionLocal() as db:
        t = Tenant(
            id=tid,
            name="Cancelled Co",
            contact_email=f"{tid}@cxl.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard",
            product=product,
            active=False,
            subscription_status=status,
            stripe_customer_id="cus_cxl",
            stripe_subscription_id=sub_id,
            stripe_payment_method_id=None,   # cancel detached the card
            trial_ends_at=None,
            created_at=now(),
        )
        db.add(t)
        db.flush()
        c = Client(tenant_id=tid, name="Cancelled Client", active=True)
        db.add(c)
        db.flush()
        for i in range(n_arrays):
            db.add(Array(tenant_id=tid, client_id=c.id, name=f"Array {i}"))
        db.commit()
    return tid


def _fire_setup_intent_webhook(client, tenant_id, *, pm="pm_reac",
                               customer="cus_cxl", event_id="evt_reac_1",
                               reactivate=True):
    meta = {"tenant_id": tenant_id}
    if reactivate:
        meta["reactivate"] = "1"
    event = {
        "id": event_id,
        "type": "setup_intent.succeeded",
        "data": {"object": {
            "id": "seti_reac_1",
            "metadata": meta,
            "customer": customer,
            "payment_method": pm,
        }},
    }
    return client.post(
        "/v1/stripe/webhook",
        content=json.dumps(event),
        headers={"content-type": "application/json"},
    )


def test_reactivate_nepool_creates_no_trial_subscription(client, monkeypatch):
    """A cancelled NEPOOL tenant adding a card → paid, no-trial subscription."""
    tid = _make_cancelled_tenant(product="nepool", status="cancelled", n_arrays=3)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "***")
    monkeypatch.setenv("STRIPE_SETUP_PRICE_ID", "price_setup")
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_array")

    with patch("api.stripe_helpers.stripe") as mock_stripe:
        mock_stripe.Subscription.create.return_value = {"id": "sub_reac"}
        resp = _fire_setup_intent_webhook(client, tid)
        assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.subscription_status == "active"
        assert t.active is True
        assert t.stripe_subscription_id == "sub_reac"
        assert t.trial_ends_at is None      # NO new trial

    call = mock_stripe.Subscription.create.call_args[1]
    # No trial_period_days anywhere — billing starts immediately.
    assert "trial_period_days" not in call
    qmap = {item["price"]: item.get("quantity") for item in call["items"]}
    assert qmap["price_array"] == 3
    assert qmap["price_setup"] == 1


def test_reactivate_array_operator_metered_no_trial(client, monkeypatch):
    """A cancelled Array Operator tenant reactivates onto the per-kWh metered
    line — no setup fee, no quantity, no trial."""
    tid = _make_cancelled_tenant(product="array_operator", status="canceled", n_arrays=5)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "***")
    monkeypatch.setenv("STRIPE_AO_KWH_PRICE_ID", "price_kwh")

    with patch("api.stripe_helpers.stripe") as mock_stripe:
        mock_stripe.Subscription.create.return_value = {"id": "sub_ao_reac"}
        resp = _fire_setup_intent_webhook(client, tid, event_id="evt_reac_ao")
        assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.subscription_status == "active"
        assert t.active is True
        assert t.trial_ends_at is None

    call = mock_stripe.Subscription.create.call_args[1]
    assert "trial_period_days" not in call
    # Metered: a single line with NO quantity, no setup fee.
    items = call["items"]
    assert len(items) == 1
    assert "quantity" not in items[0]


def test_reactivate_endpoint_rejects_non_cancelled(client, monkeypatch):
    """/v1/account/reactivate 400s for an active/trialing tenant."""
    tid = "ten_active_" + secrets.token_hex(4)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Active Co", contact_email=f"{tid}@a.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12), plan="standard",
            active=True, subscription_status="active",
            stripe_customer_id="cus_a", created_at=now(),
        ))
        db.commit()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "***")
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    resp = client.post("/v1/account/reactivate", headers={"Authorization": auth})
    assert resp.status_code == 400


def test_reactivate_endpoint_returns_checkout_for_cancelled(client, monkeypatch):
    """/v1/account/reactivate returns a Stripe Checkout URL for a cancelled tenant."""
    tid = _make_cancelled_tenant(product="nepool", status="cancelled", n_arrays=1)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "***")
    auth = f"Bearer {mint_session_for_tenant(tid)}"

    with patch("api.account.stripe") as mock_stripe:
        mock_stripe.checkout.Session.create.return_value = {"url": "https://checkout.stripe.test/reac"}
        resp = client.post("/v1/account/reactivate", headers={"Authorization": auth})
        assert resp.status_code == 200, resp.text
        assert resp.json()["checkout_url"] == "https://checkout.stripe.test/reac"

    call = mock_stripe.checkout.Session.create.call_args[1]
    assert call["mode"] == "setup"
    assert call["metadata"]["reactivate"] == "1"
