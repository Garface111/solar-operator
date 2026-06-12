"""Multi-vendor inverter framework tests.

Covers, per docs/plans/INVERTER_FRAMEWORK.md:
  - per vendor: validate success + auth-failure (mocked httpx — no network)
  - fetch_daily parsing from canned JSON fixtures
  - chint stub behavior (validate raises, live None, daily [])
  - the new POST /inverter endpoint (session-token auth)
  - GET /inverter-vendors listing endpoint
  - legacy POST /solaredge shim still works (forwards through the framework)
  - virtual-connection fallback from Array.solaredge_* fields

All HTTP is mocked via monkeypatch on the global httpx.get/httpx.post (respx is
not in requirements-dev.txt). Vendor modules call httpx.get/post directly, so
patching the module functions intercepts every vendor.
"""
from __future__ import annotations

import secrets
from datetime import date

import httpx
import pytest

from api import inverters
from api.inverters import InverterAuthError, InverterError
from api.inverters import chint, fronius, sma, solaredge
from api.db import SessionLocal
from api.models import Array, InverterConnection, Tenant


# ── fakes / helpers ───────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code: int, body: dict, text: str | None = None):
        self.status_code = status_code
        self._body = body
        self._text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self._text if self._text is not None else str(self._body)

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "inv_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Inverter Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _make_array(tenant_id: str, name: str, **kw) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tenant_id, name=name, **kw)
        db.add(arr)
        db.commit()
        return arr.id


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _clear_sma_token_cache():
    sma._TOKEN_CACHE.clear()
    yield
    sma._TOKEN_CACHE.clear()


# ── solaredge wrapper ─────────────────────────────────────────────────────────

def test_solaredge_validate_success(monkeypatch):
    body = {
        "overview": {"currentPower": {"power": 1200.0}, "lastUpdateTime": "x"},
        "details": {"id": 555, "name": "Roof A", "peakPower": 9.6},
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, body))
    result = solaredge.validate({"api_key": "k", "site_id": 555})
    assert result["site_name"] == "Roof A"
    assert result["peak_power_kw"] == 9.6


def test_solaredge_validate_auth_failure(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(401, {"error": "bad"}))
    with pytest.raises(InverterAuthError):
        solaredge.validate({"api_key": "bad", "site_id": 1})


def test_solaredge_fetch_daily_parsing(monkeypatch):
    energy_body = {
        "energy": {
            "values": [
                {"date": "2026-06-01 00:00:00", "value": 25720.0},
                {"date": "2026-06-02 00:00:00", "value": 0},       # skipped
                {"date": "2026-06-03 00:00:00", "value": None},    # skipped
                {"date": "2026-06-04 00:00:00", "value": 31000.0},
            ]
        }
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, energy_body))
    rows = solaredge.fetch_daily(
        {"api_key": "k", "site_id": 1}, date(2026, 6, 1), date(2026, 6, 4)
    )
    assert rows == [
        {"day": date(2026, 6, 1), "kwh": 25.72},
        {"day": date(2026, 6, 4), "kwh": 31.0},
    ]


def test_solaredge_fetch_live(monkeypatch):
    body = {"overview": {"currentPower": {"power": 4830.5}, "lastUpdateTime": "2026-06-12 21:29:12"}}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, body))
    live = solaredge.fetch_live({"api_key": "k", "site_id": 1})
    assert live == {"current_power_w": 4830.5, "as_of": "2026-06-12 21:29:12"}


# ── fronius (Solar.web) ───────────────────────────────────────────────────────

def test_fronius_validate_success(monkeypatch):
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp(200, {"pvSystemId": "abc", "name": "Fronius Farm", "peakPower": 12000}),
    )
    result = fronius.validate(
        {"access_key_id": "id", "access_key_value": "val", "pv_system_id": "abc"}
    )
    assert result["site_name"] == "Fronius Farm"


def test_fronius_validate_auth_failure(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(403, {"error": "nope"}))
    with pytest.raises(InverterAuthError):
        fronius.validate(
            {"access_key_id": "id", "access_key_value": "bad", "pv_system_id": "abc"}
        )


def test_fronius_fetch_live(monkeypatch):
    body = {
        "data": {
            "logDateTime": "2026-06-12T21:00:00Z",
            "channels": [
                {"channelName": "PowerLoad", "value": -100.0, "unit": "W"},
                {"channelName": "PowerPV", "value": 4200.0, "unit": "W"},
            ],
        }
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, body))
    live = fronius.fetch_live(
        {"access_key_id": "id", "access_key_value": "val", "pv_system_id": "abc"}
    )
    assert live == {"current_power_w": 4200.0, "as_of": "2026-06-12T21:00:00Z"}


