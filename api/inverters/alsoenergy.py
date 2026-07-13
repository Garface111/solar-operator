"""AlsoEnergy (PowerTrack) inverter source — WRAPS api/adapters/alsoenergy.py.

The working AlsoEnergy HTTP logic lives in api/adapters/alsoenergy.py and is the
single source of truth for AlsoEnergy calls. This module only adapts that logic
to the vendor interface (validate / fetch_live / fetch_daily + discover_sites)
and translates AlsoEnergy exceptions into the framework's InverterError family.

Config: {"username", "password", "site_id"}. One PowerTrack login can enumerate
every site it can read — the "paste one credential, attach all arrays" flow.
"""
from __future__ import annotations

from datetime import date

from ..adapters import alsoenergy as _ae
from .base import InverterAuthError, InverterError, InverterScopeError, require_fields

CODE = "alsoenergy"
LABEL = "AlsoEnergy (PowerTrack)"
AVAILABLE = True
NOTE = (
    "Requires your AlsoEnergy / PowerTrack portal username + password — the same "
    "login you use at hmi.alsoenergy.com / powertrack. No API key needed."
)
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "username", "label": "PowerTrack username (email)", "secret": False},
    {"name": "password", "label": "PowerTrack password", "secret": True},
    # Optional when using connect-account / discover — one login attaches every site.
    {"name": "site_id", "label": "Site ID (optional — leave blank to attach every site)",
     "secret": False, "optional": True},
]


def _creds(config: dict) -> dict:
    """Pull the credential fields out of config (raises if username/password blank)."""
    require_fields(config, "username", "password")
    return {
        "username": str(config["username"]).strip(),
        "password": str(config["password"]),
    }


def _site_id(config: dict) -> int:
    require_fields(config, "site_id")
    try:
        return int(config["site_id"])
    except (TypeError, ValueError) as exc:
        raise InverterError(f"site_id must be an integer, got {config['site_id']!r}") from exc


def validate(config: dict) -> dict:
    """Confirm the credentials + site by pulling site details; return name + id."""
    creds = _creds(config)
    site_id = _site_id(config)
    try:
        details = _ae.site_details(creds, site_id)
    except _ae.AlsoEnergyScopeError as exc:
        raise InverterScopeError(str(exc)) from exc
    except _ae.AlsoEnergyAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _ae.AlsoEnergyError as exc:
        raise InverterError(str(exc)) from exc
    return {"site_name": details.get("name"), "site_id": site_id}


def discover_sites(config: dict) -> list[dict]:
    """Every site a PowerTrack login can read, for the "paste one credential,
    attach all arrays" flow.

    Returns [{site_id, name, peak_power_kw, status}, ...] — the AlsoEnergy site
    list has no peak power, so peak_power_kw is always None (the key is kept so
    the UI shape matches the other vendors).

    Raises:
      InverterScopeError — credentials valid but no access (403).
      InverterAuthError  — bad/inactive credentials (401/403 at token).
      InverterError      — any other AlsoEnergy failure (5xx, network, bad JSON).
    """
    creds = _creds(config)
    try:
        sites = _ae.list_sites(creds)
    except _ae.AlsoEnergyScopeError as exc:
        raise InverterScopeError(str(exc)) from exc
    except _ae.AlsoEnergyAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _ae.AlsoEnergyError as exc:
        raise InverterError(str(exc)) from exc

    return [
        {
            "site_id": s["site_id"],
            "name": s.get("name") or "",
            "peak_power_kw": None,
            "status": "",
        }
        for s in sites
    ]


def fetch_live(config: dict) -> dict | None:
    """Current AC power (W) + timestamp summed across the site's inverters."""
    creds = _creds(config)
    site_id = _site_id(config)
    try:
        return _ae.fetch_latest_power(creds, site_id)
    except _ae.AlsoEnergyScopeError as exc:
        raise InverterScopeError(str(exc)) from exc
    except _ae.AlsoEnergyAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _ae.AlsoEnergyError as exc:
        raise InverterError(str(exc)) from exc


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    """Daily kWh between start/end — zero/offline days omitted (single API call)."""
    creds = _creds(config)
    site_id = _site_id(config)
    tz = str(config.get("timezone") or "UTC")
    try:
        rows = _ae.fetch_daily_energy(creds, site_id, start, end, tz=tz)
    except _ae.AlsoEnergyScopeError as exc:
        raise InverterScopeError(str(exc)) from exc
    except _ae.AlsoEnergyAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _ae.AlsoEnergyError as exc:
        raise InverterError(str(exc)) from exc
    return [{"day": r["day"], "kwh": r["kwh"]} for r in rows]
