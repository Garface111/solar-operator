"""GMP published-rate lookup (Anna/Bruce's ask #4 foundation).

Digitizes Bruce's "GMP Rates 2026.xlsx" (api/data/gmp_rates_2026.json) into a
lookup for the offtaker setup page's EXPECTED billing rate, and for a
"data-is-king" cross-check of a bill's actual net-metering rate against GMP's
published schedule.

Model (Bruce): an array uses **GMP Rate #1** before its 10-year anniversary and
the **Blended Statewide Rate** from the 10-year anniversary on. So the array's
AGE picks the regime; the billing YEAR + MONTH picks the cell. Rates are $/kWh;
a $0.043 solar adder is tracked separately.

Regime switch: an array uses GMP Rate #1 for its first 11 years and the Blended
Statewide Rate from age 11 on (Ford confirmed 2026-07-01: "<11 is Rate #1", so
ages 0–10 → Rate #1, age 11+ → Blended). The threshold is BLENDED_AGE_THRESHOLD.
The boundary is DAY-accurate from the commissioning DATE (Bruce 2026-07-03: "you
need to get more specific with the date to determine if 11 yo or not" — GMP has
called an array 11 two years early). Legacy year-only values are read as Jan 1
of that year, which reproduces the old whole-calendar-year math exactly, so
nothing silently flips regime on deploy.
This lookup is a REFERENCE (the invoice still bills on the bill's own rate); it
never silently overrides real billed figures.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date
from functools import lru_cache
from typing import Optional

# Age (whole years from commissioning) at which an array moves from GMP Rate #1
# to the Blended Statewide Rate. Ford confirmed 11: <11 stays on Rate #1.
BLENDED_AGE_THRESHOLD = 11

_DATA_PATH = pathlib.Path(__file__).parent / "data" / "gmp_rates_2026.json"


@lru_cache(maxsize=1)
def _rates() -> dict:
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def _nearest_year(grid: dict, year: int) -> Optional[str]:
    """The closest available year key in a monthly grid (clamps past the ends so
    a billing period outside the sheet's range still resolves to the nearest
    published schedule rather than failing)."""
    years = sorted(int(y) for y in grid.keys())
    if not years:
        return None
    if year <= years[0]:
        return str(years[0])
    if year >= years[-1]:
        return str(years[-1])
    if str(year) in grid:
        return str(year)
    return str(min(years, key=lambda y: abs(y - year)))


def regime_for_age(age_years: Optional[int]) -> str:
    """'rate1' for an array's first 11 years (age 0–10), 'blended' from age 11 on.
    Unknown age (None) defaults to 'rate1' (the common pre-switch case; the caller
    is told the age was assumed)."""
    if age_years is None:
        return "rate1"
    return "blended" if age_years >= BLENDED_AGE_THRESHOLD else "rate1"


def _add_years(d: date, years: int) -> date:
    """`d` moved `years` calendar years forward (Feb 29 → Feb 28 when the target
    year isn't a leap year)."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def blended_start(commission_date: date) -> date:
    """The first day the array is on the Blended Statewide Rate — its 11-year
    anniversary, day-accurate."""
    return _add_years(commission_date, BLENDED_AGE_THRESHOLD)


def age_years_on(commission_date: date, on: date) -> int:
    """Whole years elapsed from commissioning to `on`, day-accurate: the age
    ticks on the anniversary DAY, not on Jan 1. Never negative."""
    years = on.year - commission_date.year
    if years < 0:
        return 0
    if _add_years(commission_date, years) > on:
        years -= 1
    return max(0, years)


def expected_gmp_rate(target_year: int, target_month: int,
                      commission_year: Optional[int] = None,
                      age_years: Optional[int] = None,
                      regime: Optional[str] = None,
                      commission_date: Optional[date] = None) -> Optional[dict]:
    """Expected GMP $/kWh for a billing (year, month).

    Regime resolution order: explicit `regime` → from `age_years` → from
    `commission_date` (day-accurate: whole years elapsed at the FIRST day of the
    billing month, so a mid-month 11-year anniversary keeps Rate #1 through that
    month and is flagged via `regime_flips_within_month`) → from
    `commission_year` (legacy year-only: read as Jan 1 of that year, which
    reproduces the old whole-calendar-year math exactly) → default 'rate1'.
    Returns None only if the data file is unreadable/empty. Includes both the
    monthly cell and the insolation-weighted annual average, each with and
    without the solar adder, plus `blended_from` (the day-accurate regime-switch
    date) whenever a commissioning point is known.
    """
    data = _rates()
    m = max(1, min(12, int(target_month)))
    month_start = date(int(target_year), m, 1)
    # Resolve the commissioning point. A full date is authoritative; a bare year
    # is assumed Jan 1 (flagged in the response) — within a year-only value the
    # true regime is ambiguous, and Jan 1 is the reading that matches the old
    # year-granular behavior, so legacy values never silently change regime.
    year_only = False
    cd = commission_date
    if cd is None and commission_year is not None:
        try:
            cd = date(int(commission_year), 1, 1)
            year_only = True
        except ValueError:
            cd = None
    if age_years is None and cd is not None:
        age_years = age_years_on(cd, month_start)
    regime = regime or regime_for_age(age_years)
    if regime not in ("rate1", "blended"):
        regime = "rate1"
    blended_from = blended_start(cd) if cd is not None else None
    # The 11-year anniversary lands strictly INSIDE the billing month → the month
    # straddles the regime switch. We show the start-of-month regime (Rate #1
    # through the anniversary month — never flipping early, which is exactly the
    # GMP mistake Bruce has seen) and flag the straddle for honest UI copy.
    flips_within_month = (blended_from is not None
                          and blended_from.year == month_start.year
                          and blended_from.month == month_start.month
                          and blended_from.day > 1)

    block = data.get(regime) or {}
    monthly = block.get("monthly") or {}
    wa = block.get("weighted_avg") or {}
    yk = _nearest_year(monthly, int(target_year))
    if yk is None:
        return None
    months = monthly.get(yk) or []
    rate = months[m - 1] if len(months) >= m else None
    weighted_avg = wa.get(yk)
    adder = float(data.get("solar_adder") or 0.0)

    def _plus(v):
        return round(v + adder, 6) if isinstance(v, (int, float)) else None

    return {
        "regime": regime,
        "regime_label": "GMP Rate #1" if regime == "rate1" else "Blended Statewide Rate",
        "age_years": age_years,
        "year": int(target_year),
        "month": m,
        "source_year_used": int(yk),
        "clamped": int(yk) != int(target_year),
        "rate_per_kwh": rate,
        "weighted_avg_per_kwh": weighted_avg,
        "solar_adder": adder,
        "rate_plus_adder": _plus(rate),
        "weighted_avg_plus_adder": _plus(weighted_avg),
        # Day-accurate 11-year boundary (Bruce's C4 ask). `commission_date`
        # echoes only a REAL provided date; a year-only input sets the
        # assumed-Jan-1 flag instead so the UI can be honest about ambiguity.
        "commission_date": commission_date.isoformat() if commission_date else None,
        "year_only_assumed_jan1": year_only,
        "blended_from": blended_from.isoformat() if blended_from else None,
        "regime_flips_within_month": flips_within_month,
    }
