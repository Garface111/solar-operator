"""
Tests for api/adapters/solaredge.py

All HTTP calls are mocked via httpx.MockTransport (or unittest.mock).
No real SolarEdge API calls are made.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from api.adapters.solaredge import (
    SolarEdgeAuthError,
    SolarEdgeError,
    fetch_daily_energy,
    list_sites,
    site_details,
)


def _mock_response(status_code: int, json_body: dict):
    """Build a fake httpx.Response-like object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


# ── fetch_daily_energy ────────────────────────────────────────────────────────

ENERGY_FIXTURE = {
    "energy": {
        "timeUnit": "DAY",
        "unit": "Wh",
        "values": [
            {"date": "2024-07-01 00:00:00", "value": 25720.0},
            {"date": "2024-07-02 00:00:00", "value": 31000.0},
            {"date": "2024-07-03 00:00:00", "value": 0.0},       # zero — skip
            {"date": "2024-07-04 00:00:00", "value": None},       # null — skip
            {"date": "2024-07-05 00:00:00", "value": 18500.0},
        ],
    }
}


def test_fetch_daily_energy_wh_to_kwh_conversion():
    """Wh values are divided by 1000 to get kWh."""
    with patch("httpx.get", return_value=_mock_response(200, ENERGY_FIXTURE)):
        results = fetch_daily_energy("key", 12345, date(2024, 7, 1), date(2024, 7, 5))

    assert len(results) == 3  # 0 and None entries skipped
    kwh_values = {r["day"]: r["kwh"] for r in results}
    assert kwh_values[date(2024, 7, 1)] == pytest.approx(25.720)
    assert kwh_values[date(2024, 7, 2)] == pytest.approx(31.000)
    assert kwh_values[date(2024, 7, 5)] == pytest.approx(18.500)


def test_fetch_daily_energy_source_tag():
    """All returned entries carry source='solaredge'."""
    with patch("httpx.get", return_value=_mock_response(200, ENERGY_FIXTURE)):
        results = fetch_daily_energy("key", 12345, date(2024, 7, 1), date(2024, 7, 5))
    assert all(r["source"] == "solaredge" for r in results)


def test_fetch_daily_energy_zero_days_skipped():
    """Days with 0 or null energy are excluded from results."""
    with patch("httpx.get", return_value=_mock_response(200, ENERGY_FIXTURE)):
        results = fetch_daily_energy("key", 12345, date(2024, 7, 1), date(2024, 7, 5))
    days = {r["day"] for r in results}
    assert date(2024, 7, 3) not in days  # zero
    assert date(2024, 7, 4) not in days  # null


def test_fetch_daily_energy_empty_response():
    """No values in response → empty list (not an error)."""
    fixture = {"energy": {"timeUnit": "DAY", "unit": "Wh", "values": []}}
    with patch("httpx.get", return_value=_mock_response(200, fixture)):
        results = fetch_daily_energy("key", 12345, date(2024, 7, 1), date(2024, 7, 5))
    assert results == []


def test_fetch_daily_energy_401_raises_auth_error():
    """401 response raises SolarEdgeAuthError."""
    resp = MagicMock()
    resp.status_code = 401
    resp.is_success = False
    with patch("httpx.get", return_value=resp):
        with pytest.raises(SolarEdgeAuthError):
            fetch_daily_energy("bad_key", 12345, date(2024, 7, 1), date(2024, 7, 5))


def test_fetch_daily_energy_403_raises_auth_error():
    """403 response raises SolarEdgeAuthError."""
    resp = MagicMock()
    resp.status_code = 403
    resp.is_success = False
    with patch("httpx.get", return_value=resp):
        with pytest.raises(SolarEdgeAuthError):
            fetch_daily_energy("bad_key", 12345, date(2024, 7, 1), date(2024, 7, 5))


def test_fetch_daily_energy_500_raises_solaredge_error():
    """500 response raises SolarEdgeError (not auth-specific)."""
    resp = MagicMock()
    resp.status_code = 500
    resp.is_success = False
    resp.text = "Internal Server Error"
    with patch("httpx.get", return_value=resp):
        with pytest.raises(SolarEdgeError):
            fetch_daily_energy("key", 12345, date(2024, 7, 1), date(2024, 7, 5))


# ── list_sites ────────────────────────────────────────────────────────────────

SITES_FIXTURE = {
    "sites": {
        "count": 2,
        "site": [
            {
                "id": 1001,
                "name": "Northfield Solar",
                "peakPower": 125.4,
                "location": {"address": "123 Main St", "city": "Northfield", "state": "VT"},
            },
            {
                "id": 1002,
                "name": "Chester Array",
                "peakPower": 250.0,
                "location": {"address": "456 Oak Rd", "city": "Chester", "state": "VT"},
            },
        ],
    }
}


def test_list_sites_returns_proper_shape():
    """list_sites returns list of dicts with expected keys."""
    with patch("httpx.get", return_value=_mock_response(200, SITES_FIXTURE)):
        sites = list_sites("account_key")

    assert len(sites) == 2
    site = sites[0]
    assert site["site_id"] == 1001
    assert site["name"] == "Northfield Solar"
    assert site["peak_kw"] == pytest.approx(125.4)
    assert "Northfield" in site["address"]


def test_list_sites_401_returns_empty_list():
    """401 for list_sites → [] (site-level key, caller provides explicit site_id)."""
    resp = MagicMock()
    resp.status_code = 401
    resp.is_success = False
    with patch("httpx.get", return_value=resp):
        result = list_sites("site_level_key")
    assert result == []


def test_list_sites_single_dict_normalized():
    """API may return a single site dict instead of a list — normalised to list."""
    single = {
        "sites": {
            "count": 1,
            "site": {
                "id": 999,
                "name": "Solo Array",
                "peakPower": 50.0,
                "location": {"address": "1 Farm Rd", "city": "Stowe", "state": "VT"},
            },
        }
    }
    with patch("httpx.get", return_value=_mock_response(200, single)):
        sites = list_sites("key")
    assert len(sites) == 1
    assert sites[0]["site_id"] == 999


# ── site_details ──────────────────────────────────────────────────────────────

DETAILS_FIXTURE = {
    "details": {
        "id": 12345,
        "name": "Starlake Farm",
        "peakPower": 312.5,
        "status": "Active",
        "location": {"address": "789 Lake Rd", "city": "Stowe", "state": "VT"},
    }
}


def test_site_details_returns_expected_fields():
    """site_details returns site_id, name, peak_kw, address, status."""
    with patch("httpx.get", return_value=_mock_response(200, DETAILS_FIXTURE)):
        d = site_details("key", 12345)

    assert d["site_id"] == 12345
    assert d["name"] == "Starlake Farm"
    assert d["peak_kw"] == pytest.approx(312.5)
    assert d["status"] == "Active"
    assert "Stowe" in d["address"]


def test_site_details_401_raises_auth_error():
    """401 on site_details raises SolarEdgeAuthError with informative message."""
    resp = MagicMock()
    resp.status_code = 401
    resp.is_success = False
    with patch("httpx.get", return_value=resp):
        with pytest.raises(SolarEdgeAuthError):
            site_details("bad_key", 99999)
