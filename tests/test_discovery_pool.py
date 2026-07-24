"""The Discover staging pool — see api/discovery.py for why import is manual.

The load-bearing behavior: a partner login can surface sites belonging to OTHER
operators, so discovery must never auto-import, an operator's verdict must
survive a refresh, and importing must produce a real reporting array.
"""
from __future__ import annotations

import secrets
from unittest.mock import patch

import pytest
from sqlalchemy import select

from api import discovery
from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (
    Array,
    Client,
    DiscoveredCandidate,
    InverterConnection,
    Tenant,
    UtilityAccount,
)

SITES = [
    {"site_id": 111, "name": "Benson Site", "peak_power_kw": 42.0},
    {"site_id": 222, "name": "Tinker Hall Site", "peak_power_kw": None},
    {"site_id": 333, "name": "Someone Else's Farm", "peak_power_kw": None},
]


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Pool Owner", contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
            generation_reports=True,
        ))
        db.commit()
    return tid


def _seed_locus_login(tid: str, username: str = "acme_solar") -> int:
    """A tenant with ONE Locus-connected array — i.e. a saved partner login."""
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Benson Site", fuel_type="solar")
        db.add(arr)
        db.flush()
        db.add(InverterConnection(
            array_id=arr.id, vendor="locus",
            config={"username": username, "password": "pw", "site_id": 111},
            status="ok",
        ))
        db.commit()
        return arr.id


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def test_refresh_stages_sites_without_importing_them():
    """The whole point: discovery stages, it does NOT auto-create arrays."""
    tid = _tenant()
    _seed_locus_login(tid)

    with patch("api.inverters.locus.discover_sites", return_value=SITES):
        with SessionLocal() as db:
            result = discovery.refresh_tenant(db, tid)

    assert result["ok"] is True
    with SessionLocal() as db:
        rows = db.execute(
            select(DiscoveredCandidate).where(DiscoveredCandidate.tenant_id == tid)
        ).scalars().all()
        assert {r.external_id for r in rows} == {"111", "222", "333"}
        # 111 is already an array → imported; the other two await a decision.
        by_ext = {r.external_id: r for r in rows}
        assert by_ext["111"].status == "imported"
        assert by_ext["222"].status == "new"
        assert by_ext["333"].status == "new"
        # NOTHING new was created in the operator's system.
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        assert len(arrays) == 1


def test_ignored_survives_a_refresh():
    """An operator saying 'not mine' must stick — otherwise the nightly job
    re-offers a foreign partner's sites forever."""
    tid = _tenant()
    _seed_locus_login(tid)
    with patch("api.inverters.locus.discover_sites", return_value=SITES):
        with SessionLocal() as db:
            discovery.refresh_tenant(db, tid)
        with SessionLocal() as db:
            foreign = db.execute(
                select(DiscoveredCandidate).where(
                    DiscoveredCandidate.tenant_id == tid,
                    DiscoveredCandidate.external_id == "333",
                )
            ).scalar_one()
            discovery.set_ignored(db, tid, [foreign.id], True)
        # Nightly refresh runs again.
        with SessionLocal() as db:
            discovery.refresh_tenant(db, tid)

    with SessionLocal() as db:
        row = db.execute(
            select(DiscoveredCandidate).where(
                DiscoveredCandidate.tenant_id == tid,
                DiscoveredCandidate.external_id == "333",
            )
        ).scalar_one()
        assert row.status == "ignored"


def test_import_creates_arrays_under_one_client():
    tid = _tenant()
    _seed_locus_login(tid)
    with patch("api.inverters.locus.discover_sites", return_value=SITES):
        with SessionLocal() as db:
            discovery.refresh_tenant(db, tid)

    with SessionLocal() as db:
        pick = db.execute(
            select(DiscoveredCandidate).where(
                DiscoveredCandidate.tenant_id == tid,
                DiscoveredCandidate.external_id == "222",
            )
        ).scalar_one()
        with patch("api.array_owners._trigger_history_backfill"):
            res = discovery.import_candidates(
                db, tid, [pick.id], client_name="Acme Solar",
            )

    assert res["ok"] is True
    assert res["client"]["name"] == "Acme Solar"
    assert len(res["imported"]) == 1

    with SessionLocal() as db:
        client = db.execute(
            select(Client).where(Client.tenant_id == tid, Client.name == "Acme Solar")
        ).scalar_one()
        arr = db.get(Array, res["imported"][0]["array_id"])
        assert arr.client_id == client.id
        # The saved credential rode along, so the nightly pull just works.
        conn = db.execute(
            select(InverterConnection).where(InverterConnection.array_id == arr.id)
        ).scalar_one()
        assert conn.vendor == "locus"
        assert conn.config["site_id"] == 222
        assert conn.config["username"] == "acme_solar"
        # And the pool now reflects reality.
        row = db.execute(
            select(DiscoveredCandidate).where(DiscoveredCandidate.id == pick.id)
        ).scalar_one()
        assert row.status == "imported"
        assert row.imported_array_id == arr.id


