"""Operator-entered expected kWh/kW (specific yield) — Bruce's Analysis ask.

Covers:
  - POST /v1/array-owners/arrays/{id}/expected-ratio: set / clear / validation /
    tenant scoping / demo block
  - build_forecast math in operator_ratio mode (expected = ratio × kW per day,
    no location or irradiance needed)
  - /forecast-fleet: ratio-based arrays modeled WITHOUT a location, per-row
    kwh_per_kw_day / kwh_per_kw_window, the fleet kwh_per_kw headline, and the
    weather-model path unchanged (with POA mocked — no real network anywhere)

UNITS everywhere: expected_kwh_per_kw_day and kwh_per_kw_day are kWh per kW per
DAY; kwh_per_kw_window is the measured window total per kW (measured days only).
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

import pytest
from sqlalchemy import select

import api.array_owners as array_owners
from api import forecasting
from api.db import SessionLocal
from api.models import Array, DailyGeneration, Inverter, Tenant


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _make_tenant(*, is_demo: bool = False) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Ratio Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True, is_demo=is_demo,
        ))
        db.commit()
    return tid, key


def _make_array(tenant_id: str, name: str, *, nameplate_kw: float | None = None,
                lat: float | None = None, lng: float | None = None,
                expected_ratio: float | None = None) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tenant_id, name=name)
        if lat is not None:
            arr.latitude, arr.longitude = lat, lng
            arr.geocode_source = "manual"
            arr.geocoded_address = "test"
        if expected_ratio is not None:
            arr.expected_kwh_per_kw_day = expected_ratio
        db.add(arr)
        db.flush()
        if nameplate_kw:
            db.add(Inverter(
                tenant_id=tenant_id, array_id=arr.id, vendor="fronius",
                serial="SN-" + secrets.token_hex(4), nameplate_kw=nameplate_kw,
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


def _window_end() -> date:
    """The forecast window's last day (yesterday, by the endpoint's own clock)."""
    return array_owners.now().date() - timedelta(days=1)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Every external fetch is explicitly mocked or must not be called."""
    def _boom(*a, **k):  # pragma: no cover - only fires on a regression
        raise AssertionError("unexpected external fetch in ratio-mode test")
    monkeypatch.setattr(forecasting, "fetch_poa_daily", _boom)
    monkeypatch.setattr(forecasting, "fetch_current_weather_code", lambda *a, **k: None)
    monkeypatch.setattr(forecasting, "geocode_oneline", lambda *a, **k: None)
    yield


# ── the override endpoint ─────────────────────────────────────────────────────

