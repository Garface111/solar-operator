"""DELETE /v1/array-owners/inverters/{inverter_id} — soft-delete one inverter.

Contract (dual-auth, mirrors the array-delete surface):
  DELETE /v1/array-owners/inverters/{inverter_id}
  Auth:  Authorization: Bearer <tenant_key | session token>
  200    {"ok": true, "inverter_id": <int>}  — that ONE inverter soft-deleted
  401    unauthenticated
  404    not found / not owned by the caller's tenant / already deleted
  403    the shared read-only DEMO tenant (require_not_demo)

  POST /v1/array-owners/inverters/{inverter_id}/restore  — un-delete (undo)

Soft-delete ONLY: sets deleted_at on the one Inverter row; the parent array and
its siblings are untouched. AO billing is per-kWh metered, so deleting an
inverter touches no Stripe.

Covers:
  1. Owner deletes one inverter -> 200; that inverter soft-deleted, sibling +
     array untouched
  2. After delete, fleet-tree no longer lists that inverter (array stays)
  3. Restore revives exactly that inverter
  4. Cross-tenant delete       -> 404; victim inverter's deleted_at stays None
  5. Demo tenant delete        -> 403 (require_not_demo)
  6. Missing / already-deleted  -> 404
  7. Unauthenticated            -> 401
"""
from __future__ import annotations

import secrets

from api.db import SessionLocal
from api.models import Array, Inverter, Tenant


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _make_tenant(*, is_demo: bool = False) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Inverter Delete Test", contact_email=f"{key}@t.test",
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


def _make_inverter(tenant_id: str, array_id: int, serial: str, position: int = 1) -> int:
    with SessionLocal() as db:
        iv = Inverter(
            tenant_id=tenant_id, array_id=array_id, vendor="sma",
            serial=serial, source_array_id=array_id, position=position,
        )
        db.add(iv)
        db.commit()
        return iv.id


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ── 1. owner deletes one inverter -> 200; sibling + array untouched ───────────

def test_owner_delete_soft_deletes_only_that_inverter(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Timberworks")
    iv1 = _make_inverter(tid, arr_id, "dev-1", 1)
    iv2 = _make_inverter(tid, arr_id, "dev-2", 2)

    resp = client.delete(f"/v1/array-owners/inverters/{iv1}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "inverter_id": iv1}

    with SessionLocal() as db:
        # target soft-deleted, still EXISTS (not hard-deleted)
        d = db.get(Inverter, iv1)
        assert d is not None, "inverter was hard-deleted (must be soft-delete)"
        assert d.deleted_at is not None
        # sibling untouched
        s = db.get(Inverter, iv2)
        assert s is not None and s.deleted_at is None
        # parent array untouched
        arr = db.get(Array, arr_id)
        assert arr is not None and arr.deleted_at is None


# ── 2. after delete, fleet-tree drops the inverter but keeps the array ────────

def test_deleted_inverter_vanishes_but_array_remains(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Burke Mountain Lot")
    iv1 = _make_inverter(tid, arr_id, "keep-me", 1)
    iv2 = _make_inverter(tid, arr_id, "delete-me", 2)

    resp = client.delete(f"/v1/array-owners/inverters/{iv2}", headers=_auth(key))
    assert resp.status_code == 200, resp.text

    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    col = next((c for c in tree["columns"] if c["array_id"] == arr_id), None)
    assert col is not None, "array disappeared (only the inverter should be gone)"
    inv_ids = {i["inverter_id"] for i in col.get("inverters", [])}
    assert iv1 in inv_ids, "surviving inverter missing from fleet-tree"
    assert iv2 not in inv_ids, "soft-deleted inverter still listed in fleet-tree"


# ── 3. restore revives exactly that inverter ──────────────────────────────────

def test_delete_then_restore_inverter_roundtrips(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Newport Lakefront")
    iv = _make_inverter(tid, arr_id, "rt-1", 1)

    d = client.delete(f"/v1/array-owners/inverters/{iv}", headers=_auth(key))
    assert d.status_code == 200, d.text
    with SessionLocal() as db:
        assert db.get(Inverter, iv).deleted_at is not None

    r = client.post(f"/v1/array-owners/inverters/{iv}/restore", headers=_auth(key))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "inverter_id": iv}
    with SessionLocal() as db:
        assert db.get(Inverter, iv).deleted_at is None

    # reappears in fleet-tree
    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    col = next((c for c in tree["columns"] if c["array_id"] == arr_id), None)
    assert col is not None
    assert iv in {i["inverter_id"] for i in col.get("inverters", [])}


# ── 4. cross-tenant delete -> 404; victim untouched ───────────────────────────

def test_cross_tenant_delete_inverter_404(client):
    tid_a, _key_a = _make_tenant()
    _tid_b, key_b = _make_tenant()
    arr_id = _make_array(tid_a, "Private Array")
    iv = _make_inverter(tid_a, arr_id, "secret-1", 1)

    resp = client.delete(f"/v1/array-owners/inverters/{iv}", headers=_auth(key_b))
    assert resp.status_code == 404, resp.text
    with SessionLocal() as db:
        assert db.get(Inverter, iv).deleted_at is None


# ── 5. demo tenant delete -> 403 ──────────────────────────────────────────────

def test_demo_tenant_cannot_delete_inverter(client):
    tid, key = _make_tenant(is_demo=True)
    arr_id = _make_array(tid, "Demo Array")
    iv = _make_inverter(tid, arr_id, "demo-1", 1)

    resp = client.delete(f"/v1/array-owners/inverters/{iv}", headers=_auth(key))
    assert resp.status_code == 403, resp.text
    with SessionLocal() as db:
        assert db.get(Inverter, iv).deleted_at is None


# ── 6. missing / already-deleted -> 404 ──────────────────────────────────────

def test_delete_missing_inverter_404(client):
    _tid, key = _make_tenant()
    resp = client.delete("/v1/array-owners/inverters/99999", headers=_auth(key))
    assert resp.status_code == 404


def test_delete_inverter_idempotent_second_call_404(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "DeleteTwice")
    iv = _make_inverter(tid, arr_id, "twice-1", 1)

    first = client.delete(f"/v1/array-owners/inverters/{iv}", headers=_auth(key))
    assert first.status_code == 200, first.text
    second = client.delete(f"/v1/array-owners/inverters/{iv}", headers=_auth(key))
    assert second.status_code == 404


# ── 7. unauthenticated -> 401 ─────────────────────────────────────────────────

def test_delete_inverter_requires_auth(client):
    tid, _key = _make_tenant()
    arr_id = _make_array(tid, "NoAuthArray")
    iv = _make_inverter(tid, arr_id, "noauth-1", 1)
    resp = client.delete(f"/v1/array-owners/inverters/{iv}")
    assert resp.status_code == 401
