"""C9 — clearest-day spotlight data honesty (Bruce, 2026-07-03).

Bruce's Analysis tab showed "Timberworks 150kW made 180 kWh vs 930 kWh
expected — 19%" for 2026-06-29, while GMP metered 1,282.2 kWh returned and
SMA's own report said 1,275.87 kWh. Prod forensics: the array-day
DailyGeneration row held ONE inverter's series (179.59 kWh) while the same
array's 7 InverterDaily rows for that day summed to 1,111.4 kWh — the
forecast "actual" reader trusted the provably-partial array row.

These tests pin the cross-check in api.array_owners._clean_actual_by_day:
  * array row contradicted by a COMPLETE sibling inverter sum → sum wins;
  * streams agree → the larger (most-complete) reading wins;
  * contradicted by a PARTIAL sum → the day is dropped, never shown as a
    false catastrophic deficit;
  * estimate sources (bill_prorate/utility_meter) stay excluded everywhere.

Fixture numbers mirror the real Timberworks prod rows (array 2026,
ten_2274f94eac1050b9) so the regression stays recognizable.
"""
import os, tempfile
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))

import secrets
from datetime import date

from api.db import SessionLocal, init_db
from api.models import Array, DailyGeneration, Inverter, InverterDaily, Tenant
from api import array_owners as AO


def setup_module(m):
    init_db()


# The real per-inverter kWh for Timberworks 2026-06-29 as stored in prod
# InverterDaily (sum = 1111.44); the array-day row wrongly held only the
# first inverter's 179.59.
_TIMBERWORKS_29TH = [179.59, 178.10, 177.82, 110.80, 172.96, 114.02, 178.15]


def _mk_fleet(db, n_inverters=7):
    t = Tenant(id="ten_" + secrets.token_hex(6), name="C9 Cross-check",
               contact_email="c9@example.com",
               tenant_key="sol_live_" + secrets.token_hex(6),
               product="array_operator", active=True)
    db.add(t)
    db.flush()
    arr = Array(tenant_id=t.id, name="Timberworks 150kW")
    db.add(arr)
    db.flush()
    invs = []
    for i in range(n_inverters):
        iv = Inverter(tenant_id=t.id, array_id=arr.id, position=i + 1,
                      vendor="sma", serial=f"19124{i:04d}", nameplate_kw=24.0)
        db.add(iv)
        invs.append(iv)
    db.flush()
    return t, arr, invs


def _dg(db, arr, day, kwh, source="extension_pull"):
    db.add(DailyGeneration(tenant_id=arr.tenant_id, array_id=arr.id,
                           day=day, kwh=kwh, source=source))


def _idaily(db, iv, day, kwh, source="extension_pull"):
    db.add(InverterDaily(tenant_id=iv.tenant_id, inverter_id=iv.id,
                         day=day, kwh=kwh, source=source))


def test_single_inverter_array_row_replaced_by_complete_inverter_sum():
    """THE Bruce bug: array-day = one inverter's kWh, all 7 inverters reported
    → the complete sibling sum (1111.44) must replace the 179.59."""
    day = date(2026, 6, 29)
    with SessionLocal() as db:
        t, arr, invs = _mk_fleet(db)
        _dg(db, arr, day, 179.59)                       # the poisoned array row
        for iv, kwh in zip(invs, _TIMBERWORKS_29TH):    # full 7/7 coverage
            _idaily(db, iv, day, kwh)
        db.commit()
        out = AO._clean_actual_by_day(db, arr, day, day)
    assert out == {day.isoformat(): round(sum(_TIMBERWORKS_29TH), 10)}
    assert abs(out[day.isoformat()] - 1111.44) < 0.01   # never 179.59 again


def test_agreeing_streams_keep_the_larger_reading():
    """DG 1270.71 vs inverter sum 1275.87 (within 15%) → most-complete wins."""
    day = date(2026, 6, 29)
    with SessionLocal() as db:
        t, arr, invs = _mk_fleet(db)
        _dg(db, arr, day, 1270.71)
        per_inv = [205.69, 204.37, 203.93, 127.42, 199.59, 130.33, 204.54]
        for iv, kwh in zip(invs, per_inv):
            _idaily(db, iv, day, kwh)
        db.commit()
        out = AO._clean_actual_by_day(db, arr, day, day)
    assert abs(out[day.isoformat()] - 1275.87) < 0.01


def test_partial_inverter_sum_contradiction_drops_the_day():
    """Array row contradicted but only 3/7 inverters reported: the sum is a
    lower bound, not a truth — the day must vanish from the comparison rather
    than feed a knowingly-wrong number to the spotlight / energy-at-risk."""
    day = date(2026, 6, 29)
    other = date(2026, 6, 28)
    with SessionLocal() as db:
        t, arr, invs = _mk_fleet(db)
        _dg(db, arr, day, 100.0)                        # contradicted
        _dg(db, arr, other, 1111.44)                    # healthy sibling day
        for iv, kwh in zip(invs[:3], [205.69, 204.37, 203.93]):
            _idaily(db, iv, day, kwh)                   # partial 3/7, sum 613.99
        db.commit()
        out = AO._clean_actual_by_day(db, arr, min(other, day), max(other, day))
    assert day.isoformat() not in out                   # dropped, not falsified
    assert out[other.isoformat()] == 1111.44            # untouched sibling day


def test_array_row_larger_than_partial_sum_is_kept():
    """Partial inverter capture (sum below the array row) is normal — the
    array-level reading stays authoritative."""
    day = date(2026, 6, 29)
    with SessionLocal() as db:
        t, arr, invs = _mk_fleet(db)
        _dg(db, arr, day, 1275.87)
        for iv, kwh in zip(invs[:2], [205.69, 204.37]):
            _idaily(db, iv, day, kwh)
        db.commit()
        out = AO._clean_actual_by_day(db, arr, day, day)
    assert out[day.isoformat()] == 1275.87


def test_day_without_inverter_siblings_unchanged():
    day = date(2026, 6, 29)
    with SessionLocal() as db:
        t, arr, invs = _mk_fleet(db)
        _dg(db, arr, day, 927.57)
        db.commit()
        out = AO._clean_actual_by_day(db, arr, day, day)
    assert out == {day.isoformat(): 927.57}


def test_estimate_sources_stay_excluded_everywhere():
    """bill_prorate array rows never count as actual; estimate-source
    InverterDaily rows neither raise the sum nor count toward coverage."""
    day = date(2026, 6, 29)
    with SessionLocal() as db:
        t, arr, invs = _mk_fleet(db)
        _dg(db, arr, day, 612.0, source="bill_prorate")   # smear — excluded
        for iv in invs:
            _idaily(db, iv, day, 500.0, source="bill_prorate")
        db.commit()
        out = AO._clean_actual_by_day(db, arr, day, day)
    assert out == {}


def test_deleted_inverters_do_not_count_toward_coverage_or_sum():
    """A soft-deleted inverter's rows must not poison the cross-check."""
    day = date(2026, 6, 29)
    with SessionLocal() as db:
        t, arr, invs = _mk_fleet(db)
        from api.models import now as _now
        invs[0].deleted_at = _now()
        _dg(db, arr, day, 179.59)
        # remaining 6 live inverters all report → complete live coverage
        for iv, kwh in zip(invs[1:], _TIMBERWORKS_29TH[1:]):
            _idaily(db, iv, day, kwh)
        db.commit()
        out = AO._clean_actual_by_day(db, arr, day, day)
    expected = sum(_TIMBERWORKS_29TH[1:])
    assert abs(out[day.isoformat()] - expected) < 0.01
