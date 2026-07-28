"""get_daily_series_bulk must equal get_daily_series, especially MULTI-METER.

Added 2026-07-27 with the fleet-trends N+1 fix. Live prod validated 164/164
arrays identical across 501,669 (array,day) points — but prod currently has
ZERO multi-meter GMP arrays, so the cross-meter SUM and the array_id regrouping
(the parts most likely to diverge) had no live coverage. These construct that
case explicitly.
"""
from __future__ import annotations

import secrets
from datetime import date

from api.db import SessionLocal
from api.models import (
    Array,
    GmpDailyGeneration,
    Tenant,
    UtilityAccount,
)
from api.reports import gmp_daily_read as g


def _seed(n_meters: int, *, days: int = 3) -> tuple[str, int]:
    """One array fed by n_meters GMP accounts, each with `days` of data."""
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Bulk Co", contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True,
        ))
        arr = Array(tenant_id=tid, name="Multi", fuel_type="solar")
        db.add(arr)
        db.flush()
        for m in range(n_meters):
            acct_no = f"acct-{m}-{secrets.token_hex(3)}"
            ua = UtilityAccount(
                tenant_id=tid, array_id=arr.id, provider="gmp",
                account_number=acct_no,
            )
            db.add(ua)
            db.flush()
            for d in range(days):
                db.add(GmpDailyGeneration(
                    tenant_id=tid, account_id=ua.id, array_id=arr.id,
                    account_number=acct_no,
                    day=date(2026, 5, d + 1),
                    # distinct per meter so a dropped meter changes the sum
                    kwh=10.0 + m + d, interval_count=96, source="gmp_api",
                ))
        aid = arr.id
        db.commit()
    return tid, aid


def test_bulk_matches_single_for_multi_meter_array():
    """THE case with no live coverage: one array, three GMP meters."""
    _tid, aid = _seed(3)
    with SessionLocal() as db:
        single = {p["day"]: p["kwh"] for p in g.get_daily_series(aid, db=db)}
        bulk = g.get_daily_series_bulk([aid], db=db).get(aid, {})
    assert single == bulk
    # Three meters really did sum: day1 = 10 + 11 + 12.
    assert bulk[date(2026, 5, 1)] == 33.0
    assert len(bulk) == 3


def test_bulk_matches_single_for_one_meter_array():
    _tid, aid = _seed(1)
    with SessionLocal() as db:
        single = {p["day"]: p["kwh"] for p in g.get_daily_series(aid, db=db)}
        bulk = g.get_daily_series_bulk([aid], db=db).get(aid, {})
    assert single == bulk and bulk


def test_bulk_keeps_arrays_separate():
    """Regrouping must not smear one array's meters onto another."""
    _t1, a1 = _seed(2)
    _t2, a2 = _seed(3)
    with SessionLocal() as db:
        bulk = g.get_daily_series_bulk([a1, a2], db=db)
        s1 = {p["day"]: p["kwh"] for p in g.get_daily_series(a1, db=db)}
        s2 = {p["day"]: p["kwh"] for p in g.get_daily_series(a2, db=db)}
    assert bulk.get(a1) == s1
    assert bulk.get(a2) == s2
    assert bulk[a1] != bulk[a2]


def test_bulk_omits_arrays_with_no_gmp_accounts():
    """Absent, mirroring get_daily_series returning [] — not a zero-filled entry."""
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="No GMP", contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True,
        ))
        arr = Array(tenant_id=tid, name="Bare", fuel_type="solar")
        db.add(arr)
        db.flush()
        aid = arr.id
        db.commit()
    with SessionLocal() as db:
        assert g.get_daily_series(aid, db=db) == []
        assert aid not in g.get_daily_series_bulk([aid], db=db)


def test_bulk_empty_input():
    with SessionLocal() as db:
        assert g.get_daily_series_bulk([], db=db) == {}
