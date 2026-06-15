"""Array Owners (EnergyAgent) overview API tests.

Covers:
  - overview with no arrays (empty list + zeroed totals)
  - generation aggregation + value math (rate selected by provider)
  - health transitions: no_source / stale / ok
  - live power via a mocked SolarEdge overview (success + offline)
  - solaredge connect endpoint: success saves key+site; 401 -> 400, no save

All SolarEdge HTTP is mocked — no real network calls.
"""
from __future__ import annotations

import math
import secrets
from datetime import date, timedelta

import pytest
from sqlalchemy import select

import api.array_owners as array_owners
from api.db import SessionLocal
from api.models import Array, Client, DailyGeneration, InverterConnection, Tenant, UtilityAccount
from api.rates import REC_PRICE_USD_PER_MWH


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Owners Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _make_array(
    tenant_id: str,
    name: str,
    *,
    provider: str | None = None,
    client_id: int | None = None,
    solaredge_site_id: int | None = None,
) -> int:
    with SessionLocal() as db:
        arr = Array(
            tenant_id=tenant_id,
            name=name,
            client_id=client_id,
            solaredge_api_key="key_xyz" if solaredge_site_id else None,
            solaredge_site_id=solaredge_site_id,
        )
        db.add(arr)
        db.flush()
        if provider:
            db.add(UtilityAccount(
                tenant_id=tenant_id, array_id=arr.id, provider=provider,
                account_number=f"acct_{secrets.token_hex(3)}",
            ))
        db.commit()
        return arr.id


def _add_daily(tenant_id: str, array_id: int, rows: list[tuple[date, float]]) -> None:
    with SessionLocal() as db:
        for day, kwh in rows:
            db.add(DailyGeneration(
                tenant_id=tenant_id, array_id=array_id, day=day, kwh=kwh,
                source="csv",
            ))
        db.commit()


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


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


@pytest.fixture(autouse=True)
def _clear_overview_cache():
    array_owners._overview_cache.clear()
    yield
    array_owners._overview_cache.clear()


# ── overview: empty ───────────────────────────────────────────────────────────

