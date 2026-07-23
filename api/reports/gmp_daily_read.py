"""
GMP daily-generation READ INTERFACE  —  the contract the Reports agent consumes.

╔══════════════════════════════════════════════════════════════════════════╗
║  PROVISIONAL CONTRACT (v0).                                                ║
║  Ford is supplying the EXACT table + query-function contract that must be  ║
║  identical in the Reports agent's prompt. Until that lands, these          ║
║  signatures are a best-effort draft built to the data we actually have.    ║
║  The shapes here are STABLE enough to build against; names/return keys may  ║
║  be renamed to match Ford's wording — when they are, this is the ONE file  ║
║  to change and agent-3 re-reads it. Agent-3 MUST NOT query the gmp_* tables ║
║  directly; it goes through these functions so the storage layer stays ours.║
╚══════════════════════════════════════════════════════════════════════════╝

OWNERSHIP
  • This module (api/reports/gmp_daily_read.py) is OWNED by the data-sponge side.
  • The Reports agent (agent-3) is a READ-ONLY consumer: it calls these functions
    and never imports GmpUsageRaw / GmpDailyGeneration or writes to those tables.

DATA MODEL BEHIND THE CONTRACT
  • gmp_daily_generation: one row per (utility account == GMP meter, calendar day),
    kwh = Σ that day's real 15-min interval Quantity. source='gmp_api'.
  • An ARRAY may aggregate several GMP meters (e.g. Starlake = 3 sub-meters), so
    the per-array reads SUM across the array's accounts for each day.
  • gmp_usage_raw: the verbatim CSV sponge (attached to invoices later) — exposed
    read-only via get_raw_windows() for provenance / invoice attachment.

EVERY function is READ-ONLY (SELECT only) and returns plain dicts/lists — no ORM
objects leak across the contract boundary.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import GmpDailyGeneration, GmpUsageRaw, UtilityAccount, Array


# ── helpers ──────────────────────────────────────────────────────────────────

def _account_ids_for_array(db: Session, array_id: int) -> list[int]:
    """The enabled GMP utility-account IDs that feed one array (1..N meters)."""
    return list(db.execute(
        select(UtilityAccount.id).where(
            UtilityAccount.array_id == array_id,
            UtilityAccount.provider == "gmp",
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalars().all())


# ── the READ contract ─────────────────────────────────────────────────────────

def get_daily_series(
    array_id: int,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Optional[Session] = None,
) -> list[dict[str, Any]]:
    """Per-DAY generation for an array, summed across its GMP meters.

    Returns an ascending list of:
        {"day": date, "kwh": float, "meters": int, "intervals": int}
      meters    = how many GMP accounts contributed a row that day
      intervals = total 15-min intervals summed that day (96/meter = full day)

    Inclusive of start/end when given. Empty list if the array has no GMP data.
    """
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        acct_ids = _account_ids_for_array(db, array_id)
        if not acct_ids:
            return []
        q = select(
            GmpDailyGeneration.day,
            func.sum(GmpDailyGeneration.kwh),
            func.count(GmpDailyGeneration.id),
            func.sum(GmpDailyGeneration.interval_count),
        ).where(GmpDailyGeneration.account_id.in_(acct_ids))
        if start:
            q = q.where(GmpDailyGeneration.day >= start)
        if end:
            q = q.where(GmpDailyGeneration.day <= end)
        q = q.group_by(GmpDailyGeneration.day).order_by(GmpDailyGeneration.day)
        return [
            {"day": d, "kwh": round(float(k or 0.0), 4),
             "meters": int(m or 0), "intervals": int(iv or 0)}
            for d, k, m, iv in db.execute(q).all()
        ]
    finally:
        if _own:
            db.close()


def get_monthly_totals(
    array_id: int,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Optional[Session] = None,
) -> list[dict[str, Any]]:
    """Per-(year,month) kWh totals for an array, summed across its GMP meters.

    Returns ascending list of:
        {"year": int, "month": int, "kwh": float, "days": int}
      days = number of distinct calendar days with data in that month (so the
             consumer can tell a full month from a partial one).
    """
    series = get_daily_series(array_id, start=start, end=end, db=db)
    buckets: dict[tuple[int, int], dict[str, Any]] = {}
    for row in series:
        d: date = row["day"]
        key = (d.year, d.month)
        b = buckets.setdefault(key, {"year": d.year, "month": d.month, "kwh": 0.0, "days": 0})
        b["kwh"] += row["kwh"]
        b["days"] += 1
    out = [{"year": y, "month": m, "kwh": round(v["kwh"], 4), "days": v["days"]}
           for (y, m), v in buckets.items()]
    out.sort(key=lambda r: (r["year"], r["month"]))
    return out


def get_coverage(array_id: int, *, db: Optional[Session] = None) -> dict[str, Any]:
    """What GMP daily data we actually hold for an array — the evidence summary
    the Reports agent should trust before rendering.

    Returns:
        {
          "array_id": int,
          "meters": int,             # GMP accounts feeding this array
          "day_count": int,          # distinct days with data
          "first_day": date|None,
          "last_day": date|None,
          "total_kwh": float,
        }
    """
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        acct_ids = _account_ids_for_array(db, array_id)
        if not acct_ids:
            return {"array_id": array_id, "meters": 0, "day_count": 0,
                    "first_day": None, "last_day": None, "total_kwh": 0.0}
        row = db.execute(
            select(
                func.count(func.distinct(GmpDailyGeneration.day)),
                func.min(GmpDailyGeneration.day),
                func.max(GmpDailyGeneration.day),
                func.sum(GmpDailyGeneration.kwh),
            ).where(GmpDailyGeneration.account_id.in_(acct_ids))
        ).one()
        return {
            "array_id": array_id, "meters": len(acct_ids),
            "day_count": int(row[0] or 0),
            "first_day": row[1], "last_day": row[2],
            "total_kwh": round(float(row[3] or 0.0), 4),
        }
    finally:
        if _own:
            db.close()


def get_account_daily_series(
    account_id: int,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Optional[Session] = None,
) -> list[dict[str, Any]]:
    """Per-day generation for ONE GMP meter (account). Same row shape as
    get_daily_series but without the cross-meter sum. Use when a report needs
    per-meter detail (e.g. Starlake's 3 sub-meters shown separately)."""
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        q = select(GmpDailyGeneration.day, GmpDailyGeneration.kwh,
                   GmpDailyGeneration.interval_count).where(
            GmpDailyGeneration.account_id == account_id)
        if start:
            q = q.where(GmpDailyGeneration.day >= start)
        if end:
            q = q.where(GmpDailyGeneration.day <= end)
        q = q.order_by(GmpDailyGeneration.day)
        return [{"day": d, "kwh": round(float(k or 0.0), 4), "intervals": int(iv or 0)}
                for d, k, iv in db.execute(q).all()]
    finally:
        if _own:
            db.close()


def get_hourly_series(
    array_id: int,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Optional[Session] = None,
) -> list[dict[str, Any]]:
    """Per-HOUR generation for an array, summed across its GMP meters.

    Re-derives hourly totals from the raw sponge (GmpUsageRaw CSV windows) —
    the 15-min Quantity rows aggregated into the hour of IntervalStart. Used by
    the generation-reports XLSX detail export.

    Returns ascending list of:
        {"day": date, "hour": int (0-23), "kwh": float,
         "meters": int, "intervals": int}
      intervals = 15-min slots summed into that hour (4 = full hour / meter)

    Empty list when the array has no GMP raw interval data (or no meters).
    """
    from ..adapters import gmp as gmp_adapter  # noqa: PLC0415

    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        acct_ids = _account_ids_for_array(db, array_id)
        if not acct_ids:
            return []

        # Per-account first (window overlap → last write wins for that hour),
        # then sum across meters so multi-meter arrays don't double-count a
        # re-fetched window.
        merged: dict[tuple[date, int], dict[str, Any]] = {}
        for acct_id in acct_ids:
            q = select(GmpUsageRaw).where(
                GmpUsageRaw.account_id == acct_id,
                GmpUsageRaw.http_status == 200,
                GmpUsageRaw.raw_csv.isnot(None),
            )
            # Windows that could cover any day in [start, end].
            if start is not None:
                q = q.where(GmpUsageRaw.window_end >= start)
            if end is not None:
                q = q.where(GmpUsageRaw.window_start <= end)
            q = q.order_by(GmpUsageRaw.window_start, GmpUsageRaw.fetched_at)
            account_hours: dict[tuple[date, int], dict[str, float]] = {}
            for raw in db.execute(q).scalars().all():
                parsed = gmp_adapter.parse_usage_csv_to_hourly(raw.raw_csv or "")
                for (d, h), cell in (parsed.get("by_hour") or {}).items():
                    if start is not None and d < start:
                        continue
                    if end is not None and d > end:
                        continue
                    # Later windows overwrite same hour for this account.
                    account_hours[(d, int(h))] = {
                        "kwh": float(cell.get("kwh") or 0.0),
                        "intervals": int(cell.get("intervals") or 0),
                    }
            for key, cell in account_hours.items():
                slot = merged.setdefault(
                    key, {"kwh": 0.0, "intervals": 0, "meters": 0},
                )
                slot["kwh"] += cell["kwh"]
                slot["intervals"] += cell["intervals"]
                slot["meters"] += 1

        out = [
            {
                "day": d,
                "hour": h,
                "kwh": round(float(v["kwh"]), 4),
                "meters": int(v["meters"]),
                "intervals": int(v["intervals"]),
            }
            for (d, h), v in sorted(merged.items(), key=lambda x: (x[0][0], x[0][1]))
        ]
        return out
    finally:
        if _own:
            db.close()


def get_raw_windows(
    account_id: int,
    *,
    include_payload: bool = False,
    db: Optional[Session] = None,
) -> list[dict[str, Any]]:
    """Provenance / invoice-attachment accessor for the raw GMP sponge.

    Returns ascending-by-window list of:
        {"window_start": date, "window_end": date, "http_status": int,
         "row_count": int, "interval_min": date|None, "interval_max": date|None,
         "fetched_at": datetime, ["raw_csv": str|None]}
    raw_csv is included ONLY when include_payload=True (it can be large) — that's
    the verbatim source Ford attaches to invoices.
    """
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        rows = db.execute(
            select(GmpUsageRaw).where(GmpUsageRaw.account_id == account_id)
            .order_by(GmpUsageRaw.window_start)
        ).scalars().all()
        out = []
        for r in rows:
            d = {"window_start": r.window_start, "window_end": r.window_end,
                 "http_status": r.http_status, "row_count": r.row_count,
                 "interval_min": r.interval_min, "interval_max": r.interval_max,
                 "fetched_at": r.fetched_at}
            if include_payload:
                d["raw_csv"] = r.raw_csv
            out.append(d)
        return out
    finally:
        if _own:
            db.close()
