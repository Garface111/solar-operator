"""Stress + edge tests for account-level SolarEdge discovery / connect.

This is the failure mode that bit the real pilot (Bruce: ~7 arrays on ONE
SolarEdge account, multi-inverter connect FAILED). The "one box" product vector
is: paste ONE credential, we discover and attach ALL their arrays. These tests
hammer the edges:

  - account with 100+ sites (pagination over /sites/list?startIndex=)
  - 0 sites (friendly empty message), duplicate site names
  - re-running connect-account twice -> idempotent (no duplicate arrays/conns)
  - site-level key (403) fallback, invalid key (401), SolarEdge 5xx -> 502
  - two tenants sharing one key don't cross-contaminate
  - two arrays claiming the same site_id -> clean reject (no 500)

All httpx is mocked — no real network. Auth uses the SPA session-token pattern
from tests/test_array_owners.py.
"""
from __future__ import annotations

import secrets

import httpx
import pytest
from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, InverterConnection, Tenant


# ── fakes / helpers ────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return str(self._body)

    def json(self) -> dict:
        return self._body


def _make_tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    key = "stress_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Stress Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid


def _auth(tid: str) -> dict:
    """SPA-style signed session-token auth (see test_array_owners.py)."""
    from api.account import _sign_session
    return {"Authorization": f"Bearer {_sign_session(tid)}"}


def _make_array(tid: str, name: str, **kw) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name=name, **kw)
        db.add(arr)
        db.commit()
        return arr.id


def _site(sid: int, name: str, peak: float = 10.0, status: str = "Active") -> dict:
    return {
        "id": sid, "name": name, "peakPower": peak, "status": status,
        "location": {"city": "Montpelier", "state": "VT"},
    }


def _sites_get(sites: list[dict], *, list_status: int = 200):
    """A fake httpx.get serving /sites/list (paginated by startIndex/size) and
    /site/{id}/details from `sites`. `list_status` forces a status on the
    /sites/list call only (e.g. 401 bad key, 403 site-level, 500 server error);
    the per-site /details endpoint still answers 200 so the site-level fallback
    path can be exercised."""
    by_id = {s["id"]: s for s in sites}

    def fake_get(url, params=None, timeout=None):
        params = params or {}
        if url.endswith("/sites/list"):
            if list_status != 200:
                return _FakeResp(list_status, {"error": "forced"})
            start = int(params.get("startIndex", 0))
            size = int(params.get("size", 100))
            page = sites[start:start + size]
            return _FakeResp(200, {"sites": {"count": len(sites), "site": page}})
        if url.endswith("/details"):
            sid = int(url.rstrip("/").split("/")[-2])
            s = by_id.get(sid)
            if s is None:
                return _FakeResp(400, {"error": "no such site"})
            return _FakeResp(200, {"details": {
                "id": sid, "name": s["name"], "peakPower": s["peakPower"],
                "status": s["status"],
            }})
        return _FakeResp(404, {"error": f"unexpected url {url}"})

    return fake_get


def _arrays_for(tid: str) -> list[Array]:
    with SessionLocal() as db:
        return list(db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all())


def _conn_count(array_id: int) -> int:
    with SessionLocal() as db:
        return db.query(InverterConnection).filter_by(array_id=array_id).count()


# ── discover: pagination ───────────────────────────────────────────────────────

