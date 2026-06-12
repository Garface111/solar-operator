"""Locus Energy (SolarNOC) v3 API adapter.

Pulls daily generation + live power per site, persisted into DailyGeneration by
the generalized inverter pull. This module is the single source of truth for the
Locus HTTP calls; the thin vendor wrapper lives in api/inverters/locus.py.

Auth: OAuth2 Resource Owner Password grant. One partner credential
(client_id/secret + SolarNOC username/password) can enumerate every site under
the partner — the "paste one credential, attach all arrays" flow. Tokens are
cached per client_id with their expiry; a cached refresh_token is reused before
falling back to a fresh password grant.

Rate limit: Locus default concurrency is 1 request at a time; a 429 surfaces as
a clear LocusError rather than crashing the caller.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx

from ..inverters.base import TIMEOUT

log = logging.getLogger(__name__)

AUTH_URL = "https://api.locusenergy.com/oauth/token"
API_BASE = "https://api.locusenergy.com/v3"


class LocusError(Exception):
    """Any Locus API failure: bad config, network, rate limit, unexpected payload."""


class LocusAuthError(LocusError):
    """Raised specifically for 401 — bad client_id/secret or username/password."""


class LocusScopeError(LocusAuthError):
    """Raised for 403 — credentials are VALID but lack permission for the
    requested partner/site. Distinct from a 401 so callers can tell a bad
    credential apart from a forbidden entity."""


# Token cache: client_id -> (access_token, refresh_token, expires_at). Module-
# scoped so a daily poll across many sites under one partner reuses one token.
_TOKEN_CACHE: dict[str, tuple[str, str | None, datetime]] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _post_token(data: dict) -> dict:
    """POST the OAuth token endpoint, returning the parsed JSON body.

    Raises LocusAuthError on 401/403, LocusError on any other failure.
    """
    try:
        resp = httpx.post(AUTH_URL, data=data, timeout=TIMEOUT)
    except httpx.RequestError as exc:
        raise LocusError(f"Network error contacting Locus OAuth: {exc}") from exc
    if resp.status_code in (401, 403):
        raise LocusAuthError("Locus OAuth rejected the credentials (401/403).")
    if not resp.is_success:
        raise LocusError(
            f"Locus token endpoint returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is a Locus error
        raise LocusError(f"Locus token endpoint returned non-JSON: {exc}") from exc


def get_token(
    client_id: str, client_secret: str, username: str, password: str
) -> str:
    """Return a cached (or freshly minted) bearer access token for `client_id`.

    Reuses a still-valid cached token. When the access token has expired but a
    refresh_token is cached, tries the refresh grant first; on a 401 from refresh
    (or no cached refresh token), falls back to a fresh password grant.
    """
    cid = str(client_id)
    cached = _TOKEN_CACHE.get(cid)
    if cached is not None and cached[2] > _now():
        return cached[0]

    refresh_token = cached[1] if cached is not None else None
    body: dict | None = None
    if refresh_token:
        try:
            body = _post_token({
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            })
        except LocusAuthError:
            # Refresh token expired/revoked — fall back to a password grant.
            body = None
    if body is None:
        body = _post_token({
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
        })

    token = body.get("access_token")
    if not token:
        raise LocusError("Locus token endpoint returned no access_token")
    new_refresh = body.get("refresh_token") or refresh_token
    ttl = int(body.get("expires_in") or 3600)
    _TOKEN_CACHE[cid] = (token, new_refresh, _now() + timedelta(seconds=max(ttl - 60, 60)))
    return token


def _get(creds: dict, path: str, params: dict | None = None) -> dict:
    """Authenticated GET against the Locus v3 API, returning the parsed body.

    Maps 401 -> LocusAuthError, 403 -> LocusScopeError, 429 -> LocusError (rate
    limit), 5xx/other -> LocusError.
    """
    token = get_token(
        creds["client_id"], creds["client_secret"], creds["username"], creds["password"]
    )
    try:
        resp = httpx.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise LocusError(f"Network error contacting Locus API: {exc}") from exc

    if resp.status_code == 401:
        raise LocusAuthError("Locus API rejected the token (401) — check credentials.")
    if resp.status_code == 403:
        raise LocusScopeError(
            "Locus API returned 403 — the credentials are valid but lack access "
            "to this partner/site."
        )
    if resp.status_code == 429:
        raise LocusError(
            "Locus rate limit (429) — Locus allows one request at a time; retry shortly."
        )
    if not resp.is_success:
        raise LocusError(
            f"Locus API {path} returned {resp.status_code}: {resp.text[:200]}"
        )

    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is a Locus error
        raise LocusError(f"Locus API returned non-JSON response: {exc}") from exc


def _build_address(site: dict) -> str:
    """Join a site's address fields (address1, city, state, postal), skipping blanks."""
    parts = [
        site.get("address1"),
        site.get("locale3"),
        site.get("localeCode1"),
        site.get("postalCode"),
    ]
    return ", ".join(str(p) for p in parts if p)


