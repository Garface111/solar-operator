"""Solis (SolisCloud HMAC) + Tigo (EI v3 login) adapters — code-shape tests with
mocked httpx. These prove parsing/flow against the documented contract; they do
NOT prove the live APIs behave identically (no real accounts yet — see the LOUD
CAVEATs in the adapters).
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from api.inverters import VENDORS, solis, tigo
from api.inverters.base import InverterAuthError


class _FakeResp:
    def __init__(self, status_code, body, text=None):
        self.status_code = status_code
        self._body = body
        self._text = text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    @property
    def text(self):
        return self._text if self._text is not None else str(self._body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


# ── Solis ─────────────────────────────────────────────────────────────────────

SOLIS_CFG = {"key_id": "kid", "key_secret": "ksecret", "station_id": "ST1"}
STATION_LIST = {"success": True, "code": "0", "data": {"page": {"records": [
    {"id": "ST1", "stationName": "Maple Roof", "capacity": 9.6},
    {"id": "ST2", "stationName": "Barn", "capacity": 22.0},
]}}}
STATION_DETAIL = {"success": True, "code": "0", "data": {
    "id": "ST1", "stationName": "Maple Roof", "power": 4.2, "powerStr": "kW",
}}
DAY_ENERGY = {"success": True, "code": "0", "data": {"page": {"records": [
    {"date": "2026-06-01", "energy": 31.5, "energyStr": "kWh"},
    {"date": "2026-06-02", "energy": 28.0, "energyStr": "kWh"},
]}}}


def _solis_post(url, *a, **k):
    if url.endswith("/userStationList"):
        return _FakeResp(200, STATION_LIST)
    if url.endswith("/stationDetail"):
        return _FakeResp(200, STATION_DETAIL)
    if url.endswith("/stationDayEnergyList"):
        return _FakeResp(200, DAY_ENERGY)
    return _FakeResp(404, {"success": False, "code": "404"})


def test_solis_registered():
    assert "solis" in VENDORS and VENDORS["solis"].LABEL.startswith("Solis")


def test_solis_validate_and_discover(monkeypatch):
    monkeypatch.setattr(httpx, "post", _solis_post)
    assert solis.validate(dict(SOLIS_CFG)) == {"site_name": "Maple Roof"}
    sites = solis.discover_sites(dict(SOLIS_CFG))
    assert {s["site_id"] for s in sites} == {"ST1", "ST2"}


def test_solis_fetch_live_kw_to_w(monkeypatch):
    monkeypatch.setattr(httpx, "post", _solis_post)
    live = solis.fetch_live(dict(SOLIS_CFG))
    assert live["current_power_w"] == 4200.0   # 4.2 kW -> W


def test_solis_fetch_daily(monkeypatch):
    monkeypatch.setattr(httpx, "post", _solis_post)
    rows = solis.fetch_daily(dict(SOLIS_CFG), date(2026, 6, 1), date(2026, 6, 2))
    assert {"day": date(2026, 6, 1), "kwh": 31.5} in rows
    assert {"day": date(2026, 6, 2), "kwh": 28.0} in rows


def test_solis_auth_failure(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(401, {"msg": "bad"}))
    with pytest.raises(InverterAuthError):
        solis.validate(dict(SOLIS_CFG))


# ── Tigo ──────────────────────────────────────────────────────────────────────

TIGO_CFG = {"username": "u@e.test", "password": "pw", "system_id": "77"}
TIGO_LOGIN = {"user": {"auth": "tok-123"}}
TIGO_SYSTEMS = {"systems": [{"system_id": 77, "name": "Hillside"}, {"system_id": 88, "name": "Garage"}]}
TIGO_SUMMARY = {"summary": {"last_power_dc": 5300, "last_data_received": "2026-06-21T12:00:00Z"}}


@pytest.fixture(autouse=True)
def _clear_tigo_cache():
    tigo._TOKEN_CACHE.clear()
    yield
    tigo._TOKEN_CACHE.clear()


def _tigo_get(url, *a, **k):
    if url.endswith("/systems"):
        return _FakeResp(200, TIGO_SYSTEMS)
    if url.endswith("/summary"):
        return _FakeResp(200, TIGO_SUMMARY)
    return _FakeResp(404, {"error": "unmapped"})


def test_tigo_registered():
    assert "tigo" in VENDORS and VENDORS["tigo"].LABEL.startswith("Tigo")


def test_tigo_validate_and_discover(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(200, TIGO_LOGIN))
    monkeypatch.setattr(httpx, "get", _tigo_get)
    assert tigo.validate(dict(TIGO_CFG)) == {"site_name": "Hillside"}
    sites = tigo.discover_sites(dict(TIGO_CFG))
    assert {s["site_id"] for s in sites} == {77, 88}


def test_tigo_fetch_live(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(200, TIGO_LOGIN))
    monkeypatch.setattr(httpx, "get", _tigo_get)
    live = tigo.fetch_live(dict(TIGO_CFG))
    assert live["current_power_w"] == 5300.0


def test_tigo_login_auth_failure(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(401, {"error": "nope"}))
    with pytest.raises(InverterAuthError):
        tigo.validate(dict(TIGO_CFG))