def test_expected_ratio_set_clear_and_validation(client):
    tid, key = _make_tenant()
    aid = _make_array(tid, "Tannery Brook", nameplate_kw=100)

    r = client.post(f"/v1/array-owners/arrays/{aid}/expected-ratio",
                    json={"expected_kwh_per_kw_day": 3.5}, headers=_auth(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expected_kwh_per_kw_day"] == 3.5
    assert "per day" in body["units"]
    with SessionLocal() as db:
        assert db.get(Array, aid).expected_kwh_per_kw_day == 3.5

    # physical-range validation: 0 and negatives and desert-beating typos → 400
    for bad in (0, -1, 12.5, 400):
        r = client.post(f"/v1/array-owners/arrays/{aid}/expected-ratio",
                        json={"expected_kwh_per_kw_day": bad}, headers=_auth(key))
        assert r.status_code == 400, f"{bad} should be rejected"

    # null clears → back to the weather model
    r = client.post(f"/v1/array-owners/arrays/{aid}/expected-ratio",
                    json={"expected_kwh_per_kw_day": None}, headers=_auth(key))
    assert r.status_code == 200
    with SessionLocal() as db:
        assert db.get(Array, aid).expected_kwh_per_kw_day is None


def test_expected_ratio_tenant_scoped_and_demo_blocked(client):
    tid, _key = _make_tenant()
    aid = _make_array(tid, "Mine", nameplate_kw=10)
    _tid2, key2 = _make_tenant()
    r = client.post(f"/v1/array-owners/arrays/{aid}/expected-ratio",
                    json={"expected_kwh_per_kw_day": 3.0}, headers=_auth(key2))
    assert r.status_code == 404

    demo_tid, demo_key = _make_tenant(is_demo=True)
    demo_aid = _make_array(demo_tid, "Demo", nameplate_kw=10)
    r = client.post(f"/v1/array-owners/arrays/{demo_aid}/expected-ratio",
                    json={"expected_kwh_per_kw_day": 3.0}, headers=_auth(demo_key))
    assert r.status_code == 403


# ── the math core (pure) ──────────────────────────────────────────────────────

def test_build_forecast_operator_ratio_math():
    today = date(2026, 7, 3)
    # 5-day window = 06-28..07-02. Two measured days at 250 kWh.
    actual = {"2026-06-30": 250.0, "2026-07-01": 250.0}
    fc = forecasting.build_forecast(
        nameplate_kw=100.0, lat=None, lng=None, tilt_deg=0.0, azimuth_deg=0.0,
        tilt_assumed=True, azimuth_assumed=True, geocode_source=None,
        geocoded_address=None, actual_by_day=actual, window_days=5,
        today=today, expected_kwh_per_kw_day=3.0,
    )
    assert fc.available
    d = fc.to_dict()
    # expected = 3.0 kWh/kW/day × 100 kW = 300/day, flat across all 5 days
    assert d["expected_kwh"] == 1500.0
    assert d["expected_matched_kwh"] == 600.0      # 2 measured days × 300
    assert d["actual_kwh"] == 500.0
    assert d["ratio_pct"] == round(500 / 600 * 100)
    assert d["inputs"]["expected_basis"] == "operator_ratio"
    assert d["inputs"]["expected_kwh_per_kw_day"] == 3.0
    assert d["inputs"]["measured_days"] == 2
    assert len(d["days"]) == 5
    # no irradiance in ratio mode: poa is None and nothing claims Open-Meteo
    assert all(x["poa_kwh_m2"] is None for x in d["days"])
    assert "irradiance" not in d["inputs"]


def test_build_forecast_weather_mode_unchanged():
    today = date(2026, 7, 3)
    poa = {(today - timedelta(days=i)).isoformat(): 5.0 for i in range(1, 6)}
    actual = {(today - timedelta(days=1)).isoformat(): 30.0,
              (today - timedelta(days=2)).isoformat(): 30.0}
    fc = forecasting.build_forecast(
        nameplate_kw=10.0, lat=44.2, lng=-72.5, tilt_deg=44.0, azimuth_deg=0.0,
        tilt_assumed=True, azimuth_assumed=True, geocode_source="manual",
        geocoded_address="test", actual_by_day=actual, window_days=5,
        today=today, _poa_by_day=poa,
    )
    d = fc.to_dict()
    # expected/day = 10 kW × (5.0/1.0) × 0.84 = 42
    assert d["expected_matched_kwh"] == pytest.approx(84.0)
    assert d["ratio_pct"] == round(60 / 84 * 100)
    assert d["inputs"]["expected_basis"] == "weather_model"
    assert d["inputs"]["expected_kwh_per_kw_day"] is None
    assert d["inputs"]["irradiance"]["best_day_poa_kwh_m2"] == 5.0


# ── the fleet endpoint ────────────────────────────────────────────────────────

def test_forecast_fleet_ratio_override_models_without_location(client):
    tid, key = _make_tenant()
    end = _window_end()
    # A: 100 kW, NO location, operator expected 3.0 → modeled on the ratio basis
    a = _make_array(tid, "Ratio Array", nameplate_kw=100, expected_ratio=3.0)
    _add_daily(tid, a, [(end, 250.0), (end - timedelta(days=1), 250.0)])
    # B: no location, no override → honestly skipped (but see C)
    b = _make_array(tid, "Unlocated", nameplate_kw=50)
    # C: no location, no override, BUT measured data → skipped from the weather
    # model yet still carries kWh/kW (the health ranking needs no weather model)
    c = _make_array(tid, "Unlocated Measured", nameplate_kw=50)
    _add_daily(tid, c, [(end, 100.0), (end - timedelta(days=1), 100.0)])

    r = client.get("/v1/array-owners/forecast-fleet?window_days=10", headers=_auth(key))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["available"] is True
    assert body["arrays_modeled"] == 1
    assert body["arrays_ratio_based"] == 1
    rows = {row["array_id"]: row for row in body["rows"]}
    row = rows[a]
    assert row["expected_basis"] == "operator_ratio"
    assert row["expected_kwh_per_kw_day"] == 3.0
    assert row["measured_days"] == 2
    # kWh/kW: 500 kWh ÷ 100 kW = 5.0 over the measured days; ÷2 days = 2.5/day
    assert row["kwh_per_kw_window"] == 5.0
    assert row["kwh_per_kw_day"] == 2.5
    # ratio vs the operator target: 500 / (2 × 300) = 83%
    assert row["ratio_pct"] == round(500 / 600 * 100)
    assert row["weather_code"] is None            # no location → no sky claim

    # fleet kWh/kW headline: nameplate-weighted, measured days only, and it
    # INCLUDES the unlocated-but-measured array C:
    # (500 + 200) kWh ÷ (100×2 + 50×2) kW·days = 700/300 ≈ 2.33
    kk = body["kwh_per_kw"]
    assert kk["fleet_per_day"] == round(700 / 300, 2)
    assert kk["nameplate_kw"] == 150.0
    assert kk["arrays_counted"] == 2
    assert "per day" in kk["units"]

    skipped = {s["array_id"]: s for s in body["skipped"]}
    assert skipped[b]["reason"] == "no_location"
    assert "kwh_per_kw_day" not in skipped[b]      # no data → no number, no fake
    # C is still skipped from the weather model but carries its measured kWh/kW
    assert skipped[c]["reason"] == "no_location"
    assert skipped[c]["kwh_per_kw_day"] == 2.0     # 200 ÷ 50 kW ÷ 2 days
    assert skipped[c]["kwh_per_kw_window"] == 4.0
    assert skipped[c]["measured_days"] == 2


def test_forecast_fleet_weather_mode_carries_kwh_per_kw(client, monkeypatch):
    tid, key = _make_tenant()
    end = _window_end()
    poa = {(end - timedelta(days=i)).isoformat(): 5.0 for i in range(0, 10)}
    monkeypatch.setattr(forecasting, "fetch_poa_daily", lambda *a, **k: dict(poa))
    monkeypatch.setattr(forecasting, "fetch_current_weather_code", lambda *a, **k: 0)

    a = _make_array(tid, "Weather Array", nameplate_kw=10, lat=44.2, lng=-72.5)
    _add_daily(tid, a, [(end, 30.0), (end - timedelta(days=1), 30.0)])

    r = client.get("/v1/array-owners/forecast-fleet?window_days=10", headers=_auth(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["arrays_ratio_based"] == 0
    row = body["rows"][0]
    assert row["expected_basis"] == "weather_model"
    assert row["expected_kwh_per_kw_day"] is None
    assert row["kwh_per_kw_day"] == 3.0           # 60 ÷ 10 kW ÷ 2 days
    assert row["kwh_per_kw_window"] == 6.0
    assert body["kwh_per_kw"]["fleet_per_day"] == 3.0
    # the "how" panel inputs stay the full weather model
    assert body["inputs"]["expected_basis"] == "weather_model"
    assert "irradiance" in body["inputs"]


def test_forecast_fleet_prefers_weather_inputs_for_how_panel(client, monkeypatch):
    """rep inputs for the card's math panel come from a WEATHER-modeled array even
    when a bigger array uses the operator-ratio basis."""
    tid, key = _make_tenant()
    end = _window_end()
    poa = {(end - timedelta(days=i)).isoformat(): 5.0 for i in range(0, 10)}
    monkeypatch.setattr(forecasting, "fetch_poa_daily", lambda *a, **k: dict(poa))
    monkeypatch.setattr(forecasting, "fetch_current_weather_code", lambda *a, **k: 0)

    big = _make_array(tid, "Big Ratio", nameplate_kw=500, expected_ratio=3.0)
    _add_daily(tid, big, [(end, 900.0)])
    small = _make_array(tid, "Small Weather", nameplate_kw=10, lat=44.2, lng=-72.5)
    _add_daily(tid, small, [(end, 30.0)])

    r = client.get("/v1/array-owners/forecast-fleet?window_days=10", headers=_auth(key))
    body = r.json()
    assert body["arrays_modeled"] == 2
    assert body["arrays_ratio_based"] == 1
    assert body["inputs"]["expected_basis"] == "weather_model"


# ── the single-array endpoint ─────────────────────────────────────────────────

def test_single_array_forecast_honors_override_without_location(client):
    tid, key = _make_tenant()
    end = _window_end()
    a = _make_array(tid, "Solo Ratio", nameplate_kw=100, expected_ratio=3.0)
    _add_daily(tid, a, [(end, 250.0)])

    r = client.get(f"/v1/array-owners/forecast?array_id={a}&window_days=10",
                   headers=_auth(key))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["available"] is True
    assert d["inputs"]["expected_basis"] == "operator_ratio"
    assert d["expected_kwh"] == 3000.0            # 3.0 × 100 kW × 10 days
    assert d["expected_matched_kwh"] == 300.0
    assert d["ratio_pct"] == round(250 / 300 * 100)


# ── the instant-load snapshot cache ───────────────────────────────────────────

def test_forecast_fleet_snapshot_caches_and_invalidates(client, monkeypatch):
    """The fleet forecast is cached in a snapshot (instant repeat loads), and a
    model-changing mutation drops it so the next load recomputes fresh."""
    from api.models import FleetForecastSnapshot
    tid, key = _make_tenant()
    end = _window_end()
    poa = {(end - timedelta(days=i)).isoformat(): 5.0 for i in range(0, 10)}
    monkeypatch.setattr(forecasting, "fetch_poa_daily", lambda *a, **k: dict(poa))
    monkeypatch.setattr(forecasting, "fetch_current_weather_code", lambda *a, **k: 0)

    a = _make_array(tid, "Cache Array", nameplate_kw=10, lat=44.2, lng=-72.5)
    _add_daily(tid, a, [(end, 30.0), (end - timedelta(days=1), 30.0)])

    # 1) First load computes AND stores exactly one snapshot for the window.
    r1 = client.get("/v1/array-owners/forecast-fleet?window_days=10", headers=_auth(key))
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    with SessionLocal() as db:
        snaps = db.execute(select(FleetForecastSnapshot).where(
            FleetForecastSnapshot.tenant_id == tid)).scalars().all()
    assert len(snaps) == 1 and snaps[0].window_days == 10

    # 2) A repeat load is served from the snapshot — no recompute. Prove it by
    # making the POA fetch explode: if the endpoint recomputed, it would raise.
    def _explode(*a, **k):
        raise AssertionError("served from snapshot should NOT recompute")
    monkeypatch.setattr(forecasting, "fetch_poa_daily", _explode)
    r2 = client.get("/v1/array-owners/forecast-fleet?window_days=10", headers=_auth(key))
    assert r2.status_code == 200
    assert r2.json()["expected_kwh"] == body1["expected_kwh"]

    # 3) A location change invalidates the snapshot (the mutation POST geocodes,
    # it doesn't touch POA, so _explode is safe here).
    monkeypatch.setattr(forecasting, "geocode_oneline",
                        lambda *a, **k: {"lat": 45.0, "lng": -71.0,
                                         "source": "census", "matched": "Montpelier, VT"})
    rp = client.post(f"/v1/array-owners/arrays/{a}/location",
                     json={"place": "Montpelier, VT"}, headers=_auth(key))
    assert rp.status_code == 200, rp.text
    with SessionLocal() as db:
        remaining = db.execute(select(FleetForecastSnapshot).where(
            FleetForecastSnapshot.tenant_id == tid)).scalars().all()
    assert remaining == []            # snapshot dropped → next load recomputes fresh
