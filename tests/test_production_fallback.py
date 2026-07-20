"""Vendor-offline production continuity (utility fallback).

Cases:
  1. Vendor alive (recent positive vendor day) → utility must NOT overwrite
  2. Vendor dead + utility present → gap-fill zeros + production_fallback active
  3. Vendor reconnects (new positive day) → vendor wins again, flag off
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, DailyGeneration, Tenant, now
from api import production_fallback as pf


def _tid() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="PF Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _array(tid: str, name: str = "West Glover") -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name=name, fuel_type="solar")
        db.add(arr)
        db.commit()
        return arr.id


def _add_day(tid: str, aid: int, day: date, kwh: float, source: str) -> None:
    with SessionLocal() as db:
        db.add(DailyGeneration(
            tenant_id=tid, array_id=aid, day=day, kwh=kwh,
            source=source, uploaded_at=now(),
        ))
        db.commit()


def test_vendor_alive_no_overwrite():
    """Recent positive Chint day keeps the feed alive — utility cannot gap-fill."""
    tid, _ = _tid()
    aid = _array(tid)
    today = date(2026, 7, 20)
    # Positive vendor production yesterday → alive
    _add_day(tid, aid, today - timedelta(days=1), 120.0, "extension_pull")
    # A zero vendor day today (mid-day blank) should still NOT be gap-filled
    # because feed is alive from yesterday's positive.
    _add_day(tid, aid, today, 0.0, "extension_pull")

    with SessionLocal() as db:
        dead, _ = pf.vendor_feed_is_dead(db, aid, today=today)
        assert dead is False

        action = pf.apply_utility_day(
            db, tenant_id=tid, array_id=aid, day=today,
            utility_kwh=350.0, utility_source="utility_meter", today=today,
        )
        assert action == "skipped"
        row = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == aid, DailyGeneration.day == today)
        ).scalar_one()
        assert row.source == "extension_pull"
        assert float(row.kwh) == 0.0

        fb = pf.compute_production_fallback(db, aid, today=today)
        assert fb["active"] is False


def test_vendor_dead_utility_gap_fills_and_flags():
    """No positive vendor day in the window + zero vendor rows + utility → fill."""
    tid, _ = _tid()
    aid = _array(tid)
    today = date(2026, 7, 20)
    # Old positive vendor day well outside the dead window
    _add_day(tid, aid, today - timedelta(days=10), 400.0, "chint")
    # Recent stale zeros from a broken portal
    for i in range(3):
        _add_day(tid, aid, today - timedelta(days=i), 0.0, "extension_pull")

    with SessionLocal() as db:
        dead, last = pf.vendor_feed_is_dead(db, aid, today=today)
        assert dead is True
        assert last == today  # zeros still update last_any

        # Gap-fill today's zero with utility
        action = pf.apply_utility_day(
            db, tenant_id=tid, array_id=aid, day=today,
            utility_kwh=368.5, utility_source="utility_meter", today=today,
        )
        assert action == "gap_filled"
        row = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == aid, DailyGeneration.day == today)
        ).scalar_one()
        assert row.source == "utility_meter"  # never relabeled as chint
        assert abs(float(row.kwh) - 368.5) < 0.01

        # Also write a couple utility-only history days for the flag
        for i in (1, 2):
            d = today - timedelta(days=i)
            pf.apply_utility_day(
                db, tenant_id=tid, array_id=aid, day=d,
                utility_kwh=300.0 + i, utility_source="utility_meter", today=today,
            )
        db.commit()

        fb = pf.compute_production_fallback(db, aid, today=today)
        assert fb["active"] is True
        assert fb["source"] == "utility_meter"
        assert fb["days_filled"] >= 1
        assert fb["vendor_last_day"] is not None


def test_vendor_reconnect_wins_again():
    """After gap-fill, a new positive vendor day re-kills the fallback flag and
    is never overwritten by a later utility capture."""
    tid, _ = _tid()
    aid = _array(tid)
    today = date(2026, 7, 20)

    # Dead feed + gap-filled day
    _add_day(tid, aid, today - timedelta(days=5), 0.0, "extension_pull")
    with SessionLocal() as db:
        pf.apply_utility_day(
            db, tenant_id=tid, array_id=aid,
            day=today - timedelta(days=1),
            utility_kwh=200.0, utility_source="utility_meter", today=today - timedelta(days=1),
        )
        db.commit()

    # Vendor reconnects with real production today
    _add_day(tid, aid, today, 410.0, "extension_pull")

    with SessionLocal() as db:
        dead, _ = pf.vendor_feed_is_dead(db, aid, today=today)
        assert dead is False

        # Utility must not clobber the reconnected vendor day
        action = pf.apply_utility_day(
            db, tenant_id=tid, array_id=aid, day=today,
            utility_kwh=999.0, utility_source="utility_meter", today=today,
        )
        assert action == "skipped"
        row = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == aid, DailyGeneration.day == today)
        ).scalar_one()
        assert row.source == "extension_pull"
        assert abs(float(row.kwh) - 410.0) < 0.01

        fb = pf.compute_production_fallback(db, aid, today=today)
        # Active requires days where utility stands in; with vendor positive
        # today and feed alive, flag should be off.
        assert fb["active"] is False


def test_missing_day_inserts_utility():
    """No existing row → plain insert (gap fill not required)."""
    tid, _ = _tid()
    aid = _array(tid)
    today = date(2026, 7, 20)
    with SessionLocal() as db:
        action = pf.apply_utility_day(
            db, tenant_id=tid, array_id=aid, day=today,
            utility_kwh=50.0, utility_source="utility_meter", today=today,
        )
        db.commit()
        assert action == "inserted"
        row = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == aid, DailyGeneration.day == today)
        ).scalar_one()
        assert row.source == "utility_meter"


def test_capture_endpoint_gap_fills_when_vendor_dead(client):
    """End-to-end: utility-meter-capture gap-fills a dead Chint zero day."""
    tid, key = _tid()
    aid = _array(tid, name="Roaring Brook")
    today = date(2026, 7, 18)
    # Stale zeros only (vendor dead)
    for i in range(4):
        _add_day(tid, aid, today - timedelta(days=i), 0.0, "extension_pull")

    resp = client.post(
        "/v1/array-owners/utility-meter-capture",
        json={
            "provider": "vec",
            "accounts": [{
                "account_number": "6578300",
                "nickname": "Roaring Brook",
                "summary": {},
                "daily": [
                    {"date": today.isoformat(), "generated_kwh": 368.0},
                    {"date": (today - timedelta(days=1)).isoformat(),
                     "generated_kwh": 340.0},
                ],
            }],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        rows = {
            r.day: r for r in db.execute(
                select(DailyGeneration).where(DailyGeneration.array_id == aid)
            ).scalars().all()
        }
        assert rows[today].source == "utility_meter"
        assert abs(float(rows[today].kwh) - 368.0) < 0.01
        assert rows[today - timedelta(days=1)].source == "utility_meter"

        fb = pf.compute_production_fallback(db, aid, today=today)
        assert fb["active"] is True
        assert fb["days_filled"] >= 1