def list_partner_sites(creds: dict, partner_id) -> list[dict]:
    """Every site under a partner — the "one credential, all arrays" call.

    Returns [{site_id, name, address, timezone, client_id}, ...].

    Raises LocusScopeError on 403 (forbidden partner), LocusAuthError on 401,
    LocusError otherwise.
    """
    body = _get(creds, f"/partners/{partner_id}/sites")
    out: list[dict] = []
    for s in body.get("sites", []) or []:
        out.append({
            "site_id": int(s.get("id", 0)),
            "name": s.get("name") or "",
            "address": _build_address(s),
            "timezone": s.get("locationTimezone") or "",
            "client_id": s.get("clientId"),
        })
    return out


def fetch_daily_energy(
    creds: dict, site_id: int, start: date, end: date, tz: str = "UTC"
) -> list[dict]:
    """Return [{day: date, kwh: float, source: 'locus'}, ...] for a site.

    One call to GET /sites/{id}/data?gran=daily&fields=Wh_sum (no per-day loop).
    Response is in Wh — divide by 1000 for kWh. Days with 0 or null Wh_sum are
    skipped (inverter offline). The day is parsed from the first 10 chars of the
    row's `ts` (YYYY-MM-DDThh:mm:ss±tz).
    """
    params = {
        "fields": "Wh_sum",
        "start": f"{start.isoformat()}T00:00:00",
        "end": f"{end.isoformat()}T00:00:00",
        "tz": tz or "UTC",
        "gran": "daily",
    }
    body = _get(creds, f"/sites/{site_id}/data", params=params)

    results: list[dict] = []
    for row in body.get("data", []) or []:
        wh = row.get("Wh_sum")
        if wh is None or wh == 0:
            continue
        raw_ts = row.get("ts") or ""
        try:
            day = date.fromisoformat(raw_ts[:10])
        except (ValueError, TypeError):
            log.warning("Locus: unparseable ts %r in site %s response", raw_ts, site_id)
            continue
        results.append({"day": day, "kwh": float(wh) / 1000.0, "source": "locus"})
    return results


def fetch_latest_power(creds: dict, site_id: int) -> dict:
    """Return {current_power_w, as_of} from the latest W_avg datapoint.

    Uses GET /sites/{id}/data?gran=latest&fields=W_avg (start/end omitted).
    """
    body = _get(creds, f"/sites/{site_id}/data", params={"fields": "W_avg", "gran": "latest"})
    data = body.get("data") or []
    if not data:
        return {"current_power_w": None, "as_of": None}
    row = data[0]
    w = row.get("W_avg")
    return {
        "current_power_w": float(w) if w is not None else None,
        "as_of": row.get("ts"),
    }


def site_details(creds: dict, site_id: int) -> dict:
    """Return {site_id, name, timezone, address} for a single site (GET /sites/{id}).

    Raises LocusScopeError on 403, LocusAuthError on 401, LocusError otherwise.
    """
    body = _get(creds, f"/sites/{site_id}")
    site = body.get("site") if isinstance(body.get("site"), dict) else body
    return {
        "site_id": int(site.get("id") or site_id),
        "name": site.get("name") or "",
        "timezone": site.get("locationTimezone") or "",
        "address": _build_address(site),
    }
