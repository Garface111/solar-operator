"""AlsoEnergy connect-account — one PowerTrack login attaches every site."""
from __future__ import annotations

import secrets
from unittest.mock import patch

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, Array, InverterConnection
from sqlalchemy import select


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="AE Owner",
            contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    return tid


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def test_alsoenergy_connect_account_creates_arrays(client):
    tid = _tenant()
    sites = [
        {"site_id": 111, "name": "Barn Roof", "peak_power_kw": None, "status": ""},
        {"site_id": 222, "name": "Field Array", "peak_power_kw": None, "status": ""},
    ]

    def fake_validate(config):
        return {"site_name": f"Site {config['site_id']}", "site_id": int(config["site_id"])}

    with patch("api.inverters.alsoenergy.discover_sites", return_value=sites), \
         patch("api.inverters.alsoenergy.validate", side_effect=fake_validate), \
         patch("api.array_owners._trigger_history_backfill"):
        r = client.post(
            "/v1/array-owners/alsoenergy/connect-account",
            headers=_auth(tid),
            json={"username": "owner@ae.test", "password": "secret"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert len(body["connected"]) == 2
    assert len(body["created"]) == 2
    assert {c["site_id"] for c in body["connected"]} == {111, 222}

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        assert len(arrays) == 2
        conns = db.execute(select(InverterConnection)).scalars().all()
        ae = [c for c in conns if c.vendor == "alsoenergy"]
        assert len(ae) == 2
        assert all(c.config.get("username") == "owner@ae.test" for c in ae)
        assert {int(c.config["site_id"]) for c in ae} == {111, 222}


def test_alsoenergy_connect_account_idempotent_match(client):
    tid = _tenant()
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Barn Roof", fuel_type="solar")
        db.add(arr)
        db.flush()
        db.add(InverterConnection(
            array_id=arr.id, vendor="alsoenergy",
            config={"username": "u", "password": "p", "site_id": 111},
            status="ok",
        ))
        db.commit()

    sites = [
        {"site_id": 111, "name": "Barn Roof", "peak_power_kw": None, "status": ""},
    ]

    def fake_validate(config):
        return {"site_name": "Barn Roof", "site_id": 111}

    with patch("api.inverters.alsoenergy.discover_sites", return_value=sites), \
         patch("api.inverters.alsoenergy.validate", side_effect=fake_validate), \
         patch("api.array_owners._trigger_history_backfill"):
        r = client.post(
            "/v1/array-owners/alsoenergy/connect-account",
            headers=_auth(tid),
            json={"username": "u2", "password": "p2"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["matched"]) == 1
    assert len(body["created"]) == 0
    with SessionLocal() as db:
        n = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        assert len(n) == 1


def test_alsoenergy_connect_account_site_filter(client):
    tid = _tenant()
    sites = [
        {"site_id": 1, "name": "A", "peak_power_kw": None, "status": ""},
        {"site_id": 2, "name": "B", "peak_power_kw": None, "status": ""},
    ]

    def fake_validate(config):
        return {"site_name": f"S{config['site_id']}", "site_id": int(config["site_id"])}

    with patch("api.inverters.alsoenergy.discover_sites", return_value=sites), \
         patch("api.inverters.alsoenergy.validate", side_effect=fake_validate), \
         patch("api.array_owners._trigger_history_backfill"):
        r = client.post(
            "/v1/array-owners/alsoenergy/connect-account",
            headers=_auth(tid),
            json={"username": "u", "password": "p", "site_ids": [2]},
        )
    assert r.status_code == 200, r.text
    assert len(r.json()["connected"]) == 1
    assert r.json()["connected"][0]["site_id"] == 2


def test_alsoenergy_preview_uses_discover(client):
    sites = [
        {"site_id": 9, "name": "Preview Site", "peak_power_kw": None, "status": ""},
    ]
    with patch("api.inverters.alsoenergy.discover_sites", return_value=sites):
        r = client.post(
            "/v1/array-owners/public/preview",
            json={"vendor": "alsoenergy",
                  "config": {"username": "u", "password": "p"}},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("vendor") == "alsoenergy"
    assert len(body.get("sites") or []) == 1
    assert body["sites"][0]["site_id"] in (9, "9")
