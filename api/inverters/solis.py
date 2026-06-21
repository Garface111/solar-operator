"""Solis / Ginlong inverter source — SolisCloud Platform API (soliscloud.com:13333).

╔════════════════════════════════════════════════════════════════════════════╗
║ LOUD CAVEAT — UNVERIFIED AGAINST A LIVE SOLIS ACCOUNT.                      ║
║                                                                            ║
║ Auth + the station-list endpoint are grounded to SolisCloud's published    ║
║ HMAC contract (oss.soliscloud.com docs, 2026-06-21). The telemetry FIELD    ║
║ names (current power / today energy / units) are parsed DEFENSIVELY and are ║
║ best-effort until a real Solis account confirms the exact JSON shapes —     ║
║ same posture as the SMA adapter. Needs a SolisCloud API key (KeyId +        ║
║ KeySecret, requested in-portal under API Management) on a real account.     ║
╚════════════════════════════════════════════════════════════════════════════╝

Auth: HMAC-SHA1 per request. Authorization = "API {KeyId}:{Sign}" where
  Sign = base64(HmacSHA1(KeySecret, VERB + "\n" + Content-MD5 + "\n" +
                         Content-Type + "\n" + Date(GMT) + "\n" + Resource))
Content-MD5 = base64(md5(body)). All calls are POST application/json.
Config: {key_id, key_secret, station_id?}.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from .base import TIMEOUT, InverterAuthError, InverterError, require_fields

CODE = "solis"
LABEL = "Solis (SolisCloud)"
AVAILABLE = True
NOTE = (
    "Solis connects through the SolisCloud Platform API. Request an API key "
    "(KeyId + KeySecret) in SolisCloud under API Management, then enter it here. "
    "Endpoints are in final verification against live Solis accounts."
)
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "key_id", "label": "SolisCloud KeyId", "secret": False},
    {"name": "key_secret", "label": "SolisCloud KeySecret", "secret": True},
    {"name": "station_id", "label": "Station ID (optional)", "secret": False},
]

BASE = "https://www.soliscloud.com:13333"
CONTENT_TYPE = "application/json;charset=UTF-8"


def _b64_md5(body: bytes) -> str:
    return base64.b64encode(hashlib.md5(body).digest()).decode()


def _gmt_now() -> str:
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


def _post(config: dict, resource: str, payload: dict) -> dict:
    require_fields(config, "key_id", "key_secret")
    body = json.dumps(payload).encode("utf-8")
    content_md5 = _b64_md5(body)
    date_str = _gmt_now()
    string_to_sign = f"POST\n{content_md5}\n{CONTENT_TYPE}\n{date_str}\n{resource}"
    sign = base64.b64encode(
        hmac.new(str(config["key_secret"]).encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    headers = {
        "Content-MD5": content_md5,
        "Content-Type": CONTENT_TYPE,
        "Date": date_str,
        "Authorization": f"API {config['key_id']}:{sign}",
    }
    try:
        resp = httpx.post(f"{BASE}{resource}", content=body, headers=headers, timeout=TIMEOUT)
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting SolisCloud: {exc}") from exc
    if resp.status_code in (401, 403):
        raise InverterAuthError("SolisCloud rejected the API key/signature (401/403).")
    if not resp.is_success:
        raise InverterError(f"SolisCloud {resource} returned {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"SolisCloud returned non-JSON: {exc}") from exc
    # SolisCloud envelope: {"success": true/false, "code": "0", "data": {...}, "msg": ...}
    if data.get("success") is False or (data.get("code") not in (None, "0", 0, "1", 1)):
        # code "1"/"0" both seen as OK across versions; treat explicit success:false as auth/biz error.
        if str(data.get("code")) in ("B0009", "403", "401"):
            raise InverterAuthError(f"SolisCloud auth error: {data.get('msg')}")
    return data.get("data") if isinstance(data.get("data"), (dict, list)) else data


def _power_to_w(value, unit) -> float | None:
    """Solis returns power as a number + a separate unit string ('W'/'kW'/'MW')."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    u = (str(unit or "")).strip().lower()
    if u in ("kw",):
        return v * 1000.0
    if u in ("mw",):
        return v * 1_000_000.0
    return v  # assume W when unit absent/unknown


def _energy_to_kwh(value, unit) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    u = (str(unit or "")).strip().lower()
    if u in ("wh",):
        return v / 1000.0
    if u in ("mwh",):
        return v * 1000.0
    return v  # assume kWh


def _stations(config: dict) -> list[dict]:
    data = _post(config, "/v1/api/userStationList", {"pageNo": 1, "pageSize": 100})
    if isinstance(data, dict):
        page = data.get("page") or {}
        return page.get("records") or data.get("records") or []
    return data or []


def discover_sites(config: dict) -> list[dict]:
    out: list[dict] = []
    for s in _stations(config):
        cap = s.get("capacity")
        out.append({
            "site_id": s.get("id") or s.get("stationId"),
            "name": s.get("stationName") or s.get("name"),
            "peak_power_kw": float(cap) if isinstance(cap, (int, float)) and cap else None,
        })
    return out


def validate(config: dict) -> dict:
    require_fields(config, "key_id", "key_secret")
    stations = _stations(config)  # a successful signed call confirms the credentials
    sid = config.get("station_id")
    name = None
    for s in stations:
        if not sid or str(s.get("id") or s.get("stationId")) == str(sid):
            name = s.get("stationName") or s.get("name")
            break
    return {"site_name": name}


def _station_detail(config: dict) -> dict:
    require_fields(config, "key_id", "key_secret", "station_id")
    d = _post(config, "/v1/api/stationDetail", {"id": config["station_id"]})
    return d if isinstance(d, dict) else {}


def fetch_live(config: dict) -> dict | None:
    require_fields(config, "key_id", "key_secret", "station_id")
    d = _station_detail(config)
    # 'power' (+ 'powerStr' unit) is the station's current AC power on SolisCloud.
    power_w = _power_to_w(d.get("power"), d.get("powerStr"))
    if power_w is None:
        power_w = _power_to_w(d.get("pac"), d.get("pacStr"))
    return {"current_power_w": power_w, "as_of": None}


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    require_fields(config, "key_id", "key_secret", "station_id")
    # /v1/api/stationDayEnergyList returns per-day energy for a station over a range.
    # Shapes vary by SolisCloud version, so parse defensively and skip what doesn't fit.
    try:
        data = _post(config, "/v1/api/stationDayEnergyList", {
            "id": config["station_id"],
            "pageNo": 1, "pageSize": 100,
            "startTime": start.isoformat(), "endTime": end.isoformat(),
        })
    except InverterError:
        return []
    records = []
    if isinstance(data, dict):
        records = (data.get("page") or {}).get("records") or data.get("records") or []
    elif isinstance(data, list):
        records = data
    out: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        raw_day = r.get("dataTimestamp") or r.get("time") or r.get("date")
        kwh = _energy_to_kwh(r.get("energy") or r.get("dayEnergy"), r.get("energyStr") or r.get("dayEnergyStr"))
        if raw_day is None or kwh is None:
            continue
        try:
            if isinstance(raw_day, str) and len(raw_day) >= 10 and "-" in raw_day:
                d = date.fromisoformat(raw_day[:10])
            else:
                d = datetime.utcfromtimestamp(int(raw_day) / 1000).date()
        except (TypeError, ValueError):
            continue
        if start <= d <= end:
            out.append({"day": d, "kwh": kwh})
    return out
