"""
Tests for api/jobs/smarthub_pull.pull_daily_generation_for_account.

Covers:
  1. Mock the SmartHub pull, call twice with same range → idempotent (same row count)
  2. Pull with no stored session → skips gracefully
  3. Pull with empty generation data → ok, 0 rows
  4. Source is set to "smarthub" in DailyGeneration rows
"""
from __future__ import annotations

import secrets
from datetime import date
from unittest.mock import patch

import pytest

from sqlalchemy import select

from api.db import SessionLocal
from api.jobs.smarthub_pull import pull_daily_generation_for_account
from api.models import Array, Client, DailyGeneration, Tenant, UtilityAccount, UtilitySession


def _make_tenant_with_wec_array() -> tuple[str, int, int]:
    """Returns (tenant_id, array_id, account_id)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="SmartHub Pull Test", contact_email=f"{tid}@test.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.flush()
        c = Client(tenant_id=tid, name="Test Client WEC", active=True)
        db.add(c)
        db.flush()
        arr = Array(tenant_id=tid, client_id=c.id, name="WEC Array Test")
        db.add(arr)
        db.flush()
        acct = UtilityAccount(
            tenant_id=tid, array_id=arr.id,
            provider="wec", account_number="8001234",
            enabled=True,
        )
        db.add(acct)
        # Add a stored session with a fake auth token
        sess = UtilitySession(
            tenant_id=tid, provider="wec",
            api_token="tok_wec_test",
            raw_payload={"user": {"email": "farm@wec.vt", "username": "farm@wec.vt"}},
        )
        db.add(sess)
        db.commit()
        return tid, arr.id, acct.id


def _fake_fetch_account_list(host, session):
    return [{"service_location_number": "SL_WEC_001", "account_number": "8001234", "description": "Hill Farm", "services": ["ELEC"]}]


def _fake_fetch_daily_generation(host, session, service_location, account_number, start, end):
    """Return 30 days of fake 10 kWh/day generation data."""
    from datetime import timedelta
    rows = []
    d = start
    while d <= end:
        rows.append({
            "day": d,
            "kwh_generated": 10.0,
            "kwh_consumed": 2.0,
            "kwh_net_export": 8.0,
        })
        d += timedelta(days=1)
    return rows


# ─── Test 1: Idempotent double-run ────────────────────────────────────────────

def test_pull_daily_generation_idempotent():
    tid, array_id, _ = _make_tenant_with_wec_array()

    with (
        patch("api.jobs.smarthub_pull.fetch_account_list", side_effect=_fake_fetch_account_list),
        patch("api.jobs.smarthub_pull.fetch_daily_generation", side_effect=_fake_fetch_daily_generation),
    ):
        r1 = pull_daily_generation_for_account(None, tid, array_id, days_back=30)
        r2 = pull_daily_generation_for_account(None, tid, array_id, days_back=30)

    assert r1["status"] == "ok"
    assert r2["status"] == "ok"

    # Same range → same row count on both runs
    assert r1["inserted"] + r1["updated"] == r2["inserted"] + r2["updated"]
    # Second run should have 0 inserts (everything already exists)
    assert r2["inserted"] == 0
    assert r2["updated"] > 0

    with SessionLocal() as db:
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == array_id)
        ).scalars().all()

    # Only one row per day — no duplicates
    days_seen = [r.day for r in rows]
    assert len(days_seen) == len(set(days_seen))
    assert len(rows) == 31  # days_back=30 → 31 days inclusive


# ─── Test 2: Source is "smarthub" ─────────────────────────────────────────────

def test_pull_sets_source_smarthub():
    tid, array_id, _ = _make_tenant_with_wec_array()

    with (
        patch("api.jobs.smarthub_pull.fetch_account_list", side_effect=_fake_fetch_account_list),
        patch("api.jobs.smarthub_pull.fetch_daily_generation", side_effect=_fake_fetch_daily_generation),
    ):
        pull_daily_generation_for_account(None, tid, array_id, days_back=5)

    with SessionLocal() as db:
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == array_id)
        ).scalars().all()

    assert all(r.source == "smarthub" for r in rows)


# ─── Test 3: No stored session → skip gracefully ─────────────────────────────

def test_pull_no_session_skips():
    # Create array/account with NO UtilitySession
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="No Session Test", contact_email=f"{tid}@t.t",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
        ))
        db.flush()
        c = Client(tenant_id=tid, name="C", active=True)
        db.add(c); db.flush()
        arr = Array(tenant_id=tid, client_id=c.id, name="Arr")
        db.add(arr); db.flush()
        db.add(UtilityAccount(
            tenant_id=tid, array_id=arr.id,
            provider="wec", account_number="X001", enabled=True,
        ))
        db.commit()
        array_id = arr.id

    result = pull_daily_generation_for_account(None, tid, array_id, days_back=7)
    assert result["status"] == "skipped"
    assert "session" in result["reason"].lower()


# ─── Test 4: Empty generation data → ok, 0 rows ───────────────────────────────

def test_pull_empty_generation_ok():
    tid, array_id, _ = _make_tenant_with_wec_array()

    with (
        patch("api.jobs.smarthub_pull.fetch_account_list", side_effect=_fake_fetch_account_list),
        patch("api.jobs.smarthub_pull.fetch_daily_generation", return_value=[]),
    ):
        result = pull_daily_generation_for_account(None, tid, array_id, days_back=30)

    assert result["status"] == "ok"
    assert result.get("inserted", 0) == 0
    assert result.get("updated", 0) == 0

    with SessionLocal() as db:
        count = len(db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == array_id)
        ).scalars().all())

    assert count == 0


# ─── Test 5: Array not found → graceful skip ──────────────────────────────────

def test_pull_array_not_found():
    result = pull_daily_generation_for_account(None, "ten_doesnotexist", 999999, days_back=30)
    assert result["status"] == "skipped"
