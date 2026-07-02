"""Canonical registry of DailyGeneration / InverterDaily `source` keys.

ONE source of truth for "what is a REAL measured production reading" vs "what is
an ESTIMATE". Three allowlists had silently diverged
(forecasting.MEASURED_DAILY_SOURCES, inverter_fleet._VENDOR_SOURCES, and
bill_to_daily._REAL_SOURCES), so a fleet reading a vendor that only one list knew
about (Fronius / SMA / Enphase / SmartHub …) showed "No measured production yet"
and the morning digest called live arrays "asleep". Only SolarEdge / Locus
worked end-to-end. This module makes every one of those sets DERIVE from the
canonical constants below so they can never drift again (audit #12).

A source is REAL/MEASURED when it is an actual metered single-day energy reading:
  • inverter telemetry (every vendor in api.inverters.VENDORS),
  • the Chrome extension's captures,
  • operator-supplied independent production (CSV / manual entry),
  • a live in-window inverter reading,
  • a genuine utility read (GMP 15-min API, GMP portal scrape, SmartHub per-day).

The ONLY estimate sources — a monthly bill smeared flat across its days — are
`bill_prorate` and `utility_meter`. Everything else is real.
"""
from __future__ import annotations

from .inverters import VENDORS

# Every inverter-telemetry vendor slug. Sourced live from the vendor registry so
# adding a vendor there automatically makes its daily pulls count as "measured"
# (inverter_pull writes DailyGeneration.source = the vendor slug). Currently:
# solaredge, enphase, solis, tigo, locus, fronius, sma, chint, alsoenergy.
VENDOR_TELEMETRY_SOURCES: frozenset[str] = frozenset(VENDORS.keys())

# Chrome-extension captures (raw + the corrected variant).
EXTENSION_SOURCES: frozenset[str] = frozenset({
    "extension_pull", "extension_pull_corrected",
})

# Operator-supplied independent production + a live inverter reading.
OPERATOR_SOURCES: frozenset[str] = frozenset({
    "csv", "manual", "live",
})

# Genuine utility-side reads (NOT the bill_prorate/utility_meter smear estimates).
UTILITY_REAL_SOURCES: frozenset[str] = frozenset({
    "gmp_api", "gmp_portal_scrape", "smarthub",
})

# The one canonical set of REAL / measured daily-generation source keys. Anything
# NOT in here is treated as an estimate and never allowed to overwrite, raise, or
# relabel a measured reading.
MEASURED_SOURCES: frozenset[str] = (
    VENDOR_TELEMETRY_SOURCES
    | EXTENSION_SOURCES
    | OPERATOR_SOURCES
    | UTILITY_REAL_SOURCES
)

# The estimate sources (a monthly bill spread flat across its days). Explicit so
# callers can assert MEASURED_SOURCES and ESTIMATE_SOURCES stay disjoint.
ESTIMATE_SOURCES: frozenset[str] = frozenset({
    "bill_prorate", "utility_meter",
})

assert MEASURED_SOURCES.isdisjoint(ESTIMATE_SOURCES), (
    "a source cannot be both measured and an estimate"
)


def is_measured(source: str | None) -> bool:
    """True if `source` is a REAL metered reading (not an estimate / unknown)."""
    return (source or "").strip().lower() in MEASURED_SOURCES
