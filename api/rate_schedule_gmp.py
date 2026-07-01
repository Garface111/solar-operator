"""GMP published-rate lookup (Anna/Bruce's ask #4 foundation).

Digitizes Bruce's "GMP Rates 2026.xlsx" (api/data/gmp_rates_2026.json) into a
lookup for the offtaker setup page's EXPECTED billing rate, and for a
"data-is-king" cross-check of a bill's actual net-metering rate against GMP's
published schedule.

Model (Bruce): an array uses **GMP Rate #1** before its 10-year anniversary and
the **Blended Statewide Rate** from the 10-year anniversary on. So the array's
AGE picks the regime; the billing YEAR + MONTH picks the cell. Rates are $/kWh;
a $0.043 solar adder is tracked separately.

NOTE (confirm w/ Ford/Bruce): the regime switch is set at the 10-year anniversary
(age >= 10 -> Blended), per the sheet's "Pre 10 year anniversary arrays" label.
Bruce's prose said both "<11 is Rate #1" and "10+ is Blended" — a one-year
ambiguity. The threshold is the single constant BLENDED_AGE_THRESHOLD below.
This lookup is a REFERENCE (the invoice still bills on the bill's own rate); it
never silently overrides real billed figures.
"""
from __future__ import annotations

import json
import pathlib
from functools import lru_cache
from typing import Optional

# The 10-year anniversary switch (age in whole years from commissioning). See the
# module docstring — flagged for Ford/Bruce confirmation.
BLENDED_AGE_THRESHOLD = 10

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
    """'rate1' before the 10-year anniversary, else 'blended'. Unknown age
    (None) defaults to 'rate1' (the pre-anniversary case is the common one and
    the caller is told the age was assumed)."""
    if age_years is None:
        return "rate1"
    return "blended" if age_years >= BLENDED_AGE_THRESHOLD else "rate1"


def expected_gmp_rate(target_year: int, target_month: int,
                      commission_year: Optional[int] = None,
                      age_years: Optional[int] = None,
                      regime: Optional[str] = None) -> Optional[dict]:
    """Expected GMP $/kWh for a billing (year, month).

    Regime resolution order: explicit `regime` → from `age_years` → from
    (target_year - commission_year) → default 'rate1'. Returns None only if the
    data file is unreadable/empty. Includes both the monthly cell and the
    insolation-weighted annual average, each with and without the solar adder.
    """
    data = _rates()
    if age_years is None and commission_year is not None:
        age_years = max(0, target_year - int(commission_year))
    regime = regime or regime_for_age(age_years)
    if regime not in ("rate1", "blended"):
        regime = "rate1"

    block = data.get(regime) or {}
    monthly = block.get("monthly") or {}
    wa = block.get("weighted_avg") or {}
    yk = _nearest_year(monthly, int(target_year))
    if yk is None:
        return None
    m = max(1, min(12, int(target_month)))
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
    }
