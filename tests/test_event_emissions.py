"""Event emissions on user-facing mutation paths.

Every dashboard-mutating endpoint must publish a tenant event AFTER its commit
(see api/events.py — subscribers refetch on receipt, so an event that beats its
own commit reads stale data). These tests patch api.events.publish and drive
the three main entry families — client CRUD (/v1/account/clients), a vendor
connect-account (Locus), and the Discover-pool import — plus the per-array
daily CSV ingest, asserting the right tenant id + event types fire.
"""
from __future__ import annotations

import secrets
from unittest.mock import patch

from sqlalchemy import select

from api import discovery
from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, DiscoveredCandidate, InverterConnection, Tenant


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Events Owner", contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
            generation_reports=True,
        ))
        db.commit()
    return tid


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def _pairs(pub) -> list[tuple[str, str]]:
    """(tenant_id, event_type) for every publish() call, in order."""
    return [(c.args[0], c.args[1]) for c in pub.call_args_list]


def test_create_client_emits_clients_changed(client):
    tid = _tenant()
    with patch("api.events.publish", autospec=True) as pub:
        r = client.post("/v1/account/clients", headers=_auth(tid),
                        json={"name": "Bruce Genereaux"})
    assert r.status_code == 200, r.text
    cid = r.json()["client"]["id"]
    assert (tid, "clients.changed") in _pairs(pub)
    # Payload carries the id — small, ids only, never customer data.
    pub.assert_any_call(tid, "clients.changed", {"client_id": cid})


def test_update_and_delete_client_emit_clients_changed(client):
    tid = _tenant()
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Editable", active=True)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    with patch("api.events.publish", autospec=True) as pub:
        r = client.patch(f"/v1/account/clients/{cid}", headers=_auth(tid),
                         json={"notes": "hello"})
    assert r.status_code == 200, r.text
    assert (tid, "clients.changed") in _pairs(pub)

    with patch("api.events.publish", autospec=True) as pub:
        r = client.delete(f"/v1/account/clients/{cid}", headers=_auth(tid))
    assert r.status_code == 200, r.text
    assert (tid, "clients.changed") in _pairs(pub)


def test_locus_connect_account_emits_one_arrays_changed(client):
    tid = _tenant()
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="four.general", active=True)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    sites = [
        {"site_id": 111, "name": "Benson Site", "peak_power_kw": None},
        {"site_id": 222, "name": "Tinker Hall Site", "peak_power_kw": None},
    ]
    with patch("api.inverters.locus.discover_sites", return_value=sites), \
         patch("api.array_owners._attach_locus"), \
         patch("api.array_owners._trigger_history_backfill"), \
         patch("api.events.publish", autospec=True) as pub:
        r = client.post(
            "/v1/array-owners/locus/connect-account",
            headers=_auth(tid),
            json={"username": "four.general", "password": "x", "client_id": cid},
        )
    assert r.status_code == 200, r.text
    pairs = _pairs(pub)
    assert (tid, "arrays.changed") in pairs
    # One coarse event per REQUEST — never one per connected array.
    assert pairs.count((tid, "arrays.changed")) == 1


def test_discovery_import_emits_clients_and_arrays_changed():
    tid = _tenant()
    # A saved Locus login: one connected array carrying the credential.
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Benson Site", fuel_type="solar")
        db.add(arr)
        db.flush()
        db.add(InverterConnection(
            array_id=arr.id, vendor="locus",
            config={"username": "acme_solar", "password": "pw", "site_id": 111},
            status="ok",
        ))
        db.commit()
    sites = [
        {"site_id": 111, "name": "Benson Site", "peak_power_kw": None},
        {"site_id": 222, "name": "Tinker Hall Site", "peak_power_kw": None},
    ]
    with patch("api.inverters.locus.discover_sites", return_value=sites):
        with SessionLocal() as db:
            discovery.refresh_tenant(db, tid)
    with SessionLocal() as db:
        cand_id = db.execute(
            select(DiscoveredCandidate.id).where(
                DiscoveredCandidate.tenant_id == tid,
                DiscoveredCandidate.external_id == "222",
            )
        ).scalar_one()
    with patch("api.array_owners._trigger_history_backfill"), \
         patch("api.events.publish", autospec=True) as pub:
        with SessionLocal() as db:
            result = discovery.import_candidates(
                db, tid, [cand_id], client_name="Acme Solar")
    assert result["ok"] is True
    pairs = _pairs(pub)
    # Import lands BOTH: the destination client and its new arrays.
    assert (tid, "clients.changed") in pairs
    assert (tid, "arrays.changed") in pairs


def test_discovery_set_ignored_emits_nothing():
    """Hiding a candidate is Discover-local — no dashboard state changes."""
    tid = _tenant()
    with SessionLocal() as db:
        db.add(DiscoveredCandidate(
            tenant_id=tid, provider="locus", source_kind="vendor",
            source_login="acme_solar", external_id="999", name="Foreign Farm",
            status="new",
        ))
        db.commit()
    with SessionLocal() as db:
        cand_id = db.execute(
            select(DiscoveredCandidate.id).where(
                DiscoveredCandidate.tenant_id == tid)
        ).scalar_one()
    with patch("api.events.publish", autospec=True) as pub:
        with SessionLocal() as db:
            discovery.set_ignored(db, tid, [cand_id], True)
    assert pub.call_count == 0


def test_daily_csv_upload_emits_generation_updated(client):
    tid = _tenant()
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="CSV Array", fuel_type="solar")
        db.add(arr)
        db.flush()
        aid = arr.id
        db.commit()
    csv_bytes = b"date,kWh generated\n2026-07-01,12.5\n2026-07-02,13.0\n"
    with patch("api.events.publish", autospec=True) as pub:
        r = client.post(
            f"/v1/account/arrays/{aid}/daily-csv",
            headers=_auth(tid),
            files={"file": ("daily.csv", csv_bytes, "text/csv")},
        )
    assert r.status_code == 200, r.text
    pairs = _pairs(pub)
    assert (tid, "generation.updated") in pairs
    assert (tid, "arrays.changed") in pairs
    pub.assert_any_call(tid, "generation.updated", {"array_id": aid})
