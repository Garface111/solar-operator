"""Fronius Solar.web (provider "fronius") — server-side live capture.

Ported from solarweb_content.js. Everything is a first-party JSON API on
www.solarweb.com (session-cookie authed), so context.request carries the warm
session. A nice win over the extension: reading the API directly SIDESTEPS the
lazy-load-tile partial-capture bug that plagued the DOM scrape (only ~14/20
tiles rendered) — GetActualPvSystemData returns every device in one series.

Live per-inverter power comes from GetActualPvSystemData (the REALTIME tab's own
endpoint) — a direct read, so a ~4-min cycle stays well under 5 minutes.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .base import CaptureRequest, ScrapeResult

# Fronius's FormatedDateTimeStamp is the account's LOCAL time (ET for VT arrays).
_TZ = ZoneInfo("America/New_York")

log = logging.getLogger("harvester.fronius")

BASE = "https://www.solarweb.com"


class FroniusVendor:
    provider = "fronius"

    async def login_url(self, creds) -> str:
        return BASE + "/"

    async def is_logged_in(self, page) -> bool:
        try:
            # max_redirects=0 is essential: a logged-OUT session 302s this probe to
            # the WSO2 login page, and following that redirect returns 200 — a false
            # "logged in" that makes us skip login entirely and scrape an empty
            # session ("no PV systems"). Not following it: 302 → not ok → re-login.
            r = await page.context.request.get(
                f"{BASE}/Messages/GetUnreadMessageCountForUser?_=0",
                headers={"Accept": "application/json"}, max_redirects=0)
            return r.ok
        except Exception:
            return False

    async def scrape(self, page, context, creds) -> ScrapeResult:
        req = context.request

        async def get(path):
            r = await req.get(f"{BASE}{path}", headers={"Accept": "application/json"})
            if not r.ok:
                return None
            try:
                return await r.json()
            except Exception:
                return None

        listing = await get("/PvSystems/GetPvSystemsForListView?_=0")
        systems = (listing or {}).get("data") or []
        if not systems:
            raise RuntimeError("Fronius: no PV systems (session invalid?)")

        # Authoritative SITE live power in one call (WATTS per system).
        actual = await get("/ActualData/GetActualValues?withOnlineState=False&_=0")
        site_power = {}
        for a in (actual or []):
            pid = a.get("PvSystemId")
            if pid is not None and isinstance(a.get("TotalPower"), (int, float)):
                site_power[str(pid)] = a["TotalPower"]

        sites = []
        for s in systems:
            pid = s.get("PvSystemId") or s.get("pvSystemId")
            if not pid:
                continue
            try:
                sites.append(await self._system(get, s, str(pid), site_power.get(str(pid))))
            except Exception as exc:
                log.warning("Fronius system %s failed: %s", pid, exc)

        sites = [s for s in sites if s]
        if not sites:
            raise RuntimeError("Fronius: no systems captured")
        return ScrapeResult(
            requests=[CaptureRequest(
                path="/v1/array-owners/inverter-capture",
                body={"provider": "fronius", "sites": sites},
                note=f"fronius {len(sites)} system(s)")],
            summary=f"{len(sites)} system(s)",
        )

    async def _system(self, get, s: dict, pid: str, site_power_w) -> dict:
        name = s.get("PvSystemName") or s.get("Name") or f"Fronius {pid}"
        inverters = await self._inverters(get, pid)
        # Apply the authoritative live per-inverter power (REALTIME endpoint).
        rt = await get(f"/ActualData/GetActualPvSystemData?pvSystemId={pid}&_=0")
        by_name, ts = self._realtime(rt)
        for iv in inverters:
            w = by_name.get(iv["name"])
            if w is not None:
                iv["current_power_w"] = w
                iv["last_report"] = ts

        # Prefer the list view's own site totals; fall back to summing inverters.
        energy = s.get("EnergyTodayInkWh")
        if not isinstance(energy, (int, float)):
            energy = round(sum(iv["energy_today_kwh"] for iv in inverters
                               if iv.get("energy_today_kwh")), 3) if inverters else None
        power = site_power_w
        if power is None:
            power = sum(iv["current_power_w"] for iv in inverters
                        if iv.get("current_power_w")) or None
        peak = None
        try:
            if energy and s.get("KwhPerKwp"):
                peak = round(float(energy) / float(s["KwhPerKwp"]), 1)
        except (TypeError, ValueError, ZeroDivisionError):
            peak = None
        if peak is None:
            peak = sum(iv["peak_power_kw"] for iv in inverters
                       if iv.get("peak_power_kw")) or None
        return {
            "site_id": str(pid),
            "name": name,
            "peak_power_kw": round(peak, 2) if peak else None,
            "inverter_count": len(inverters) or s.get("InverterCount"),
            "energy_today_kwh": round(energy, 3) if isinstance(energy, (int, float)) else None,
            "current_power_w": power,
            "error_count_today": s.get("ErrorCntToday") or 0,
            "status": "producing" if (power or 0) > 0 else "idle",
            "last_report": ts,
            "inverters": inverters,
        }

    async def _inverters(self, get, pid) -> list[dict]:
        """Device identity + today's energy (integrated from the devwork chart).
        Ported from captureInverters (solarweb_content.js:232)."""
        now = datetime.utcnow()
        dq = f"year={now.year}&month={now.month}&day={now.day}&interval=day"
        meta = await get(f"/Chart/GetAnalysisChart?pvSystemId={pid}&{dq}&compareView=false&kwhkwpView=false&_=0")
        devices = (((meta or {}).get("deviceChannels") or {}).get("devices")) or []
        inv = [d for d in devices if d and d.get("isActiveDevice") is not False
               and not d.get("isMeterOrConsumerDevice") and d.get("deviceId")]
        if not inv:
            return []
        ids = [d["deviceId"] for d in inv]
        dev_qs = "&".join(f"devices={i}" for i in ids)
        data = await get(f"/Chart/GetAnalysisChart?pvSystemId={pid}&{dq}&channels=devwork&{dev_qs}"
                         f"&compareView=false&kwhkwpView=false&_=0")
        series = (((data or {}).get("settings") or {}).get("series")) or []
        kwh_by, peak_by = {}, {}
        for ser in series:
            nm = str(ser.get("name") or "")
            if not re.search(r"Total Power\s*\|", nm):
                continue
            disp = re.sub(r"^.*\|\s*", "", nm).strip()
            # devwork is in WATTS → normalize to kW before integrating.
            kw = [[p[0], p[1] / 1000.0] for p in (ser.get("data") or [])
                  if isinstance(p, list) and len(p) == 2 and isinstance(p[1], (int, float))]
            kwh_by[disp] = _integrate_kwh(kw)
            peak_by[disp] = max((p[1] for p in kw), default=None)

        out = []
        for i, d in enumerate(inv):
            disp = str(d.get("displayName") or f"Inverter {i+1}")
            out.append({
                "serial": str(d["deviceId"]),
                "name": disp,
                "model": disp,
                "nameplate_kw": _nameplate(disp),
                "energy_today_kwh": kwh_by.get(disp),
                "peak_power_kw": peak_by.get(disp),
                "current_power_w": None,               # filled from realtime
            })
        return out

    @staticmethod
    def _realtime(r) -> tuple[dict, str | None]:
        """Parse GetActualPvSystemData → {displayName: watts}, ts. Honors the
        auto-scaled unit (W/kW/MW). Ported from getRealtimePerInverter:386."""
        by_name = {}
        data = (((r or {}).get("series") or [{}])[0] or {}).get("data") or []
        for d in data:
            nm = str((d or {}).get("name") or "").strip()
            c = (d or {}).get("custom") or {}
            p = c.get("power")
            if not nm or not isinstance(p, (int, float)) or p < 0:
                continue
            u = str(c.get("unit") or "kW").strip().lower()
            mult = 1 if u == "w" else 1e6 if u == "mw" else 1e9 if u == "gw" else 1000
            by_name[nm] = round(p * mult)
        ts = None
        try:
            stamp = (((r or {}).get("SensorData") or [{}])[0] or {}).get("FormatedDateTimeStamp")
            if stamp:
                # The stamp is account-LOCAL (ET). Treat it as ET and convert to UTC,
                # else the backend reads it as UTC and the data looks hours stale
                # (ET is UTC-4/5 → the "Source 4h old" symptom). Clamp a future parse.
                dt_utc = datetime.strptime(stamp, "%m/%d/%Y %I:%M %p").replace(tzinfo=_TZ).astimezone(timezone.utc)
                now_utc = datetime.now(timezone.utc)
                ts = (now_utc if dt_utc > now_utc else dt_utc).isoformat()
        except Exception:
            ts = None
        return by_name, ts or datetime.now(timezone.utc).isoformat()


def _integrate_kwh(kw_series: list) -> float | None:
    """Trapezoidal integration of a [[ts_ms, kW], …] power curve → kWh."""
    pts = sorted(kw_series, key=lambda p: p[0])
    if len(pts) < 2:
        return None
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        dt_h = (b[0] - a[0]) / 3_600_000.0
        total += (a[1] + b[1]) / 2.0 * dt_h
    return round(total, 3)


def _nameplate(model: str) -> float | None:
    """Parse Fronius display names like 'Primo 12.5-1 208-240 (7)' → 12.5.

    Prefer an explicit '…kW' / '…k ' token; else the first number after a family
    word (Primo/Symo/Galvo/…). The old ``\\d+\\s*k`` pattern missed '12.5-1'
    (no letter k), leaving nameplate_kw NULL on Waterford Primos.
    """
    if not model:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*k(?:w|wp)?\b", model, re.I)
    if m:
        return float(m.group(1))
    m = re.search(
        r"(?:Primo|Symo|Galvo|Eco|IG|Pr|Sy)\s+(\d+(?:\.\d+)?)",
        model,
        re.I,
    )
    if m:
        return float(m.group(1))
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", model)
    return float(m.group(1)) if m else None
