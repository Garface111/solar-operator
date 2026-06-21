"""Enphase inverter source — Enlighten Systems API v4 (api.enphaseenergy.com).

╔════════════════════════════════════════════════════════════════════════════╗
║ LOUD CAVEAT — UNVERIFIED AGAINST A LIVE ENPHASE ACCOUNT.                    ║
║                                                                            ║
║ Built to Enphase's PUBLISHED v4 docs (OAuth + /api/v4/systems/{id}/summary ║
║ + /energy_lifetime), grounded against developer-v4.enphase.com on          ║
║ 2026-06-21, but NOT run against a real account/token. Needs an Enphase      ║
║ developer/partner app (api_key + client_id/secret) on a plan that exposes   ║
║ systems + summary + energy_lifetime, PLUS a real Enphase owner to confirm   ║
║ the exact JSON shapes. Treat response parsing as best-effort until then.    ║
║                                                                            ║
║ NOTE: the 2026-03-16 v4 deprecation retires only management endpoints       ║
║ (ACB telemetry, meter/array/tariff/user ops) — the systems/summary/        ║
║ energy_lifetime endpoints used here are NOT affected.                       ║
╚════════════════════════════════════════════════════════════════════════════╝

OAuth2 (Basic auth = base64(client_id:client_secret)). Config:
  {api_key, client_id, client_secret, system_id,
   refresh_token?  |  username?+password?}
- refresh_token grant (hosted-OAuth owner consent) is preferred.
- password grant (partner flow: owner's Enlighten login) is the field-enterable
  fallback when no refresh_token is held.
Every API request carries `?key=<api_key>` AND `Authorization: Bearer <token>`.
Access tokens last ~1 day; refresh tokens ~1 month and ROTATE on use — we cache
the freshest one and write it back into `config` so the poller can persist it.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta

import httpx

from .base import TIMEOUT, InverterAuthError, InverterError, require_fields

CODE = "enphase"
LABEL = "Enphase (Enlighten)"
AVAILABLE = True
NOTE = (
    "Enphase connects through the Enlighten developer API (an app key + the "
    "owner's login). Endpoints are in final verification against live Enphase "
    "accounts; one-click owner sign-in is coming."
)
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "api_key", "label": "Enphase API key", "secret": True},
    {"name": "client_id", "label": "App Client ID", "secret": False},
    {"name": "client_secret", "label": "App Client Secret", "secret": True},
    {"name": "system_id", "label": "System ID", "secret": False},
    {"name": "refresh_token", "label": "Refresh token (optional)", "secret": True},
]

OAUTH_TOKEN_URL = "https://api.enphaseenergy.com/oauth/token"
API_BASE = "https://api.enphaseenergy.com/api/v4"

# Token cache: client_id -> (access_token, refresh_token, expires_at). Module-
# scoped so a daily poll across many systems under one app reuses a single token.
_TOKEN_CACHE: dict[str, tuple[str, str | None, datetime]] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _basic_auth(config: dict) -> str:
    raw = f"{config['client_id']}:{config['client_secret']}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _get_token(config: dict) -> str:
    cid = str(config["client_id"])
    cached = _TOKEN_CACHE.get(cid)
    if cached is not None and cached[2] > _now():
        return cached[0]

    # Prefer the freshest rotated refresh_token (from the cache) over config's.
    refresh_token = (cached[1] if cached is not None else None) or config.get("refresh_token")
    if refresh_token:
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    elif config.get("username") and config.get("password"):
        # Partner "password" grant — the owner's Enlighten login.
        data = {
            "grant_type": "password",
            "username": config["username"],
            "password": config["password"],
        }
    else:
        raise InverterError(
            "Enphase needs a refresh_token (hosted OAuth) or username + password "
            "(partner flow) to obtain an access token."
        )

    try:
        resp = httpx.post(
            OAUTH_TOKEN_URL,
            data=data,
            headers={"Authorization": _basic_auth(config)},
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting Enphase OAuth: {exc}") from exc
    if resp.status_code in (401, 403):
        # Dead/rotated refresh token. Drop the cache + clear it from config so the
        # next call can fall back to the password grant instead of looping on a
        # dead token.
        _TOKEN_CACHE.pop(cid, None)
        if config.get("refresh_token"):
            config["refresh_token"] = None
        raise InverterAuthError("Enphase OAuth rejected the credentials (401/403).")
    if not resp.is_success:
        raise InverterError(
            f"Enphase token endpoint returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"Enphase token endpoint returned non-JSON: {exc}") from exc

    token = body.get("access_token")
    if not token:
        raise InverterError("Enphase token endpoint returned no access_token")
    ttl = int(body.get("expires_in") or 86400)
    new_refresh = body.get("refresh_token") or refresh_token
    _TOKEN_CACHE[cid] = (token, new_refresh, _now() + timedelta(seconds=max(ttl - 120, 120)))
    if new_refresh and new_refresh != config.get("refresh_token"):
        config["refresh_token"] = new_refresh
    return token


def _get(config: dict, path: str, params: dict | None = None) -> dict:
    require_fields(config, "api_key", "client_id", "client_secret")
    token = _get_token(config)
    p = dict(params or {})
    p["key"] = config["api_key"]  # Enphase requires the app key as ?key= on every call
    try:
        resp = httpx.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=p,
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting Enphase API: {exc}") from exc
    if resp.status_code in (401, 403):
        raise InverterAuthError("Enphase API rejected the token/key (401/403).")
    if resp.status_code == 429:
        raise InverterError("Enphase API rate limit reached (429) — try again shortly.")
    if not resp.is_success:
        raise InverterError(
            f"Enphase {path} returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"Enphase API returned non-JSON response: {exc}") from exc


def discover_sites(config: dict) -> list[dict]:
    """Account-level discovery — every system under the app key (Enphase /systems).

    Mirrors SolarEdge's one-key→all-sites cascade. system_size is in Wac.
    """
    body = _get(config, "/systems")
    out: list[dict] = []
    for s in (body.get("systems") or []):
        kw = s.get("system_size")
        peak = (float(kw) / 1000.0) if isinstance(kw, (int, float)) and kw and kw > 0 else None
        out.append({
            "site_id": s.get("system_id"),
            "name": s.get("name") or s.get("public_name"),
            "peak_power_kw": peak,
        })
    return out


def validate(config: dict) -> dict:
    require_fields(config, "api_key", "client_id", "client_secret", "system_id")
    # /summary confirms the system is reachable with these creds; it carries no
    # name, so best-effort pull the name from the systems list (don't fail if the
    # plan doesn't expose /systems).
    _get(config, f"/systems/{config['system_id']}/summary")
    name = None
    try:
        for s in (_get(config, "/systems").get("systems") or []):
            if str(s.get("system_id")) == str(config["system_id"]):
                name = s.get("name") or s.get("public_name")
                break
    except InverterError:
        pass
    return {"site_name": name}


def fetch_live(config: dict) -> dict | None:
    require_fields(config, "api_key", "client_id", "client_secret", "system_id")
    body = _get(config, f"/systems/{config['system_id']}/summary")
    cp = body.get("current_power")  # Enphase summary current_power is in W
    power_w = float(cp) if cp is not None else None
    as_of = None
    lr = body.get("last_report_at")  # unix epoch seconds
    if isinstance(lr, (int, float)) and lr > 0:
        as_of = datetime.utcfromtimestamp(lr).isoformat() + "Z"
    return {"current_power_w": power_w, "as_of": as_of}


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    require_fields(config, "api_key", "client_id", "client_secret", "system_id")
    # /energy_lifetime returns a `production` array (Wh per day) anchored at
    # `start_date`. Index i → start_date + i days.
    body = _get(
        config,
        f"/systems/{config['system_id']}/energy_lifetime",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    series = body.get("production") or []
    try:
        anchor = date.fromisoformat(str(body.get("start_date") or start.isoformat()))
    except Exception:  # noqa: BLE001
        anchor = start
    out: list[dict] = []
    for i, wh in enumerate(series):
        if wh is None:
            continue
        day = anchor + timedelta(days=i)
        if day < start or day > end:
            continue
        try:
            out.append({"day": day, "kwh": float(wh) / 1000.0})
        except (TypeError, ValueError):
            continue
    return out
