"""Fronius inverter source — Solar.web Query API.

╔════════════════════════════════════════════════════════════════════════════╗
║ STATUS — VERIFIED LIVE 2026-07-08 against a real Solar.web account (Ford   ║
║ got Beta API Management Portal access + minted a real key). US AVAILABILITY║
║ IS CONFIRMED — this key reads Bruce's real VT systems in production.       ║
║                                                                            ║
║ TWO real bugs were fixed vs the 2026-07-04 version (found by reading the   ║
║ Solar.web Query API manual v52.0 before assuming the earlier guess was     ║
║ right) and BOTH are now live-confirmed:                                    ║
║  1. HOST: swqapi.solarweb.com (no /swqapi path) is the documented-current  ║
║     host per the manual's own "Endpoint cleanup" changelog note — VERIFIED ║
║     200 OK against real pvsystems/flowdata/aggrdata calls. (The old         ║
║     api.solarweb.com/swqapi host also still answered when tested — likely  ║
║     a compat alias — but this is the documented-current one.)              ║
║  2. SMART-METER FALLBACK CHANNELS: PowerPV/EnergyProductionTotal need a     ║
║     Smart Meter; without one they're null forever and Solar.web returns    ║
║     PowerOutput/EnergyOutput instead — a system with no Smart Meter would  ║
║     have silently reported zero production forever without this fallback. ║
║     Bruce's two real systems (Waterford 150kW, Chester 150kW) happen to    ║
║     carry BOTH channels populated identically, so the fallback wasn't      ║
║     exercised by this test, but it's a correct, harmless no-op when the    ║
║     primary channel IS present, and a real fix for whichever future        ║
║     system doesn't have one.                                               ║
║                                                                            ║
║ CROSS-VALIDATED against a fully independent source: this adapter's         ║
║ fetch_daily for Waterford 150kW matched the SAME array's existing          ║
║ extension-scraped DailyGeneration rows (source=extension_pull) to within   ║
║ ~0.05% across 7 consecutive real days (2026-07-01..07). Wired live:         ║
║ Bruce's real key is connected to his real Waterford 150kW (array 2025) and ║
║ Chester 150kW (array 2024) via the existing discover/connect-account       ║
║ cascade — poller.py now pulls these server-side, no browser needed.        ║
║                                                                            ║
║ Note (2026-07-08, out of scope, flagged to backlog): Chester's live        ║
║ PowerPV (~88.6kW) exceeds its Solar.web-listed peakPower (60.8kW) — almost  ║
║ certainly stale nameplate metadata in Solar.web, not an adapter bug        ║
║ (confirmed via both raw API + our own discover endpoint returning the same ║
║ mismatched peak).                                                          ║
╚════════════════════════════════════════════════════════════════════════════╝

Auth: every request carries AccessKeyId + AccessKeyValue headers.
Config: {"access_key_id", "access_key_value", "pv_system_id"}.
"""
from __future__ import annotations

import os
from datetime import date

import httpx

from .base import TIMEOUT, InverterAuthError, InverterError, require_fields

CODE = "fronius"
LABEL = "Fronius (Solar.web)"
AVAILABLE = True
NOTE = (
    "Solar.web Query API is a paid business API — contact your Fronius sales "
    "rep for API Management Portal access to create a key (verified working "
    "for US arrays 2026-07-08)."
)
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "access_key_id", "label": "Access Key ID", "secret": False},
    {"name": "access_key_value", "label": "Access Key Value", "secret": True},
    {"name": "pv_system_id", "label": "PV System ID", "secret": False},
]

# Manual's own version-52 changelog: "Endpoint cleanup (swqapi.solarweb.com vs.
# the older api.solarweb.com/swqapi)" — every current example uses this host
# directly, no /swqapi path. Env-overridable in case the old host is retired
# on a different timeline than the docs suggest.
BASE = os.environ.get("FRONIUS_API_BASE", "https://swqapi.solarweb.com")


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


def _addr_str(addr: object) -> str | None:
    """Flatten Solar.web's polymorphic pvsystem `address` into one geocodable line.
    The API returns a dict {street,city,state,zip,country}; tolerate a bare string
    or None. Returns None when there's nothing usable."""
    if isinstance(addr, dict):
        return ", ".join(
            str(p) for p in (addr.get("street"), addr.get("city"),
                             addr.get("state"), addr.get("zip"), addr.get("country"))
            if p
        ) or None
    return (str(addr).strip() or None) if addr else None


