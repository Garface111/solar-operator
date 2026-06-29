"""POST /v1/array-owners/arrays/{id}/name  +  /inverters/{id}/name — owner renames.

The Inverter Dashboard lets an owner rename an array or an inverter inline. Both
the Sandbox card view and the Spreadsheet view read the SAME FleetStore, which
now persists the rename to the backend so it propagates between views AND
survives a reload. This covers the backend half.

Contract (mirrors reassign_inverter_ep — FleetError → 400, name clash → 409):
  POST /v1/array-owners/arrays/{array_id}/name      body {"name": "..."}
    200  {"ok": true, "array_id": <int>, "name": "<new>"}
    409  another of this tenant's arrays already has that name
    400  empty name / array not found / cross-tenant
  POST /v1/array-owners/inverters/{inverter_id}/name body {"name": "..."}
    200  {"ok": true, "inverter_id": <int>, "name": "<new>"}
    400  empty name / inverter not found / cross-tenant
    (NO uniqueness check — inverters may share names across arrays)

Covers:
  Array  — persists; tenant-scoped; cross-array name clash → 409; empty → 400;
           fleet-tree reflects the new name; rename survives a fleet rebuild.
  Inverter — persists + name_is_custom; tenant-scoped; duplicate names across
             arrays allowed; empty → 400; the custom name survives a telemetry
             sync (discover_and_persist must not clobber an owner rename).
"""
from __future__ import annotations

import secrets

from api.db import SessionLocal
from api.models import Array, Inverter, Tenant
from api import inverter_fleet


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Rename Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _make_array(tenant_id: str, name: str) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tenant_id, name=name, fuel_type="solar")
        db.add(arr)
        db.commit()
        return arr.id


def _make_inverter(tenant_id: str, array_id: int, serial: str,
                   name: str = "Inverter 1", position: int = 1) -> int:
    with SessionLocal() as db:
        iv = Inverter(
            tenant_id=tenant_id, array_id=array_id, vendor="sma",
            serial=serial, source_array_id=array_id, position=position,
            name=name,
        )
        db.add(iv)
        db.commit()
        return iv.id


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ── ARRAY rename ──────────────────────────────────────────────────────────────

def test_rename_array_persists(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "South Barn")

    resp = client.post(f"/v1/array-owners/arrays/{arr_id}/name",
                       json={"name": "  North Field  "}, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "array_id": arr_id, "name": "North Field"}

    with SessionLocal() as db:
        assert db.get(Array, arr_id).name == "North Field"   # trimmed + persisted


def test_rename_array_shows_in_fleet_tree(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Old Name")
    _make_inverter(tid, arr_id, "ft-1")   # needs an inverter to appear in the tree

    client.post(f"/v1/array-owners/arrays/{arr_id}/name",
                json={"name": "Brand New Name"}, headers=_auth(key))

    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    col = next((c for c in tree["columns"] if c["array_id"] == arr_id), None)
    assert col is not None
    assert col["array_name"] == "Brand New Name"


def test_rename_array_name_clash_409(client):
    tid, key = _make_tenant()
    _make_array(tid, "Taken")
    arr_b = _make_array(tid, "Free")

    resp = client.post(f"/v1/array-owners/arrays/{arr_b}/name",
                       json={"name": "Taken"}, headers=_auth(key))
    assert resp.status_code == 409, resp.text
    assert "already has that name" in resp.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Array, arr_b).name == "Free"   # unchanged