def test_fronius_fetch_daily_parsing(monkeypatch):
    body = {
        "data": [
            {"logDateTime": "2026-06-10", "channels": [{"channelName": "EnergyProductionTotal", "value": 25720.0}]},
            {"logDateTime": "2026-06-11", "channels": [{"channelName": "EnergyProductionTotal", "value": 31000.0}]},
            {"logDateTime": "2026-06-12", "channels": [{"channelName": "SomethingElse", "value": 5.0}]},  # no production -> skip
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, body))
    rows = fronius.fetch_daily(
        {"access_key_id": "id", "access_key_value": "val", "pv_system_id": "abc"},
        date(2026, 6, 10), date(2026, 6, 12),
    )
    assert rows == [
        {"day": date(2026, 6, 10), "kwh": 25.72},
        {"day": date(2026, 6, 11), "kwh": 31.0},
    ]


# ── sma (monitoring API, OAuth2) ──────────────────────────────────────────────

def _sma_token_ok(*a, **k):
    return _FakeResp(200, {"access_token": "tok123", "expires_in": 3600})


def test_sma_validate_success(monkeypatch):
    monkeypatch.setattr(httpx, "post", _sma_token_ok)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, {"name": "SMA Plant"}))
    result = sma.validate({"client_id": "c", "client_secret": "s", "system_id": "p1"})
    assert result["site_name"] == "SMA Plant"


def test_sma_validate_auth_failure_token(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(401, {"error": "bad client"}))
    with pytest.raises(InverterAuthError):
        sma.validate({"client_id": "c", "client_secret": "bad", "system_id": "p1"})


def test_sma_validate_auth_failure_monitoring(monkeypatch):
    monkeypatch.setattr(httpx, "post", _sma_token_ok)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(403, {"error": "forbidden"}))
    with pytest.raises(InverterAuthError):
        sma.validate({"client_id": "c2", "client_secret": "s", "system_id": "p1"})


def test_sma_fetch_live(monkeypatch):
    monkeypatch.setattr(httpx, "post", _sma_token_ok)
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp(200, {"pvGeneration": {"value": 5100.0, "time": "2026-06-12T20:00:00Z"}}),
    )
    live = sma.fetch_live({"client_id": "c3", "client_secret": "s", "system_id": "p1"})
    assert live == {"current_power_w": 5100.0, "as_of": "2026-06-12T20:00:00Z"}


def test_sma_fetch_daily_parsing(monkeypatch):
    monkeypatch.setattr(httpx, "post", _sma_token_ok)
    # Day endpoint returns pvGeneration energy in Wh; one call per day in range.
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp(200, {"pvGeneration": {"value": 42000.0}}),
    )
    rows = sma.fetch_daily(
        {"client_id": "c4", "client_secret": "s", "system_id": "p1"},
        date(2026, 6, 10), date(2026, 6, 11),
    )
    assert rows == [
        {"day": date(2026, 6, 10), "kwh": 42.0},
        {"day": date(2026, 6, 11), "kwh": 42.0},
    ]


def test_sma_token_uses_refresh_grant(monkeypatch):
    captured = {}

    def fake_post(url, data=None, timeout=None):
        captured.update(data or {})
        return _FakeResp(200, {"access_token": "tok", "expires_in": 3600})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, {"name": "P"}))
    sma.validate({
        "client_id": "c5", "client_secret": "s", "system_id": "p1",
        "refresh_token": "rt-abc",
    })
    assert captured["grant_type"] == "refresh_token"
    assert captured["refresh_token"] == "rt-abc"


# ── chint stub ────────────────────────────────────────────────────────────────

def test_chint_stub_behavior():
    with pytest.raises(InverterError) as exc:
        chint.validate({})
    assert "no public API" in str(exc.value)
    assert chint.fetch_live({}) is None
    assert chint.fetch_daily({}, date(2026, 1, 1), date(2026, 1, 2)) == []
    assert chint.AVAILABLE is False


# ── vendors listing endpoint ──────────────────────────────────────────────────

