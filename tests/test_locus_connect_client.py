"""Locus connect-account onboarding — a login's sites attach to a chosen client.

The generation-reports onboarding flow is: enter a Locus login → discover its
sites → create a client named from the login → connect the picked sites INTO
that client (login → client → arrays underneath). These tests cover the
client_id attach + the cross-tenant guard.
"""
from __future__ import annotations

import secrets
from unittest.mock import patch

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant


def _tenant_and_client() -> tuple[str, int]:
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Locus Owner", contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
            generation_reports=True,
        ))
        c = Client(tenant_id=tid, name="four.general", active=True)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    return tid, cid


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def test_locus_connect_attaches_new_arrays_to_client(client):
    tid, cid = _tenant_and_client()
    sites = [
        {"site_id": 111, "name": "Benson Site", "peak_power_kw": None},
        {"site_id": 222, "name": "Tinker Hall Site", "peak_power_kw": None},
    ]
    with patch("api.inverters.locus.discover_sites", return_value=sites), \
         patch("api.array_owners._attach_locus"), \
         patch("api.array_owners._trigger_history_backfill"):
        r = client.post(
            "/v1/array-owners/locus/connect-account",
            headers=_auth(tid),
            json={"username": "four.general", "password": "x", "client_id": cid},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["created"]) == 2
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        assert len(arrays) == 2
        # login → client → arrays: every connected site lives under the client.
        assert all(a.client_id == cid for a in arrays)


def test_locus_connect_pulls_unassigned_match_onto_client(client):
    tid, cid = _tenant_and_client()
    # Pre-existing array with the same name but NO client (a raced/orphan array).
    with SessionLocal() as db:
        db.add(Array(tenant_id=tid, name="Benson Site", client_id=None,
                     fuel_type="solar"))
        db.commit()
    sites = [{"site_id": 111, "name": "Benson Site", "peak_power_kw": None}]
    with patch("api.inverters.locus.discover_sites", return_value=sites), \
         patch("api.array_owners._attach_locus"), \
         patch("api.array_owners._trigger_history_backfill"):
        r = client.post(
            "/v1/array-owners/locus/connect-account",
            headers=_auth(tid),
            json={"username": "four.general", "password": "x", "client_id": cid},
        )
    assert r.status_code == 200, r.text
    assert len(r.json()["matched"]) == 1
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.name == "Benson Site")
        ).scalar_one()
        assert arr.client_id == cid


def test_locus_connect_rejects_foreign_client(client):
    tid, _ = _tenant_and_client()
    other = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=other, name="Other", contact_email=f"{other}@x.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True,
        ))
        oc = Client(tenant_id=other, name="foreign", active=True)
        db.add(oc)
        db.flush()
        ocid = oc.id
        db.commit()
    sites = [{"site_id": 1, "name": "A", "peak_power_kw": None}]
    with patch("api.inverters.locus.discover_sites", return_value=sites), \
         patch("api.array_owners._attach_locus"), \
         patch("api.array_owners._trigger_history_backfill"):
        r = client.post(
            "/v1/array-owners/locus/connect-account",
            headers=_auth(tid),
            json={"username": "u", "password": "x", "client_id": ocid},
        )
    assert r.status_code == 404, r.text
