"""
Unit tests for the payment_method.detached webhook handler.

Covers:
  1. Known PM detached → tenant row cleared, handler returns pm_cleared=True
  2. Unknown PM detached → ignored (no tenant matched)
"""
from __future__ import annotations

import json
import secrets

import api.stripe_webhook as wh
from api.db import SessionLocal
from api.models import Tenant


def _make_trialing_tenant(db, pm_id: str) -> Tenant:
    tid = "ten_" + secrets.token_hex(8)
    t = Tenant(
        id=tid,
        name="Detach Test Tenant",
        contact_email="detach@example.com",
        tenant_key="sol_live_" + secrets.token_hex(8),
        active=True,
        subscription_status="trialing",
        stripe_customer_id="cus_detach_test_" + secrets.token_hex(4),
        stripe_payment_method_id=pm_id,
    )
    db.add(t)
    db.commit()
    return t


def _post_detached_event(client, monkeypatch, pm_id: str,
                         event_id: str | None = None) -> dict:
    monkeypatch.setattr(wh, "send_internal_alert", lambda *a, **k: None)
    evt = {
        "id": event_id or ("evt_det_" + secrets.token_hex(4)),
        "type": "payment_method.detached",
        "data": {"object": {
            "id": pm_id,
            "object": "payment_method",
            "customer": None,
        }},
    }
    resp = client.post(
        "/v1/stripe/webhook",
        content=json.dumps(evt),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_payment_method_detached_clears_pm_on_tenant(client, monkeypatch):
    """Handler clears stripe_payment_method_id when a known PM is detached."""
    pm_id = "pm_det_" + secrets.token_hex(4)
    with SessionLocal() as db:
        t = _make_trialing_tenant(db, pm_id=pm_id)
        tid = t.id

    result = _post_detached_event(client, monkeypatch, pm_id=pm_id)
    assert result["ok"] is True
    assert result.get("pm_cleared") is True
    assert result.get("tenant") == tid

    with SessionLocal() as db:
        refreshed = db.get(Tenant, tid)
        assert refreshed.stripe_payment_method_id is None


def test_payment_method_detached_unknown_pm_is_ignored(client, monkeypatch):
    """Handler returns 'ignored' when the PM doesn't match any tenant row."""
    result = _post_detached_event(
        client, monkeypatch, pm_id="pm_nobody_" + secrets.token_hex(4)
    )
    assert result["ok"] is True
    assert "ignored" in result