def test_overview_no_arrays(client):
    _tid, key = _make_tenant()
    resp = client.get("/v1/array-owners/overview", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["arrays"] == []
    totals = body["totals"]
    assert totals["current_power_w"] == 0.0
    assert totals["today_kwh"] == 0.0
    assert totals["month_kwh"] == 0.0
    assert totals["lifetime_kwh"] == 0.0
    assert totals["today_usd"] == 0.0
    assert totals["month_usd"] == 0.0
    assert totals["lifetime_usd"] == 0.0


# ── overview: aggregation + value math ────────────────────────────────────────

def test_overview_aggregation_and_value_math(client):
    tid, key = _make_tenant()
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Green Mountain Community Solar")
        db.add(c)
        db.commit()
        client_id = c.id

    # provider 'vec' -> rate 0.22 (distinct from the 0.21 default).
    array_id = _make_array(tid, "Starlake", provider="vec", client_id=client_id)

    today = date.today()
    month_start = today.replace(day=1)
    rows = [
        (today, 30.0),
        (today - timedelta(days=1), 25.0),
        (date(2024, 7, 15), 1200.0),  # old row -> lifetime > 1 MWh for floor REC
    ]
    _add_daily(tid, array_id, rows)

    resp = client.get("/v1/array-owners/overview", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    arr = resp.json()["arrays"][0]

    assert arr["array_id"] == array_id
    assert arr["name"] == "Starlake"
    assert arr["client_name"] == "Green Mountain Community Solar"
    assert arr["fuel_type"] == "solar"
    assert arr["live"] is None  # no solaredge source

    # Expected kWh computed with the SAME predicates the endpoint uses.
    today_kwh = sum(k for d, k in rows if d == today)
    month_kwh = sum(k for d, k in rows if d >= month_start)
    lifetime_kwh = sum(k for d, k in rows)

    assert arr["today"]["kwh"] == pytest.approx(today_kwh)
    assert arr["month"]["kwh"] == pytest.approx(month_kwh)
    assert arr["lifetime"]["kwh"] == pytest.approx(lifetime_kwh)

    rate = 0.22
    rec = REC_PRICE_USD_PER_MWH
    val = arr["value"]
    assert val["breakdown"]["energy_rate_usd_per_kwh"] == rate
    assert val["breakdown"]["rec_usd_per_mwh"] == rec
    assert val["breakdown"]["energy_usd"] == round(lifetime_kwh * rate, 2)
    assert val["breakdown"]["rec_usd"] == round(math.floor(lifetime_kwh / 1000.0) * rec, 2)

    # today/month REC value is pro-rated (no floor); lifetime REC is floored.
    assert val["today_usd"] == round(today_kwh * rate + (today_kwh / 1000.0) * rec, 2)
    assert val["month_usd"] == round(month_kwh * rate + (month_kwh / 1000.0) * rec, 2)
    assert val["lifetime_usd"] == round(
        lifetime_kwh * rate + math.floor(lifetime_kwh / 1000.0) * rec, 2
    )

    # fresh data today -> healthy
    assert arr["health"]["status"] == "ok"
    assert arr["health"]["days_since_data"] == 0


def test_overview_unknown_provider_uses_default_rate(client):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "NoProviderArray")  # no UtilityAccount
    _add_daily(tid, array_id, [(date.today(), 10.0)])

    resp = client.get("/v1/array-owners/overview", headers=_auth(key))
    arr = resp.json()["arrays"][0]
    assert arr["value"]["breakdown"]["energy_rate_usd_per_kwh"] == 0.21


# ── health transitions ────────────────────────────────────────────────────────

def test_health_transitions(client):
    tid, key = _make_tenant()
    today = date.today()

    no_source_id = _make_array(tid, "NoSource")  # no key, no rows

    stale_id = _make_array(tid, "Stale")
    _add_daily(tid, stale_id, [(today - timedelta(days=5), 12.0)])

    ok_id = _make_array(tid, "Healthy")
    _add_daily(tid, ok_id, [(today, 8.0)])

    resp = client.get("/v1/array-owners/overview", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    by_id = {a["array_id"]: a for a in resp.json()["arrays"]}

    assert by_id[no_source_id]["health"]["status"] == "no_source"
    assert by_id[no_source_id]["health"]["last_data_day"] is None
    assert by_id[no_source_id]["today"] is None

    assert by_id[stale_id]["health"]["status"] == "stale"
    assert by_id[stale_id]["health"]["days_since_data"] == 5

    assert by_id[ok_id]["health"]["status"] == "ok"
    assert by_id[ok_id]["health"]["days_since_data"] == 0


# ── live power (mocked SolarEdge overview) ────────────────────────────────────

def test_overview_live_power_success(client, monkeypatch):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "LiveArray", solaredge_site_id=555)
    _add_daily(tid, array_id, [(date.today(), 5.0)])

    overview_body = {
        "overview": {
            "currentPower": {"power": 4830.5},
            "lastUpdateTime": "2026-06-12 21:29:12",
            "lifeTimeData": {"energy": 48211000.0},
        }
    }

    def fake_get(url, params=None, timeout=None):
        return _FakeResp(200, overview_body)

    monkeypatch.setattr(array_owners.httpx, "get", fake_get)

    resp = client.get("/v1/array-owners/overview", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    arr = resp.json()["arrays"][0]
    assert arr["live"] == {
        "source": "solaredge",
        "current_power_w": 4830.5,
        "as_of": "2026-06-12 21:29:12",
    }
    assert arr["health"]["status"] == "ok"
    assert resp.json()["totals"]["current_power_w"] == 4830.5


def test_overview_live_source_offline(client, monkeypatch):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "OfflineArray", solaredge_site_id=777)
    _add_daily(tid, array_id, [(date.today(), 5.0)])

    def fake_get(url, params=None, timeout=None):
        return _FakeResp(500, {"error": "boom"})

    monkeypatch.setattr(array_owners.httpx, "get", fake_get)

    resp = client.get("/v1/array-owners/overview", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    arr = resp.json()["arrays"][0]
    # Live source configured but unreachable -> offline, regardless of fresh data.
    assert arr["health"]["status"] == "offline"
    assert arr["live"]["current_power_w"] is None
    assert resp.json()["totals"]["current_power_w"] == 0.0


# ── solaredge connect ─────────────────────────────────────────────────────────

def test_connect_solaredge_success_saves(client, monkeypatch):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "ConnectMe")

    body = {
        "overview": {"currentPower": {"power": 1200.0}, "lastUpdateTime": "x"},
        "details": {"name": "Starlake Roof", "peakPower": 9.6},
    }

    def fake_get(url, params=None, timeout=None):
        return _FakeResp(200, body)

    monkeypatch.setattr(array_owners.httpx, "get", fake_get)

    resp = client.post(
        f"/v1/array-owners/arrays/{array_id}/solaredge",
        json={"api_key": "valid_key", "site_id": 12345},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"] is True
    assert out["site_name"] == "Starlake Roof"
    assert out["peak_power_kw"] == 9.6
    assert out["site_id"] == 12345

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        assert arr.solaredge_api_key == "valid_key"
        assert arr.solaredge_site_id == 12345


def test_connect_solaredge_401_returns_400_and_does_not_save(client, monkeypatch):
    tid, key = _make_tenant()
    array_id = _make_array(tid, "RejectMe")

    def fake_get(url, params=None, timeout=None):
        return _FakeResp(401, {"error": "bad key"})

    monkeypatch.setattr(array_owners.httpx, "get", fake_get)

    resp = client.post(
        f"/v1/array-owners/arrays/{array_id}/solaredge",
        json={"api_key": "bad_key", "site_id": 999},
        headers=_auth(key),
    )
    assert resp.status_code == 400, resp.text

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        assert arr.solaredge_api_key is None
        assert arr.solaredge_site_id is None


def test_connect_solaredge_array_not_found(client):
    _tid, key = _make_tenant()
    resp = client.post(
        "/v1/array-owners/arrays/99999/solaredge",
        json={"api_key": "k", "site_id": 1},
        headers=_auth(key),
    )
    assert resp.status_code == 404


def test_overview_requires_auth(client):
    resp = client.get("/v1/array-owners/overview")
    assert resp.status_code == 401


def test_overview_accepts_dashboard_session_token(client):
    """The SPA authenticates with a signed session token, not the tenant key."""
    from api.account import _sign_session
    tid, _key = _make_tenant()
    token = _sign_session(tid)
    resp = client.get(
        "/v1/array-owners/overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["arrays"] == []


# ── connect-single (Fronius / SMA one-system attach) ──────────────────────────

def test_connect_single_creates_array_and_attaches(client, monkeypatch):
    from api.inverters import VENDORS
    tid, key = _make_tenant()
    monkeypatch.setattr(VENDORS["fronius"], "validate",
                        lambda config: {"site_name": "Hilltop house", "peak_power": 8200.0})

    resp = client.post(
        "/v1/array-owners/connect-single",
        json={"vendor": "fronius",
              "config": {"access_key_id": "a", "access_key_value": "b", "pv_system_id": "P1"}},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"] is True and out["vendor"] == "fronius"
    assert out["created"] is True
    assert out["name"] == "Hilltop house"

    # The InverterConnection was persisted on a new array for this tenant.
    with SessionLocal() as db:
        arr = db.get(Array, out["array_id"])
        assert arr is not None and arr.tenant_id == tid
        conn = db.execute(
            select(InverterConnection).where(InverterConnection.array_id == arr.id)
        ).scalar_one()
        assert conn.vendor == "fronius" and conn.status == "ok"


def test_connect_single_matches_existing_array_by_name(client, monkeypatch):
    from api.inverters import VENDORS
    tid, key = _make_tenant()
    existing = _make_array(tid, "Plant 7")
    monkeypatch.setattr(VENDORS["sma"], "validate",
                        lambda config: {"site_name": "Plant 7"})

    resp = client.post(
        "/v1/array-owners/connect-single",
        json={"vendor": "sma",
              "config": {"client_id": "c", "client_secret": "s", "system_id": "7"}},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["created"] is False
    assert out["array_id"] == existing  # matched the existing array, no dup


def test_connect_single_bad_creds_400_and_no_write(client, monkeypatch):
    from api.inverters import VENDORS
    from api.inverters.base import InverterAuthError
    tid, key = _make_tenant()

    def _boom(config):
        raise InverterAuthError("401 bad creds")
    monkeypatch.setattr(VENDORS["fronius"], "validate", _boom)

    resp = client.post(
        "/v1/array-owners/connect-single",
        json={"vendor": "fronius",
              "config": {"access_key_id": "x", "access_key_value": "y", "pv_system_id": "z"}},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    # Nothing was created.
    with SessionLocal() as db:
        n = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert n == []


def test_connect_single_unavailable_vendor_400(client):
    _tid, key = _make_tenant()
    resp = client.post(
        "/v1/array-owners/connect-single",
        json={"vendor": "chint", "config": {}},
        headers=_auth(key),
    )
    assert resp.status_code == 400


# ── inverter-capture (extension readings ingest: Fronius) ─────────────────────

def _fronius_payload():
    return {
        "provider": "fronius",
        "sites": [
            {
                "site_id": "6c97d4a9-25c3-4ab3-9ab9-a62f0107c53a",
                "name": "Waterford",
                "peak_power_kw": 157.2,
                "inverter_count": 12,
                "energy_today_kwh": 488.82,
                "current_power_w": 57017.0,
                "error_count_today": 0,
                "online": True,
                "status": "producing",
            },
            {
                "site_id": "3d6d03aa-3acf-4dbb-b853-a4de015d5731",
                "name": "west chester",
                "peak_power_kw": 151.2,
                "inverter_count": 20,
                "energy_today_kwh": 98.25,
                "current_power_w": 0.0,
                "error_count_today": 1,
                "online": True,
                "status": "fault",
            },
        ],
    }


def test_inverter_capture_creates_arrays_and_records_kwh(client):
    """A Fronius extension capture creates one Array per system and writes
    today's energy as a DailyGeneration row the overview value model reads."""
    tid, key = _make_tenant()
    resp = client.post(
        "/v1/array-owners/inverter-capture",
        json=_fronius_payload(),
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"] is True
    assert out["sites_captured"] == 2
    assert out["arrays_created"] == 2
    assert out["faults_detected"] == 1  # west chester had error_count_today=1

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid)
        ).scalars().all()
        names = sorted(a.name for a in arrays)
        assert names == ["Waterford", "west chester"]
        # Today's energy recorded as extension_pull DailyGeneration.
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.tenant_id == tid)
        ).scalars().all()
        assert len(rows) == 2
        assert all(r.source == "extension_pull" for r in rows)
        by_kwh = sorted(r.kwh for r in rows)
        assert math.isclose(by_kwh[0], 98.25) and math.isclose(by_kwh[1], 488.82)


def test_inverter_capture_is_idempotent_and_takes_max_kwh(client):
    """Re-capturing the same day must NOT duplicate arrays/rows, and the daily
    kWh takes the max (Solar.web's EnergyTodayInkWh climbs through the day)."""
    tid, key = _make_tenant()
    client.post("/v1/array-owners/inverter-capture",
                json=_fronius_payload(), headers=_auth(key))
    # Second capture later in the day: Waterford climbed, a re-read of others same.
    p2 = _fronius_payload()
    p2["sites"][0]["energy_today_kwh"] = 510.40  # climbed
    p2["sites"][1]["energy_today_kwh"] = 50.00   # bogus drop — must be ignored
    resp = client.post("/v1/array-owners/inverter-capture",
                       json=p2, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["arrays_created"] == 0  # matched existing by name

    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(arrays) == 2  # no duplicates
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.tenant_id == tid)
        ).scalars().all()
        assert len(rows) == 2  # no duplicate day rows
        kwhs = sorted(r.kwh for r in rows)
        # Waterford -> max(488.82, 510.40)=510.40; west chester -> max(98.25,50)=98.25
        assert math.isclose(kwhs[0], 98.25) and math.isclose(kwhs[1], 510.40)


def test_inverter_capture_accepts_dashboard_session_token(client):
    """Dual-auth: the AO page authenticates with a signed session token."""
    from api.account import _sign_session
    tid, _key = _make_tenant()
    token = _sign_session(tid)
    resp = client.post(
        "/v1/array-owners/inverter-capture",
        json=_fronius_payload(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sites_captured"] == 2


def test_inverter_capture_rejects_non_capture_vendor(client):
    """SolarEdge has a real API key path — it must NOT use the readings-ingest
    endpoint (guards against a cred-bearing vendor sneaking through)."""
    _tid, key = _make_tenant()
    resp = client.post(
        "/v1/array-owners/inverter-capture",
        json={"provider": "solaredge", "sites": [{"site_id": "1", "name": "x"}]},
        headers=_auth(key),
    )
    assert resp.status_code == 400


def test_inverter_capture_empty_sites_400(client):
    _tid, key = _make_tenant()
    resp = client.post(
        "/v1/array-owners/inverter-capture",
        json={"provider": "fronius", "sites": []},
        headers=_auth(key),
    )
    assert resp.status_code == 400
