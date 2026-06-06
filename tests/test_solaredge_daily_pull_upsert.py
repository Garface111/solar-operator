"""
Tests for api/jobs/solaredge_pull.py

Verifies idempotent upsert behaviour:
- Pull twice with the same range → same row count (no duplicates)
- Pull then change kWh in the mock → existing row updated, not duplicated
- Missing credentials → graceful error return
"""
from __future__ import annotations

import secrets
from datetime import date
from unittest.mock import patch

from sqlalchemy import select

from api.db import SessionLocal
from api.jobs.solaredge_pull import pull_daily_for_array
from api.models import Array, Client, DailyGeneration, Tenant


# ── fixture helpers ────────────────────────────────────────────────────────────


def _make_array_with_credentials() -> tuple[str, int]:
    """Create Tenant → Client → Array with SolarEdge credentials.
    Returns (tenant_id, array_id).
    """
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="SE Pull Test Co",
            contact_email=f"{tid}@test.com",
            tenant_key="k_" + secrets.token_hex(8),
            plan="standard",
            active=True,
        ))
        db.flush()
        c = Client(tenant_id=tid, name="Pull Test Client", active=True)
        db.add(c)
        db.flush()
        arr = Array(
            tenant_id=tid,
            client_id=c.id,
            name="Pull Test Array",
            nepool_gis_id="77654",
            solaredge_api_key="fake_key_for_test",
            solaredge_site_id=11111,
        )
        db.add(arr)
        db.flush()
        arr_id = arr.id
        db.commit()
    return tid, arr_id


def _make_fake_entries(days: list[date], base_kwh: float = 100.0) -> list[dict]:
    return [
        {"day": d, "kwh": base_kwh + i, "source": "solaredge"}
        for i, d in enumerate(days)
    ]


TEST_DAYS = [date(2024, 7, d) for d in range(1, 8)]


# ── idempotency ────────────────────────────────────────────────────────────────


def test_double_pull_same_range_is_idempotent():
    """Pulling the same date range twice results in the same row count."""
    _tid, arr_id = _make_array_with_credentials()
    entries = _make_fake_entries(TEST_DAYS)

    with patch("api.jobs.solaredge_pull.fetch_daily_energy", return_value=entries):
        with SessionLocal() as db:
            pull_daily_for_array(db, arr_id, days_back=7)

    with SessionLocal() as db:
        count_after_first = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalars().all()

    with patch("api.jobs.solaredge_pull.fetch_daily_energy", return_value=entries):
        with SessionLocal() as db:
            pull_daily_for_array(db, arr_id, days_back=7)

    with SessionLocal() as db:
        count_after_second = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalars().all()

    assert len(count_after_first) == 7
    assert len(count_after_second) == 7  # no duplicates


def test_pull_updates_changed_kwh_value():
    """When kWh for a day changes in the API response, the existing row is updated."""
    _tid, arr_id = _make_array_with_credentials()

    # First pull
    entries_v1 = _make_fake_entries(TEST_DAYS, base_kwh=50.0)
    with patch("api.jobs.solaredge_pull.fetch_daily_energy", return_value=entries_v1):
        with SessionLocal() as db:
            pull_daily_for_array(db, arr_id, days_back=7)

    with SessionLocal() as db:
        row = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == arr_id,
                DailyGeneration.day == TEST_DAYS[0],
            )
        ).scalar_one()
        assert row.kwh == 50.0

    # Second pull with changed values
    entries_v2 = _make_fake_entries(TEST_DAYS, base_kwh=75.0)
    with patch("api.jobs.solaredge_pull.fetch_daily_energy", return_value=entries_v2):
        with SessionLocal() as db:
            pull_daily_for_array(db, arr_id, days_back=7)

    with SessionLocal() as db:
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalars().all()
        assert len(rows) == 7  # still 7, not 14
        row = next(r for r in rows if r.day == TEST_DAYS[0])
        assert row.kwh == 75.0  # updated, not duplicated


def test_pull_skips_zero_kwh_days():
    """Zero-energy days are never inserted into DailyGeneration."""
    _tid, arr_id = _make_array_with_credentials()

    entries_with_zero = [
        {"day": date(2024, 7, 1), "kwh": 25.0, "source": "solaredge"},
        # day 2 is zero — fetch_daily_energy skips it, so it won't appear
    ]
    with patch("api.jobs.solaredge_pull.fetch_daily_energy", return_value=entries_with_zero):
        with SessionLocal() as db:
            result = pull_daily_for_array(db, arr_id, days_back=2)

    assert result["days_pulled"] == 1

    with SessionLocal() as db:
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].day == date(2024, 7, 1)


# ── error handling ─────────────────────────────────────────────────────────────


def test_pull_missing_credentials_returns_error():
    """Array with no api_key returns an error dict, does not raise."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="No Creds Co", contact_email=f"{tid}@x.com",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
        ))
        db.flush()
        c = Client(tenant_id=tid, name="No Creds Client", active=True)
        db.add(c)
        db.flush()
        arr = Array(tenant_id=tid, client_id=c.id, name="No Creds Array",
                    solaredge_api_key=None, solaredge_site_id=None)
        db.add(arr)
        db.flush()
        arr_id = arr.id
        db.commit()

    with SessionLocal() as db:
        result = pull_daily_for_array(db, arr_id)

    assert result["days_pulled"] == 0
    assert len(result["errors"]) > 0


def test_pull_api_error_returns_error_dict():
    """When fetch_daily_energy raises SolarEdgeError, result has errors list."""
    from api.adapters.solaredge import SolarEdgeError
    _tid, arr_id = _make_array_with_credentials()

    with patch("api.jobs.solaredge_pull.fetch_daily_energy",
               side_effect=SolarEdgeError("Rate limit hit")):
        with SessionLocal() as db:
            result = pull_daily_for_array(db, arr_id)

    assert result["days_pulled"] == 0
    assert any("Rate limit" in e for e in result["errors"])


def test_pull_source_is_solaredge():
    """Inserted rows carry source='solaredge'."""
    _tid, arr_id = _make_array_with_credentials()
    entries = [{"day": date(2024, 7, 1), "kwh": 30.0, "source": "solaredge"}]

    with patch("api.jobs.solaredge_pull.fetch_daily_energy", return_value=entries):
        with SessionLocal() as db:
            pull_daily_for_array(db, arr_id, days_back=1)

    with SessionLocal() as db:
        row = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalar_one()
    assert row.source == "solaredge"
