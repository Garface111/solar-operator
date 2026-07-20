"""Vendor-offline production continuity (utility as display + gap-fill).

When an array's inverter feed (Chint/Fronius/SMA/extension/…) is dead but a
linked utility meter (VEC/GMP/SmartHub/…) still has daily generation, we:

  1. Surface a ``production_fallback`` block on fleet-tree / overview so the UI
     can draw the utility series with honest provenance.
  2. On utility capture, allow utility kWh to replace a *stale zero* vendor day
     (source stays utility — never relabeled as the vendor).

Blood-brain barrier preserved:
  • Never write source="chint" / "extension_pull" / vendor slugs from utility.
  • Never invent per-inverter InverterDaily from a site meter.
  • Live vendor positive days always win (no overwrite while feed is alive).
  • Offtaker / paper-bill settlement is untouched.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select

from . import generation_sources
from .models import DailyGeneration, local_today

# Align with capture_debt.VENDOR_STALE_DAYS: no *positive* vendor day in this
# window means the inverter feed is dead for gap-fill / display purposes.
# Zeros do NOT keep the feed alive (a broken Chint portal often writes 0s).
VENDOR_DEAD_DAYS = 2

# Near-zero vendor kWh treated as a "stale zero" eligible for utility fill.
_ZERO_EPS = 1e-6

_VENDOR_SOURCES = (
    generation_sources.VENDOR_TELEMETRY_SOURCES
    | generation_sources.EXTENSION_SOURCES
    | {"csv", "manual"}
)
_UTILITY_SOURCES = generation_sources.UTILITY_REAL_SOURCES | {"utility_meter"}


def is_vendor_source(source: str | None) -> bool:
    return (source or "").strip().lower() in _VENDOR_SOURCES


def is_utility_source(source: str | None) -> bool:
    return (source or "").strip().lower() in _UTILITY_SOURCES


def vendor_last_day(db, array_id: int) -> Optional[date]:
    """Most recent day that has ANY vendor-source DailyGeneration row."""
    rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.source, DailyGeneration.kwh)
        .where(DailyGeneration.array_id == array_id)
        .order_by(DailyGeneration.day.desc())
        .limit(400)
    ).all()
    last_any: Optional[date] = None
    for day, src, _kwh in rows:
        if is_vendor_source(src):
            if last_any is None or day > last_any:
                last_any = day
    return last_any


def vendor_last_positive_day(db, array_id: int) -> Optional[date]:
    """Most recent day with vendor-source kWh > 0."""
    rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.source, DailyGeneration.kwh)
        .where(DailyGeneration.array_id == array_id)
        .order_by(DailyGeneration.day.desc())
        .limit(400)
    ).all()
    for day, src, kwh in rows:
        if is_vendor_source(src) and float(kwh or 0.0) > _ZERO_EPS:
            return day
    return None


def vendor_feed_is_dead(
    db, array_id: int, *, today: date | None = None
) -> tuple[bool, Optional[date]]:
    """True when no positive vendor production exists inside the dead window.

    Returns ``(dead, vendor_last_day)`` where vendor_last_day is the most recent
    vendor-source day of any kWh (including zeros) for the UI badge.
    """
    today = today or local_today()
    cutoff = today - timedelta(days=VENDOR_DEAD_DAYS)
    last_pos = vendor_last_positive_day(db, array_id)
    last_any = vendor_last_day(db, array_id)
    if last_pos is not None and last_pos >= cutoff:
        return False, last_any
    return True, last_any


def _utility_source_label(db, array_id: int) -> Optional[str]:
    """Pick a representative utility source slug for the provenance chip."""
    rows = db.execute(
        select(DailyGeneration.source)
        .where(DailyGeneration.array_id == array_id)
        .order_by(DailyGeneration.day.desc())
        .limit(60)
    ).all()
    for (src,) in rows:
        if is_utility_source(src):
            return (src or "").strip().lower() or None
    return None


def compute_production_fallback(
    db, array_id: int, *, days: int = 14, today: date | None = None
) -> dict:
    """Build the per-array production_fallback block for fleet-tree / overview.

    Shape::
        {
          "active": bool,
          "source": "utility_meter" | "smarthub" | "gmp_api" | ... | None,
          "days_filled": int,          # utility days standing in for vendor gaps
          "vendor_last_day": "YYYY-MM-DD" | None,
        }
    """
    today = today or local_today()
    dead, last_any = vendor_feed_is_dead(db, array_id, today=today)
    window_start = today - timedelta(days=max(1, days) - 1)

    rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.source, DailyGeneration.kwh)
        .where(
            DailyGeneration.array_id == array_id,
            DailyGeneration.day >= window_start,
            DailyGeneration.day <= today,
        )
    ).all()

    by_day: dict[date, list[tuple[str, float]]] = {}
    for day, src, kwh in rows:
        by_day.setdefault(day, []).append(
            ((src or "").strip().lower(), float(kwh or 0.0))
        )

    days_filled = 0
    has_utility = False
    util_src: Optional[str] = None
    for day, entries in by_day.items():
        util = [(s, k) for s, k in entries if is_utility_source(s)]
        vend = [(s, k) for s, k in entries if is_vendor_source(s)]
        if util:
            has_utility = True
            if util_src is None:
                util_src = util[0][0]
            util_kwh = max(k for _, k in util)
            vend_kwh = max((k for _, k in vend), default=None)
            # A "filled" day: utility is carrying production the vendor isn't.
            if vend_kwh is None or vend_kwh <= _ZERO_EPS:
                if util_kwh > _ZERO_EPS:
                    days_filled += 1
            elif dead and util_kwh > vend_kwh:
                # vendor has a positive but feed is dead overall — still count
                # only when utility is strictly better (rare; prefer gap zeros)
                pass

    # Also count utility-only days outside by_day? already covered.
    if util_src is None and has_utility:
        util_src = _utility_source_label(db, array_id)
    elif util_src is None:
        util_src = _utility_source_label(db, array_id)
        if util_src:
            has_utility = True

    active = bool(dead and has_utility and days_filled > 0)

    return {
        "active": active,
        "source": util_src if active else (util_src if dead and has_utility else None),
        "days_filled": days_filled if active else 0,
        "vendor_last_day": last_any.isoformat() if last_any else None,
    }


def should_gap_fill_vendor_zero(
    db,
    array_id: int,
    *,
    existing_source: str | None,
    existing_kwh: float | None,
    utility_kwh: float,
    today: date | None = None,
) -> bool:
    """True when utility capture may replace an existing vendor row for one day.

    Rules:
      • utility_kwh must be strictly better than existing
      • existing source must be a vendor stream (not another utility / estimate)
      • existing kWh must be zero/near-zero (stale-zero only)
      • vendor feed must be dead (no positive vendor day in VENDOR_DEAD_DAYS)
    """
    if utility_kwh is None or utility_kwh <= _ZERO_EPS:
        return False
    if not is_vendor_source(existing_source):
        return False
    if float(existing_kwh or 0.0) > _ZERO_EPS:
        return False
    if utility_kwh <= float(existing_kwh or 0.0) + _ZERO_EPS:
        return False
    dead, _ = vendor_feed_is_dead(db, array_id, today=today)
    return dead


def apply_utility_day(
    db,
    *,
    tenant_id: str,
    array_id: int,
    day: date,
    utility_kwh: float,
    utility_source: str = "utility_meter",
    today: date | None = None,
    insert_fn=None,
) -> str:
    """Upsert one utility day with vendor-alive / stale-zero gap-fill rules.

    Returns one of: ``"inserted"``, ``"updated"``, ``"gap_filled"``, ``"skipped"``.

    ``insert_fn`` optional race-safe insert callback used by array_owners
    (signature: insert_fn(db, tenant_id, array_id, day, kwh, source) -> bool).
    """
    from .models import now as _now

    today = today or local_today()
    src = (utility_source or "utility_meter").strip().lower()
    row = db.execute(
        select(DailyGeneration).where(
            DailyGeneration.array_id == array_id,
            DailyGeneration.day == day,
        )
    ).scalar_one_or_none()

    if row is None:
        if insert_fn is not None:
            ok = insert_fn(
                db, tenant_id=tenant_id, array_id=array_id,
                day=day, kwh=utility_kwh, source=src,
            )
            return "inserted" if ok else "skipped"
        db.add(DailyGeneration(
            tenant_id=tenant_id, array_id=array_id, day=day,
            kwh=utility_kwh, source=src, uploaded_at=_now(),
        ))
        return "inserted"

    # Non-measured (estimate / empty) → utility may refresh (existing rule).
    if not generation_sources.is_measured(row.source):
        row.kwh = utility_kwh
        row.source = src
        row.uploaded_at = _now()
        return "updated"

    # Measured utility already owns the day — climb only, never relabel away.
    if is_utility_source(row.source):
        if utility_kwh > float(row.kwh or 0.0):
            row.kwh = utility_kwh
            row.uploaded_at = _now()
            return "updated"
        return "skipped"

    # Measured vendor day: only stale-zero gap fill when feed is dead.
    if should_gap_fill_vendor_zero(
        db, array_id,
        existing_source=row.source,
        existing_kwh=row.kwh,
        utility_kwh=utility_kwh,
        today=today,
    ):
        row.kwh = utility_kwh
        row.source = src  # stay utility — never pretend to be the vendor
        row.uploaded_at = _now()
        return "gap_filled"

    return "skipped"
