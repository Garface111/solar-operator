"""
Tests for POST /v1/sandbox/array/reassign — array-level drag in the sandbox canvas.

Verifies: success path, cross-tenant rejection, missing target client, sub-meter
array preserves UtilityAccount→Array linkage on move, and null target = unclassify.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant, UtilityAccount


def _make_tenant() -> tuple[str, str]:
    """Create a fresh tenant; return (tenant_id, 'Bearer <session_token>')."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="ArrayReassign Test", contact_email=f"{tid}@test.com",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _post(http_client, auth: str, body: dict):
    return http_client.post(
        "/v1/sandbox/array/reassign",
        json=body,
        headers={"Authorization": auth},
    )


# ── (1) Success path ───────────────────────────────────────────────────────────

def test_array_reassign_success(client):
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        c_a = Client(tenant_id=tid, name="Client A", active=True)
        c_b = Client(tenant_id=tid, name="Client B", active=True)
        db.add_all([c_a, c_b])
        db.flush()
        arr = Array(tenant_id=tid, client_id=c_a.id, name="Meadow Solar")
        db.add(arr)
        db.commit()
        arr_id, ca_id, cb_id = arr.id, c_a.id, c_b.id

    resp = _post(client, auth, {"array_id": arr_id, "client_id": cb_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["array_id"] == arr_id
    assert body["client_id"] == cb_id
    assert body["prior_client_id"] == ca_id

    with SessionLocal() as db:
        arr_row = db.get(Array, arr_id)
        assert arr_row.client_id == cb_id
        assert arr_row.reassigned_at is not None


# ── (2) Cross-tenant rejection ─────────────────────────────────────────────────

def test_array_reassign_wrong_tenant_rejected(client):
    tid_a, auth_a = _make_tenant()
    tid_b, _ = _make_tenant()
    with SessionLocal() as db:
        c_a = Client(tenant_id=tid_a, name="Owner Client", active=True)
        c_b = Client(tenant_id=tid_b, name="Other Client", active=True)
        db.add_all([c_a, c_b])
        db.flush()
        arr = Array(tenant_id=tid_a, client_id=c_a.id, name="Wind Ridge")
        db.add(arr)
        db.commit()
        arr_id, cb_id = arr.id, c_b.id

    # tenant_a auth, but targeting tenant_b's client — should 404
    resp = _post(client, auth_a, {"array_id": arr_id, "client_id": cb_id})
    assert resp.status_code == 404


# ── (3) Missing target client ──────────────────────────────────────────────────

def test_array_reassign_nonexistent_client_rejected(client):
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Home", active=True)
        db.add(c)
        db.flush()
        arr = Array(tenant_id=tid, client_id=c.id, name="Oak Hill")
        db.add(arr)
        db.commit()
        arr_id = arr.id

    resp = _post(client, auth, {"array_id": arr_id, "client_id": 99999999})
    assert resp.status_code == 404


# ── (4) Sub-meter: UtilityAccount→Array linkage is preserved on move ───────────

def test_submeter_array_move_preserves_account_links(client):
    """Moving a sub-meter array (3 accounts → 1 Array) keeps all UtilityAccount.array_id
    pointers intact. The array just moves to a new client; bill-data links unchanged."""
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        c_src = Client(tenant_id=tid, name="Starlake Source", active=True)
        c_dst = Client(tenant_id=tid, name="Starlake Dest", active=True)
        db.add_all([c_src, c_dst])
        db.flush()
        arr = Array(tenant_id=tid, client_id=c_src.id, name="Starlake")
        db.add(arr)
        db.flush()
        sub1 = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp", account_number="S-001")
        sub2 = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp", account_number="S-002")
        sub3 = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp", account_number="S-003")
        db.add_all([sub1, sub2, sub3])
        db.commit()
        arr_id = arr.id
        dst_id = c_dst.id
        acc_ids = [sub1.id, sub2.id, sub3.id]

    resp = _post(client, auth, {"array_id": arr_id, "client_id": dst_id})
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        # Array moved to new client
        arr_row = db.get(Array, arr_id)
        assert arr_row.client_id == dst_id
        # All UtilityAccount.array_id pointers unchanged — bill data preserved
        for aid in acc_ids:
            acc = db.get(UtilityAccount, aid)
            assert acc.array_id == arr_id, f"account {aid} lost its array link"


# ── (5) Null target = unclassify ───────────────────────────────────────────────

def test_array_reassign_null_unclassifies(client):
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Parent", active=True)
        db.add(c)
        db.flush()
        arr = Array(tenant_id=tid, client_id=c.id, name="Free Agent Array")
        db.add(arr)
        db.commit()
        arr_id, c_id = arr.id, c.id

    resp = _post(client, auth, {"array_id": arr_id, "client_id": None})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["client_id"] is None
    assert body["prior_client_id"] == c_id

    with SessionLocal() as db:
        arr_row = db.get(Array, arr_id)
        assert arr_row.client_id is None
        assert arr_row.reassigned_at is not None
