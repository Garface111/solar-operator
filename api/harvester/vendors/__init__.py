"""Vendor registry: map a provider code to its capture module.

The flagship targets are the UTILITIES (GMP + the ~530 SmartHub co-ops) — they
have no public API and are cookie-bound, so a server-side browser is the only
way to reach them (the ceiling the old raw-HTTP pull couldn't break). The
inverter clouds (Fronius/SMA/Chint/SolarEdge) already have server-side official
APIs / pulls, so a browser path for them is redundant and intentionally not
built here — ``module_for`` returns None for them and the engine records a clean
"skipped".
"""
from __future__ import annotations

from .gmp import GMPVendor
from .smarthub import SmartHubVendor

_GMP = GMPVendor()
_SMARTHUB = SmartHubVendor()

# Inverter clouds are covered by server-side official APIs elsewhere in the app.
_INVERTER_CODES = {"fronius", "sma", "chint", "solaredge", "solis", "enphase",
                   "tigo", "alsoenergy", "locus"}


def module_for(provider: str):
    """Return the vendor module for a provider code, or None if unsupported.

    Any code that is not GMP and not a known inverter cloud is treated as a
    SmartHub co-op (vec / wec / sh_* / a registry code) — one generic adapter.
    """
    p = (provider or "").strip().lower()
    if not p:
        return None
    if p == "gmp":
        return _GMP
    if p in _INVERTER_CODES:
        return None
    return _SMARTHUB
