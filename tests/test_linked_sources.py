"""GET /v1/array-owners/linked-sources — every linked vendor + utility for LIVE board."""
from __future__ import annotations

import secrets
from datetime import datetime

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (
    Tenant, Array, InverterConnection, UtilityAccount, DailyGeneration,
)


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Linked Owner",
            contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    return tid


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def test_linked_sources_lists_inverters_and_utilities(client):
    tid = _tenant()
    with SessionLocal() as db:
        a1 = Array(tenant_id=tid, name="Barn", fuel_type="solar")
        a2 = Array(tenant_id=tid, name="Field", fuel_type="solar",
                   solaredge_api_key="SEKEY123456", solaredge_site_id=99)
        db.add_all([a1, a2])
        db.flush()
        db.add(InverterConnection(
            array_id=a1.id, vendor="alsoenergy",
            config={"username": "u", "password": "p", "site_id": 1},
            status="ok",
        ))
        db.add(InverterConnection(
            array_id=a2.id, vendor="solaredge",
            config={"api_key": "SEKEY123456", "site_id": 99},
            status="ok",
        ))
        db.add(UtilityAccount(
            tenant_id=tid, provider="gmp",
            account_number="12345", nickname="House meter",
        ))
        db.add(DailyGeneration(
            tenant_id=tid, array_id=a1.id, day=datetime(2026, 7, 1).date(),
            kwh=10.0, source="alsoenergy",
            uploaded_at=datetime(2026, 7, 12, 15, 0, 0),
        ))
        db.commit()

    r = client.get("/v1/array-owners/linked-sources", headers=_auth(tid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    by = {s["code"]: s for s in body["sources"]}
    assert "alsoenergy" in by
    assert by["alsoenergy"]["kind"] == "inverter"
    assert by["alsoenergy"]["count"] >= 1
    assert by["alsoenergy"]["last_synced_at"] is not None
    assert "solaredge" in by
    assert by["solaredge"]["kind"] == "inverter"
    assert "gmp" in by
    assert by["gmp"]["kind"] == "utility"


def test_linked_sources_empty_tenant(client):
    tid = _tenant()
    r = client.get("/v1/array-owners/linked-sources", headers=_auth(tid))
    assert r.status_code == 200
    assert r.json()["sources"] == []
