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


def test_inverter_capture_reuses_soft_deleted_array_by_name(client):
    """uq_array_per_tenant spans (tenant_id, name) including soft-deleted rows.
    A re-capture of a previously-deleted array must reactivate it by name — not
    INSERT a colliding name (which raised IntegrityError on every retry)."""
    from datetime import datetime
    tid, key = _make_tenant()
    client.post("/v1/array-owners/inverter-capture",
                json=_fronius_payload(), headers=_auth(key))

    # Owner soft-deletes "Waterford"; its name stays reserved by the constraint.
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.name == "Waterford")
        ).scalar_one()
        arr.deleted_at = datetime(2026, 6, 1, 12, 0, 0)
        db.commit()

    # Re-capturing must not crash and must not duplicate the array.
    resp = client.post("/v1/array-owners/inverter-capture",
                       json=_fronius_payload(), headers=_auth(key))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        arrs = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.name == "Waterford")
        ).scalars().all()
        assert len(arrs) == 1  # reused, not duplicated
        assert arrs[0].deleted_at is None  # reactivated


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


# ── per-inverter capture → real sandbox comb ──────────────────────────────────

def _fronius_payload_with_inverters():
    """One system with 3 inverters — one clearly underproducing (the peer signal)."""
    return {
        "provider": "fronius",
        "sites": [{
            "site_id": "6c97d4a9-25c3-4ab3-9ab9-a62f0107c53a",
            "name": "Waterford",
            "energy_today_kwh": 120.0,
            "current_power_w": 30000.0,
            "error_count_today": 0,
            "online": True,
            "status": "producing",
            "inverters": [
                {"serial": "dev-1", "name": "Primo 12.5-1 (1)", "model": "Primo 12.5-1",
                 "nameplate_kw": 12.5, "energy_today_kwh": 41.5},
                {"serial": "dev-2", "name": "Primo 12.5-1 (2)", "model": "Primo 12.5-1",
                 "nameplate_kw": 12.5, "energy_today_kwh": 41.0},
                {"serial": "dev-3", "name": "Primo 12.5-1 (3)", "model": "Primo 12.5-1",
                 "nameplate_kw": 12.5, "energy_today_kwh": 6.0},  # laggard
            ],
        }],
    }


def test_inverter_capture_persists_per_inverter_rows(client):
    from api.models import Inverter, InverterDaily
    tid, key = _make_tenant()
    resp = client.post("/v1/array-owners/inverter-capture",
                       json=_fronius_payload_with_inverters(), headers=_auth(key))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["inverters_persisted"] == 3

    with SessionLocal() as db:
        invs = db.execute(select(Inverter).where(Inverter.tenant_id == tid)).scalars().all()
        assert len(invs) == 3
        assert all(iv.vendor == "fronius" for iv in invs)
        assert sorted(iv.serial for iv in invs) == ["dev-1", "dev-2", "dev-3"]
        assert all(iv.nameplate_kw == 12.5 for iv in invs)
        daily = db.execute(select(InverterDaily).where(InverterDaily.tenant_id == tid)).scalars().all()
        assert len(daily) == 3
        assert math.isclose(sorted(d.kwh for d in daily)[0], 6.0)


def test_inverter_capture_per_inverter_idempotent(client):
    """Re-capture must not duplicate Inverter or InverterDaily rows."""
    from api.models import Inverter, InverterDaily
    tid, key = _make_tenant()
    client.post("/v1/array-owners/inverter-capture",
                json=_fronius_payload_with_inverters(), headers=_auth(key))
    # second capture, laggard climbed a bit
    p2 = _fronius_payload_with_inverters()
    p2["sites"][0]["inverters"][2]["energy_today_kwh"] = 9.0
    client.post("/v1/array-owners/inverter-capture", json=p2, headers=_auth(key))
    with SessionLocal() as db:
        invs = db.execute(select(Inverter).where(Inverter.tenant_id == tid)).scalars().all()
        assert len(invs) == 3  # no dup inverters
        daily = db.execute(select(InverterDaily).where(InverterDaily.tenant_id == tid)).scalars().all()
        assert len(daily) == 3  # no dup day rows
        lag = db.execute(
            select(InverterDaily).join(Inverter).where(
                Inverter.tenant_id == tid, Inverter.serial == "dev-3"
            )
        ).scalar_one()
        assert math.isclose(lag.kwh, 9.0)  # max(6,9)


