"""
Tests for GET /v1/account/clients — the CaptureCeremony welcome-reveal
data source and main dashboard client list.

Uses session-token auth (mint_session_for_tenant) rather than the extension
bearer-token auth used by /v1/sync.  Verifies correct shape, empty-state,
soft-delete exclusion, placeholder flag, and array_count accuracy.
"""
from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant


def _tenant_with_session() -> tuple[str, str]:
    """Create a fresh tenant; return (tenant_id, 'Bearer <session_token>')."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="ClientEP Test", contact_email=f"{tid}@test.com",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _get_clients(client, auth: str):
    return client.get("/v1/account/clients", headers={"Authorization": auth})


# ── (1) Empty state ────────────────────────────────────────────────────────────

def test_empty_state_returns_empty_list(client):
    _, auth = _tenant_with_session()
    resp = _get_clients(client, auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["clients"] == []


# ── (2) Correct response shape ─────────────────────────────────────────────────

def test_list_clients_returns_correct_shape(client):
    tid, auth = _tenant_with_session()
    with SessionLocal() as db:
        db.add(Client(
            tenant_id=tid, name="Maple Farm",
            contact_email="maple@farm.test",
            gmp_email="gmp@farm.test", gmp_autopopulate=True,
            active=True,
        ))
        db.commit()

    resp = _get_clients(client, auth)
    assert resp.status_code == 200, resp.text
    clients = resp.json()["clients"]
    assert len(clients) == 1
    c = clients[0]

    assert c["name"] == "Maple Farm"
    assert c["contact_email"] == "maple@farm.test"
    assert c["active"] is True
    assert c["array_count"] == 0
    assert c["gmp_email"] == "gmp@farm.test"
    assert c["gmp_autopopulate"] is True
    assert c["is_placeholder"] is False
    # All nullable delivery/sync fields present in payload
    for field in ("last_delivery_at", "gmp_last_sync_at", "vec_email",
                  "vec_autopopulate", "vec_last_sync_at",
                  "last_delivered_at", "last_bounced_at", "last_bounce_reason"):
        assert field in c, f"field {field!r} missing from client response"


# ── (3) Soft-deleted clients are excluded ──────────────────────────────────────

def test_soft_deleted_clients_excluded(client):
    tid, auth = _tenant_with_session()
    with SessionLocal() as db:
        db.add(Client(tenant_id=tid, name="Visible Client", active=True))
        db.add(Client(
            tenant_id=tid, name="Deleted Client", active=True,
            deleted_at=datetime(2024, 1, 1),
        ))
        db.commit()

    resp = _get_clients(client, auth)
    clients = resp.json()["clients"]
    assert len(clients) == 1
    assert clients[0]["name"] == "Visible Client"


# ── (4) Placeholder flag exposed in response ────────────────────────────────────

def test_placeholder_flag_exposed(client):
    tid, auth = _tenant_with_session()
    with SessionLocal() as db:
        db.add(Client(
            tenant_id=tid, name="Your first client",
            is_placeholder=True, active=True,
        ))
        db.commit()

    resp = _get_clients(client, auth)
    clients = resp.json()["clients"]
    assert len(clients) == 1
    assert clients[0]["is_placeholder"] is True


# ── (5) array_count reflects actual live arrays ─────────────────────────────────

def test_array_count_is_accurate(client):
    tid, auth = _tenant_with_session()
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Array Count Test", active=True)
        db.add(c); db.flush()
        db.add(Array(tenant_id=tid, client_id=c.id, name="Array A"))
        db.add(Array(tenant_id=tid, client_id=c.id, name="Array B"))
        db.commit()

    resp = _get_clients(client, auth)
    clients = resp.json()["clients"]
    assert len(clients) == 1
    assert clients[0]["array_count"] == 2
