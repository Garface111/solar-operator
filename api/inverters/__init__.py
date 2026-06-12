"""Multi-vendor inverter framework.

Each vendor module exposes a uniform interface — validate / fetch_live /
fetch_daily plus a metadata block — and registers in VENDORS. Callers dispatch
by vendor code through the helpers below or hit VENDORS directly.

See docs/plans/INVERTER_FRAMEWORK.md for the spec.
"""
from __future__ import annotations

from datetime import date

from . import chint, fronius, locus, sma, solaredge
from .base import InverterAuthError, InverterError, InverterScopeError, require_fields

# Insertion order is the order the connect UI lists vendors in.
VENDORS = {
    "solaredge": solaredge,
    "locus": locus,
    "fronius": fronius,
    "sma": sma,
    "chint": chint,
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
    """The connect-form spec the frontend renders: one entry per vendor."""
    out: list[dict] = []
    for code, module in VENDORS.items():
        out.append({
            "code": code,
            "label": module.LABEL,
            "fields": module.FIELDS,
            "available": getattr(module, "AVAILABLE", True),
            "note": getattr(module, "NOTE", None),
        })
    return out
