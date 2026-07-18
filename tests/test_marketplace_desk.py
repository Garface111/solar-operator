"""Unit tests for Marketplace expand: exchange match + REC desk (no DB money paths)."""
from __future__ import annotations

from types import SimpleNamespace

from api.exchange_match import parse_desired_kwh_mo, suggest_pairings, lead_dict
from api.rec_desk import OWNERSHIP_VALUES


def test_parse_desired_kwh_mo_shapes():
    assert parse_desired_kwh_mo("~2,000 kWh/mo") == 2000.0
    assert parse_desired_kwh_mo("1500") == 1500.0
    assert parse_desired_kwh_mo("2.5 MWh/mo") == 2500.0
    assert parse_desired_kwh_mo("") is None
    assert parse_desired_kwh_mo("nothing") is None


def test_suggest_pairings_same_utility_and_size():
    vacs = [{
        "array_id": 1,
        "array_name": "Starlake",
        "provider": "gmp",
        "vacancy_frac": 0.25,
        "vacancy_kwh": 12000,
        "vacancy_usd": 1800,
        "pool_kwh": 48000,
        "expiring_soon_kwh": 500,
        "confidence": "high",
    }, {
        "array_id": 2,
        "array_name": "Other",
        "provider": "vec",
        "vacancy_frac": 0.4,
        "vacancy_kwh": 8000,
        "vacancy_usd": 900,
        "pool_kwh": 20000,
        "expiring_soon_kwh": 0,
        "confidence": "medium",
    }]
    leads = [
        SimpleNamespace(
            id=10, contact_name="Ann", contact_email="a@x.com", utility="gmp",
            desired_band="~1000 kWh/mo", status="new",
        ),
        SimpleNamespace(
            id=11, contact_name="Bob", contact_email=None, utility="vec",
            desired_band="500", status="dead",  # excluded
        ),
    ]
    pairs = suggest_pairings(vacancies=vacs, leads=leads)
    assert pairs
    assert all(p["lead_id"] == 10 for p in pairs)
    assert pairs[0]["array_id"] == 1
    assert pairs[0]["suggested_allocation_pct"] is not None
    assert pairs[0]["suggested_allocation_pct"] > 0


def test_lead_dict_includes_parsed_kwh():
    row = SimpleNamespace(
        id=1, contact_name="X", contact_email=None, contact_phone=None,
        utility="gmp", desired_band="2,000 kWh/mo", monthly_bill_usd=120,
        source="operator_waitlist", status="new", notes=None,
        suggested_array_id=5, linked_subscription_id=None, created_at=None,
    )
    d = lead_dict(row)
    assert d["desired_kwh_mo"] == 2000.0
    assert d["suggested_array_id"] == 5


def test_ownership_values_cover_desk():
    assert "owner_retained" in OWNERSHIP_VALUES
    assert "assigned_to_utility" in OWNERSHIP_VALUES
    assert "unknown" in OWNERSHIP_VALUES
