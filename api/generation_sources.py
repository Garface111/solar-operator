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
  • a genuine utility read (GMP 15-min API, GMP portal scrape, SmartHub per-day,
    including source=utility_meter from the co-op usage capture),
  • third-party production meters (eGauge, Meter Mate, LangSends, …).

The ONLY estimate source — a monthly bill smeared flat across its days — is
`bill_prorate`. (Historically `utility_meter` was mis-listed as an estimate; that
collided with the SmartHub/VEC daily-export capture which ALSO writes
source=utility_meter for REAL per-day net-export kWh.)
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

# Genuine utility-side reads. `utility_meter` is the SmartHub/VEC/WEC daily
# net-export capture (NOT a bill smear — bill smears are `bill_prorate` only).
UTILITY_REAL_SOURCES: frozenset[str] = frozenset({
    "gmp_api", "gmp_portal_scrape", "smarthub", "utility_meter",
})

# Third-party production / site meters that are not utility portals and not
# inverter OEM APIs. Used by generation reports as first-class monitoring
# sources (same tier as Locus / AlsoEnergy). CSV import normalizes aliases
# (see SOURCE_ALIASES) into these slugs.
THIRD_PARTY_METER_SOURCES: frozenset[str] = frozenset({
    "egauge",       # eGauge site monitors
    "metermate",    # Meter Mate
    "langsend",     # LangSendsData custom feeds
    "dave_lahar",   # manual VEC data-request channel (import)
})

# Aliases → canonical source slug (CSV import, spreadsheet paste, sheet labels
# from Crown/Bruce-style tracking workbooks). Keys are lowercased + stripped.
SOURCE_ALIASES: dict[str, str] = {
    # Locus / AlsoEnergy family
    "locus": "locus",
    "locus energy": "locus",
    "solarnoc": "locus",
    "also": "alsoenergy",
    "also energy": "alsoenergy",
    "alsoenergy": "alsoenergy",
    "powertrack": "alsoenergy",
    # Utility
    "vec": "smarthub",
    "vec website": "smarthub",
    "wec": "smarthub",
    "smarthub": "smarthub",
    "gmp": "gmp_api",
    "utility meter": "utility_meter",
    "utility_meter": "utility_meter",
    # Third-party meters from the tracking sheet
    "meter mate": "metermate",
    "metermate": "metermate",
    "egauge": "egauge",
    "e-gauge": "egauge",
    "e gauge": "egauge",
    "eguage": "egauge",  # typo seen in sheets
    "langsend": "langsend",
    "langsendsdata": "langsend",
    "lang sends data": "langsend",
    "dave lahar": "dave_lahar",
    "dave lahar data request": "dave_lahar",
    "vec - dave lahar data request": "dave_lahar",
    "vec dave lahar data request": "dave_lahar",
}

# Monitoring / production-meter sources for generation reports (native, not
# "last resort"). Utility still outranks these when both cover a month.
# Includes operator CSV/manual uploads (the classic "I downloaded daily kWh"
# path) so those continue to fill report months.
MONITORING_REPORT_SOURCES: frozenset[str] = (
    VENDOR_TELEMETRY_SOURCES
    | EXTENSION_SOURCES
    | THIRD_PARTY_METER_SOURCES
    | OPERATOR_SOURCES
)

# The one canonical set of REAL / measured daily-generation source keys. Anything
# NOT in here is treated as an estimate and never allowed to overwrite, raise, or
# relabel a measured reading.
MEASURED_SOURCES: frozenset[str] = (
    VENDOR_TELEMETRY_SOURCES
    | EXTENSION_SOURCES
    | OPERATOR_SOURCES
    | UTILITY_REAL_SOURCES
    | THIRD_PARTY_METER_SOURCES
)

# The estimate sources (a monthly bill spread flat across its days). Explicit so
# callers can assert MEASURED_SOURCES and ESTIMATE_SOURCES stay disjoint.
ESTIMATE_SOURCES: frozenset[str] = frozenset({
    "bill_prorate",
})

assert MEASURED_SOURCES.isdisjoint(ESTIMATE_SOURCES), (
    "a source cannot be both measured and an estimate"
)
assert UTILITY_REAL_SOURCES.isdisjoint(MONITORING_REPORT_SOURCES), (
    "a source cannot be both settlement-utility and monitoring tier"
)


def is_measured(source: str | None) -> bool:
    """True if `source` is a REAL metered reading (not an estimate / unknown)."""
    return (source or "").strip().lower() in MEASURED_SOURCES


def normalize_source(raw: str | None, *, default: str = "csv") -> str:
    """Map a free-text sheet label to a canonical DailyGeneration.source slug."""
    if raw is None:
        return default
    key = " ".join(str(raw).strip().lower().split())
    if not key:
        return default
    if key in SOURCE_ALIASES:
        return SOURCE_ALIASES[key]
    # Already a known measured slug?
    if key in MEASURED_SOURCES or key in ESTIMATE_SOURCES:
        return key
    # Underscore form of multi-word labels
    us = key.replace(" ", "_").replace("-", "_")
    if us in MEASURED_SOURCES:
        return us
    if us in SOURCE_ALIASES:
        return SOURCE_ALIASES[us]
    # Prefix match for labels like "Eguage - CAROLYN" / "LOCUS (site 12)"
    for alias in sorted(SOURCE_ALIASES.keys(), key=len, reverse=True):
        if (
            key.startswith(alias + " ")
            or key.startswith(alias + "-")
            or key.startswith(alias + " -")
            or key.startswith(alias + "(")
            or key.startswith(alias + "/")
        ):
            return SOURCE_ALIASES[alias]
    return default
