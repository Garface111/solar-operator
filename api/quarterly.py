"""Quarterly bill readiness computation for client report pipeline.

For each non-excluded, non-deleted array under a client, checks whether all
three calendar months of the current quarter have at least one bill whose
billing period overlaps that month (via period_start / period_end). Bill
offset (bill_offset_months) does not factor in here — we use the actual
billing period, not the bill_date, so GMP (offset=1) and SmartHub (offset=0)
arrays are treated identically.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select

from .models import Array, Bill, UtilityAccount


def _quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def _quarter_months(year: int, q: int) -> list[tuple[int, int]]:
    start_month = (q - 1) * 3 + 1
    return [(year, start_month + i) for i in range(3)]


def _quarter_label(year: int, q: int) -> str:
    return f"Q{q}-{year}"


def _quarter_start(year: int, q: int) -> date:
    return date(year, (q - 1) * 3 + 1, 1)


def _quarter_end(year: int, q: int) -> date:
    end_month = q * 3
    _, last = calendar.monthrange(year, end_month)
    return date(year, end_month, last)


def _to_date(d) -> Optional[date]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    return d


def _bill_covers_month(bill: "Bill", year: int, month: int) -> bool:
    """Return True if this bill's period intersects (year, month).

    Uses period_start / period_end when available; falls back to bill_date
    for partial bills ingested without period info.
    """
    start = _to_date(bill.period_start)
    end = _to_date(bill.period_end)
    bill_dt = _to_date(bill.bill_date)

    _, last = calendar.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, last)

    if start is None and end is None:
        if bill_dt is None:
            return False
        return bill_dt.year == year and bill_dt.month == month

    if start is None:
        start = end
    if end is None:
        end = start

    # Overlap: bill period intersects [month_start, month_end]
    return start <= month_end and end >= month_start


def compute_quarterly_progress(
    client_id: int,
    db,
    today: Optional[date] = None,
) -> dict:
    """Compute quarterly bill readiness for all reportable arrays under a client.

    Returns:
      quarter        — e.g. "Q2-2026"
      quarter_start  — ISO date string "2026-04-01"
      quarter_end    — ISO date string "2026-06-30"
      ready_arrays   — [{id, name}] arrays with coverage for all 3 months
      missing_arrays — [{id, name, missing_months: ["2026-06"]}] arrays missing ≥1 month
      total_arrays   — count of reportable arrays (excluded/deleted arrays omitted)
      all_ready      — True only when total_arrays > 0 and every array is ready
    """
    if today is None:
        today = date.today()

    year = today.year
    q = _quarter_of(today.month)
    quarter_months = _quarter_months(year, q)

    arrays = db.execute(
        select(Array).where(
            Array.client_id == client_id,
            Array.deleted_at.is_(None),
            Array.excluded.is_(False),
        )
    ).scalars().all()

    total = len(arrays)
    ready_arrays: list[dict] = []
    missing_arrays: list[dict] = []

    for arr in arrays:
        accounts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.array_id == arr.id,
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all()

        account_ids = [a.id for a in accounts]

        if not account_ids:
            # Array exists but has no linked accounts — all months missing.
            missing_arrays.append({
                "id": arr.id,
                "name": arr.name,
                "missing_months": [f"{y}-{m:02d}" for y, m in quarter_months],
            })
            continue

        bills = db.execute(
            select(Bill).where(
                Bill.account_id.in_(account_ids),
                Bill.kwh_generated.isnot(None),
                Bill.kwh_generated > 0,
            )
        ).scalars().all()

        missing_months = [
            f"{qy}-{qm:02d}"
            for qy, qm in quarter_months
            if not any(_bill_covers_month(b, qy, qm) for b in bills)
        ]

        if not missing_months:
            ready_arrays.append({"id": arr.id, "name": arr.name})
        else:
            missing_arrays.append({
                "id": arr.id,
                "name": arr.name,
                "missing_months": missing_months,
            })

    return {
        "quarter": _quarter_label(year, q),
        "quarter_start": _quarter_start(year, q).isoformat(),
        "quarter_end": _quarter_end(year, q).isoformat(),
        "ready_arrays": ready_arrays,
        "missing_arrays": missing_arrays,
        "total_arrays": total,
        "all_ready": total > 0 and len(ready_arrays) == total,
    }
