"""
Task 7 — Test 4: cancel-trial endpoint detaches PM and tombstones the tenant.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

import api.onboarding as onboarding
import api.account as account
from api.db import SessionLocal
from api.models import Tenant, now


def _make_trialing_tenant(email: str) -> tuple[str, str]:
    """Returns (tenant_id, session_token)."""
    import secrets
    tid = "ten_cancel_" + secrets.token_hex(4)
    with SessionLocal() as db:
        t = Tenant(
            id=tid,
            name="Cancel Test Solar",
            contact_email=email,
            tenant_key="sol_live_cancel_" + secrets.token_hex(8),
            plan="standard",
            active=True,
            subscription_status="trialing",
            stripe_customer_id="cus_cancel_test",
            stripe_payment_method_id="pm_cancel_test",
            trial_ends_at=datetime.utcnow() + timedelta(days=3),
            trial_extended=False,
            created_at=now(),
            onboarding_stage="done",
        )
        db.add(t)
        db.commit()
    session_token = account.mint_session_for_tenant(tid)
    return tid, session_token


def test_cancel_trial_detaches_pm_and_deactivates(client, monkeypatch):
    """cancel-trial must detach the PM via Stripe and set active=False."""
    tid, session_token = _make_trialing_tenant("cancel1@example.com")

    detached = []

    def fake_detach(pm_id):
        detached.append(pm_id)
        return {"id": pm_id, "customer": None}

    monkeypatch.setattr(onboarding.stripe.PaymentMethod, "detach", fake_detach)
    monkeypatch.setattr(
        onboarding, "send_internal_alert",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(onboarding, "STRIPE_SECRET_KEY", "sk_test_dummy")

    resp = client.post(
        "/v1/onboarding/cancel-trial",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # PM was detached
    assert "pm_cancel_test" in detached

    # Tenant is now inactive + cancelled
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.active is False
        assert t.subscription_status == "cancelled"
        assert t.trial_ends_at is None
        assert t.stripe_payment_method_id is None


def test_cancel_trial_rejects_non_trialing(client, monkeypatch):
    """Tenants not in 'trialing' state must get a 400."""
    import secrets
    tid = "ten_nocancel_" + secrets.token_hex(4)
    with SessionLocal() as db:
        t = Tenant(
            id=tid,
            name="Active Tenant",
            contact_email="nocancel@example.com",
            tenant_key="sol_live_nc_" + secrets.token_hex(8),
            plan="standard",
            active=True,
            subscription_status="active",  # NOT trialing
            created_at=now(),
            onboarding_stage="done",
        )
        db.add(t)
        db.commit()
    session_token = account.mint_session_for_tenant(tid)

    monkeypatch.setattr(onboarding, "STRIPE_SECRET_KEY", "sk_test_dummy")

    resp = client.post(
        "/v1/onboarding/cancel-trial",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 400


def test_cancel_trial_requires_auth(client):
    """No auth header → 401."""
    resp = client.post("/v1/onboarding/cancel-trial")
    assert resp.status_code == 401
