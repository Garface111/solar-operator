"""Dashboard array fuel CRUD round-trip (June 2026).

The dashboard's expanded array panel edits fuel via save({fuel_type}) →
PATCH /v1/account/clients/{cid}/arrays/{aid}. These prove the backend accepts
and persists fuel on create + update + read-back, and normalizes garbage —
the contract the new FuelPicker in ArrayList.tsx depends on.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant


def _active_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="FuelCRUD", contact_email=f"{tid}@test.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(18),
            plan="standard", active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _client(tid: str, default_fuel: str = "solar") -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="C-" + secrets.token_hex(3),
                   active=True, default_fuel_type=default_fuel)
        db.add(c); db.flush()
        cid = c.id
        db.commit()
    return cid


def test_create_array_persists_and_returns_fuel(client):
    tid, auth = _active_tenant()
    cid = _client(tid)
    r = client.post(f"/v1/account/clients/{cid}/arrays",
                    headers={"Authorization": auth},
                    json={"name": "Ridge Turbine", "fuel_type": "wind"})
    assert r.status_code == 200, r.text
    assert r.json()["array"]["fuel_type"] == "wind"


def test_create_array_inherits_client_default_fuel(client):
    tid, auth = _active_tenant()
    cid = _client(tid, default_fuel="hydro")
    r = client.post(f"/v1/account/clients/{cid}/arrays",
                    headers={"Authorization": auth},
                    json={"name": "Mill Stream"})  # no fuel → inherit client default
    assert r.status_code == 200, r.text
    assert r.json()["array"]["fuel_type"] == "hydro"


def test_patch_array_updates_fuel(client):
    """This is exactly what the dashboard FuelPicker triggers."""
    tid, auth = _active_tenant()
    cid = _client(tid)
    aid = client.post(f"/v1/account/clients/{cid}/arrays",
                      headers={"Authorization": auth},
                      json={"name": "Flip Me"}).json()["array"]["id"]

    r = client.patch(f"/v1/account/clients/{cid}/arrays/{aid}",
                     headers={"Authorization": auth},
                     json={"fuel_type": "digester"})
    assert r.status_code == 200, r.text
    assert r.json()["array"]["fuel_type"] == "digester"

    with SessionLocal() as db:
        assert db.get(Array, aid).fuel_type == "digester"


def test_patch_array_garbage_fuel_floors_to_solar(client):
    tid, auth = _active_tenant()
    cid = _client(tid)
    aid = client.post(f"/v1/account/clients/{cid}/arrays",
                      headers={"Authorization": auth},
                      json={"name": "Junk", "fuel_type": "wind"}).json()["array"]["id"]

    r = client.patch(f"/v1/account/clients/{cid}/arrays/{aid}",
                     headers={"Authorization": auth},
                     json={"fuel_type": "plutonium"})
    assert r.status_code == 200, r.text
    assert r.json()["array"]["fuel_type"] == "solar"
