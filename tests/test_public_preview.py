"""Tests for the public (unauthenticated) pre-signup SolarEdge preview.

Verifies: no auth required, real sites + value estimate returned, friendly
ok:false bodies for bad/site-level keys, and per-IP rate limiting.
"""
import pytest
from fastapi.testclient import TestClient

from api.app import app
import api.array_owners as ao


client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    ao._PREVIEW_HITS.clear()
    yield
    ao._PREVIEW_HITS.clear()


def test_preview_returns_real_sites_and_value(monkeypatch):
    monkeypatch.setattr(
        ao.inverters.solaredge, "discover_sites",
        lambda key: [
            {"site_id": 1, "name": "Barn roof", "peak_power_kw": 10.0, "status": "Active"},
            {"site_id": 2, "name": "South field", "peak_power_kw": 6.0, "status": "Active"},
        ],
    )
    r = client.post("/v1/array-owners/public/preview", json={"api_key": "ACCT_KEY"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert len(data["sites"]) == 2
    # each site carries a positive annual value estimate
    for s in data["sites"]:
        assert s["annual_value_usd"] > 0
        assert s["annual_kwh"] > 0
    assert data["totals"]["count"] == 2
    assert data["totals"]["peak_power_kw"] == 16.0
    assert data["totals"]["annual_value_usd"] > 0


def test_preview_needs_no_auth():
    # No Authorization header at all — must not 401.
    r = client.post("/v1/array-owners/public/preview", json={"api_key": ""})
    assert r.status_code == 200
    assert r.json()["ok"] is False  # empty key → friendly false, not an error


def test_preview_vendor_locus_partner_discovery(monkeypatch):
    """A Locus credential with a partner_id enumerates the partner's sites."""
    monkeypatch.setattr(
        ao.inverters.VENDORS["locus"], "discover_sites",
        lambda config: [
            {"site_id": 11, "name": "Co-op A", "peak_power_kw": 25.0, "status": ""},
            {"site_id": 12, "name": "Co-op B", "peak_power_kw": None, "status": ""},
        ],
    )
    r = client.post("/v1/array-owners/public/preview", json={
        "vendor": "locus",
        "config": {"client_id": "c", "client_secret": "s", "username": "u",
                   "password": "p", "partner_id": "99"},
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True and data["vendor"] == "locus"
    assert len(data["sites"]) == 2
    # site with peak gets a value; the one without is None (no estimate)
    assert data["sites"][0]["annual_value_usd"] > 0
    assert data["sites"][1]["annual_value_usd"] is None
    assert data["totals"]["annual_value_usd"] > 0


def test_preview_vendor_fronius_single_system(monkeypatch):
    """Fronius has no discovery — validate the one system, peak in Wp→kW."""
    monkeypatch.setattr(
        ao.inverters.VENDORS["fronius"], "validate",
        lambda config: {"site_name": "Rooftop", "peak_power": 8200.0},  # 8.2 kWp
    )
    r = client.post("/v1/array-owners/public/preview", json={
        "vendor": "fronius",
        "config": {"access_key_id": "a", "access_key_value": "b", "pv_system_id": "P1"},
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True and data["vendor"] == "fronius"
    assert len(data["sites"]) == 1
    assert data["sites"][0]["peak_power_kw"] == 8.2
    assert data["sites"][0]["annual_value_usd"] > 0


def test_preview_vendor_sma_no_peak_no_value(monkeypatch):
    """SMA gives no peak power — the array still previews, value is None."""
    monkeypatch.setattr(
        ao.inverters.VENDORS["sma"], "validate",
        lambda config: {"site_name": "Plant 7"},
    )
    r = client.post("/v1/array-owners/public/preview", json={
        "vendor": "sma",
        "config": {"client_id": "c", "client_secret": "s", "system_id": "7"},
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True and data["vendor"] == "sma"
    assert data["sites"][0]["name"] == "Plant 7"
    assert data["sites"][0]["annual_value_usd"] is None
    assert data["totals"]["annual_value_usd"] is None  # nothing to estimate


def test_preview_unavailable_vendor_friendly():
    """Chint is AVAILABLE=False — defensive friendly message, not a 502."""
    r = client.post("/v1/array-owners/public/preview", json={"vendor": "chint", "config": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "CSV" in body["message"] or "doesn't offer" in body["message"]


def test_preview_missing_fields_friendly(monkeypatch):
    """Missing credential fields → friendly ok:false, not a 500."""
    r = client.post("/v1/array-owners/public/preview", json={
        "vendor": "locus", "config": {"client_id": "only"},
    })
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "credentials" in r.json()["message"].lower()


def test_preview_bad_key_is_friendly(monkeypatch):
    def _boom(key):
        raise ao.InverterAuthError("401 bad key")
    monkeypatch.setattr(ao.inverters.solaredge, "discover_sites", _boom)
    r = client.post("/v1/array-owners/public/preview", json={"api_key": "BAD"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "didn't work" in r.json()["message"]


def test_preview_site_level_key_hint(monkeypatch):
    def _scope(key):
        raise ao.InverterScopeError("403 site-level")
    monkeypatch.setattr(ao.inverters.solaredge, "discover_sites", _scope)
    r = client.post("/v1/array-owners/public/preview", json={"api_key": "SITE"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body.get("scope") == "site"
    assert "account-level" in body["message"]


def test_preview_rate_limited(monkeypatch):
    monkeypatch.setattr(ao.inverters.solaredge, "discover_sites", lambda key: [])
    # _PREVIEW_MAX attempts allowed, then 429.
    last = None
    for _ in range(ao._PREVIEW_MAX + 3):
        last = client.post("/v1/array-owners/public/preview", json={"api_key": "K"})
    assert last.status_code == 429