def test_inverter_vendors_listing(client):
    _tid, key = _make_tenant()
    resp = client.get("/v1/array-owners/inverter-vendors", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    by_code = {v["code"]: v for v in resp.json()}
    assert set(by_code) == {"solaredge", "fronius", "sma", "chint"}
    # solaredge: 2 fields; fronius: 3; sma: 4; chint: unavailable + note
    assert len(by_code["solaredge"]["fields"]) == 2
    assert len(by_code["fronius"]["fields"]) == 3
    assert len(by_code["sma"]["fields"]) == 4
    assert by_code["solaredge"]["available"] is True
    assert by_code["chint"]["available"] is False
    assert by_code["chint"]["note"]
    # secret flags are present so the form can mask sensitive inputs
    se_fields = {f["name"]: f["secret"] for f in by_code["solaredge"]["fields"]}
    assert se_fields["api_key"] is True
    assert se_fields["site_id"] is False


def test_inverter_vendors_requires_auth(client):
    resp = client.get("/v1/array-owners/inverter-vendors")
    assert resp.status_code == 401


# ── POST /inverter endpoint ───────────────────────────────────────────────────

def test_connect_inverter_solaredge_success(client, monkeypatch):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "ConnectViaInverter")

    body = {"details": {"name": "Inverter Site", "peakPower": 7.7}}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, body))

    resp = client.post(
        f"/v1/array-owners/arrays/{array_id}/inverter",
        json={"vendor": "solaredge", "config": {"api_key": "k", "site_id": 42}},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"] is True
    assert out["site_name"] == "Inverter Site"

    with SessionLocal() as db:
        conn = db.query(InverterConnection).filter_by(array_id=array_id).one()
        assert conn.vendor == "solaredge"
        assert conn.status == "ok"
        assert conn.config["site_id"] == 42
        # legacy columns mirrored for backward compat
        arr = db.get(Array, array_id)
        assert arr.solaredge_api_key == "k"
        assert arr.solaredge_site_id == 42


def test_connect_inverter_auth_failure_persists_nothing(client, monkeypatch):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "RejectInverter")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(401, {"error": "bad"}))

    resp = client.post(
        f"/v1/array-owners/arrays/{array_id}/inverter",
        json={"vendor": "solaredge", "config": {"api_key": "bad", "site_id": 9}},
        headers=_auth(key),
    )
    assert resp.status_code == 400, resp.text

    with SessionLocal() as db:
        assert db.query(InverterConnection).filter_by(array_id=array_id).first() is None
        arr = db.get(Array, array_id)
        assert arr.solaredge_api_key is None


