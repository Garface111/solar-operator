"""DELETE /v1/array-owners/arrays/{array_id} — soft-delete an owner array.

Contract (built against the array-owners surface, dual-auth like its siblings):
  DELETE /v1/array-owners/arrays/{array_id}
  Auth:  Authorization: Bearer <tenant-key | session token>
  200    {"ok": true, "array_id": <int>}  — array + its inverters soft-deleted
  401    unauthenticated
  404    not found / not owned by the caller's tenant
  403    the shared read-only DEMO tenant (require_not_demo)

Soft-delete ONLY: sets deleted_at on the Array AND its Inverter rows (never a
hard delete). The array vanishes from GET /v1/array-owners/fleet-tree the moment
deleted_at is set. AO billing is per-kWh metered, so deleting touches no Stripe.

Covers:
  1. Owner deletes own array  -> 200; deleted_at set on array AND its inverter
  2. After delete, fleet-tree no longer lists the array
  3. Cross-tenant delete       -> 404; victim array's deleted_at stays None
  4. Demo tenant delete        -> 403 (require_not_demo)
  5. Missing / already-deleted  -> 404
  6. Unauthenticated            -> 401
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, Inverter, Tenant


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _make_tenant(*, is_demo: bool = False) -> tuple[str, str]:
    """Create a fresh tenant; return (tenant_id, raw tenant_key)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Owners Delete Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True, is_demo=is_demo,
        ))
        db.commit()
    return tid, key


def _make_array(tenant_id: str, name: str = "Waterford") -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tenant_id, name=name, fuel_type="solar")
        db.add(arr)
        db.commit()
        return arr.id


def _make_inverter(tenant_id: str, array_id: int, serial: str) -> int:
    with SessionLocal() as db:
        iv = Inverter(
            tenant_id=tenant_id, array_id=array_id, vendor="fronius",
            serial=serial, source_array_id=array_id, position=1,
        )
        db.add(iv)
        db.commit()
        return iv.id


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ── 1. owner deletes own array -> 200; soft-delete on array AND inverter ──────

def test_owner_delete_soft_deletes_array_and_inverter(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Waterford")
    iv_id = _make_inverter(tid, arr_id, "dev-1")

    resp = client.delete(f"/v1/array-owners/arrays/{arr_id}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "array_id": arr_id}

    # Re-open the session: deleted_at must be set on the array AND its inverter,
    # and the rows must still EXIST (soft-delete, not a hard db.delete).
    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        assert arr is not None, "array was hard-deleted (must be soft-delete)"
        assert arr.deleted_at is not None
        iv = db.get(Inverter, iv_id)
        assert iv is not None, "inverter was hard-deleted (must be soft-delete)"
        assert iv.deleted_at is not None


# ── 2. after delete, fleet-tree no longer lists the array ─────────────────────

def test_deleted_array_vanishes_from_fleet_tree(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Timberworks")
    _make_inverter(tid, arr_id, "dev-7")

    # Present before delete.
    before = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    assert any(c["array_id"] == arr_id for c in before["columns"])

    resp = client.delete(f"/v1/array-owners/arrays/{arr_id}", headers=_auth(key))
    assert resp.status_code == 200, resp.text

    after = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    assert not any(c["array_id"] == arr_id for c in after["columns"]), \
        "soft-deleted array still listed in fleet-tree"
    names = {c["array_name"] for c in after["columns"]}
    assert "Timberworks" not in names


# ── 3. cross-tenant delete -> 404; victim array's deleted_at stays None ───────

def test_cross_tenant_delete_returns_404_and_leaves_array(client):
    tid_a, _key_a = _make_tenant()
    _tid_b, key_b = _make_tenant()
    arr_id = _make_array(tid_a, "Private Array")  # owned by tenant A

    # Tenant B tries to delete tenant A's array.
    resp = client.delete(f"/v1/array-owners/arrays/{arr_id}", headers=_auth(key_b))
    assert resp.status_code == 404, resp.text

    # A's array must be untouched.
    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        assert arr is not None
        assert arr.deleted_at is None


# ── 4. demo tenant delete -> 403 (require_not_demo) ───────────────────────────

def test_demo_tenant_cannot_delete(client):
    tid, key = _make_tenant(is_demo=True)
    arr_id = _make_array(tid, "Demo Array")

    resp = client.delete(f"/v1/array-owners/arrays/{arr_id}", headers=_auth(key))
    assert resp.status_code == 403, resp.text

    # The demo array must remain live.
    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        assert arr is not None
        assert arr.deleted_at is None


# ── 5. missing / already-deleted -> 404 ──────────────────────────────────────

def test_delete_missing_array_returns_404(client):
    _tid, key = _make_tenant()
    resp = client.delete("/v1/array-owners/arrays/99999", headers=_auth(key))
    assert resp.status_code == 404


def test_delete_is_idempotent_second_call_404(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "DeleteTwice")

    first = client.delete(f"/v1/array-owners/arrays/{arr_id}", headers=_auth(key))
    assert first.status_code == 200, first.text
    second = client.delete(f"/v1/array-owners/arrays/{arr_id}", headers=_auth(key))
    assert second.status_code == 404


# ── 6. unauthenticated -> 401 ─────────────────────────────────────────────────

def test_delete_requires_auth(client):
    tid, _key = _make_tenant()
    arr_id = _make_array(tid, "NoAuthArray")
    resp = client.delete(f"/v1/array-owners/arrays/{arr_id}")
    assert resp.status_code == 401
