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
    """Raised specifically for 401 — bad or expired API key."""


class SolarEdgeScopeError(SolarEdgeAuthError):
    """Raised for 403 on an account-level endpoint — the key is VALID but is a
    site-level key, so it can't enumerate every site. Distinct from a 401 so
    callers can fall back to the single-site (known site_id) path instead of
    telling the user their key is bad."""


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

    sites_block, _count = _parse_sites_block(body)
    return sites_block


def _parse_sites_block(body: dict) -> tuple[list[dict], int]:
    """Parse a /sites/list response into ([{site_id, name, address, peak_kw,
    status}, ...], total_count). Shared by list_sites + list_all_sites."""
    sites_block = body.get("sites", {}) or {}
    raw_sites = sites_block.get("site", []) or []
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
        results.append({
            "site_id": int(s.get("id", 0)),
            "name": s.get("name", ""),
            "address": address,
            "peak_kw": float(s.get("peakPower") or 0),
            "status": s.get("status", ""),
        })

    try:
        total = int(sites_block.get("count", len(results)))
    except (TypeError, ValueError):
        total = len(results)
    return results, total


# SolarEdge caps /sites/list at 100 rows per page; paginate via startIndex.
_SITES_PAGE_SIZE = 100


def _fetch_sites_page(api_key: str, start_index: int, size: int) -> tuple[list[dict], int]:
    """One page of /sites/list. Raises SolarEdgeScopeError on 403 (site-level
    key), SolarEdgeAuthError on 401, SolarEdgeError otherwise. Returns
    (sites_page, total_count)."""
    url = f"{SOLAREDGE_API_BASE}/sites/list"
    params = {"api_key": api_key, "size": size, "startIndex": start_index}
    try:
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise SolarEdgeError(f"Network error contacting SolarEdge: {exc}") from exc

    if resp.status_code == 403:
        raise SolarEdgeScopeError(
            "SolarEdge rejected /sites/list with 403 — this is a site-level API "
            "key. Use an account-level key (SolarEdge Admin → Site Access → API "
            "Access) to auto-discover every site."
        )
    if resp.status_code == 401:
        raise SolarEdgeAuthError(
            "SolarEdge API key rejected (401). Check the key is valid and active."
        )
    if not resp.is_success:
        raise SolarEdgeError(
            f"SolarEdge /sites/list returned {resp.status_code}: {resp.text[:200]}"
        )

    try:
        body = resp.json()
    except Exception as exc:
        raise SolarEdgeError(f"SolarEdge returned non-JSON response: {exc}") from exc

    return _parse_sites_block(body)


def list_all_sites(api_key: str, page_size: int = _SITES_PAGE_SIZE) -> list[dict]:
    """Every site an ACCOUNT-LEVEL key can read, paginating /sites/list via
    startIndex (size capped at 100 by the API).

    Unlike list_sites(), this RAISES on auth/scope failures instead of swallowing
    them — the account-discovery flow needs to tell a bad key (401) apart from a
    site-level key (403) apart from an empty account ([]).
    """
    page_size = max(1, min(int(page_size), _SITES_PAGE_SIZE))
    all_sites: list[dict] = []
    start = 0
    seen_ids: set[int] = set()
    while True:
        page, total = _fetch_sites_page(api_key, start, page_size)
        for s in page:
            if s["site_id"] not in seen_ids:
                seen_ids.add(s["site_id"])
                all_sites.append(s)
        start += page_size
        # Stop once we've covered the reported total or the page came back short
        # (defends against a server that ignores startIndex — no infinite loop).
        if not page or len(page) < page_size or start >= total:
            break
    return all_sites


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


# ── per-inverter (equipment-level) ────────────────────────────────────────────
import re as _re
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    _ZoneInfo = None

# SolarEdge timestamps (equipment telemetry `date`, overview `lastUpdateTime`)
# are in the SITE's LOCAL time with NO timezone marker. Reading them as UTC made
# the outage clock run hours fast (a VT site, America/New_York, looked ~4-5h more
# stale than reality). We fetch the site's IANA timeZone from /site/{id}/details
# ONCE (it never changes) and cache it, then stamp every naive timestamp with it.
_SITE_TZ_CACHE: dict[int, str] = {}


def _site_timezone(api_key: str, site_id: int) -> str | None:
    """The site's IANA timeZone (e.g. 'America/New_York'), cached. None on any
    failure — callers then fall back to leaving the timestamp naive."""
    if site_id in _SITE_TZ_CACHE:
        return _SITE_TZ_CACHE[site_id] or None
    tzname = None
    try:
        resp = httpx.get(f"{SOLAREDGE_API_BASE}/site/{site_id}/details",
                         params={"api_key": api_key}, timeout=_TIMEOUT)
        if resp.is_success:
            loc = ((resp.json() or {}).get("details") or {}).get("location") or {}
            tzname = loc.get("timeZone") or None
    except Exception:
        tzname = None
    _SITE_TZ_CACHE[site_id] = tzname or ""
    return tzname


def _localize_to_utc_iso(naive_ts: str | None, tzname: str | None) -> str | None:
    """Turn a naive 'YYYY-MM-DD HH:MM:SS' SolarEdge timestamp (site-local) into a
    timezone-AWARE ISO string. Uses the site's IANA zone when known; otherwise
    leaves it naive (downstream treats naive as UTC — the pre-fix behavior)."""
    if not naive_ts:
        return None
    iso = str(naive_ts).replace(" ", "T")
    if not (tzname and _ZoneInfo):
        return iso
    try:
        dt = _dt.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_ZoneInfo(tzname))
        return dt.astimezone(_tz.utc).isoformat()
    except Exception:
        return iso


