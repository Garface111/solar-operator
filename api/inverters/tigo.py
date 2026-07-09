"""Tigo inverter/optimizer source — Energy Intelligence (EI) REST API v3.

╔════════════════════════════════════════════════════════════════════════════╗
║ LOUD CAVEAT — UNVERIFIED AGAINST A LIVE TIGO ACCOUNT.                       ║
║                                                                            ║
║ Login + base + the systems/summary endpoints are grounded to Tigo's        ║
║ published REST API v3 (api2.tigoenergy.com, support.tigoenergy.com docs +   ║
║ the community python clients, 2026-06-21). Telemetry FIELD names are parsed ║
║ DEFENSIVELY and best-effort until a real Tigo account confirms the JSON     ║
║ shapes — same posture as SMA. NOTE: the Tigo API requires the owner's       ║
║ account to have a PREMIUM data subscription.                                ║
╚════════════════════════════════════════════════════════════════════════════╝

Auth: POST /api/v3/users/login with HTTP Basic (username:password) → an auth
token; later requests send `Authorization: Bearer {token}`. Token is cached.
Config: {username, password, system_id?}.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta

import httpx

from .base import TIMEOUT, InverterAuthError, InverterError, require_fields

CODE = "tigo"
LABEL = "Tigo (Energy Intelligence)"
AVAILABLE = True
NOTE = (
    "Tigo connects with your Energy Intelligence (EI) login. Your Tigo account "
    "needs a Premium data subscription for API access. Endpoints are in final "
    "verification against live Tigo accounts."
)
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "username", "label": "Tigo EI username", "secret": False},
    {"name": "password", "label": "Tigo EI password", "secret": True},
    {"name": "system_id", "label": "System ID (optional)", "secret": False},
]

BASE = "https://api2.tigoenergy.com/api/v3"

# Token cache keyed by username. Tigo tokens are long-lived; re-login on 401.
_TOKEN_CACHE: dict[str, str] = {}


def _login(config: dict) -> str:
    require_fields(config, "username", "password")
    user = str(config["username"])
    cached = _TOKEN_CACHE.get(user)
    if cached:
        return cached
    basic = base64.b64encode(f"{user}:{config['password']}".encode()).decode()
    try:
        resp = httpx.post(
            f"{BASE}/users/login",
            headers={"Authorization": f"Basic {basic}"},
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting Tigo login: {exc}") from exc
    if resp.status_code in (401, 403):
        raise InverterAuthError("Tigo rejected the login (401/403).")
    if not resp.is_success:
        raise InverterError(f"Tigo login returned {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"Tigo login returned non-JSON: {exc}") from exc
    token = (
        (body.get("user") or {}).get("auth")
        or body.get("auth")
        or body.get("token")
    )
    if not token:
        raise InverterError("Tigo login returned no auth token")
    _TOKEN_CACHE[user] = token
    return token


def _get(config: dict, path: str, params: dict | None = None) -> dict:
    token = _login(config)
    try:
        resp = httpx.get(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting Tigo API: {exc}") from exc
    if resp.status_code in (401, 403):
        # Token may have expired — drop it so the next call re-logs-in.
        _TOKEN_CACHE.pop(str(config.get("username")), None)
        raise InverterAuthError("Tigo API rejected the token (401/403).")
    if not resp.is_success:
        raise InverterError(f"Tigo {path} returned {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"Tigo API returned non-JSON: {exc}") from exc


def _systems(config: dict) -> list[dict]:
    body = _get(config, "/systems")
    if isinstance(body, dict):
        return body.get("systems") or body.get("results") or []
    return body if isinstance(body, list) else []


def discover_sites(config: dict) -> list[dict]:
    out: list[dict] = []
    for s in _systems(config):
        out.append({
            "site_id": s.get("system_id") or s.get("id"),
            "name": s.get("name"),
            "peak_power_kw": None,  # Tigo systems list doesn't reliably carry nameplate
        })
    return out


def validate(config: dict) -> dict:
    require_fields(config, "username", "password")
    systems = _systems(config)
    sid = config.get("system_id")
    name = None
    for s in systems:
        if not sid or str(s.get("system_id") or s.get("id")) == str(sid):
            name = s.get("name")
            break
    return {"site_name": name}


def _summary(config: dict) -> dict:
    require_fields(config, "username", "password", "system_id")
    body = _get(config, f"/systems/{config['system_id']}/summary")
    s = body.get("summary") if isinstance(body, dict) else None
    return s if isinstance(s, dict) else (body if isinstance(body, dict) else {})


def fetch_live(config: dict) -> dict | None:
    require_fields(config, "username", "password", "system_id")
    s = _summary(config)
    # last_power_dc is the most-recent DC power (W) on the Tigo summary.
    val = s.get("last_power_dc")
    if val is None:
        val = s.get("power_dc") or s.get("last_power")
    try:
        power_w = float(val) if val is not None else None
    except (TypeError, ValueError):
        power_w = None
    as_of = s.get("last_data_received") or s.get("last_updated")
    return {"current_power_w": power_w, "as_of": str(as_of) if as_of else None}


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    require_fields(config, "username", "password", "system_id")
    # Daily aggregate. Shape varies, so parse defensively and skip what doesn't fit.
    # A real API failure (auth/5xx/network) propagates as InverterError -- it must
    # never read as "zero production that day" (Ford, 2026-07-08: "find every
    # instance of us intentionally sabotaging our own reliability").
    body = _get(config, "/data/aggregate", params={
        "system_id": config["system_id"],
        "start": start.isoformat(), "end": end.isoformat(),
        "level": "day", "param": "energy_dc",
    })
    rows = []
    if isinstance(body, dict):
        rows = body.get("results") or body.get("data") or body.get("aggregate") or []
    elif isinstance(body, list):
        rows = body
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        raw_day = r.get("datetime") or r.get("date") or r.get("day")
        wh = r.get("energy_dc")
        if wh is None:
            wh = r.get("energy") or r.get("value")
        if raw_day is None or wh is None:
            continue
        try:
            d = date.fromisoformat(str(raw_day)[:10])
            kwh = float(wh) / 1000.0  # Tigo energy values are Wh
        except (TypeError, ValueError):
            continue
        if start <= d <= end:
            out.append({"day": d, "kwh": kwh})
    return out
