"""
Status-aware mutation gating for paused_no_card tenants (SWEEP Task 1).

A tenant auto-paused at trial end for lack of a card
(subscription_status == 'paused_no_card') must be read-only over the API:
GETs work, data mutations return 402, but the two unpause paths
(add-payment-method / resume-from-pause) stay open so the operator can get out.
Comped (Bruce) and active tenants are never gated by this guard.
"""
from __future__ import annotations

import secrets
from unittest.mock import patch

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, Client, now


def _make_tenant(status: str, *, active: bool, pm_id: str | None = None) -> str:
    tid = f"ten_gate_{secrets.token_hex(4)}"
    with SessionLocal() as db:
        t = Tenant(
            id=tid,
            name="Gate Co",
            company_name="Gate Co",
            contact_email=f"{tid}@gate.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard",
            active=active,
            subscription_status=status,
            stripe_customer_id="cus_gate",
            stripe_payment_method_id=pm_id,
            created_at=now(),
        )
        db.add(t)
        db.flush()
        db.add(Client(tenant_id=tid, name="Existing Client", active=True))
        db.commit()
    return tid


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def test_paused_tenant_post_client_is_402(client):
    tid = _make_tenant("paused_no_card", active=False)
    resp = client.post("/v1/account/clients", json={"name": "New Client"},
                       headers=_auth(tid))
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["error"] == "paused_no_card"


def test_paused_tenant_get_clients_is_200(client):
    tid = _make_tenant("paused_no_card", active=False)
    resp = client.get("/v1/account/clients", headers=_auth(tid))
    assert resp.status_code == 200, resp.text


def test_paused_tenant_add_payment_method_is_200(client, monkeypatch):
    """The unpause path must work for a paused tenant — it's how they resume."""
    tid = _make_tenant("paused_no_card", active=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    with patch("api.account.stripe") as mock_stripe:
        mock_stripe.checkout.Session.create.return_value = {
            "url": "https://checkout.stripe.test/sess"}
        resp = client.post("/v1/account/add-payment-method", headers=_auth(tid))
    assert resp.status_code == 200, resp.text
    assert resp.json()["checkout_url"] == "https://checkout.stripe.test/sess"


def test_paused_tenant_resume_from_pause_not_gated(client):
    """resume-from-pause must not be gated. With no card it 400s (card required),
    NOT 402 — proving the paused guard isn't applied to the resume path."""
    tid = _make_tenant("paused_no_card", active=False, pm_id=None)
    resp = client.post("/v1/account/resume-from-pause", headers=_auth(tid))
    assert resp.status_code == 400, resp.text


def test_active_tenant_post_client_is_200(client):
    tid = _make_tenant("active", active=True)
    resp = client.post("/v1/account/clients", json={"name": "Active New Client"},
                       headers=_auth(tid))
    assert resp.status_code == 200, resp.text


def test_comped_tenant_post_client_is_200(client):
    """Comped pilot (Bruce) is never paused — the guard must let comped through."""
    tid = _make_tenant("comped", active=True)
    resp = client.post("/v1/account/clients", json={"name": "Comped New Client"},
                       headers=_auth(tid))
    assert resp.status_code == 200, resp.text


def test_paused_tenant_create_array_is_402(client):
    """Breadth check: array mutation is gated too, not just clients."""
    tid = _make_tenant("paused_no_card", active=False)
    with SessionLocal() as db:
        cid = db.execute(
            select(Client.id).where(Client.tenant_id == tid)
        ).scalars().first()
    resp = client.post(
        f"/v1/account/clients/{cid}/arrays",
        json={"name": "Gated Array"},
        headers=_auth(tid),
    )
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["error"] == "paused_no_card"
