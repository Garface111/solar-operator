"""Helpers for classifying utility accounts as residential vs generation."""
from __future__ import annotations


def classify_residential(provider: str, account_payload: dict) -> bool:
    """Return True iff this account is a residential non-generation customer.

    A residential account has no NEPOOL-GIS participation and no solar
    net-metering. Used to filter auto-capture so the operator's dashboard
    only fills with generation/business accounts.

    Today GMP-only: GMP payloads carry solarNetMeter + groupNetMetered flags.
    VEC/WEC fall through to False until those adapters surface a generation
    indicator.
    # TODO(skip-residential): when VEC/WEC payloads carry a generation flag, apply the same filter here.
    """
    provider = (provider or "").upper()
    if provider != "GMP":
        return False  # No signal yet — don't filter.
    extra = account_payload.get("extra") or {}
    solar = bool(extra.get("solarNetMeter"))
    group = bool(extra.get("groupNetMetered"))
    return not (solar or group)