def validate(config: dict) -> dict:
    require_fields(config, "access_key_id", "access_key_value", "pv_system_id")
    pid = config["pv_system_id"]
    body = _get(config, f"/pvsystems/{pid}")
    return {"site_name": body.get("name"), "peak_power": body.get("peakPower")}


def fetch_details(config: dict) -> dict:
    """One system's display metadata + its geocodable address (GET /pvsystems/{id}).
    Mirrors solaredge.site_details so the daily-pull location backfill can self-heal
    a Fronius array that was attached before/without a resolved address. Address is a
    single line ready for the geocoder, or None when Solar.web has none on file."""
    require_fields(config, "access_key_id", "access_key_value", "pv_system_id")
    pid = config["pv_system_id"]
    body = _get(config, f"/pvsystems/{pid}")
    return {
        "site_name": body.get("name"),
        "peak_power": body.get("peakPower"),
        "address": _addr_str(body.get("address")),
    }


def discover_systems(config: dict) -> list[dict]:
    """List every PV system this AccessKey can read (GET /pvsystems, paginated).

    Needs only access_key_id + access_key_value — the "paste one credential,
    attach every array" cascade, mirroring solaredge.discover_sites /
    locus.discover_sites. Returns [{pv_system_id, name, peak_power_kw,
    address}] with pv_system_id as a STRING (Fronius uses UUIDs, not ints).

    peakPower unit note: Solar.web metadata documents peakPower in Wp; we
    convert to kW for display parity with the other vendors. Display-only in
    the site picker — never used for billing. Confirm the magnitude against
    the first real account (the /pvsystems listing itself is grounded:
    exercised live 2026-07-04 via scripts/verify_inverter_apis).
    """
    require_fields(config, "access_key_id", "access_key_value")
    out: list[dict] = []
    offset = 0
    limit = 50
    for _page in range(40):                       # hard cap — never walk forever
        body = _get(config, "/pvsystems", params={"offset": offset, "limit": limit})
        systems = body.get("pvSystems") or []
        for s in systems:
            sid = s.get("pvSystemId") or s.get("id")
            if not sid:
                continue
            peak = s.get("peakPower")
            peak_kw = None
            if peak not in (None, ""):
                try:
                    peak_kw = round(float(peak) / 1000.0, 2)
                except (TypeError, ValueError):
                    peak_kw = None
            out.append({
                "pv_system_id": str(sid),
                "name": (s.get("name") or "").strip() or f"PV system {sid}",
                "peak_power_kw": peak_kw,
                "address": _addr_str(s.get("address")),
            })
        if len(systems) < limit:
            break
        offset += limit
    return out


def _channels(body: dict) -> list[dict]:
    # flowdata nests channels under "data"; tolerate a flat shape too.
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return data.get("channels") or []


def _channel_value(channels: list[dict], *names: str) -> float | None:
    """First non-null value among `names`, in priority order. Per the Solar.web
    manual, several channels are mutually exclusive depending on whether the
    system has a Smart Meter (e.g. PowerPV/EnergyProductionTotal need one;
    PowerOutput/EnergyOutput are what's returned INSTEAD when there isn't one)
    — a system without a Smart Meter would authenticate fine and return 200s
    forever with the primary channel stuck at null, silently reporting zero
    production for a healthy array. Trying the fallback closes that gap."""
    by_name = {ch.get("channelName"): ch.get("value") for ch in channels}
    for name in names:
        value = by_name.get(name)
        if value is not None:
            return float(value)
    return None


def fetch_live(config: dict) -> dict | None:
    require_fields(config, "access_key_id", "access_key_value", "pv_system_id")
    pid = config["pv_system_id"]
    body = _get(config, f"/pvsystems/{pid}/flowdata")
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    # PowerPV needs a Smart Meter; without one Solar.web returns PowerOutput instead.
    power_w = _channel_value(_channels(body), "PowerPV", "PowerOutput")
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
        # EnergyProductionTotal needs a Smart Meter; without one Solar.web
        # returns EnergyOutput instead (same fallback pairing as flowdata).
        wh = _channel_value(entry.get("channels") or [], "EnergyProductionTotal", "EnergyOutput")
        if wh is None:
            continue
        out.append({"day": day, "kwh": wh / 1000.0})
    return out
