"""
Task 7 — Test 3: zero-array trial expiry behavior.
  - First run: extends trial 3 days, sends add-array email.
  - Second run (still no arrays): min-bills at quantity=1.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, now


def _make_trialing_tenant(email: str, trial_extended: bool = False) -> str:
    import secrets
    tid = "ten_zero_" + secrets.token_hex(4)
    with SessionLocal() as db:
        t = Tenant(
            id=tid,
            name="Zero Arrays Solar",
            contact_email=email,
            tenant_key="sol_live_zero_" + secrets.token_hex(8),
            plan="standard",
            active=True,
            subscription_status="trialing",
            stripe_customer_id="cus_zero_test",
            stripe_payment_method_id="pm_zero_test",
            trial_ends_at=datetime.utcnow() - timedelta(hours=1),  # just expired
            trial_extended=trial_extended,
            created_at=now(),
            onboarding_stage="done",
        )
        db.add(t)
        db.commit()
    return tid


def test_first_run_extends_trial_and_sends_email():
    """When zero arrays and not yet extended: extend 3 days, send email."""
    tid = _make_trialing_tenant("zero1@example.com", trial_extended=False)

    with patch("api.scheduler.stripe") as mock_stripe, \
         patch("api.scheduler.send_add_first_array_email") as mock_email, \
         patch("api.scheduler.send_internal_alert"):
        import api.scheduler as sched
        sched.finalize_expired_trials()
        mock_stripe.Subscription.create.assert_not_called()
        assert mock_email.called

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.subscription_status == "trialing"  # still trialing
        assert t.trial_extended is True
        # trial_ends_at pushed forward ~3 days from the original expiry
        assert t.trial_ends_at is not None
        diff = (t.trial_ends_at - datetime.utcnow()).total_seconds()
        # Should be ~3 days minus the 1h we were past expiry → ~2.9 days
        assert diff > 2 * 24 * 3600, "trial_ends_at not pushed forward enough"


def test_second_run_bills_minimum_when_still_no_arrays():
    """After trial_extended=True, create subscription at quantity=1 (minimum)."""
    tid = _make_trialing_tenant("zero2@example.com", trial_extended=True)

    fake_sub = {"id": "sub_min_1", "latest_invoice": None}

    with patch("api.scheduler.stripe") as mock_stripe, \
         patch("api.scheduler.send_trial_charged_email") as mock_charged, \
         patch("api.scheduler.send_internal_alert"):
        mock_stripe.Subscription.create.return_value = fake_sub
        mock_stripe.Invoice.retrieve.side_effect = Exception("no invoice")

        import api.scheduler as sched
        sched.finalize_expired_trials()

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.subscription_status == "active"
        assert t.stripe_subscription_id == "sub_min_1"
        assert t.trial_ends_at is None

    call_kwargs = mock_stripe.Subscription.create.call_args[1]
    # quantity for the array price must be 1 (minimum)
    items = call_kwargs["items"]
    for item in items:
        if "price_array" in item.get("price", ""):
            assert item["quantity"] == 1
    assert mock_charged.called


def test_first_run_does_not_extend_when_already_extended():
    """trial_extended=True but no arrays → skip extension, go straight to billing."""
    # Same as second_run test — just confirming the logic path.
    tid = _make_trialing_tenant("zero3@example.com", trial_extended=True)

    fake_sub = {"id": "sub_min_2", "latest_invoice": None}

    with patch("api.scheduler.stripe") as mock_stripe, \
         patch("api.scheduler.send_add_first_array_email") as mock_add_email, \
         patch("api.scheduler.send_trial_charged_email"), \
         patch("api.scheduler.send_internal_alert"):
        mock_stripe.Subscription.create.return_value = fake_sub
        mock_stripe.Invoice.retrieve.side_effect = Exception("no invoice")

        import api.scheduler as sched
        sched.finalize_expired_trials()

    mock_add_email.assert_not_called()  # no second extension
    mock_stripe.Subscription.create.assert_called_once()
