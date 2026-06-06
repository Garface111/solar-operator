"""
Tests for contact_email persistence through the sandbox canvas endpoint.

Verifies: PATCH /v1/account/clients/{id} saves contact_email and
GET /v1/sandbox/canvas returns it in the client payload — so any
loadCanvas() reload includes the saved value.
"""
from __future__ import annotations

import secrets

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Client, Tenant


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="ContactEmail Test", contact_email=f"{tid}@test.com",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _make_client(tid: str, name: str) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name=name, active=True)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


# ── (1) PATCH saves contact_email, canvas returns it ─────────────────────────

def test_contact_email_survives_canvas_reload(client):
    tid, auth = _make_tenant()
    cid = _make_client(tid, "Sunrise Farm")

    # Set the contact email via the PATCH endpoint.
    r = client.patch(
        f"/v1/account/clients/{cid}",
        json={"contact_email": "owner@sunrisefarm.com"},
        headers={"Authorization": auth},
    )
    assert r.status_code == 200
    assert r.json()["client"]["contact_email"] == "owner@sunrisefarm.com"

    # Now simulate what the frontend does on loadCanvas: fetch the canvas
    # and confirm contact_email is present in the client payload.
    r2 = client.get("/v1/sandbox/canvas", headers={"Authorization": auth})
    assert r2.status_code == 200
    clients_out = r2.json()["clients"]
    sunrise = next((c for c in clients_out if c["id"] == cid), None)
    assert sunrise is not None, "client not found in canvas response"
    assert sunrise["contact_email"] == "owner@sunrisefarm.com"


# ── (2) Clearing contact_email works too ─────────────────────────────────────

def test_contact_email_can_be_cleared(client):
    tid, auth = _make_tenant()
    cid = _make_client(tid, "Valley Solar Co")

    # Set, then clear.
    client.patch(
        f"/v1/account/clients/{cid}",
        json={"contact_email": "mgr@valleysolar.com"},
        headers={"Authorization": auth},
    )
    r = client.patch(
        f"/v1/account/clients/{cid}",
        json={"contact_email": None},
        headers={"Authorization": auth},
    )
    assert r.status_code == 200
    assert r.json()["client"]["contact_email"] is None

    r2 = client.get("/v1/sandbox/canvas", headers={"Authorization": auth})
    clients_out = r2.json()["clients"]
    valley = next((c for c in clients_out if c["id"] == cid), None)
    assert valley is not None
    assert valley["contact_email"] is None
