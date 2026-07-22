"""Meter-primary measured energy boundary for verification.

Priority per (array, day):
  1. utility meter real sources  → boundary "meter"
  2. inverter / extension / csv  → boundary "inverter"
  3. none                        → unavailable

Estimates (bill_prorate, utility_meter) never count as measured for PI.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..generation_sources import (
    ESTIMATE_SOURCES,
    UTILITY_REAL_SOURCES,
    is_measured,
)

BOUNDARY_METER = "meter"
BOUNDARY_INVERTER = "inverter"
BOUNDARY_UNAVAILABLE = "unavailable"
BOUNDARY_MIXED = "mixed"  # series-level only: some days meter, some inverter


def _src(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def select_measured_for_day(
    rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Pick the verification measured kWh for one calendar day.

    `rows` items: {"kwh": float, "source": str}. Multiple rows allowed.
    Prefer any utility-real source (sum if multiple meters). Else sum measured
    inverter/operator sources. Else unavailable.

    Returns:
      {kwh, boundary, sources: list[str], used_estimate: False}
    """
    meter_kwh = 0.0
    meter_sources: list[str] = []
    inv_kwh = 0.0
    inv_sources: list[str] = []

    for r in rows or []:
        src = _src(r.get("source"))
        try:
            kwh = float(r.get("kwh") or 0.0)
        except (TypeError, ValueError):
            continue
        if src in ESTIMATE_SOURCES:
            continue
        if src in UTILITY_REAL_SOURCES:
            meter_kwh += kwh
            meter_sources.append(src)
        elif is_measured(src):
            inv_kwh += kwh
            inv_sources.append(src)

    if meter_sources:
        return {
            "kwh": meter_kwh,
            "boundary": BOUNDARY_METER,
            "sources": sorted(set(meter_sources)),
            "used_estimate": False,
        }
    if inv_sources:
        return {
            "kwh": inv_kwh,
            "boundary": BOUNDARY_INVERTER,
            "sources": sorted(set(inv_sources)),
            "used_estimate": False,
        }
    return {
        "kwh": None,
        "boundary": BOUNDARY_UNAVAILABLE,
        "sources": [],
        "used_estimate": False,
    }


def classify_series_boundary(day_boundaries: Iterable[str]) -> str:
    """Roll day-level boundaries into a series badge."""
    bs = {b for b in day_boundaries if b and b != BOUNDARY_UNAVAILABLE}
    if not bs:
        return BOUNDARY_UNAVAILABLE
    if bs == {BOUNDARY_METER}:
        return BOUNDARY_METER
    if bs == {BOUNDARY_INVERTER}:
        return BOUNDARY_INVERTER
    if BOUNDARY_METER in bs and BOUNDARY_INVERTER in bs:
        return BOUNDARY_MIXED
    return next(iter(bs))