def test_import_is_idempotent():
    """A double-click must not create the array twice."""
    tid = _tenant()
    _seed_locus_login(tid)
    with patch("api.inverters.locus.discover_sites", return_value=SITES):
        with SessionLocal() as db:
            discovery.refresh_tenant(db, tid)
    with SessionLocal() as db:
        pick = db.execute(
            select(DiscoveredCandidate).where(
                DiscoveredCandidate.tenant_id == tid,
                DiscoveredCandidate.external_id == "222",
            )
        ).scalar_one()
        pid = pick.id
        with patch("api.array_owners._trigger_history_backfill"):
            discovery.import_candidates(db, tid, [pid], client_name="Acme Solar")
    with SessionLocal() as db:
        with patch("api.array_owners._trigger_history_backfill"):
            second = discovery.import_candidates(db, tid, [pid], client_name="Acme Solar")
    assert second["imported"] == []
    assert second["skipped"][0]["reason"] == "already in your system"
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        assert len(arrays) == 2  # the seeded one + exactly one import


def test_a_broken_login_reports_its_error_and_keeps_others_alive():
    """A stale Locus password must not hide the tenant's utility accounts."""
    tid = _tenant()
    _seed_locus_login(tid)
    with SessionLocal() as db:
        db.add(UtilityAccount(
            tenant_id=tid, provider="gmp", account_number="6208700",
            customer_number="cust1", nickname="Barn Meter",
        ))
        db.commit()

    from api.inverters.base import InverterAuthError

    with patch("api.inverters.locus.discover_sites",
               side_effect=InverterAuthError("password rejected")):
        with SessionLocal() as db:
            result = discovery.refresh_tenant(db, tid)

    assert any("password rejected" in e["error"] for e in result["errors"])
    with SessionLocal() as db:
        pool = discovery.list_pool(db, tid)
    keys = {g["key"]: g for g in pool["logins"]}
    # The broken vendor login is still listed, carrying its error…
    assert "password rejected" in keys["locus:acme_solar"]["last_error"]
    # …and the utility login came through untouched.
    gmp = keys["gmp:cust1"]
    assert gmp["source_kind"] == "utility"
    assert [c["name"] for c in gmp["candidates"]] == ["Barn Meter"]


def test_endpoints_are_tenant_scoped(client):
    """One tenant must never see or import another's candidates."""
    mine = _tenant()
    theirs = _tenant()
    _seed_locus_login(theirs, username="their_login")
    with patch("api.inverters.locus.discover_sites", return_value=SITES):
        with SessionLocal() as db:
            discovery.refresh_tenant(db, theirs)

    r = client.get("/v1/account/discovery/candidates", headers=_auth(mine))
    assert r.status_code == 200, r.text
    assert r.json()["logins"] == []

    with SessionLocal() as db:
        foreign = db.execute(
            select(DiscoveredCandidate).where(
                DiscoveredCandidate.tenant_id == theirs,
                DiscoveredCandidate.status == "new",
            )
        ).scalars().first()

    r = client.post(
        "/v1/account/discovery/import",
        headers=_auth(mine),
        json={"candidate_ids": [foreign.id], "client_name": "Mine"},
    )
    assert r.status_code == 400
    with SessionLocal() as db:
        still = db.execute(
            select(DiscoveredCandidate).where(DiscoveredCandidate.id == foreign.id)
        ).scalar_one()
        assert still.status == "new"  # untouched


def test_utility_account_import_links_the_existing_account():
    tid = _tenant()
    with SessionLocal() as db:
        db.add(UtilityAccount(
            tenant_id=tid, provider="vec", account_number="6208700",
            customer_number="c9", nickname="Field Meter",
        ))
        db.commit()
    with SessionLocal() as db:
        discovery.refresh_tenant(db, tid)
    with SessionLocal() as db:
        cand = db.execute(
            select(DiscoveredCandidate).where(DiscoveredCandidate.tenant_id == tid)
        ).scalar_one()
        assert cand.status == "new"
        res = discovery.import_candidates(db, tid, [cand.id], client_name="Coop Client")
    assert len(res["imported"]) == 1
    with SessionLocal() as db:
        acct = db.execute(
            select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
        ).scalar_one()
        arr = db.get(Array, res["imported"][0]["array_id"])
        assert acct.array_id == arr.id  # the captured account now feeds an array
