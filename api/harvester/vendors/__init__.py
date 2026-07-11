"""Vendor registry: map a provider code to its capture module.

Two families the farm harvests server-side by logging in with the owner's own
portal password (the Cloud Capture premise — "just give us your logins"):

  * UTILITIES — GMP + the ~530 SmartHub co-ops. No public API, cookie-bound;
    a real browser is the only way in (monthly bills, ~12h cadence).
  * INVERTER CLOUDS — Fronius / SMA / Chint. Live production power, scraped from
    each portal's own JSON API on a tight (<5 min) cadence for the vendor-data
    freshness SLA.

SolarEdge (and Solis/Enphase/Tigo/AlsoEnergy/Locus) are NOT harvested here — they
already have server-side official-API pulls (SolarEdge runs a 5-min poll), so a
browser path would be redundant. module_for returns None for them and the engine
records a clean "skipped".
"""
from __future__ import annotations

from .gmp import GMPVendor
from .smarthub import SmartHubVendor
from .fronius import FroniusVendor
from .sma import SMAVendor
from .chint import ChintVendor

_GMP = GMPVendor()
_SMARTHUB = SmartHubVendor()
_INVERTERS = {
    "fronius": FroniusVendor(),
    "sma": SMAVendor(),
    "chint": ChintVendor(),
}

# Inverter clouds covered by server-side official APIs elsewhere in the app.
_API_ONLY_INVERTERS = {"solaredge", "solis", "enphase", "tigo", "alsoenergy", "locus"}


def module_for(provider: str):
    """Return the vendor module for a provider code, or None if unsupported.

    Any code that is not GMP, a supported inverter cloud, or an API-only inverter
    is treated as a SmartHub co-op (vec / wec / sh_* / a registry code).
    """
    p = (provider or "").strip().lower()
    if not p:
        return None
    if p == "gmp":
        return _GMP
    if p in _INVERTERS:
        return _INVERTERS[p]
    if p in _API_ONLY_INVERTERS:
        return None
    return _SMARTHUB
