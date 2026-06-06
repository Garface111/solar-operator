"""
Shared kWh attribution helpers for bill-level data.

distribute_kwh_by_calendar_day is the canonical pro-rate function used by
both the GMCS writer and the monthly-production aggregation in account.py.
Phase 2 will replace this with per-day data from GMP CSV downloads once the
DailyGeneration table is populated.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Bill


def distribute_kwh_by_calendar_day(bill: "Bill") -> dict[tuple[int, int], float]:
    """Split a bill's kWh_generated across the calendar months its period
    spans, weighted by days. Returns {(year, month): kwh_for_that_month}.

    Why uniform pro-rate: GMP bills are ~30-day cycles starting mid-month
    (e.g., 2025-04-11 → 2025-05-12). Attributing 100% of the kWh to
    period_start month over-counts April and zeroes-out May — which is
    visibly wrong when cross-checked against the operator's source-of-
    truth workbook. Uniform pro-rate is the best approximation we can do
    without daily-generation data (Phase 2 will add a DailyGeneration
    table from GMP CSV downloads to replace this).

    Falls back to period_start month → 100% kWh if any of period_start,
    period_end, or kwh_generated are missing.
    """
    if bill.kwh_generated is None or bill.kwh_generated <= 0:
        return {}

    def _to_date(d):
        if d is None:
            return None
        if isinstance(d, datetime):
            return d.date()
        return d

    start = _to_date(bill.period_start)
    end = _to_date(bill.period_end)
    bill_dt = _to_date(bill.bill_date)

    # Fallbacks for partial data — preserve old behavior so no regression
    # on bills that were ingested without period info.
    if start is None and end is None:
        if bill_dt is None:
            return {}
        return {(bill_dt.year, bill_dt.month): float(bill.kwh_generated)}
    if end is None:
        return {(start.year, start.month): float(bill.kwh_generated)}
    if start is None:
        return {(end.year, end.month): float(bill.kwh_generated)}

    # Both ends present — pro-rate by day. Inclusive on both ends so a
    # 30-day cycle is counted as 30 days.
    total_days = (end - start).days + 1
    if total_days <= 0:
        return {(start.year, start.month): float(bill.kwh_generated)}

    per_day = float(bill.kwh_generated) / total_days
    buckets: dict[tuple[int, int], float] = {}
    cur = start
    while cur <= end:
        key = (cur.year, cur.month)
        buckets[key] = buckets.get(key, 0.0) + per_day
        cur += timedelta(days=1)
    return buckets
