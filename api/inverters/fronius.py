"""Fronius inverter source — Solar.web Query API.

╔════════════════════════════════════════════════════════════════════════════╗
║ STATUS — partially verified against the LIVE API (2026-07-04).              ║
║                                                                            ║
║ Confirmed live via scripts/verify_inverter_apis: the auth + request path   ║
║ reach api.solarweb.com/swqapi correctly (a valid AccessKey → 200; a bad    ║
║ one → clean 401 responseError 1102). What is STILL unverified against a    ║
║ live system is the response PARSING (flowdata PowerPV / aggrdata           ║
║ EnergyProductionTotal in _channels/fetch_daily) — Fronius RETIRED its      ║
║ public demo system, so a demo key now authenticates but has no PV system   ║
║ to pull data from. Verifying the shapes needs a real Solar.web account     ║
║ with the Query API enabled (see HANDOFF_API_VERIFICATION.md).              ║
║                                                                            ║
║ The Query API is a CHARGEABLE business API (pay-per-data-point) and per    ║
║ Fronius's public country list is NOT self-serve in the USA. Per-account    ║
║ US enablement via pv-support-usa@fronius.com is claimed but UNVERIFIED —   ║
║ do not assert US availability until Fronius confirms it in writing. US     ║
║ arrays may instead need the local Solar API (LAN) path (future work).      ║
╚════════════════════════════════════════════════════════════════════════════╝

Auth: every request carries AccessKeyId + AccessKeyValue headers.
Config: {"access_key_id", "access_key_value", "pv_system_id"}.
"""
from __future__ import annotations

from datetime import date

import httpx

from .base import TIMEOUT, InverterAuthError, InverterError, require_fields

CODE = "fronius"
LABEL = "Fronius (Solar.web)"
AVAILABLE = True
NOTE = (
    "Solar.web Query API is a paid business API and is not currently offered "
    "in the USA — US arrays may need the local LAN path."
)
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "access_key_id", "label": "Access Key ID", "secret": False},
    {"name": "access_key_value", "label": "Access Key Value", "secret": True},
    {"name": "pv_system_id", "label": "PV System ID", "secret": False},
]

BASE = "https://api.solarweb.com/swqapi"


def _headers(config: dict) -> dict:
    return {
        "AccessKeyId": str(config["access_key_id"]),
        "AccessKeyValue": str(config["access_key_value"]),
    }


def _get(config: dict, path: str, params: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    try:
        resp = httpx.get(url, headers=_headers(config), params=params, timeout=TIMEOUT)
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting Solar.web: {exc}") from exc
    if resp.status_code in (401, 403):
        raise InverterAuthError(
            "Solar.web rejected the access key (401/403). Check AccessKeyId / "
            "AccessKeyValue and that the key has access to this PV system."
        )
    if not resp.is_success:
        raise InverterError(
            f"Solar.web {path} returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is an inverter error
        raise InverterError(f"Solar.web returned non-JSON response: {exc}") from exc


def validate(config: dict) -> dict:
    require_fields(config, "access_key_id", "access_key_value", "pv_system_id")
    pid = config["pv_system_id"]
    body = _get(config, f"/pvsystems/{pid}")
    return {"site_name": body.get("name"), "peak_power": body.get("peakPower")}


def _channels(body: dict) -> list[dict]:
    # flowdata nests channels under "data"; tolerate a flat shape too.
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return data.get("channels") or []


def fetch_live(config: dict) -> dict | None:
    require_fields(config, "access_key_id", "access_key_value", "pv_system_id")
    pid = config["pv_system_id"]
    body = _get(config, f"/pvsystems/{pid}/flowdata")
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    power_w = None
    for ch in _channels(body):
        if ch.get("channelName") == "PowerPV":
            value = ch.get("value")
            power_w = float(value) if value is not None else None
            break
    return {"current_power_w": power_w, "as_of": data.get("logDateTime")}


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    require_fields(config, "access_key_id", "access_key_value", "pv_system_id")
    pid = config["pv_system_id"]
    body = _get(
        config,
        f"/pvsystems/{pid}/aggrdata",
        params={"from": start.isoformat(), "to": end.isoformat()},
    )
    out: list[dict] = []
    for entry in body.get("data") or []:
        raw_date = entry.get("logDateTime") or ""
        try:
            day = date.fromisoformat(raw_date[:10])
        except (ValueError, TypeError):
            continue
        wh = None
        for ch in entry.get("channels") or []:
            if ch.get("channelName") == "EnergyProductionTotal":
                wh = ch.get("value")
                break
        if wh is None:
            continue
        out.append({"day": day, "kwh": float(wh) / 1000.0})
    return out