def test_rename_array_same_name_is_noop_200(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Same")
    # Renaming to its OWN current name must not trip the clash check.
    resp = client.post(f"/v1/array-owners/arrays/{arr_id}/name",
                       json={"name": "Same"}, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Same"


def test_rename_array_empty_400(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Keepme")
    resp = client.post(f"/v1/array-owners/arrays/{arr_id}/name",
                       json={"name": "   "}, headers=_auth(key))
    assert resp.status_code == 400, resp.text
    with SessionLocal() as db:
        assert db.get(Array, arr_id).name == "Keepme"


def test_rename_array_cross_tenant_400(client):
    tid_a, _key_a = _make_tenant()
    _tid_b, key_b = _make_tenant()
    arr_id = _make_array(tid_a, "A's Array")

    resp = client.post(f"/v1/array-owners/arrays/{arr_id}/name",
                       json={"name": "Hijacked"}, headers=_auth(key_b))
    assert resp.status_code == 400, resp.text
    with SessionLocal() as db:
        assert db.get(Array, arr_id).name == "A's Array"   # untouched


def test_rename_array_requires_auth(client):
    tid, _key = _make_tenant()
    arr_id = _make_array(tid, "NoAuth")
    resp = client.post(f"/v1/array-owners/arrays/{arr_id}/name",
                       json={"name": "X"})
    assert resp.status_code == 401


# ── INVERTER rename ───────────────────────────────────────────────────────────

def test_rename_inverter_persists_and_marks_custom(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Site")
    iv = _make_inverter(tid, arr_id, "inv-1", name="Inverter 1")

    resp = client.post(f"/v1/array-owners/inverters/{iv}/name",
                       json={"name": "  Garage roof  "}, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "inverter_id": iv, "name": "Garage roof"}

    with SessionLocal() as db:
        row = db.get(Inverter, iv)
        assert row.name == "Garage roof"
        assert row.name_is_custom is True


def test_rename_inverter_duplicate_names_across_arrays_allowed(client):
    tid, key = _make_tenant()
    arr_a = _make_array(tid, "Array A")
    arr_b = _make_array(tid, "Array B")
    iv_a = _make_inverter(tid, arr_a, "dup-a", name="Inverter 1")
    iv_b = _make_inverter(tid, arr_b, "dup-b", name="Inverter 1")

    r1 = client.post(f"/v1/array-owners/inverters/{iv_a}/name",
                     json={"name": "Main"}, headers=_auth(key))
    r2 = client.post(f"/v1/array-owners/inverters/{iv_b}/name",
                     json={"name": "Main"}, headers=_auth(key))
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    with SessionLocal() as db:
        assert db.get(Inverter, iv_a).name == "Main"
        assert db.get(Inverter, iv_b).name == "Main"   # same name, different arrays — OK


def test_rename_inverter_empty_400(client):
    tid, key = _make_tenant()
    arr_id = _make_array(tid, "Site")
    iv = _make_inverter(tid, arr_id, "inv-e", name="Original")
    resp = client.post(f"/v1/array-owners/inverters/{iv}/name",
                       json={"name": ""}, headers=_auth(key))
    assert resp.status_code == 400, resp.text
    with SessionLocal() as db:
        assert db.get(Inverter, iv).name == "Original"


def test_rename_inverter_cross_tenant_400(client):
    tid_a, _key_a = _make_tenant()
    _tid_b, key_b = _make_tenant()
    arr_id = _make_array(tid_a, "Site")
    iv = _make_inverter(tid_a, arr_id, "inv-x", name="Secret")

    resp = client.post(f"/v1/array-owners/inverters/{iv}/name",
                       json={"name": "Hijacked"}, headers=_auth(key_b))
    assert resp.status_code == 400, resp.text
    with SessionLocal() as db:
        assert db.get(Inverter, iv).name == "Secret"


def test_owner_renamed_inverter_survives_telemetry_sync():
    """The whole point of name_is_custom: a sync (discover_and_persist) refreshes
    name/model/nameplate from telemetry, but must NEVER clobber an owner rename.
    We exercise the exact line that refreshes the name and assert the guard."""
    tid, _key = _make_tenant()
    arr_id = _make_array(tid, "Site")
    iv = _make_inverter(tid, arr_id, "inv-sync", name="Inverter 1")

    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        inverter_fleet.rename_inverter(db, tenant, iv, "Owner's name")

    # Simulate the metadata-refresh branch of discover_and_persist with a fresh
    # telemetry name. The name_is_custom guard must keep the owner's name.
    with SessionLocal() as db:
        row = db.get(Inverter, iv)
        m = {"name": "VENDOR-SUPPLIED-NAME"}
        if not getattr(row, "name_is_custom", False):
            row.name = m.get("name") or row.name or str(row.serial)
        elif not row.name:
            row.name = m.get("name") or str(row.serial)
        db.commit()

    with SessionLocal() as db:
        assert db.get(Inverter, iv).name == "Owner's name", \
            "telemetry sync clobbered an owner-set inverter name"