def test_discover_paginates_over_100_sites(client, monkeypatch):
    tid = _make_tenant()
    sites = [_site(1000 + i, f"Site {i}") for i in range(130)]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))

    resp = client.post(
        "/v1/array-owners/solaredge/discover",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert len(out["sites"]) == 130
    # paginated, de-duped, all distinct ids
    assert len({s["site_id"] for s in out["sites"]}) == 130
    first = out["sites"][0]
    assert set(first) == {"site_id", "name", "peak_power_kw", "status"}


def test_discover_empty_account_friendly_message(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.setattr(httpx, "get", _sites_get([]))
    resp = client.post(
        "/v1/array-owners/solaredge/discover",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["sites"] == []
    assert "No sites" in out["message"]


def test_discover_site_level_key_403_guidance(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.setattr(httpx, "get", _sites_get([], list_status=403))
    resp = client.post(
        "/v1/array-owners/solaredge/discover",
        json={"api_key": "site_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 400, resp.text
    assert "account-level" in resp.json()["detail"]


def test_discover_invalid_key_401(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.setattr(httpx, "get", _sites_get([], list_status=401))
    resp = client.post(
        "/v1/array-owners/solaredge/discover",
        json={"api_key": "bad_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 400, resp.text
    assert "401" in resp.json()["detail"]


def test_discover_solaredge_5xx_returns_502(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.setattr(httpx, "get", _sites_get([], list_status=503))
    resp = client.post(
        "/v1/array-owners/solaredge/discover",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 502, resp.text


def test_discover_requires_auth(client):
    resp = client.post("/v1/array-owners/solaredge/discover", json={"api_key": "k"})
    assert resp.status_code == 401


# ── connect-account: create / match / idempotency ──────────────────────────────

def test_connect_account_creates_all_sites(client, monkeypatch):
    tid = _make_tenant()
    sites = [_site(1, "Barn Roof"), _site(2, "South Field"), _site(3, "Carport")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))

    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert len(out["connected"]) == 3
    assert len(out["created"]) == 3
    assert out["matched"] == []
    assert "3 new" in out["message"]

    arrays = _arrays_for(tid)
    assert sorted(a.name for a in arrays) == ["Barn Roof", "Carport", "South Field"]
    # every created array got a real solaredge connection + mirrored legacy cols
    for a in arrays:
        assert _conn_count(a.id) == 1
        assert a.solaredge_api_key == "acct_key"
        assert a.solaredge_site_id in (1, 2, 3)


def test_connect_account_idempotent_no_duplicates(client, monkeypatch):
    tid = _make_tenant()
    sites = [_site(1, "Barn Roof"), _site(2, "South Field")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))

    first = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["created"]) == 2

    # Re-run with the SAME key — must update, never duplicate or 500.
    second = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert second.status_code == 200, second.text
    out = second.json()
    assert out["created"] == []
    assert len(out["matched"]) == 2
    assert "2 matched" in out["message"]

    arrays = _arrays_for(tid)
    assert len(arrays) == 2
    for a in arrays:
        assert _conn_count(a.id) == 1  # still exactly one connection each


def test_connect_account_matches_existing_by_name(client, monkeypatch):
    tid = _make_tenant()
    existing = _make_array(tid, "Barn Roof")  # exact name, no inverter yet
    sites = [_site(7, "Barn Roof"), _site(8, "New Site")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))

    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    matched_ids = [m["array_id"] for m in out["matched"]]
    assert existing in matched_ids
    assert len(out["created"]) == 1  # only "New Site"

    arrays = _arrays_for(tid)
    assert len(arrays) == 2  # existing reused, not duplicated
    with SessionLocal() as db:
        conn = db.query(InverterConnection).filter_by(array_id=existing).one()
        assert conn.config["site_id"] == 7


def test_connect_account_matches_existing_by_connection_site_id(client, monkeypatch):
    tid = _make_tenant()
    arr_id = _make_array(tid, "Totally Different Name")
    with SessionLocal() as db:
        db.add(InverterConnection(
            array_id=arr_id, vendor="solaredge",
            config={"api_key": "old", "site_id": 42}, status="ok",
        ))
        db.commit()

    sites = [_site(42, "Renamed In SolarEdge")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))
    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "new_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert [m["array_id"] for m in out["matched"]] == [arr_id]
    assert out["created"] == []
    # connection updated with the fresh key, not duplicated
    with SessionLocal() as db:
        conn = db.query(InverterConnection).filter_by(array_id=arr_id).one()
        assert conn.config["api_key"] == "new_key"


def test_connect_account_duplicate_site_names(client, monkeypatch):
    tid = _make_tenant()
    sites = [_site(1, "Solar Barn"), _site(2, "Solar Barn"), _site(3, "Solar Barn")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))

    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # Three distinct arrays despite the shared name (disambiguated by site id).
    assert len(out["created"]) == 3
    arrays = _arrays_for(tid)
    assert len(arrays) == 3
    assert len({a.name for a in arrays}) == 3
    assert len({a.solaredge_site_id for a in arrays}) == 3


def test_connect_account_subset_site_ids(client, monkeypatch):
    tid = _make_tenant()
    sites = [_site(1, "A"), _site(2, "B"), _site(3, "C")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))

    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key", "site_ids": [1, 3]}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert {e["site_id"] for e in out["connected"]} == {1, 3}
    arrays = _arrays_for(tid)
    assert sorted(a.name for a in arrays) == ["A", "C"]


def test_connect_account_empty_account(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.setattr(httpx, "get", _sites_get([]))
    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["connected"] == [] and out["created"] == [] and out["matched"] == []
    assert _arrays_for(tid) == []


# ── connect-account: failure-mode hardening ────────────────────────────────────

def test_connect_account_site_level_key_with_site_ids_falls_back(client, monkeypatch):
    """A site-level key 403s on /sites/list, but with explicit site_ids we can
    still validate each site by id (GET /site/{id}/details works site-level)."""
    tid = _make_tenant()
    sites = [_site(555, "Known Site")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites, list_status=403))

    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "site_key", "site_ids": [555]}, headers=_auth(tid),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert len(out["created"]) == 1
    assert out["created"][0]["site_id"] == 555
    assert out["created"][0]["name"] == "Known Site"


def test_connect_account_site_level_key_without_site_ids_clear_error(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.setattr(httpx, "get", _sites_get([], list_status=403))
    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "site_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "site-level" in detail and "account-level" in detail
    assert _arrays_for(tid) == []  # nothing saved


def test_connect_account_invalid_key_401(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.setattr(httpx, "get", _sites_get([], list_status=401))
    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "bad_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 400, resp.text
    assert _arrays_for(tid) == []


def test_connect_account_solaredge_5xx_returns_502(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.setattr(httpx, "get", _sites_get([], list_status=502))
    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    assert resp.status_code == 502, resp.text
    assert _arrays_for(tid) == []


def test_connect_account_two_arrays_same_site_id_rejected(client, monkeypatch):
    tid = _make_tenant()
    # Two arrays both legacy-claiming site 999 — a pre-existing integrity bug.
    _make_array(tid, "Claimant A", solaredge_api_key="k", solaredge_site_id=999)
    _make_array(tid, "Claimant B", solaredge_api_key="k", solaredge_site_id=999)
    sites = [_site(999, "Contested Site")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))

    resp = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "acct_key"}, headers=_auth(tid),
    )
    # Clean reject — not a 500.
    assert resp.status_code == 409, resp.text
    assert "claim SolarEdge site 999" in resp.json()["detail"]


# ── tenant isolation (two sequential connects, same key, different tenants) ─────

def test_connect_account_does_not_cross_contaminate_tenants(client, monkeypatch):
    tid_a = _make_tenant()
    tid_b = _make_tenant()
    sites = [_site(1, "Shared-Key Site A"), _site(2, "Shared-Key Site B")]
    monkeypatch.setattr(httpx, "get", _sites_get(sites))

    ra = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "shared_key"}, headers=_auth(tid_a),
    )
    assert ra.status_code == 200, ra.text
    assert len(ra.json()["created"]) == 2

    rb = client.post(
        "/v1/array-owners/solaredge/connect-account",
        json={"api_key": "shared_key"}, headers=_auth(tid_b),
    )
    assert rb.status_code == 200, rb.text
    # B creates its OWN arrays — it does not "match"/steal tenant A's.
    assert len(rb.json()["created"]) == 2
    assert rb.json()["matched"] == []

    a_arrays = _arrays_for(tid_a)
    b_arrays = _arrays_for(tid_b)
    assert len(a_arrays) == 2 and len(b_arrays) == 2
    assert {a.id for a in a_arrays}.isdisjoint({b.id for b in b_arrays})