def test_fleet_tree_renders_fronius_comb(client):
    """The sandbox fleet tree shows the captured Fronius inverters as a real comb,
    peer-analyzed — the laggard flagged, fed from InverterDaily (no API conn)."""
    tid, key = _make_tenant()
    client.post("/v1/array-owners/inverter-capture",
                json=_fronius_payload_with_inverters(), headers=_auth(key))
    resp = client.get("/v1/array-owners/fleet-tree", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    tree = resp.json()
    assert tree["summary"]["inverters_total"] == 3
    col = next(c for c in tree["columns"] if c["array_name"] == "Waterford")
    assert col["inverter_count"] == 3
    serials = sorted(i["sn"] for i in col["inverters"])
    assert serials == ["dev-1", "dev-2", "dev-3"]
    # The laggard (dev-3, 6 kWh vs ~41) must have a low peer_index / attention.
    lag = next(i for i in col["inverters"] if i["sn"] == "dev-3")
    healthy = next(i for i in col["inverters"] if i["sn"] == "dev-1")
    assert lag["peer_index"] is not None
    assert lag["peer_index"] < healthy["peer_index"]


# ── SMA (ennexOS) per-inverter capture — same ingest path as Fronius ──────────

def test_inverter_capture_sma_persists_per_inverter(client):
    """SMA ingests through the same readings endpoint; per-inverter rows persist
    and the fleet tree renders the comb (grounded on Bruce's real STP inverters)."""
    from api.models import Inverter, InverterDaily
    tid, key = _make_tenant()
    payload = {
        "provider": "sma",
        "sites": [{
            "site_id": "8296660", "name": "Timberworks",
            "energy_today_kwh": 496.9, "current_power_w": 92571,
            "error_count_today": 0, "status": "producing",
            "inverters": [
                {"serial": "191245395", "name": "#4 24kW", "model": "STP 24kTL-US-10",
                 "nameplate_kw": 24.0, "energy_today_kwh": 80.0},
                {"serial": "191218141", "name": "#7 15kW", "model": "STP 15kTL-US-10",
                 "nameplate_kw": 15.0, "energy_today_kwh": 49.1},
                {"serial": "191217427", "name": "#6 15kW", "model": "STP 15kTL-US-10",
                 "nameplate_kw": 15.0, "energy_today_kwh": 51.5},
            ],
        }],
    }
    resp = client.post("/v1/array-owners/inverter-capture", json=payload, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["inverters_persisted"] == 3
    with SessionLocal() as db:
        invs = db.execute(select(Inverter).where(Inverter.tenant_id == tid)).scalars().all()
        assert len(invs) == 3
        assert all(iv.vendor == "sma" for iv in invs)
        assert {iv.serial for iv in invs} == {"191245395", "191218141", "191217427"}
    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    assert tree["summary"]["inverters_total"] == 3
    col = next(c for c in tree["columns"] if c["array_name"] == "Timberworks")
    assert col["vendor"] == "sma"


def test_delete_then_restore_array_roundtrips(client):
    """DELETE soft-deletes an array + its inverters; POST .../restore revives exactly
    those rows. Powers the sandbox 'Undo delete'."""
    from api.models import Inverter
    tid, key = _make_tenant()
    aid = _make_array(tid, "Undo Me")
    with SessionLocal() as db:
        db.add_all([
            Inverter(tenant_id=tid, array_id=aid, name="Inv A", vendor="solaredge", serial="UNDO-A"),
            Inverter(tenant_id=tid, array_id=aid, name="Inv B", vendor="solaredge", serial="UNDO-B"),
        ])
        db.commit()

    # Present in the tree before delete.
    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    assert any(c["array_id"] == aid for c in tree["columns"])

    # DELETE → gone from the tree.
    r = client.delete(f"/v1/array-owners/arrays/{aid}", headers=_auth(key))
    assert r.status_code == 200, r.text
    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    assert not any(c["array_id"] == aid for c in tree["columns"])

    # RESTORE → back in the tree with both inverters.
    r = client.post(f"/v1/array-owners/arrays/{aid}/restore", headers=_auth(key))
    assert r.status_code == 200, r.text
    assert r.json()["array_id"] == aid
    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    col = next((c for c in tree["columns"] if c["array_id"] == aid), None)
    assert col is not None
    assert {iv["name"] for iv in col["inverters"]} == {"Inv A", "Inv B"}

    # Restoring a non-deleted array now 404s (idempotent guard).
    r = client.post(f"/v1/array-owners/arrays/{aid}/restore", headers=_auth(key))
    assert r.status_code == 404


# ── Site-level daily history backfill (instant graph on connect) ──────────────

# ── Daylight flag for the card "Sleeping" night state ─────────────────────────

def test_fleet_tree_exposes_is_daylight_flag(client):
    """The card layer needs a server-computed sun-up flag to gate the calm
    'Sleeping' state on (night AND zero output) — never zero-output alone, which
    would mask a daytime fault. fleet-tree must expose is_daylight per column +
    in the summary."""
    tid, key = _make_tenant()
    client.post("/v1/array-owners/inverter-capture",
                json={"provider": "chint", "sites": [{
                    "site_id": "s1", "name": "Sun Test Array",
                    "current_power_w": 5000.0,
                    "inverters": [{"serial": "ST-1", "name": "I1",
                                   "energy_today_kwh": 10.0, "current_power_w": 5000.0}],
                }]}, headers=_auth(key))
    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    assert isinstance(tree["summary"]["is_daylight"], bool)
    col = next(c for c in tree["columns"] if c["array_name"] == "Sun Test Array")
    assert isinstance(col["is_daylight"], bool)


def test_solar_elevation_is_seasonally_correct():
    """The daylight calc uses real solar elevation (NOAA), not a fixed-hour rule —
    so it's right across seasons. The key win over a fixed h<5||h>=21 fallback:
    a VT winter 6am is correctly NIGHT (the fallback would call it day)."""
    import datetime as dt
    from api.inverter_fleet import _is_daylight
    # Vermont = UTC-4 (EDT) summer, UTC-5 (EST) winter.
    assert _is_daylight(when=dt.datetime(2026, 6, 17, 16, 0)) is True    # Jun local noon
    assert _is_daylight(when=dt.datetime(2026, 6, 17, 4, 0)) is False    # Jun local midnight
    assert _is_daylight(when=dt.datetime(2026, 12, 17, 11, 0)) is False  # Dec ~6am EST — dark
    assert _is_daylight(when=dt.datetime(2026, 12, 17, 17, 0)) is True   # Dec ~noon EST — up


def test_inverter_capture_backfills_per_inverter_history(client):
    """REGRESSION/feat (Jun 2026): the per-inverter SPARKLINE needs >=2 days of
    InverterDaily or it shows 'no history yet'. Vendors with per-device history
    (Fronius devwork, SMA per-device measurements) send a per-inverter daily[];
    capturing it must persist each day to InverterDaily (idempotent, max-wins)."""
    from api.models import InverterDaily, Inverter
    from sqlalchemy import select as _sel
    tid, key = _make_tenant()
    payload = {
        "provider": "fronius",
        "sites": [{
            "site_id": "sysX", "name": "Waterford",
            "inverters": [{
                "serial": "dev-1", "name": "Primo 12.5 (1)", "energy_today_kwh": 49.4,
                "current_power_w": 6700.0,
                "daily": [
                    {"date": "2026-06-14", "kwh": 60.1},
                    {"date": "2026-06-15", "kwh": 55.3},
                    {"date": "2026-06-16", "kwh": 0.0},     # quiet day persists as 0
                ],
            }],
        }],
    }
    resp = client.post("/v1/array-owners/inverter-capture", json=payload, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        iv = db.execute(_sel(Inverter).where(Inverter.tenant_id == tid)).scalars().first()
        rows = {r.day.isoformat(): r.kwh for r in db.execute(
            _sel(InverterDaily).where(InverterDaily.inverter_id == iv.id)
        ).scalars().all()}
        # 3 backfilled days + today's row from energy_today_kwh = >=2 → sparkline renders
        assert rows["2026-06-14"] == 60.1
        assert rows["2026-06-15"] == 55.3
        assert rows["2026-06-16"] == 0.0
        assert len(rows) >= 3

    # RE-CAPTURE the SAME account (user did exactly this — "add Fronius again
    # without deleting") → must NOT 500 on uq_inverter_daily_inv_day. The
    # SELECT-then-INSERT-per-row version raised IntegrityError at db.commit()
    # (Sentry PYTHON-FASTAPI-3). Also send a higher kwh to prove max-wins update.
    payload["sites"][0]["inverters"][0]["daily"][0]["kwh"] = 99.9   # 06-14 climbs
    resp2 = client.post("/v1/array-owners/inverter-capture", json=payload, headers=_auth(key))
    assert resp2.status_code == 200, resp2.text
    with SessionLocal() as db:
        iv = db.execute(_sel(Inverter).where(Inverter.tenant_id == tid)).scalars().first()
        rows = {r.day.isoformat(): r.kwh for r in db.execute(
            _sel(InverterDaily).where(InverterDaily.inverter_id == iv.id)
        ).scalars().all()}
        assert rows["2026-06-14"] == 99.9   # max-wins applied, no duplicate row
        assert rows["2026-06-15"] == 55.3


def test_capture_allowed_for_paused_no_card_tenant(client):
    """MULTI-USER (Jun 2026): a 14-day trial with no card auto-pauses
    (active=False, subscription_status='paused_no_card', 'read-only, resume
    anytime'). Capture must STILL work via the tenant-key bearer — data keeps
    flowing; only report delivery gates on active. Pre-fix the strict
    app.tenant_from_bearer hard-403'd every inactive tenant, silently killing
    capture the moment a trial paused."""
    tid, key = _make_tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        t.active = False
        t.subscription_status = "paused_no_card"
        db.commit()
    payload = {"provider": "fronius", "sites": [{
        "site_id": "s1", "name": "Paused Site",
        "inverters": [{"serial": "p-1", "energy_today_kwh": 10.0, "current_power_w": 1000.0}],
    }]}
    resp = client.post("/v1/array-owners/inverter-capture", json=payload, headers=_auth(key))
    assert resp.status_code == 200, resp.text


def test_capture_blocked_for_cancelled_tenant_with_402(client):
    """A HARD-cancelled tenant (chose to leave / payment hard-failed) is refused
    capture — but with an actionable 402 'add a card to resume', NOT a silent
    403, so the extension can show a real upgrade prompt."""
    tid, key = _make_tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        t.active = False
        t.subscription_status = "cancelled"
        db.commit()
    payload = {"provider": "fronius", "sites": [{
        "site_id": "s1", "name": "Gone", "inverters": [{"serial": "g-1", "energy_today_kwh": 1.0}],
    }]}
    resp = client.post("/v1/array-owners/inverter-capture", json=payload, headers=_auth(key))
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["error"] == "subscription-cancelled"


def test_inverter_capture_backfills_site_daily_history(client):
    """REGRESSION/feat (Jun 2026): the Chint extension integrates the production
    chart's 30-min PV power curve into daily kWh (getSiteTimeSharingChart2) and
    sends site.daily[]. Capturing it must backfill DailyGeneration so the array
    graph renders REAL history on connect. Idempotent + max-wins per (array,day)."""
    from api.models import DailyGeneration
    tid, key = _make_tenant()
    payload = {
        "provider": "chint",
        "sites": [{
            "site_id": "5e15c66df12588458ffc011a",
            "name": "Londonderry 186",
            "current_power_w": 150000.0,
            "daily": [
                {"date": "2026-06-11", "kwh": 380.0},
                {"date": "2026-06-12", "kwh": 412.5},
                {"date": "2026-06-13", "kwh": 0.0},      # a quiet day — must persist as 0
                {"date": "2026-06-14", "kwh": 401.2},
            ],
            "inverters": [
                {"serial": "0001013791738041", "name": "Inv 1",
                 "energy_today_kwh": 98.6, "current_power_w": 51000.0},
            ],
        }],
    }
    resp = client.post("/v1/array-owners/inverter-capture", json=payload, headers=_auth(key))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        rows = {r.day.isoformat(): r.kwh for r in db.execute(
            select(DailyGeneration).where(DailyGeneration.tenant_id == tid)
        ).scalars().all()}
        # all 4 backfilled days landed (incl. the literal 0-output day)
        assert rows["2026-06-11"] == 380.0
        assert rows["2026-06-12"] == 412.5
        assert rows["2026-06-13"] == 0.0
        assert rows["2026-06-14"] == 401.2

    # Re-capture with a higher value for one day → max-wins, no duplicate row.
    payload["sites"][0]["daily"][0]["kwh"] = 395.0   # 2026-06-11 climbs
    resp = client.post("/v1/array-owners/inverter-capture", json=payload, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        d11 = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.tenant_id == tid,
                DailyGeneration.day == date.fromisoformat("2026-06-11"),
            )
        ).scalars().all()
        assert len(d11) == 1                 # no duplicate
        assert d11[0].kwh == 395.0           # max(380, 395)

    # And the array graph series surfaces on the fleet tree column.
    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    col = next(c for c in tree["columns"] if c["array_name"] == "Londonderry 186")
    dates = {d["date"] for d in col["daily"]}
    assert {"2026-06-11", "2026-06-12", "2026-06-13", "2026-06-14"} <= dates

def test_inverter_capture_chint_keeps_per_inverter_live_power(client):
    """REGRESSION (Jun 2026): Chint's portal reports live AC power PER inverter
    (commDevice.currentPower), but CaptureInverter had no current_power_w field,
    so Pydantic silently dropped it — every card showed 'not producing right now'
    even mid-day. The real per-inverter watts must now persist as Inverter.last_power_w
    and surface on the fleet tree (NOT a site-allocated estimate).
    """
    from api.models import Inverter
    tid, key = _make_tenant()
    payload = {
        "provider": "chint",
        "sites": [{
            "site_id": "5e15c66df12588458ffc011a",
            "name": "Londonderry 186",
            "current_power_w": 150000.0,   # site total (would drive the OLD split path)
            "inverters": [
                {"serial": "0001013791738041", "name": "Inv 1",
                 "energy_today_kwh": 98.6, "current_power_w": 51000.0},
                {"serial": "0001013791738043", "name": "Inv 2",
                 "energy_today_kwh": 105.6, "current_power_w": 54000.0},
            ],
        }],
    }
    resp = client.post("/v1/array-owners/inverter-capture", json=payload, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["inverters_persisted"] == 2

    # Each inverter keeps its OWN measured watts, not a derived share of the site.
    with SessionLocal() as db:
        rows = {iv.serial: iv for iv in db.execute(
            select(Inverter).where(Inverter.tenant_id == tid)
        ).scalars().all()}
        assert rows["0001013791738041"].last_power_w == 51000.0
        assert rows["0001013791738043"].last_power_w == 54000.0

    # And it surfaces live on the fleet tree (the card's "Current kW").
    tree = client.get("/v1/array-owners/fleet-tree", headers=_auth(key)).json()
    col = next(c for c in tree["columns"] if c["array_name"] == "Londonderry 186")
    powers = {i["sn"]: i["current_power_w"] for i in col["inverters"]}
    assert powers["0001013791738041"] == 51000.0
    assert powers["0001013791738043"] == 54000.0


