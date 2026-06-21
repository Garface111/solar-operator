"""Enphase (Enlighten v4) adapter — code-shape tests with mocked httpx.

These prove the adapter's parsing/flow against the DOCUMENTED v4 shapes; they do
NOT prove the live API behaves identically (no real Enphase account yet — see the
LOUD CAVEAT in api/inverters/enphase.py). All HTTP is monkeypatched.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from api.inverters import VENDORS, enphase
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


CFG = {
    "api_key": "appkey", "client_id": "cid", "client_secret": "secret",
    "system_id": "123", "refresh_token": "rt-old",
}

TOKEN_BODY = {"access_token": "at-1", "refresh_token": "rt-new", "expires_in": 86400}
SUMMARY_BODY = {
    "system_id": 123, "current_power": 4200, "energy_today": 18400,
    "energy_lifetime": 9000000, "last_report_at": 1782000000, "status": "normal",
}
SYSTEMS_BODY = {
    "systems": [
        {"system_id": 123, "name": "Maple St", "public_name": "Residential", "system_size": 7600},
        {"system_id": 456, "name": "Barn", "system_size": 12000},
    ]
}
ENERGY_BODY = {"system_id": 123, "start_date": "2026-06-01", "production": [12000, 15000, None, 9000]}


@pytest.fixture(autouse=True)
def _clear_cache():
    enphase._TOKEN_CACHE.clear()
    yield
    enphase._TOKEN_CACHE.clear()


def _token_ok(*a, **k):
    return _FakeResp(200, dict(TOKEN_BODY))


def _route_get(url, *a, **k):
    if url.endswith("/summary"):
        return _FakeResp(200, SUMMARY_BODY)
    if url.endswith("/energy_lifetime"):
        return _FakeResp(200, ENERGY_BODY)
    if url.endswith("/systems"):
        return _FakeResp(200, SYSTEMS_BODY)
    return _FakeResp(404, {"error": "unmapped " + url})


def test_registered_in_vendor_dict():
    assert "enphase" in VENDORS
    assert VENDORS["enphase"].LABEL.startswith("Enphase")


def test_validate_success(monkeypatch):
    monkeypatch.setattr(httpx, "post", _token_ok)
    monkeypatch.setattr(httpx, "get", _route_get)
    out = enphase.validate(dict(CFG))
    assert out == {"site_name": "Maple St"}


def test_validate_auth_failure(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(401, {"error": "bad"}))
    with pytest.raises(InverterAuthError):
        enphase.validate(dict(CFG))


def test_fetch_live_parses_current_power(monkeypatch):
    monkeypatch.setattr(httpx, "post", _token_ok)
    monkeypatch.setattr(httpx, "get", _route_get)
    live = enphase.fetch_live(dict(CFG))
    assert live["current_power_w"] == 4200.0
    assert live["as_of"] and live["as_of"].endswith("Z")


def test_fetch_daily_wh_to_kwh_and_dates(monkeypatch):
    monkeypatch.setattr(httpx, "post", _token_ok)
    monkeypatch.setattr(httpx, "get", _route_get)
    rows = enphase.fetch_daily(dict(CFG), date(2026, 6, 1), date(2026, 6, 4))
    # production [12000,15000,None,9000] Wh from 2026-06-01 -> kWh, None skipped
    assert {"day": date(2026, 6, 1), "kwh": 12.0} in rows
    assert {"day": date(2026, 6, 2), "kwh": 15.0} in rows
    assert {"day": date(2026, 6, 4), "kwh": 9.0} in rows
    assert all(r["day"] != date(2026, 6, 3) for r in rows)   # None gap skipped


def test_discover_sites_account_level(monkeypatch):
    monkeypatch.setattr(httpx, "post", _token_ok)
    monkeypatch.setattr(httpx, "get", _route_get)
    sites = enphase.discover_sites(dict(CFG))
    assert {s["site_id"] for s in sites} == {123, 456}
    maple = next(s for s in sites if s["site_id"] == 123)
    assert maple["name"] == "Maple St" and maple["peak_power_kw"] == 7.6


def test_refresh_token_rotation_written_back(monkeypatch):
    monkeypatch.setattr(httpx, "post", _token_ok)
    monkeypatch.setattr(httpx, "get", _route_get)
    cfg = dict(CFG)
    enphase.fetch_live(cfg)
    # Enphase rotates the refresh token on use; the adapter must persist the new one.
    assert cfg["refresh_token"] == "rt-new"


def test_no_credentials_raises(monkeypatch):
    monkeypatch.setattr(httpx, "post", _token_ok)
    monkeypatch.setattr(httpx, "get", _route_get)
    cfg = {"api_key": "appkey", "client_id": "cid", "client_secret": "secret", "system_id": "123"}
    with pytest.raises(Exception):
        enphase.fetch_live(cfg)   # no refresh_token and no username/password
