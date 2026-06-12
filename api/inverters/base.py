"""Common types + helpers for the multi-vendor inverter framework.

Every vendor module (solaredge, fronius, sma, chint) exposes the same surface:

    validate(config: dict) -> dict          # {"site_name": str, ...}; raises on bad creds
    fetch_live(config: dict) -> dict | None # {"current_power_w": float, "as_of": iso} or None
    fetch_daily(config: dict, start, end) -> list[{"day": date, "kwh": float}]

and a small metadata block (CODE, LABEL, FIELDS, AVAILABLE, NOTE,
SUPPORTS_LIVE, SUPPORTS_DAILY) used by the connect UI and the scheduler.

All HTTP must use a 20s timeout and raise InverterAuthError on 401/403 so the
connect endpoints can return a clean 400 and the scheduler can mark the
connection errored without crashing.
"""
from __future__ import annotations

# Shared HTTP timeout for every vendor request (seconds).
TIMEOUT = 20.0


class InverterError(Exception):
    """Any inverter-source failure: bad config, network, unexpected payload."""


class InverterAuthError(InverterError):
    """Specifically a credential rejection (401) — bad/expired key/secret."""


class InverterScopeError(InverterAuthError):
    """Credentials are VALID but lack the scope for an account-level call (403)
    — e.g. a SolarEdge site-level key used against /sites/list. Callers can fall
    back to a single-site path (when the site id is known) instead of treating
    the key as bad."""


def require_fields(config: dict, *names: str) -> None:
    """Raise InverterError if any of `names` is missing/blank from `config`."""
    missing = [n for n in names if not (config or {}).get(n)]
    if missing:
        raise InverterError(f"Missing required field(s): {', '.join(missing)}")
