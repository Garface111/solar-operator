"""Locus Energy (SolarNOC) inverter source — WRAPS api/adapters/locus.py.

The working Locus HTTP logic lives in api/adapters/locus.py and is the single
source of truth for Locus calls. This module only adapts that logic to the
vendor interface (validate / fetch_live / fetch_daily + discover_sites) and
translates Locus exceptions into the framework's InverterError family.

Config: {"client_id", "client_secret", "username", "password", "site_id"};
discovery additionally takes {"partner_id"}.
"""
from __future__ import annotations

from datetime import date

from ..adapters import locus as _locus
from .base import InverterAuthError, InverterError, InverterScopeError, require_fields

CODE = "locus"
LABEL = "Locus Energy (SolarNOC)"
AVAILABLE = True
NOTE = "Requires Locus API credentials (client_id/secret + SolarNOC username/password) from your Locus account manager."
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "client_id", "label": "Client ID", "secret": False},
    {"name": "client_secret", "label": "Client Secret", "secret": True},
    {"name": "username", "label": "SolarNOC username", "secret": False},
    {"name": "password", "label": "SolarNOC password", "secret": True},
    {"name": "site_id", "label": "Site ID", "secret": False},
    {"name": "partner_id", "label": "Partner ID (for discovery)", "secret": False},
]


def _creds(config: dict) -> dict:
    """Pull the four credential fields out of config (raises if any is blank)."""
    require_fields(config, "client_id", "client_secret", "username", "password")
    return {
        "client_id": str(config["client_id"]).strip(),
        "client_secret": str(config["client_secret"]).strip(),
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
        details = _locus.site_details(creds, site_id)
    except _locus.LocusScopeError as exc:
        raise InverterScopeError(str(exc)) from exc
    except _locus.LocusAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _locus.LocusError as exc:
        raise InverterError(str(exc)) from exc
    return {"site_name": details.get("name"), "site_id": site_id}


def discover_sites(config: dict) -> list[dict]:
    """Every site under a partner an account credential can read, for the "paste
    one credential, attach all arrays" flow.

    Returns [{site_id, name, peak_power_kw, status}, ...] — Locus's site list has
    no peak power, so peak_power_kw is always None (the key is kept so the UI
    shape matches SolarEdge).

    Raises:
      InverterScopeError — credentials valid but no access to the partner (403).
      InverterAuthError  — bad/inactive credentials (401).
      InverterError      — any other Locus failure (5xx, network, bad JSON).
    """
    creds = _creds(config)
    require_fields(config, "partner_id")
    partner_id = config["partner_id"]
    try:
        sites = _locus.list_partner_sites(creds, partner_id)
    except _locus.LocusScopeError as exc:
        raise InverterScopeError(str(exc)) from exc
    except _locus.LocusAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _locus.LocusError as exc:
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
    """Current AC power (W) + timestamp from the latest W_avg datapoint."""
    creds = _creds(config)
    site_id = _site_id(config)
    try:
        return _locus.fetch_latest_power(creds, site_id)
    except _locus.LocusScopeError as exc:
        raise InverterScopeError(str(exc)) from exc
    except _locus.LocusAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _locus.LocusError as exc:
        raise InverterError(str(exc)) from exc


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    """Daily kWh between start/end — zero/offline days omitted (single API call)."""
    creds = _creds(config)
    site_id = _site_id(config)
    tz = str(config.get("timezone") or "UTC")
    try:
        rows = _locus.fetch_daily_energy(creds, site_id, start, end, tz=tz)
    except _locus.LocusScopeError as exc:
        raise InverterScopeError(str(exc)) from exc
    except _locus.LocusAuthError as exc:
        raise InverterAuthError(str(exc)) from exc
    except _locus.LocusError as exc:
        raise InverterError(str(exc)) from exc
    return [{"day": r["day"], "kwh": r["kwh"]} for r in rows]
