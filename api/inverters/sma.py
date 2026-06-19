"""SMA inverter source — Monitoring API (ennexOS / smaapis.de).

╔════════════════════════════════════════════════════════════════════════════╗
║ LOUD CAVEAT — UNVERIFIED AGAINST A LIVE SMA ACCOUNT.                        ║
║                                                                            ║
║ This adapter requires an app registration with SMA (client_id / client_    ║
║ secret issued by the SMA developer portal) AND a plant-owner consent flow. ║
║ The endpoints below follow SMA's PUBLISHED docs but have NOT been run       ║
║ against a real account/token — treat the response parsing as best-effort    ║
║ until a live SMA system confirms the exact JSON shapes.                     ║
╚════════════════════════════════════════════════════════════════════════════╝

OAuth2. Config: {"client_id", "client_secret", "system_id", "refresh_token"?}.
client_credentials grant when no refresh_token is supplied, else refresh_token
grant. Tokens are cached per client_id with their expiry.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx

from .base import TIMEOUT, InverterAuthError, InverterError, require_fields

CODE = "sma"
LABEL = "SMA (Sunny Portal / ennexOS)"
AVAILABLE = True
NOTE = "Requires SMA developer app registration + owner consent. Endpoints unverified."
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "client_id", "label": "Client ID", "secret": False},
    {"name": "client_secret", "label": "Client Secret", "secret": True},
    {"name": "system_id", "label": "Plant / System ID", "secret": False},
    {"name": "refresh_token", "label": "Refresh token (optional)", "secret": True},
]

AUTH_URL = "https://auth.smaapis.de/oauth2/token"
MON_BASE = "https://monitoring.smaapis.de/v1"

# Token cache: client_id -> (access_token, refresh_token, expires_at). Module-
# scoped so a daily poll across many plants under one app reuses a single token.
# The refresh_token is cached too because SMA ROTATES it on every refresh grant —
# the response hands back a NEW refresh_token and invalidates the one just used.
# We must reuse the freshest one (and persist it back to the connection config,
# see _get_token's mutation of `config`) or the next refresh fails 401 and the
# plant goes dark until the owner manually reconnects.
_TOKEN_CACHE: dict[str, tuple[str, str | None, datetime]] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _get_token(config: dict) -> str:
    cid = str(config["client_id"])
    cached = _TOKEN_CACHE.get(cid)
    if cached is not None and cached[2] > _now():
        return cached[0]

    # Prefer the freshest refresh_token we hold: the rotated one from the cache
    # (set by a prior refresh) over the original stored in config.
    refresh_token = (cached[1] if cached is not None else None) or config.get("refresh_token")

    if refresh_token:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cid,
            "client_secret": config["client_secret"],
        }
    else:
        data = {
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": config["client_secret"],
        }

    try:
        resp = httpx.post(AUTH_URL, data=data, timeout=TIMEOUT)
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting SMA OAuth: {exc}") from exc
    if resp.status_code in (401, 403):
        # A rotated/expired refresh token lands here. Drop the dead cache entry
        # AND clear it from config so the NEXT call falls back to a fresh
        # client_credentials grant instead of retrying the dead token forever.
        _TOKEN_CACHE.pop(cid, None)
        if config.get("refresh_token"):
            config["refresh_token"] = None
        raise InverterAuthError("SMA OAuth rejected the credentials (401/403).")
    if not resp.is_success:
        raise InverterError(
            f"SMA token endpoint returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"SMA token endpoint returned non-JSON: {exc}") from exc

    token = body.get("access_token")
    if not token:
        raise InverterError("SMA token endpoint returned no access_token")
    ttl = int(body.get("expires_in") or 3600)
    # Capture the ROTATED refresh_token SMA just issued (falls back to the one we
    # sent if the response omits it). Cache it AND write it back into `config` in
    # place so the caller (poller) can persist it to the DB — surviving both
    # access-token expiry and a server redeploy that clears the in-memory cache.
    new_refresh = body.get("refresh_token") or refresh_token
    _TOKEN_CACHE[cid] = (token, new_refresh, _now() + timedelta(seconds=max(ttl - 60, 60)))
    if new_refresh and new_refresh != config.get("refresh_token"):
        config["refresh_token"] = new_refresh
    return token


def _get(config: dict, path: str, params: dict | None = None) -> dict:
    token = _get_token(config)
    try:
        resp = httpx.get(
            f"{MON_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting SMA Monitoring: {exc}") from exc
    if resp.status_code in (401, 403):
        raise InverterAuthError("SMA Monitoring rejected the token (401/403).")
    if not resp.is_success:
        raise InverterError(
            f"SMA Monitoring {path} returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"SMA Monitoring returned non-JSON response: {exc}") from exc


def _pv_generation(body: dict) -> tuple[float | None, str | None]:
    """Pull the pvGeneration measurement value (+ time) from a measurement set.

    Tolerates {"pvGeneration": {"value", "time"}} and {"pvGeneration": value}.
    """
    pv = (body or {}).get("pvGeneration")
    if isinstance(pv, dict):
        return pv.get("value"), pv.get("time") or pv.get("timestamp")
    return pv, None


def validate(config: dict) -> dict:
    require_fields(config, "client_id", "client_secret", "system_id")
    body = _get(config, f"/plants/{config['system_id']}")
    return {"site_name": body.get("name")}


def fetch_live(config: dict) -> dict | None:
    require_fields(config, "client_id", "client_secret", "system_id")
    body = _get(
        config,
        f"/plants/{config['system_id']}/measurements/sets/EnergyAndPowerPv/Recent",
    )
    value, as_of = _pv_generation(body)
    power_w = float(value) if value is not None else None
    return {"current_power_w": power_w, "as_of": as_of}


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    require_fields(config, "client_id", "client_secret", "system_id")
    out: list[dict] = []
    day = start
    # Cap the per-call loop so a wide range can't fan out unbounded.
    for _ in range(90):
        if day > end:
            break
        body = _get(
            config,
            f"/plants/{config['system_id']}/measurements/sets/EnergyAndPowerPv/Day",
            params={"Date": day.isoformat()},
        )
        wh, _ts = _pv_generation(body)
        if wh is not None:
            out.append({"day": day, "kwh": float(wh) / 1000.0})
        day += timedelta(days=1)
    return out
