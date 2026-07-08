"""Multi-vendor inverter framework.

Each vendor module exposes a uniform interface — validate / fetch_live /
fetch_daily plus a metadata block — and registers in VENDORS. Callers dispatch
by vendor code through the helpers below or hit VENDORS directly.

See docs/plans/INVERTER_FRAMEWORK.md for the spec.
"""
from __future__ import annotations

from datetime import date

from . import alsoenergy, chint, enphase, fronius, locus, sma, solaredge, solis, tigo
from .base import InverterAuthError, InverterError, InverterScopeError, require_fields

# Insertion order is the order the connect UI lists vendors in.
VENDORS = {
    "solaredge": solaredge,
    "enphase": enphase,
    "solis": solis,
    "tigo": tigo,
    "locus": locus,
    "fronius": fronius,
    "sma": sma,
    "chint": chint,
    "alsoenergy": alsoenergy,
}

__all__ = [
    "VENDORS",
    "InverterError",
    "InverterAuthError",
    "InverterScopeError",
    "require_fields",
    "get_vendor",
    "validate",
    "fetch_live",
    "fetch_daily",
    "vendor_catalog",
]


def get_vendor(vendor: str):
    """Return the module for `vendor` or raise InverterError for an unknown code."""
    try:
        return VENDORS[vendor]
    except KeyError as exc:
        raise InverterError(f"Unknown inverter vendor: {vendor!r}") from exc


def validate(vendor: str, config: dict) -> dict:
    return get_vendor(vendor).validate(config)


def fetch_live(vendor: str, config: dict) -> dict | None:
    return get_vendor(vendor).fetch_live(config)


def fetch_daily(vendor: str, config: dict, start: date, end: date) -> list[dict]:
    return get_vendor(vendor).fetch_daily(config, start, end)


def vendor_catalog() -> list[dict]:
    """The connect-form spec the frontend renders: one entry per vendor.

    `connect_mode` tells the UI which connect surface to render:
      • "key"      → the field form (paste API key / credentials) — default.
      • "account"  → one credential, then discover the whole account (SolarEdge).
      • "consent"  → owner enters only their email, approves in their portal, we
                     discover + attach (SMA). `consent_email_label` labels the
                     single input in that flow.
    """
    out: list[dict] = []
    for code, module in VENDORS.items():
        out.append({
            "code": code,
            "label": module.LABEL,
            "fields": module.FIELDS,
            "available": getattr(module, "AVAILABLE", True),
            "note": getattr(module, "NOTE", None),
            "connect_mode": getattr(module, "CONNECT_MODE", "key"),
            "consent_email_label": getattr(module, "CONSENT_EMAIL_LABEL", None),
        })
    return out