def _nameplate_from_model(model: str | None) -> float | None:
    """SolarEdge model strings encode nameplate. Two families:

      * Commercial three-phase: ``SE##K`` / ``SE##.#K`` / ``RSE##K`` → kilowatts
        (SE20K=20, SE33.3KUS=33.3, RSE33.3K-USR48BNU4=33.3).
      * Residential single-phase: ``SE####`` → WATTS (SE10000=10 kW, SE7600=7.6,
        SE10000H-US000BNU4=10). The old K-only regex left these as null, so
        Starlake/Cover-class sites never stamped nameplate_kw on inventory and
        showed "not modeled yet" until a separate fleet model-parse path ran.

    Returns kW or None. Mirrors api.inverter_fleet._nameplate_from_model for
    vendor=solaredge so inventory stamping and forecast denominators agree.
    """
    s = model or ""
    # kW-form first so SE100KUS isn't misread as 100 W by the watt pattern.
    mk = _re.search(r"(?:^|[^A-Za-z0-9])R?SE(\d{1,3}(?:\.\d+)?)K", s, _re.I)
    if mk:
        try:
            kw = float(mk.group(1))
        except (TypeError, ValueError):
            kw = None
        return kw if kw is not None and 0 < kw <= 1000 else None
    mw = _re.search(r"(?:^|[^A-Za-z0-9])SE(\d{3,5})(?!\d)", s, _re.I)
    if mw:
        try:
            kw = float(mw.group(1)) / 1000.0
        except (TypeError, ValueError):
            kw = None
        return kw if kw is not None and 0 < kw <= 1000 else None
    return None


def fetch_inventory(api_key: str, site_id: int) -> list[dict]:
    """Return the site's inverters: [{sn, name, model, nameplate_kw,
    connected_optimizers}, ...].

    Calls GET /site/{id}/inventory. Raises SolarEdgeAuthError on 401/403,
    SolarEdgeError otherwise.
    """
    url = f"{SOLAREDGE_API_BASE}/site/{site_id}/inventory"
    try:
        resp = httpx.get(url, params={"api_key": api_key}, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise SolarEdgeError(f"Network error contacting SolarEdge: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SolarEdgeAuthError(
            f"SolarEdge API key rejected for site {site_id} (401/403)."
        )
    if not resp.is_success:
        raise SolarEdgeError(
            f"SolarEdge /site/{site_id}/inventory returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )
    try:
        body = resp.json()
    except Exception as exc:
        raise SolarEdgeError(f"SolarEdge returned non-JSON response: {exc}") from exc

    inverters = (body.get("Inventory", {}) or {}).get("inverters", []) or []
    out: list[dict] = []
    for it in inverters:
        model = it.get("model", "")
        out.append({
            "sn": it.get("SN"),
            "name": it.get("name") or it.get("SN") or "inverter",
            "model": model,
            "nameplate_kw": _nameplate_from_model(model),
            "connected_optimizers": it.get("connectedOptimizers"),
        })
    return out


_FAULT_MODES = {"FAULT", "ERROR", "SHUTDOWN", "LOCKED"}


def fetch_inverter_telemetry(
    api_key: str, site_id: int, sn: str, days_back: int = 7
) -> dict:
    """Per-inverter daily kWh + current mode over the last `days_back` days
    (<=7; SolarEdge caps the equipment-data span at 7 days per call).

    Returns {daily:[{date,kwh}], error_code, last_report, last_mode,
    last_power_w}. error_code is set only when the latest mode is a fault state.
    """
    end = _dt.now(_tz.utc).replace(microsecond=0)
    start = end - _td(days=min(max(days_back, 1), 7))
    fmt = "%Y-%m-%d %H:%M:%S"
    url = f"{SOLAREDGE_API_BASE}/equipment/{site_id}/{sn}/data"
    params = {
        "startTime": start.strftime(fmt),
        "endTime": end.strftime(fmt),
        "api_key": api_key,
    }
    try:
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise SolarEdgeError(f"Network error contacting SolarEdge: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SolarEdgeAuthError(
            f"SolarEdge API key rejected for site {site_id} (401/403)."
        )
    if not resp.is_success:
        raise SolarEdgeError(
            f"SolarEdge /equipment/{site_id}/{sn}/data returned "
            f"{resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except Exception as exc:
        raise SolarEdgeError(f"SolarEdge returned non-JSON response: {exc}") from exc

    tel = (body.get("data", {}) or {}).get("telemetries", []) or []
    by_day: dict[str, list[float]] = {}
    last_mode = last_ts = None
    last_power_w = None
    for s in tel:
        te = s.get("totalEnergy")
        day = (s.get("date") or "")[:10]
        if te is not None and day:
            by_day.setdefault(day, []).append(float(te))
        last_mode = s.get("inverterMode", last_mode)
        last_ts = s.get("date", last_ts)
        if s.get("totalActivePower") is not None:
            last_power_w = float(s["totalActivePower"])

    daily = [
        {"date": day, "kwh": round((max(v) - min(v)) / 1000.0, 2) if len(v) >= 2 else 0.0}
        for day, v in sorted(by_day.items())
    ]
    err = last_mode if (last_mode and last_mode.upper() in _FAULT_MODES) else None
    # last_ts is site-LOCAL naive time → convert to a tz-aware UTC ISO using the
    # site's timezone so the downstream outage-age math is correct.
    tzname = _site_timezone(api_key, site_id)
    return {
        "daily": daily,
        "error_code": err,
        "last_mode": last_mode,
        "last_report": _localize_to_utc_iso(last_ts, tzname),
        "last_power_w": last_power_w,
    }
