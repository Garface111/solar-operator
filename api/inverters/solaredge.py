"""SolarEdge inverter source — WRAPS api/adapters/solaredge.py.

The working SolarEdge HTTP logic lives in api/adapters/solaredge.py and is the
single source of truth for SolarEdge calls. This module only adapts that logic
to the vendor interface (validate / fetch_live / fetch_daily) and translates
SolarEdge exceptions into the framework's InverterError/InverterAuthError.

Config: {"api_key": str, "site_id": int}.
"""
from __future__ import annotations

from datetime import date

from ..adapters import solaredge as _se
from .base import InverterAuthError, InverterError, require_fields

CODE = "solaredge"
LABEL = "SolarEdge"
AVAILABLE = True
NOTE = None
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "api_key", "label": "API key", "secret": True},
    {"name": "site_id", "label": "Site ID", "secret": False},
]


def _creds(config: dict) -> tuple[str, int]:
    require_fields(config, "api_key", "site_id")
    api_key = str(config["api_key"]).strip()
    try:
        site_id = int(config["site_id"])
    except (TypeError, ValueError) as exc:
        raise InverterError(f"site_id must be an integer, got {config['site_id']!r}") from exc
    return api_key, site_id


def validate(config: dict) -> dict:
    """Confirm the key/site by pulling site details; return name + peak kW."""
    api_key, site_id = _creds(config)
    try:
        details = _se.site_details(api_key, site_id)
    except _se.SolarEdgeAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _se.SolarEdgeError as exc:
        raise InverterError(str(exc)) from exc
    return {
        "site_name": details.get("name"),
        "peak_power_kw": details.get("peak_kw"),
        "site_id": site_id,
    }


def fetch_live(config: dict) -> dict | None:
    """Current AC power (W) + last-update timestamp from the site overview."""
    api_key, site_id = _creds(config)
    try:
        overview = _se.fetch_overview(api_key, site_id)
    except _se.SolarEdgeAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _se.SolarEdgeError as exc:
        raise InverterError(str(exc)) from exc

    raw_power = (overview.get("currentPower") or {}).get("power")
    power_w = float(raw_power) if raw_power is not None else None
    return {"current_power_w": power_w, "as_of": overview.get("lastUpdateTime")}


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    """Daily kWh between start/end (inclusive) — zero/offline days omitted."""
    api_key, site_id = _creds(config)
    try:
        rows = _se.fetch_daily_energy(api_key, site_id, start, end)
    except _se.SolarEdgeAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _se.SolarEdgeError as exc:
        raise InverterError(str(exc)) from exc
    return [{"day": r["day"], "kwh": r["kwh"]} for r in rows]
