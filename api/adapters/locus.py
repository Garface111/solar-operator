"""Locus Energy (SolarNOC / AlsoEnergy) v3 API adapter.

Pulls daily generation + live power per site from the Locus v3 API, persisted
into DailyGeneration by the generalized inverter pull. This module is the single
source of truth for the Locus HTTP calls; the thin vendor wrapper lives in
api/inverters/locus.py.

Auth (grounded live against the SolarNOC portal 2026-07-16): AlsoEnergy moved
Locus login onto **AWS Cognito**, so the legacy OAuth2 password grant against
api.locusenergy.com/oauth/token is dead. The portal instead:

  1. POSTs cognito-idp.us-east-1.amazonaws.com with AuthFlow=USER_PASSWORD_AUTH
     and the SolarNOC username/password → gets a Cognito **IdToken** (JWT).
  2. Sends that IdToken as `Authorization: Bearer <IdToken>` to the v3 API.
     (The AccessToken from the same login is scoped to Cognito admin only and is
     REJECTED by v3 with a 401 — it MUST be the IdToken.)

So one SolarNOC login is all we need — no client_id/secret, no API key. The
partner id (for account-wide discovery) is read straight from the IdToken's
`custom:partnerId` claim, so the owner pastes only username + password. Tokens
are cached per username with their expiry; a cached refresh_token is reused
(REFRESH_TOKEN_AUTH) before falling back to a fresh password grant.

Rate limit: Locus default concurrency is 1 request at a time; a 429 surfaces as
a clear LocusError rather than crashing the caller.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from datetime import date, datetime, timedelta

import httpx

from ..inverters.base import TIMEOUT

log = logging.getLogger(__name__)

API_BASE = "https://api.locusenergy.com/v3"

# AlsoEnergy's Cognito user pool that backs the SolarNOC portal login. The client
# id is the portal SPA's PUBLIC app client (no secret) — the same value for every
# portal user, captured from the live login flow. A config may override it if a
# future portal ships a different app client.
COGNITO_URL = "https://cognito-idp.us-east-1.amazonaws.com/"
COGNITO_CLIENT_ID = "j70nf561ufafqnq52pqljpvr9"
_COGNITO_CONTENT_TYPE = "application/x-amz-json-1.1"

# Cognito error __type values that mean "these credentials are bad" (vs. a
# transient/service failure). Anything else is surfaced as a generic LocusError.
_COGNITO_AUTH_ERRORS = {
    "NotAuthorizedException",
    "UserNotFoundException",
    "UserNotConfirmedException",
    "PasswordResetRequiredException",
}


class LocusError(Exception):
    """Any Locus API failure: bad config, network, rate limit, unexpected payload."""


class LocusAuthError(LocusError):
    """Raised specifically for a rejected login — bad SolarNOC username/password."""


class LocusScopeError(LocusAuthError):
    """Raised for 403 — credentials are VALID but lack permission for the
    requested partner/site. Distinct from a 401 so callers can tell a bad
    credential apart from a forbidden entity."""


# Token cache: cache_key -> (id_token, refresh_token, expires_at). Module-scoped
# so a daily poll across many sites under one login reuses one token. The key
# mixes username with a hash of the password so a CHANGED password (rotation, or
# a bad reconnect attempt) forces a fresh auth instead of silently reusing the
# prior good token — which would let a wrong password validate as OK.
_TOKEN_CACHE: dict[str, tuple[str, str | None, datetime]] = {}


def _cache_key(username: str, password: str) -> str:
    pw_hash = hashlib.sha256((password or "").encode("utf-8")).hexdigest()
    return f"{username}\x00{pw_hash}"


def _now() -> datetime:
    return datetime.utcnow()


def _creds_client_id(creds: dict) -> str:
    """The Cognito app client id — the shared portal default unless overridden."""
    return str(creds.get("cognito_client_id") or COGNITO_CLIENT_ID)


def _cognito(target: str, payload: dict) -> dict:
    """POST a Cognito Identity Provider action, returning the parsed JSON body.

    `target` is the bare action name (e.g. "InitiateAuth"); it's prefixed with
    the AWSCognitoIdentityProviderService namespace. Maps a bad-credential
    __type to LocusAuthError and anything else to LocusError.
    """
    try:
        resp = httpx.post(
            COGNITO_URL,
            headers={
                "Content-Type": _COGNITO_CONTENT_TYPE,
                "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
            },
            content=json.dumps(payload),
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise LocusError(f"Network error contacting Locus (Cognito) login: {exc}") from exc

    if resp.is_success:
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — any decode failure is a Locus error
            raise LocusError(f"Locus login returned non-JSON: {exc}") from exc

    # Cognito reports errors as 400 with {"__type": "...", "message": "..."}.
    err_type = ""
    try:
        body = resp.json()
        err_type = (body.get("__type") or "").split("#")[-1]
        message = body.get("message") or ""
    except Exception:  # noqa: BLE001
        message = resp.text[:200]
    if err_type in _COGNITO_AUTH_ERRORS:
        raise LocusAuthError(
            "Locus (SolarNOC) rejected the username/password. "
            "Double-check the login you use at the SolarNOC portal."
        )
    raise LocusError(f"Locus login failed ({resp.status_code} {err_type}): {message}")


def _initiate_auth(username: str, password: str, client_id: str) -> dict:
    """USER_PASSWORD_AUTH — exchange username/password for Cognito tokens."""
    body = _cognito("InitiateAuth", {
        "AuthFlow": "USER_PASSWORD_AUTH",
        "ClientId": client_id,
        "AuthParameters": {"USERNAME": username, "PASSWORD": password},
        "ClientMetadata": {},
    })
    return body.get("AuthenticationResult") or {}


def _refresh_auth(refresh_token: str, client_id: str) -> dict:
    """REFRESH_TOKEN_AUTH — mint a fresh IdToken from a cached refresh token.

    Cognito returns a new IdToken/AccessToken but NOT a new refresh token.
    """
    body = _cognito("InitiateAuth", {
        "AuthFlow": "REFRESH_TOKEN_AUTH",
        "ClientId": client_id,
        "AuthParameters": {"REFRESH_TOKEN": refresh_token},
    })
    return body.get("AuthenticationResult") or {}


def get_token(username: str, password: str, client_id: str = COGNITO_CLIENT_ID) -> str:
    """Return a cached (or freshly minted) Cognito **IdToken** for `username`.

    Reuses a still-valid cached token. When it has expired but a refresh_token is
    cached, tries the refresh grant first; on failure (or no cached refresh
    token), falls back to a fresh USER_PASSWORD_AUTH grant. The IdToken — NOT the
    AccessToken — is what the v3 API authorizes against.
    """
    key = _cache_key(str(username), str(password))
    cached = _TOKEN_CACHE.get(key)
    if cached is not None and cached[2] > _now():
        return cached[0]

    refresh_token = cached[1] if cached is not None else None
    result: dict = {}
    if refresh_token:
        try:
            result = _refresh_auth(refresh_token, client_id)
        except LocusAuthError:
            # Refresh token expired/revoked — fall back to a password grant.
            result = {}
    if not result.get("IdToken"):
        result = _initiate_auth(username, password, client_id)

    id_token = result.get("IdToken")
    if not id_token:
        raise LocusError("Locus login returned no IdToken")
    new_refresh = result.get("RefreshToken") or refresh_token
    ttl = int(result.get("ExpiresIn") or 3600)
    _TOKEN_CACHE[key] = (id_token, new_refresh, _now() + timedelta(seconds=max(ttl - 60, 60)))
    return id_token


def _jwt_claims(token: str) -> dict:
    """Decode a JWT's payload segment WITHOUT signature verification.

    We only read claims from a token WE just obtained via our own login, so
    there is no trust boundary to enforce here — this is just base64url decode.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError) as exc:
        raise LocusError(f"Could not decode Locus IdToken claims: {exc}") from exc


