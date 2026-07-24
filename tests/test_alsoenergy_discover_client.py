"""AlsoEnergy discover + client attach — parity with the Locus onboarding.

Discover exists so the Add-a-client flow can offer AlsoEnergy sites as
checkboxes. Without it the only option was connect-everything, which is the
import-first behavior the Discover pool exists to prevent.
"""
from __future__ import annotations

import secrets
from unittest.mock import patch

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant

SITES = [
    {"site_id": 111, "name": "Barn Roof", "peak_power_kw": None, "status": ""},
    {"site_id": 222, "name": "Field Array", "peak_power_kw": None, "status": ""},
]


def _tenant_and_client() -> tuple[str, int]:
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="AE Owner", contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
            generation_reports=True,
        ))
        c = Client(tenant_id=tid, name="Powertrack Co", active=True)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    return tid, cid


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def test_discover_lists_sites_without_saving_anything(client):
    tid, _ = _tenant_and_client()
    with patch("api.inverters.alsoenergy.discover_sites", return_value=SITES):
        r = client.post(
            "/v1/array-owners/alsoenergy/discover",
            headers=_auth(tid),
            json={"username": "u", "password": "p"},
        )
    assert r.status_code == 200, r.text
    assert [s["site_id"] for s in r.json()["sites"]] == [111, 222]
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        assert arrays == []  # preview only


def test_discover_surfaces_a_bad_login_as_400(client):
    tid, _ = _tenant_and_client()
    from api.inverters.base import InverterAuthError
    with patch("api.inverters.alsoenergy.discover_sites",
               side_effect=InverterAuthError("password rejected")):
        r = client.post(
            "/v1/array-owners/alsoenergy/discover",
            headers=_auth(tid),
            json={"username": "u", "password": "bad"},
        )
    assert r.status_code == 400
    assert "password rejected" in r.text


def test_connect_account_files_sites_under_the_chosen_client(client):
    tid, cid = _tenant_and_client()

    def fake_validate(config):
        return {"site_name": f"S{config['site_id']}", "site_id": int(config["site_id"])}

    with patch("api.inverters.alsoenergy.discover_sites", return_value=SITES), \
         patch("api.inverters.alsoenergy.validate", side_effect=fake_validate), \
         patch("api.array_owners._trigger_history_backfill"):
        r = client.post(
            "/v1/array-owners/alsoenergy/connect-account",
            headers=_auth(tid),
            json={"username": "u", "password": "p",
                  "site_ids": [222], "client_id": cid},
        )
    assert r.status_code == 200, r.text
    assert len(r.json()["connected"]) == 1
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        assert len(arrays) == 1
        assert arrays[0].client_id == cid


def test_connect_account_rejects_a_foreign_client(client):
    tid, _ = _tenant_and_client()
    other, other_cid = _tenant_and_client()

    def fake_validate(config):
        return {"site_name": "x", "site_id": int(config["site_id"])}

    with patch("api.inverters.alsoenergy.discover_sites", return_value=SITES), \
         patch("api.inverters.alsoenergy.validate", side_effect=fake_validate), \
         patch("api.array_owners._trigger_history_backfill"):
        r = client.post(
            "/v1/array-owners/alsoenergy/connect-account",
            headers=_auth(tid),
            json={"username": "u", "password": "p", "client_id": other_cid},
        )
    assert r.status_code == 404
    with SessionLocal() as db:
        assert db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all() == []