def test_connect_inverter_unknown_vendor(client):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "UnknownVendorArray")
    resp = client.post(
        f"/v1/array-owners/arrays/{array_id}/inverter",
        json={"vendor": "bogus", "config": {}},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    assert "Unknown inverter vendor" in resp.json()["detail"]


def test_connect_inverter_chint_returns_guidance(client):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "ChintArray")
    resp = client.post(
        f"/v1/array-owners/arrays/{array_id}/inverter",
        json={"vendor": "chint", "config": {}},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    assert "no public API" in resp.json()["detail"]


def test_connect_inverter_session_token_auth(client, monkeypatch):
    """The SPA authenticates with a signed session token, not the tenant key."""
    from api.account import _sign_session
    tid, _key = _make_tenant()
    array_id = _make_array(tid, "SessionTokenArray")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, {"details": {"name": "S"}}))

    token = _sign_session(tid)
    resp = client.post(
        f"/v1/array-owners/arrays/{array_id}/inverter",
        json={"vendor": "solaredge", "config": {"api_key": "k", "site_id": 5}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text


# ── legacy /solaredge shim still works (forwards through framework) ────────────

def test_legacy_solaredge_shim_creates_connection(client, monkeypatch):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "LegacyShimArray")

    body = {
        "overview": {"currentPower": {"power": 1200.0}, "lastUpdateTime": "x"},
        "details": {"name": "Starlake Roof", "peakPower": 9.6},
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, body))

    resp = client.post(
        f"/v1/array-owners/arrays/{array_id}/solaredge",
        json={"api_key": "valid_key", "site_id": 12345},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out == {
        "ok": True, "site_name": "Starlake Roof", "peak_power_kw": 9.6, "site_id": 12345,
    }

    # The shim now also lands a real InverterConnection row (vendor=solaredge).
    with SessionLocal() as db:
        conn = db.query(InverterConnection).filter_by(array_id=array_id).one()
        assert conn.vendor == "solaredge"
        arr = db.get(Array, array_id)
        assert arr.solaredge_api_key == "valid_key"
        assert arr.solaredge_site_id == 12345


# ── virtual-connection fallback from legacy Array.solaredge_* fields ───────────

def test_virtual_connection_fallback_overview(client, monkeypatch):
    """An array with legacy solaredge_* columns and NO InverterConnection row
    is still read as a live solaredge source by the overview."""
    tid, key = _make_tenant()
    array_id = _make_array(
        tid, "VirtualConn",
        solaredge_api_key="legacy_key", solaredge_site_id=888,
    )
    # Sanity: no InverterConnection row exists for this array.
    with SessionLocal() as db:
        assert db.query(InverterConnection).filter_by(array_id=array_id).first() is None

    import api.array_owners as array_owners
    array_owners._overview_cache.clear()
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp(200, {"overview": {"currentPower": {"power": 2750.0}, "lastUpdateTime": "t"}}),
    )

    resp = client.get("/v1/array-owners/overview", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    arr = resp.json()["arrays"][0]
    assert arr["live"] == {"source": "solaredge", "current_power_w": 2750.0, "as_of": "t"}
    array_owners._overview_cache.clear()


def test_resolve_connection_prefers_row_over_legacy(monkeypatch):
    """A real InverterConnection row wins over legacy columns."""
    import api.array_owners as array_owners
    tid, _key = _make_tenant()
    array_id = _make_array(
        tid, "RowWinsArray",
        solaredge_api_key="legacy_key", solaredge_site_id=1,
    )
    with SessionLocal() as db:
        db.add(InverterConnection(
            array_id=array_id, vendor="fronius",
            config={"access_key_id": "a", "access_key_value": "b", "pv_system_id": "p"},
            status="ok",
        ))
        db.commit()

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        conn = array_owners._resolve_connection(db, arr)
        assert conn.vendor == "fronius"


# ── generalized scheduler pull (api.jobs.inverter_pull) ───────────────────────

def test_pull_all_inverters_dispatches_and_skips_chint(monkeypatch):
    from api.jobs.inverter_pull import pull_all_inverters
    from api.models import DailyGeneration

    tid, _key = _make_tenant()
    # A real solaredge connection row.
    se_array = _make_array(tid, "PullSolarEdge")
    # A legacy solaredge array (virtual connection, no row).
    legacy_array = _make_array(
        tid, "PullLegacy", solaredge_api_key="lk", solaredge_site_id=2,
    )
    # A chint connection — must be skipped gracefully.
    chint_array = _make_array(tid, "PullChint")
    with SessionLocal() as db:
        db.add(InverterConnection(
            array_id=se_array, vendor="solaredge",
            config={"api_key": "k", "site_id": 1}, status="unverified",
        ))
        db.add(InverterConnection(
            array_id=chint_array, vendor="chint", config={}, status="unverified",
        ))
        db.commit()

    energy_body = {"energy": {"values": [{"date": "2026-06-01", "value": 10000.0}]}}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(200, energy_body))

    result = pull_all_inverters(days_back=5)
    by_array = {r["array_id"]: r for r in result["results"]}

    # solaredge connections pulled; chint skipped.
    assert by_array[se_array]["days_pulled"] == 1
    assert by_array[legacy_array]["days_pulled"] == 1
    assert "skipped" in by_array[chint_array]

    with SessionLocal() as db:
        # Rows landed for the two solaredge arrays.
        assert db.query(DailyGeneration).filter_by(array_id=se_array).count() == 1
        assert db.query(DailyGeneration).filter_by(array_id=legacy_array).count() == 1
        # The connection row was marked ok + stamped.
        conn = db.query(InverterConnection).filter_by(array_id=se_array).one()
        assert conn.status == "ok"
        assert conn.last_sync_at is not None


def test_pull_all_inverters_records_connection_error(monkeypatch):
    from api.jobs.inverter_pull import pull_all_inverters

    tid, _key = _make_tenant()
    arr_id = _make_array(tid, "PullErr")
    with SessionLocal() as db:
        db.add(InverterConnection(
            array_id=arr_id, vendor="solaredge",
            config={"api_key": "k", "site_id": 1}, status="ok",
        ))
        db.commit()

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(500, {"error": "boom"}))
    result = pull_all_inverters(days_back=2)
    by_array = {r["array_id"]: r for r in result["results"]}
    assert by_array[arr_id]["errors"]

    with SessionLocal() as db:
        conn = db.query(InverterConnection).filter_by(array_id=arr_id).one()
        assert conn.status == "error"
        assert conn.last_error
