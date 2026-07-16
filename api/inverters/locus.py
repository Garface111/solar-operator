"""Locus Energy (SolarNOC / AlsoEnergy) inverter source — WRAPS api/adapters/locus.py.

The working Locus HTTP logic lives in api/adapters/locus.py and is the single
source of truth for Locus calls. This module only adapts that logic to the
vendor interface (validate / fetch_live / fetch_daily + discover_sites) and
translates Locus exceptions into the framework's InverterError family.

Config: {"username", "password", "site_id"}. One SolarNOC login enumerates every
site under the partner — the "paste one credential, attach all arrays" flow. The
partner id is read from the login itself (no client_id/secret, no API key).
"""
from __future__ import annotations

from datetime import date

from ..adapters import locus as _locus
from .base import InverterAuthError, InverterError, InverterScopeError, require_fields

CODE = "locus"
LABEL = "Locus Energy (SolarNOC)"
AVAILABLE = True
NOTE = (
    "Requires your Locus SolarNOC portal username + password — the same login "
    "you use at the SolarNOC / AlsoEnergy portal. No API key or client secret needed."
)
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "username", "label": "SolarNOC username", "secret": False},
    {"name": "password", "label": "SolarNOC password", "secret": True},
    # Optional when using connect-account / discover — one login attaches every site.
    {"name": "site_id", "label": "Site ID (optional — leave blank to attach every site)",
     "secret": False, "optional": True},
]


def _creds(config: dict) -> dict:
    """Pull the credential fields out of config (raises if username/password blank)."""
    require_fields(config, "username", "password")
    creds = {
        "username": str(config["username"]).strip(),
        "password": str(config["password"]),
    }
    # Optional override of the portal's Cognito app client id (defaults to the
    # shared portal client baked into the adapter).
    if config.get("cognito_client_id"):
        creds["cognito_client_id"] = str(config["cognito_client_id"]).strip()
    return creds


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
    """Every site the login can read, for the "paste one credential, attach all
    arrays" flow. The partner id is derived from the login (an explicit
    `partner_id` in config still overrides it).

    Returns [{site_id, name, peak_power_kw, status}, ...].

    Raises:
      InverterScopeError — credentials valid but no access to the partner (403).
      InverterAuthError  — bad/inactive login (401).
      InverterError      — any other Locus failure (5xx, network, bad JSON).
    """
    creds = _creds(config)
    partner = config.get("partner_id") or None
    try:
        sites = _locus.list_partner_sites(creds, partner)
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
            "peak_power_kw": s.get("peak_power_kw"),
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
