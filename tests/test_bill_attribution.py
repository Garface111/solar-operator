"""
Unit tests for api/bill_attribution.distribute_kwh_by_calendar_day.

These tests FAIL on main (where _bill_target_month assigns 100% to one month)
and PASS on this branch after the pro-rate fix.

All tests use a plain mock Bill — no DB needed.
"""
from __future__ import annotations

from datetime import datetime, date
from types import SimpleNamespace

import pytest

from api.bill_attribution import distribute_kwh_by_calendar_day


def _bill(*, period_start=None, period_end=None, bill_date=None, kwh_generated):
    """Build a minimal Bill-like object with only the fields the helper reads."""
    return SimpleNamespace(
        period_start=period_start,
        period_end=period_end,
        bill_date=bill_date,
        kwh_generated=kwh_generated,
    )


# ── single-month bills ────────────────────────────────────────────────────────

def test_full_month_bill_attributes_entirely_to_one_month():
    """April 1–30: all 30 kWh go to (2025, 4)."""
    b = _bill(
        period_start=date(2025, 4, 1),
        period_end=date(2025, 4, 30),
        kwh_generated=30000,
    )
    result = distribute_kwh_by_calendar_day(b)
    assert list(result.keys()) == [(2025, 4)]
    assert abs(result[(2025, 4)] - 30000.0) < 0.01


# ── cross-month bills ─────────────────────────────────────────────────────────

def test_cross_month_bill_splits_proportionally():
    """April 11 – May 12: April=20 days, May=12 days, total=32.
    kwh=30000 → April≈18750, May≈11250."""
    b = _bill(
        period_start=date(2025, 4, 11),
        period_end=date(2025, 5, 12),
        kwh_generated=30000,
    )
    result = distribute_kwh_by_calendar_day(b)
    assert set(result.keys()) == {(2025, 4), (2025, 5)}
    expected_apr = 30000 * 20 / 32
    expected_may = 30000 * 12 / 32
    assert abs(result[(2025, 4)] - expected_apr) < 0.01
    assert abs(result[(2025, 5)] - expected_may) < 0.01


def test_chester_july_2024_real_bill():
    """Bill 2024-06-13 → 2024-07-11, kwh=28800.
    Days: 29 total. June=18, July=11."""
    b = _bill(
        period_start=date(2024, 6, 13),
        period_end=date(2024, 7, 11),
        kwh_generated=28800,
    )
    result = distribute_kwh_by_calendar_day(b)
    assert set(result.keys()) == {(2024, 6), (2024, 7)}
    expected_jun = 28800 * 18 / 29
    expected_jul = 28800 * 11 / 29
    assert abs(result[(2024, 6)] - expected_jun) < 0.01
    assert abs(result[(2024, 7)] - expected_jul) < 0.01
    assert abs(result[(2024, 6)] + result[(2024, 7)] - 28800.0) < 0.01


# ── fallback paths ────────────────────────────────────────────────────────────

def test_missing_period_end_falls_back_to_period_start_month():
    """No period_end → 100% goes to period_start month."""
    b = _bill(
        period_start=date(2025, 3, 15),
        period_end=None,
        kwh_generated=10000,
    )
    result = distribute_kwh_by_calendar_day(b)
    assert result == {(2025, 3): 10000.0}


def test_missing_both_falls_back_to_bill_date():
    """No period_start or period_end → 100% goes to bill_date month."""
    b = _bill(
        period_start=None,
        period_end=None,
        bill_date=date(2025, 3, 15),
        kwh_generated=5000,
    )
    result = distribute_kwh_by_calendar_day(b)
    assert result == {(2025, 3): 5000.0}


def test_missing_both_and_no_bill_date_returns_empty():
    """No period info at all → empty dict."""
    b = _bill(period_start=None, period_end=None, bill_date=None, kwh_generated=5000)
    result = distribute_kwh_by_calendar_day(b)
    assert result == {}


# ── zero / None kWh ───────────────────────────────────────────────────────────

def test_zero_kwh_returns_empty():
    b = _bill(
        period_start=date(2025, 4, 1),
        period_end=date(2025, 4, 30),
        kwh_generated=0,
    )
    assert distribute_kwh_by_calendar_day(b) == {}


def test_none_kwh_returns_empty():
    b = _bill(
        period_start=date(2025, 4, 1),
        period_end=date(2025, 4, 30),
        kwh_generated=None,
    )
    assert distribute_kwh_by_calendar_day(b) == {}


# ── conservation: sum of buckets == kwh_generated ────────────────────────────

def test_total_kwh_preserved_across_buckets():
    """For any cross-month bill, sum of bucket values == kwh_generated."""
    b = _bill(
        period_start=date(2025, 4, 11),
        period_end=date(2025, 5, 12),
        kwh_generated=24960,
    )
    result = distribute_kwh_by_calendar_day(b)
    total = sum(result.values())
    assert abs(total - 24960.0) < 0.01


# ── datetime inputs (Bill stores datetime, not date) ─────────────────────────

def test_accepts_datetime_period_inputs():
    """Bill model stores DateTime columns — helper must handle them."""
    b = _bill(
        period_start=datetime(2025, 4, 11, 0, 0),
        period_end=datetime(2025, 5, 12, 0, 0),
        kwh_generated=32000,
    )
    result = distribute_kwh_by_calendar_day(b)
    assert set(result.keys()) == {(2025, 4), (2025, 5)}
    assert abs(sum(result.values()) - 32000.0) < 0.01