def partner_id(creds: dict) -> int:
    """The partner id this login belongs to, read from the IdToken's
    `custom:partnerId` claim — so account-wide discovery needs no extra input."""
    token = get_token(creds["username"], creds["password"], _creds_client_id(creds))
    claims = _jwt_claims(token)
    pid = claims.get("custom:partnerId")
    if pid in (None, ""):
        raise LocusError("Locus IdToken has no custom:partnerId claim.")
    try:
        return int(pid)
    except (TypeError, ValueError) as exc:
        raise LocusError(f"Locus custom:partnerId is not an int: {pid!r}") from exc


def _get(creds: dict, path: str, params: dict | None = None) -> dict:
    """Authenticated GET against the Locus v3 API, returning the parsed body.

    Maps 401 -> LocusAuthError, 403 -> LocusScopeError, 429 -> LocusError (rate
    limit), 5xx/other -> LocusError.
    """
    token = get_token(creds["username"], creds["password"], _creds_client_id(creds))
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


def list_partner_sites(creds: dict, partner: int | None = None) -> list[dict]:
    """Every site under a partner — the "one login, all arrays" call.

    When `partner` is omitted it's read from the login's IdToken claim, so the
    caller only needs a username/password. Pages through /search/sites (200 at a
    time) so a large partner returns every site.

    Returns [{site_id, name, address, timezone, peak_power_kw, client_id}, ...].

    Raises LocusScopeError on 403 (forbidden partner), LocusAuthError on a bad
    login, LocusError otherwise.
    """
    pid = int(partner) if partner not in (None, "") else partner_id(creds)
    out: list[dict] = []
    offset = 0
    limit = 200
    while True:
        body = _get(creds, f"/partners/{pid}/search/sites", params={
            "sortby": "clientName,siteName",
            "offset": offset,
            "limit": limit,
        })
        sites = body.get("sites", []) or []
        for s in sites:
            cap = s.get("siteDCCapacity")
            out.append({
                "site_id": int(s.get("siteId") or s.get("id") or 0),
                "name": s.get("siteName") or s.get("name") or "",
                "address": _build_address(s),
                "timezone": s.get("locationTimezone") or "",
                "peak_power_kw": float(cap) if cap not in (None, "") else None,
                "client_id": s.get("clientId"),
            })
        total = ((body.get("paging") or {}).get("total")) or len(out)
        offset += limit
        if offset >= int(total) or not sites:
            break
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
        "end": f"{end.isoformat()}T23:59:59",
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

    Raises LocusScopeError on 403, LocusAuthError on a bad login, LocusError otherwise.
    """
    body = _get(creds, f"/sites/{site_id}")
    site = body.get("site") if isinstance(body.get("site"), dict) else body
    return {
        "site_id": int(site.get("id") or site_id),
        "name": site.get("name") or "",
        "timezone": site.get("locationTimezone") or "",
        "address": _build_address(site),
    }
