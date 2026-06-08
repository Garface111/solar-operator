"""
Task 7 — Test 2: trial-end cron creates subscription for tenants with arrays.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array, now


def _make_trialing_tenant(db, email: str, pm_id: str = "pm_test_fin",
                           trial_offset_days: int = -1) -> Tenant:
    """Insert a trialing tenant whose trial has already expired (by default)."""
    import secrets
    tid = "ten_fin_" + secrets.token_hex(4)
    t = Tenant(
        id=tid,
        name="Finalization Solar",
        contact_email=email,
        tenant_key="sol_live_fin_" + secrets.token_hex(8),
        plan="standard",
        active=True,
        subscription_status="trialing",
        stripe_customer_id="cus_fin_test",
        stripe_payment_method_id=pm_id,
        trial_ends_at=datetime.utcnow() + timedelta(days=trial_offset_days),
        trial_extended=False,
        created_at=now(),
        onboarding_stage="done",
    )
    db.add(t)
    return t


def _add_client_with_arrays(db, tenant_id: str, n_arrays: int) -> Client:
    c = Client(
        tenant_id=tenant_id,
        name="Test Client",
        active=True,
        created_at=now(),
    )
    db.add(c)
    db.flush()
    for i in range(n_arrays):
        db.add(Array(
            tenant_id=tenant_id,
            client_id=c.id,
            name=f"Test Array {i}",
        ))
    return c


def test_finalize_creates_subscription_for_tenant_with_arrays(monkeypatch):
    """When trial expires and tenant has arrays, finalize_expired_trials must
    create a Stripe subscription and mark the tenant active."""
    with SessionLocal() as db:
        t = _make_trialing_tenant(db, "fin1@example.com")
        tid = t.id
        _add_client_with_arrays(db, tid, 3)
        db.commit()

    fake_sub = {
        "id": "sub_fin_new",
        "latest_invoice": "in_fin_1",
    }
    fake_inv = {"amount_due": 13500}

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_SETUP_PRICE_ID", "price_setup")
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_array")

    with patch("api.scheduler.stripe") as mock_stripe, \
         patch("api.scheduler.send_trial_charged_email") as mock_charged, \
         patch("api.scheduler.send_internal_alert"):
        mock_stripe.Subscription.create.return_value = fake_sub
        mock_stripe.Invoice.retrieve.return_value = fake_inv

        import api.scheduler as sched
        sched.finalize_expired_trials()

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.subscription_status == "active"
        assert t.stripe_subscription_id == "sub_fin_new"
        assert t.trial_ends_at is None

    call_kwargs = mock_stripe.Subscription.create.call_args[1]
    assert call_kwargs["customer"] == "cus_fin_test"
    assert call_kwargs["default_payment_method"] == "pm_test_fin"
    items = call_kwargs["items"]
    quantities = {item["price"]: item["quantity"] for item in items}
    assert quantities.get("price_array") == 3
    assert mock_charged.called


def test_finalize_pauses_tenant_with_no_card(monkeypatch):
    """No-upfront-payment: trial expires with arrays but NO card on file →
    auto-pause (paused_no_card, active=False, trial cleared), email sent, and NO
    Stripe.Subscription.create call."""
    with SessionLocal() as db:
        t = _make_trialing_tenant(db, "nocard@example.com", pm_id=None)
        tid = t.id
        _add_client_with_arrays(db, tid, 2)
        db.commit()

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_SETUP_PRICE_ID", "price_setup")
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_array")

    with patch("api.scheduler.stripe") as mock_stripe, \
         patch("api.scheduler.send_trial_paused_no_card_email") as mock_paused, \
         patch("api.scheduler.send_trial_charged_email") as mock_charged, \
         patch("api.scheduler.send_internal_alert"):
        import api.scheduler as sched
        sched.finalize_expired_trials()
        mock_stripe.Subscription.create.assert_not_called()
        assert mock_paused.called
        mock_charged.assert_not_called()

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.subscription_status == "paused_no_card"
        assert t.active is False
        assert t.trial_ends_at is None
        # Nothing deleted — the arrays are still there.
        n = db.execute(
            select(Array).where(Array.tenant_id == tid)
        ).scalars().all()
        assert len(n) == 2


def test_finalize_skips_future_trials(monkeypatch):
    """Tenants whose trial_ends_at is in the future must NOT be processed."""
    with SessionLocal() as db:
        t = _make_trialing_tenant(db, "fin_future@example.com",
                                   trial_offset_days=2)
        tid = t.id
        _add_client_with_arrays(db, tid, 1)
        db.commit()

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")

    with patch("api.scheduler.stripe") as mock_stripe, \
         patch("api.scheduler.send_internal_alert"), \
         patch("api.scheduler.send_trial_charged_email"):
        import api.scheduler as sched
        sched.finalize_expired_trials()
        mock_stripe.Subscription.create.assert_not_called()

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.subscription_status == "trialing"  # unchanged
