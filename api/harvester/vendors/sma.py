"""SMA Sunny Portal / ennexOS (provider "sma") — server-side live capture.

Ported from sunnyportal_content.js. Auth is a Keycloak OAuth Bearer JWT the SPA
drops into localStorage['access_token'] after SSO (login.sma.energy, a separate
origin). All data is a clean Bearer JSON API at uiapi.sunnyportal.com — no
cookies, CORS-open — so once we lift the token we read it with context.request.

Live power is SITE-LEVEL only (the per-device pvPower drifted to null); the
backend allocates the site watts across inverters by energy share. A ~4-min
cycle keeps data under the 5-minute SLA.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .base import CaptureRequest, ScrapeResult

log = logging.getLogger("harvester.sma")

PORTAL = "https://ennexos.sunnyportal.com/"
UIAPI = "https://uiapi.sunnyportal.com"
TZ = ZoneInfo("America/New_York")
# Non-inverter devices to exclude (they'd show as permanently-0kW phantoms).
_NON_INVERTER = ("edmm", "data manager", "home manager", "energy meter",
                 "meter", "webconnect", "gateway", "cluster controller")


class SMAVendor:
    provider = "sma"

    async def login_url(self, creds) -> str:
        return PORTAL

    async def _token(self, page) -> str | None:
        try:
            return await page.evaluate("() => localStorage.getItem('access_token')")
        except Exception:
            return None

    async def is_logged_in(self, page) -> bool:
        # Warm session: the SPA boots and drops access_token. Give it a moment.
        import asyncio
        for _ in range(8):
            if await self._token(page):
                return True
            await asyncio.sleep(1.0)
        return False

    async def scrape(self, page, context, creds) -> ScrapeResult:
        import asyncio
        # The token appears only after the heavy SPA boots + Keycloak completes.
        tok = None
        for _ in range(30):
            tok = await self._token(page)
            if tok:
                break
            await asyncio.sleep(1.0)
        if not tok:
            raise RuntimeError("SMA access_token never appeared in localStorage")

        req = context.request
        H = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}

        async def get(path):
            r = await req.get(f"{UIAPI}{path}", headers=H)
            if r.status == 401:
                raise RuntimeError("SMA uiapi 401 — token expired")
            if not r.ok:
                return None
            try:
                return await r.json()
            except Exception:
                return None

        async def post(path, body):
            r = await req.post(f"{UIAPI}{path}", headers={**H, "Content-Type": "application/json"}, data=body)
            if not r.ok:
                return None
            try:
                return await r.json()
            except Exception:
                return None

        # Resolve ALL plants (never short-circuit on the URL's plant id).
        nav = await get("/api/v1/navigation") or []
        plants = [{"id": str(x.get("componentId")), "name": x.get("name")}
                  for x in (nav if isinstance(nav, list) else [])
                  if x.get("componentType") == "Plant" and x.get("componentId")]
        if not plants:
            menu = await get("/api/v1/navigation/menuitems") or {}
            cid = menu.get("componentId")
            if cid:
                plants = [{"id": str(cid), "name": menu.get("name")}]
        if not plants:
            raise RuntimeError("no SMA plants resolvable")

        today = datetime.now(TZ).strftime("%Y-%m-%d")
        sites = []
        for p in plants:
            try:
                sites.append(await self._plant(get, post, p, today))
            except Exception as exc:                 # isolate per-plant failures
                log.warning("SMA plant %s failed: %s", p.get("id"), exc)

        if not sites:
            raise RuntimeError("SMA: no plants captured")
        return ScrapeResult(
            requests=[CaptureRequest(
                path="/v1/array-owners/inverter-capture",
                body={"provider": "sma", "sites": sites},
                note=f"sma {len(sites)} plant(s)")],
            summary=f"{len(sites)} plant(s)",
        )

    async def _plant(self, get, post, p, today) -> dict:
        pid, name = p["id"], p.get("name")
        devices = await get(f"/api/v1/overview/{pid}/devices?todayDate={today}") or []
        inverters = []
        energy_today = 0.0
        for d in (devices if isinstance(devices, list) else []):
            if d.get("componentType") != "Device":
                continue
            blob = f"{d.get('product','')} {d.get('name','')}".lower()
            if any(k in blob for k in _NON_INVERTER):
                continue
            wh = d.get("totWhOutToday")
            kwh = round(wh / 1000.0, 3) if isinstance(wh, (int, float)) else None
            if kwh:
                energy_today += kwh
            inverters.append({
                "serial": str(d.get("serial") or d.get("componentId")),
                "name": d.get("name"),
                "model": d.get("product"),
                "nameplate_kw": _nameplate(d.get("product") or d.get("name") or ""),
                "energy_today_kwh": kwh,
                "current_power_w": None,             # per-device live is null; backend allocates
                "status": "producing" if (kwh or 0) > 0 else "idle",
            })

        # Site live power (the gauge the portal itself reads).
        power_w = last_report = None
        gauge = await get(f"/api/v1/widgets/gauge/power?componentId={pid}&type=PvProduction")
        if isinstance(gauge, dict) and isinstance(gauge.get("value"), (int, float)):
            power_w = gauge["value"]
            last_report = gauge.get("timestamp")

        daily = await self._site_daily(post, pid)
        return {
            "site_id": str(pid),
            "name": name or f"SMA plant {pid}",
            "peak_power_kw": round(sum(i["nameplate_kw"] for i in inverters
                                       if i["nameplate_kw"]), 2) or None,
            "inverter_count": len(inverters),
            "energy_today_kwh": round(energy_today, 2) if inverters else None,
            "current_power_w": power_w,
            "status": "producing" if (power_w or 0) > 0 else "idle",
            "last_report": last_report,
            "daily": daily,
            "inverters": inverters,
        }

    @staticmethod
    async def _site_daily(post, pid) -> list[dict]:
        """~7 days of site daily kWh for instant graph backfill (best-effort)."""
        end = datetime.now(TZ)
        begin = end - timedelta(days=8)
        body = {"queryItems": [{"componentId": str(pid),
                                "channelId": "Measurement.Metering.TotWhOut.Pv",
                                "resolution": "OneDay", "timezone": "America/New_York",
                                "aggregate": "Dif"}],
                "dateTimeBegin": begin.strftime("%Y-%m-%dT00:00:00Z"),
                "dateTimeEnd": end.strftime("%Y-%m-%dT23:59:59Z")}
        data = await post("/api/v1/measurements/search", body)
        out = []
        # Pick the series whose componentId matches the plant (avoid one inverter's series).
        for series in (data or []):
            if str(series.get("componentId")) not in (str(pid), "None"):
                continue
            for v in (series.get("values") or series.get("set") or []):
                ts, val = v.get("time") or v.get("timestamp"), v.get("value")
                if ts and isinstance(val, (int, float)):
                    out.append({"date": str(ts)[:10], "kwh": round(val / 1000.0, 3)})
        return out


def _nameplate(product: str) -> float | None:
    """Parse a nameplate kW hint from an SMA model string, e.g. 'STP 24kTL-US-10'→24."""
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*k", product, re.I)
    return float(m.group(1)) if m else None
