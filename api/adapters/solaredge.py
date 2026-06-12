"""SolarEdge Monitoring API adapter.

Pulls daily generation per site, persists into DailyGeneration table.
Replaces the manual CSV upload step for arrays on SolarEdge inverters.

Auth: API key per Site or Account. The operator pastes the key during array
setup; we store it in plain text (read-only scope, operator-controlled data).
Future hardening: encrypt at rest.

Rate limit: 300 req/day per account token. Daily polling of N arrays =
N requests/day, well inside the limit.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import httpx

log = logging.getLogger(__name__)

SOLAREDGE_API_BASE = "https://monitoringapi.solaredge.com"

_TIMEOUT = 20.0  # seconds


class SolarEdgeError(Exception):
    """Raised for API errors (auth failures, rate limits, unexpected responses)."""


class SolarEdgeAuthError(SolarEdgeError):
    """Raised specifically for 401/403 — bad or expired API key."""


def fetch_daily_energy(
    api_key: str, site_id: int, start: date, end: date
) -> list[dict]:
    """Return [{day: date, kwh: float, source: 'solaredge'}, ...].

    Calls GET /site/{id}/energy?timeUnit=DAY. Response is in Wh — divide by
    1000 for kWh. Days with 0 or null energy are skipped (inverter offline).
    """
    url = f"{SOLAREDGE_API_BASE}/site/{site_id}/energy"
    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "DAY",
        "api_key": api_key,
    }
    try:
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise SolarEdgeError(f"Network error contacting SolarEdge: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SolarEdgeAuthError(
            "SolarEdge API key rejected (401/403). Check the key is valid and "
            "has access to site ID {site_id}."
        )
    if not resp.is_success:
        raise SolarEdgeError(
            f"SolarEdge API returned {resp.status_code}: {resp.text[:200]}"
        )

    try:
        body = resp.json()
    except Exception as exc:
        raise SolarEdgeError(f"SolarEdge returned non-JSON response: {exc}") from exc

    energy_block = body.get("energy", {})
    values = energy_block.get("values", [])

    results: list[dict] = []
    for entry in values:
        raw_date = entry.get("date", "")
        wh = entry.get("value")
        if wh is None or wh == 0:
            continue
        try:
            # SolarEdge returns "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
            day = date.fromisoformat(raw_date[:10])
        except (ValueError, TypeError):
            log.warning("SolarEdge: unparseable date %r in site %d response", raw_date, site_id)
            continue
        kwh = float(wh) / 1000.0
        results.append({"day": day, "kwh": kwh, "source": "solaredge"})

    return results


def fetch_overview(api_key: str, site_id: int) -> dict:
    """Return the SolarEdge site `overview` block (current power, last update).

    Raises SolarEdgeAuthError on 401/403, SolarEdgeError on any other failure.
    The inverter framework's solaredge.fetch_live() wraps this; array_owners
    keeps its own short-TTL cache around it to stay inside the 300 req/day cap.
    """
    url = f"{SOLAREDGE_API_BASE}/site/{site_id}/overview"
    try:
        resp = httpx.get(url, params={"api_key": api_key}, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise SolarEdgeError(f"Network error contacting SolarEdge: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SolarEdgeAuthError(
            f"SolarEdge API key rejected for site {site_id} (401/403). "
            "Verify the key and site ID are correct."
        )
    if not resp.is_success:
        raise SolarEdgeError(
            f"SolarEdge /site/{site_id}/overview returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is a SolarEdge error
        raise SolarEdgeError(f"SolarEdge returned non-JSON response: {exc}") from exc

    return body.get("overview", {}) or {}


def list_sites(api_key: str) -> list[dict]:
    """For an Account-level API key, list all sites the operator can read.

    Returns [{site_id: int, name: str, address: str, peak_kw: float}, ...].

    For a Site-level key this endpoint returns only the one site — we still
    return a list so callers use the same code path. Returns [] on auth error
    (site-level key trying to enumerate — caller should use explicit site_id).
    """
    url = f"{SOLAREDGE_API_BASE}/sites/list"
    params = {"api_key": api_key}
    try:
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise SolarEdgeError(f"Network error contacting SolarEdge: {exc}") from exc

    if resp.status_code in (401, 403):
        # Site-level keys can't list sites; return [] so caller prompts for site_id.
        return []
    if not resp.is_success:
        raise SolarEdgeError(
            f"SolarEdge /sites/list returned {resp.status_code}: {resp.text[:200]}"
        )

    try:
        body = resp.json()
    except Exception as exc:
        raise SolarEdgeError(f"SolarEdge returned non-JSON response: {exc}") from exc

    sites_block = body.get("sites", {})
    raw_sites = sites_block.get("site", [])
    # API may return a single dict instead of a list when there's exactly one site
    if isinstance(raw_sites, dict):
        raw_sites = [raw_sites]

    results: list[dict] = []
    for s in raw_sites:
        location = s.get("location") or {}
        address_parts = [
            location.get("address", ""),
            location.get("city", ""),
            location.get("state", ""),
        ]
        address = ", ".join(p for p in address_parts if p)
        peak_kw = float(s.get("peakPower") or 0)
        results.append({
            "site_id": int(s.get("id", 0)),
            "name": s.get("name", ""),
            "address": address,
            "peak_kw": peak_kw,
        })

    return results


def site_details(api_key: str, site_id: int) -> dict:
    """Return {site_id, name, peak_kw, address, status} for sanity-check on setup.

    Raises SolarEdgeAuthError on 401/403, SolarEdgeError on other failures.
    """
    url = f"{SOLAREDGE_API_BASE}/site/{site_id}/details"
    params = {"api_key": api_key}
    try:
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise SolarEdgeError(f"Network error contacting SolarEdge: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SolarEdgeAuthError(
            f"SolarEdge API key rejected for site {site_id} (401/403). "
            "Verify the key and site ID are correct."
        )
    if not resp.is_success:
        raise SolarEdgeError(
            f"SolarEdge /site/{site_id}/details returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    try:
        body = resp.json()
    except Exception as exc:
        raise SolarEdgeError(f"SolarEdge returned non-JSON response: {exc}") from exc

    details = body.get("details", {})
    if not details:
        raise SolarEdgeError(
            f"SolarEdge returned empty details for site {site_id}"
        )

    location = details.get("location") or {}
    address_parts = [
        location.get("address", ""),
        location.get("city", ""),
        location.get("state", ""),
    ]
    address = ", ".join(p for p in address_parts if p)

    return {
        "site_id": int(details.get("id", site_id)),
        "name": details.get("name", ""),
        "peak_kw": float(details.get("peakPower") or 0),
        "address": address,
        "status": details.get("status", ""),
    }
