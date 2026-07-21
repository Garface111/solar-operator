"""Owner-arrangeable inverter fleet — the backend that makes the sandbox a real
control surface (not just a saved pixel layout).

THE MODEL (why this exists): a solar owner does not think in vendor "sites". They
think in the physical reality on their property — "the six inverters at
Londonderry". The vendor's site grouping is an installer artifact. This module
lets the owner reproduce THEIR mental model: persisted `Inverter` rows whose
`array_id` (the owner grouping) is freely reassignable by dragging, while the
telemetry SOURCE (vendor + site + serial) stays fixed because that's just where
the data physically comes from.

Moving an inverter to a different array genuinely changes its peer cohort, its
reports, and its per-array rollups — front end and back end are one system.

Responsibilities:
  * discover_and_persist  — pull live inventory per connection, upsert Inverter
                            rows idempotently (NEVER clobber owner array_id).
  * _telemetry_for_site   — cached per-site telemetry (respects SolarEdge budget).
  * build_fleet_tree      — read persisted inverters grouped the OWNER's way,
                            attach telemetry by source, peer-analyze each owner
                            group, assemble the 3-tier columns.
  * reassign_inverter / create_array / reset_layout — the mutations a drag drives.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import timedelta, date as _date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import generation_sources
from .db import SessionLocal
from .models import Array, DailyGeneration, Inverter, InverterConnection, InverterDaily, Tenant, UtilityAccount, now, local_today
from .inverters import peer_analysis

log = logging.getLogger(__name__)

# ── Nameplate fallback from the model code ────────────────────────────────────
# Some vendors never report a per-unit nameplate, but the MODEL CODE names the AC
# rating — so we parse it as a grounded denominator for "% of rated" at READ time
# (we deliberately do NOT stamp it into Inverter.nameplate_kw — that column stays
# "what the source actually reported"; a parsed spec is derived, not reported).
# Unknown patterns return None so the cell shows blank rather than a guessed number.
#
#   Chint/CPS — portal exposes capacity only at the SITE level (installedCapacity),
#     busTypeDevices has no per-device rated field. Model names kW directly, e.g.
#     "SCA50KTL-DO/US-480" = 50 kW, "SC36KTL-DO/US-480" = 36 kW.
#   SolarEdge — the Monitoring API frequently omits per-inverter nameplate. The
#     model encodes the AC rating: residential SE#### = WATTS (SE10000 = 10 kW,
#     SE7600 = 7.6 kW), optionally with an H (HD-Wave) + region suffix; commercial
#     three-phase SE##K / SE##.#K = KILOWATTS (SE33.3KUS = 33.3 kW, SE100KUS = 100).
#   Fronius — the extension captures the device MODEL, never a per-unit nameplate,
#     and the model names the AC kW between the family and the phase digit:
#     "Primo 7.6-1 208-240" = 7.6 kW, "Primo 12.5-1 208-240" = 12.5 kW,
#     "Symo 24.0-3 480" = 24 kW. (Without this, every Fronius inverter showed a
#     BLANK size — e.g. Bruce's 32 Chester/Waterford Primos.)
_CHINT_MODEL_KW = re.compile(r"SC[A-Z]{0,3}(\d{1,3}(?:\.\d+)?)KTL", re.IGNORECASE)
_SE_MODEL_KW = re.compile(r"\bSE(\d{1,3}(?:\.\d+)?)K", re.IGNORECASE)   # SE33.3KUS → kW
_SE_MODEL_W = re.compile(r"\bSE(\d{3,5})(?!\d)", re.IGNORECASE)         # SE10000   → W
# Fronius family + "<kW>-<1|3-phase>". The "-[13]" anchor keeps the voltage
# ("208-240") from being misread as the rating.
_FRONIUS_MODEL_KW = re.compile(
    r"\b(?:Primo|Symo|Galvo|Eco|Tauro|Verto)(?:\s+GEN24)?\s+(\d{1,3}(?:\.\d+)?)-[13]\b",
    re.IGNORECASE)

def _nameplate_from_model(vendor, model):
    if not model:
        return None
    v = (vendor or "").lower()
    s = str(model)
    kw = None
    if v == "chint":
        mm = _CHINT_MODEL_KW.search(s)
        if mm:
            try:
                kw = float(mm.group(1))
            except (TypeError, ValueError):
                kw = None
    elif v == "solaredge":
        # K-form (kW) is more specific — try it first so SE100KUS isn't misread as
        # 100 W by the watt pattern.
        mk = _SE_MODEL_KW.search(s)
        if mk:
            try:
                kw = float(mk.group(1))
            except (TypeError, ValueError):
                kw = None
        else:
            mw = _SE_MODEL_W.search(s)
            if mw:
                try:
                    kw = float(mw.group(1)) / 1000.0
                except (TypeError, ValueError):
                    kw = None
    elif v == "fronius":
        mf = _FRONIUS_MODEL_KW.search(s)
        if mf:
            try:
                kw = float(mf.group(1))
            except (TypeError, ValueError):
                kw = None
    if kw is None:
        return None
    return kw if 0 < kw <= 1000 else None

def _eff_nameplate_kw(iv, m):
    """Effective nameplate: the reported value if we have one, else the API
    telemetry value, else (Chint only) the rating parsed from the model code.
    getattr-guarded so it's safe on any inverter-like object (real Inverter rows
    carry these columns; lightweight test stand-ins may not)."""
    np = getattr(iv, "nameplate_kw", None)
    if np is not None:
        return np
    mp = m.get("nameplate_kw")
    if mp is not None:
        return mp
    return _nameplate_from_model(getattr(iv, "vendor", None), getattr(iv, "model", None) or m.get("model"))


def _sane_live_power_w(power_w, nameplate_kw):
    """Safety net for the W/kW unit bug in extension capture. Solar.web auto-scales the
    realtime unit (W/kW/MW); the extension once assumed kW and ×1000'd a watts reading,
    inflating it 1000x (a 12.5 kW Primo read "2575 kW" = 20600% of rated). If a per-
    inverter live value is physically impossible (>3x nameplate) but lands in a sane range
    at 1/1000, it's that unit error — recover the true watts. If still absurd after /1000,
    drop it (never display known-garbage as live). Sane value or no nameplate → unchanged."""
    if power_w is None or not nameplate_kw or nameplate_kw <= 0:
        return power_w
    cap_w = nameplate_kw * 1000.0
    if power_w > cap_w * 3.0:
        recovered = power_w / 1000.0
        if recovered <= cap_w * 1.5:
            return round(recovered, 1)
        return None
    return power_w

# ── Daylight (for the card "Sleeping" night state) ────────────────────────────
# The liquid-fill cards must distinguish "zero output because the sun is down"
# (calm "Sleeping" pool) from "zero output because of a fault" (alarming). That
# decision MUST gate on sun position, never on output==0 alone — a noon fault
# that zeroes every inverter would otherwise be mislabeled "Sleeping" and hide a
# real outage. We compute it ONCE here server-side (the spec's preferred place)
# so 40+ cards don't each recompute it.
#
# We have NO stored lat/long per array yet (no model column, no adapter supplies
# one), so a precise per-array sunrise is impossible today. Instead of the spec's
# fixed-hour fallback (h<5||h>=21 — badly wrong seasonally: VT sunrise swings
# ~5:05am Jun → ~7:25am Dec), we compute the REAL solar elevation via the NOAA
# algorithm at a regional default (central Vermont) — accurate to the day/season,
# dependency-free. If we ever capture a per-array lat/long, pass it through and
# this lights up exactly per site with zero further change.
import math as _math

_VT_LAT, _VT_LON = 44.26, -72.58   # central Vermont regional default (Montpelier-ish)
# Sun is "up" for production purposes a touch below the horizon (civil-ish): a
# panel still trickles at -2° elevation. Below this we call it night.
_DAYLIGHT_MIN_ELEVATION_DEG = -2.0
# Live peer-compare alerts ("dark while neighbors produce", "low vs peers") need
# the sun HIGH enough that orientation/row-shading/sunset gradients don't create
# fake gaps. At low elevation a west-facing unit can still make real watts while
# an east-facing sibling (or a shadier neighbor array) already reads ~0 — that is
# physics, not a fault. 12° is roughly 45–90 min from local sunset/sunrise at
# mid-latitudes. is_daylight stays softer (-2°) for the UI "Sleeping" state only.
LIVE_COMPARE_MIN_ELEVATION_DEG = 12.0


def _solar_elevation_deg(when: datetime, lat: float, lon: float) -> float:
    """Solar elevation angle (degrees above horizon) at a UTC instant + location.
    Standard NOAA solar-position approximation; good to a fraction of a degree —
    far more than enough to decide day vs night. Dependency-free."""
    # fractional day-of-year + time
    ts = when
    # day of year
    doy = ts.timetuple().tm_yday
    hour = ts.hour + ts.minute / 60.0 + ts.second / 3600.0
    # fractional year (radians)
    gamma = 2.0 * _math.pi / 365.0 * (doy - 1 + (hour - 12) / 24.0)
    # equation of time (minutes) + solar declination (radians)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * _math.cos(gamma) - 0.032077 * _math.sin(gamma)
        - 0.014615 * _math.cos(2 * gamma) - 0.040849 * _math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * _math.cos(gamma) + 0.070257 * _math.sin(gamma)
        - 0.006758 * _math.cos(2 * gamma) + 0.000907 * _math.sin(2 * gamma)
        - 0.002697 * _math.cos(3 * gamma) + 0.00148 * _math.sin(3 * gamma)
    )
    # true solar time (minutes), then hour angle (degrees)
    time_offset = eqtime + 4.0 * lon  # lon in degrees; UTC time used so no tz term
    tst = hour * 60.0 + time_offset
    ha = tst / 4.0 - 180.0
    ha_rad = _math.radians(ha)
    lat_rad = _math.radians(lat)
    cos_zenith = (
        _math.sin(lat_rad) * _math.sin(decl)
        + _math.cos(lat_rad) * _math.cos(decl) * _math.cos(ha_rad)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = _math.acos(cos_zenith)
    return 90.0 - _math.degrees(zenith)


def _solar_elevation_now(lat: float | None = None, lon: float | None = None,
                         when: datetime | None = None) -> float | None:
    """Solar elevation (deg) at location, or None if the calc fails.
    Falls back to central-Vermont coords when lat/lon omitted."""
    import datetime as _dt
    w = when or _dt.datetime.now(_dt.timezone.utc)
    if w.tzinfo is not None:
        w = w.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    try:
        return _solar_elevation_deg(
            w,
            lat if lat is not None else _VT_LAT,
            lon if lon is not None else _VT_LON,
        )
    except Exception:
        return None


def _is_daylight(lat: float | None = None, lon: float | None = None,
                 when: datetime | None = None) -> bool:
    """True when the sun is up at the given location (UTC `when`, default now).
    Falls back to the central-Vermont regional default when no per-array
    coordinates are known (current state — no lat/long stored yet)."""
    elev = _solar_elevation_now(lat, lon, when)
    if elev is None:
        return True   # never let a sun-calc error hide a real card — default to "day"
    return elev > _DAYLIGHT_MIN_ELEVATION_DEG


def _live_compare_ok_elev(elev: float | None) -> bool:
    """True when elevation is high enough for live peer-compare alerts."""
    if elev is None:
        return True  # unknown sun → don't invent a suppress (callers still have is_daylight)
    return elev >= LIVE_COMPARE_MIN_ELEVATION_DEG


def _daylight_for(arr, default: bool, _cache: dict | None = None) -> bool:
    """Per-ARRAY sun-up: use the array's own stored lat/long (captured by the
    weather-model geocoding / vendor location capture / the owner's "set
    location") when present, else the regional central-VT default. A
    geographically distant array must not be daylight-gated on Vermont's sun
    schedule — that blanked/showed its live power at the wrong hours. `_cache`
    (rounded-coord → (elev, is_daylight)) lets one fleet build reuse the sun
    calc across co-located arrays."""
    _elev, day = _solar_state_for(arr, None, default, _cache)
    return day


def _solar_state_for(arr, default_elev: float | None, default_daylight: bool,
                     _cache: dict | None = None) -> tuple[float | None, bool]:
    """Per-array (elevation_deg, is_daylight). Uses stored lat/long when present,
    else regional defaults. Cache key is rounded coords."""
    lat = getattr(arr, "latitude", None)
    lon = getattr(arr, "longitude", None)
    if lat is None or lon is None:
        # Regional / board-wide default already computed by caller
        return default_elev, default_daylight
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return default_elev, default_daylight
    key = (round(lat_f, 1), round(lon_f, 1))
    if _cache is not None and key in _cache:
        return _cache[key]
    elev = _solar_elevation_now(lat_f, lon_f)
    # Day/night verdict goes through _is_daylight so existing monkeypatches/tests
    # and the single soft-night threshold stay the source of truth.
    day = _is_daylight(lat_f, lon_f)
    if elev is None:
        elev = default_elev
    verdict = (elev, day)
    if _cache is not None:
        _cache[key] = verdict
    return verdict


# Per-site telemetry cache (inventory + N equipment calls is heavy; SolarEdge is
# 300 req/day). Keyed by "vendor:site" -> (fetched_at, {serial: row}).
_SITE_TTL = timedelta(minutes=10)
_site_cache: dict[str, tuple] = {}

# A capture-time instantaneous power stays shown for up to a day for
# extension-captured vendors (Fronius/SMA), which only update on a manual
# capture — blanking it to "—" after just a few hours made healthy fleets look
# dead between captures (esp. evenings). We keep the last real reading and label
# the card with its capture time ("as of …") so it's honest, not misleading.
# API-pulled vendors (SolarEdge) refresh on every fetch, so this never applies.
_POWER_FRESH = timedelta(hours=24)

# A per-inverter reading captured THIS recently is treated as a live instant —
# trusted as-is, regardless of the regional daylight proxy, exactly like a freshly
# polled vendor value. A capture from minutes ago reflects real current output, and
# the crude central-VT _is_daylight() proxy (no per-array lat/long) must not blank
# it (it would zero a genuine mid-day Chint per-inverter reading whenever the
# regional sun-calc reads night in UTC reckoning). Older retained captures (beyond
# this window, up to _POWER_FRESH) stay daylight-gated so a 2pm capture never claims
# "producing" at 9pm.
_POWER_LIVE_FRESH = timedelta(minutes=15)

# A captured value only counts as "live now" if the SOURCE itself reported this
# recently — otherwise we captured a frozen value from a source that stopped (the
# West Chester case: source stuck at midday while we kept re-scraping it). Set to
# match _SOURCE_STALE_HOURS (the SOURCE-OFFLINE banner threshold, 6h) so the live
# number and the banner can NEVER disagree: a site within the window shows its power
# and no banner; past it, the power blanks AND the banner fires. Must be generous —
# Fronius's Solar.web "LastImport" lags 1–2h behind its live feed even while a site
# is actively producing (Waterford: producing, LastImport 1.9h). A tight 1h window
# wrongly blanked those producing arrays ("shows on Solar.web but no live feed here").
# Only gates when we actually have the source's own timestamp (source_last_data_at).
_SOURCE_LIVE_FRESH = timedelta(hours=6)

# Vendor monitoring portals — the "origin site" that sources each inverter's data.
# Owners click an array/inverter to jump to the vendor's deep-link for analysis.
_PORTAL_BASE = {
    "solaredge": "https://monitoring.solaredge.com/",
    "fronius":   "https://www.solarweb.com/",
    "sma":       "https://ennexos.sunnyportal.com/",
    "locus":     "https://hmi.alsoenergy.com/",
    "chint":     "https://monitor.chintpowersystems.com/",
}
_VENDOR_LABEL = {"solaredge": "SolarEdge", "fronius": "Fronius", "sma": "SMA",
                 "locus": "Locus / AlsoEnergy", "chint": "Chint / CPS"}


def _portal_link(vendor: str | None, site_id: str | None) -> str | None:
    """Deep link into the vendor's monitoring portal for a given source site.
    Known key-based vendors get a site-specific URL; others (and key-less cases)
    fall back to the vendor's base URL. Returns None for unknown vendors."""
    if not vendor:
        return None
    v = vendor.lower()
    base = _PORTAL_BASE.get(v)
    if not base:
        return None
    sid = (str(site_id).strip() if site_id else "")
    if v == "solaredge" and sid:
        return f"https://monitoring.solaredge.com/solaredge-web/p/site/{sid}/#/dashboard"
    if v == "fronius" and sid:
        return f"https://www.solarweb.com/PvSystems/PvSystem?pvSystemId={sid}"
    return base  # sma/locus/chint and key-less cases -> vendor base URL


def _resolve_connection(db, arr: Array):
    """The array's inverter connection (real row, or virtual from legacy
    Array.solaredge_* columns). Mirrors array_owners._resolve_connection."""
    from types import SimpleNamespace
    conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == arr.id)
    ).scalar_one_or_none()
    if conn is not None:
        return conn
    if arr.solaredge_api_key and arr.solaredge_site_id:
        return SimpleNamespace(
            id=None, vendor="solaredge",
            config={"api_key": arr.solaredge_api_key, "site_id": arr.solaredge_site_id},
            status="ok",
        )
    return None


# ─────────────────────────── telemetry (by source) ───────────────────────────

# SolarEdge equipment telemetry is one HTTP call PER inverter. Serial calls on a
# 190-inverter fleet were the ~90s "Loading your fleet…" first-paint blocker
# (Paul/Dwight/Sana/Bru/Ken). Cap concurrency so we stay polite to SE's budget
# without blocking the whole tree on one long chain.
_SE_TEL_WORKERS = max(4, min(16, int(os.getenv("FLEET_SE_TEL_WORKERS", "12") or "12")))
_LOCUS_TEL_WORKERS = max(4, min(12, int(os.getenv("FLEET_LOCUS_TEL_WORKERS", "8") or "8")))


def _telemetry_for_site(vendor: str, config: dict, site_id, *, force: bool = False) -> dict:
    """Return {serial: {name, model, nameplate_kw, daily, error_code, last_report,
    last_mode, last_power_w}} for one source site. Cached 10 min. `config` is the
    connection's full credential dict (SolarEdge reads api_key; Locus reads the
    SolarNOC username/password). Vendors without a per-inverter branch return {}."""
    ck = f"{vendor}:{site_id}"
    if not force:
        hit = _site_cache.get(ck)
        if hit and (now() - hit[0]) < _SITE_TTL:
            return hit[1]

    config = config or {}
    out: dict[str, dict] = {}
    if vendor == "solaredge":
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from .adapters import solaredge as _se
        api_key = config.get("api_key")
        try:
            inv = _se.fetch_inventory(api_key, int(site_id))
        except _se.SolarEdgeError as exc:
            log.warning("fleet: inventory fetch failed for site %s: %s", site_id, exc)
            return _site_cache.get(ck, (None, {}))[1] if ck in _site_cache else {}

        def _se_one(it: dict) -> tuple[str, dict] | None:
            sn = it.get("sn")
            if not sn:
                return None
            try:
                tel = _se.fetch_inverter_telemetry(api_key, int(site_id), sn, days_back=7)
            except _se.SolarEdgeError:
                tel = {"daily": [], "error_code": None, "last_report": None,
                       "last_mode": None, "last_power_w": None}
            return str(sn), {
                "name": it.get("name"), "model": it.get("model"),
                "nameplate_kw": it.get("nameplate_kw"),
                "daily": tel["daily"], "error_code": tel["error_code"],
                "last_report": tel["last_report"], "last_mode": tel.get("last_mode"),
                "last_power_w": tel.get("last_power_w"),
            }

        # Parallel per-inverter equipment pulls — the inventory list is small; the
        # N equipment/{sn}/data calls were the serial cliff.
        workers = min(_SE_TEL_WORKERS, max(1, len(inv)))
        if workers <= 1 or len(inv) <= 1:
            for it in inv:
                row = _se_one(it)
                if row:
                    out[row[0]] = row[1]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(_se_one, it) for it in inv]
                for fut in as_completed(futs):
                    try:
                        row = fut.result()
                    except Exception:
                        log.warning("fleet: SE telemetry worker failed site %s", site_id, exc_info=True)
                        continue
                    if row:
                        out[row[0]] = row[1]
    elif vendor == "locus":
        # Locus exposes each inverter as an INVERTER component; identity is the
        # component id (serials are usually blank). One components call + per-unit
        # latest power + 7-day daily. Cached 10 min like every source.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from .adapters import locus as _locus
        creds = {"username": config.get("username"), "password": config.get("password")}
        if config.get("cognito_client_id"):
            creds["cognito_client_id"] = config["cognito_client_id"]
        try:
            comps = _locus.list_inverter_components(creds, int(site_id))
        except _locus.LocusError as exc:
            log.warning("fleet: locus components fetch failed for site %s: %s", site_id, exc)
            return _site_cache.get(ck, (None, {}))[1] if ck in _site_cache else {}
        end = now().date()
        start = end - timedelta(days=7)

        def _locus_one(c: dict) -> tuple[str, dict] | None:
            cid = c["component_id"]
            try:
                latest = _locus.fetch_component_latest(creds, cid)
            except _locus.LocusError:
                latest = {"last_power_w": None, "last_report": None}
            try:
                daily = _locus.fetch_component_daily(creds, cid, start, end)
            except _locus.LocusError:
                daily = []
            model = " ".join(x for x in (c.get("oem"), c.get("model")) if x) or None
            return str(cid), {
                "name": c.get("name") or f"Inverter {cid}",
                "model": model,
                "nameplate_kw": c.get("nameplate_kw"),
                "daily": [{"date": r["day"].isoformat(), "kwh": r["kwh"]} for r in daily],
                "error_code": None,
                "last_report": latest.get("last_report"),
                "last_mode": None,
                "last_power_w": latest.get("last_power_w"),
            }

        workers = min(_LOCUS_TEL_WORKERS, max(1, len(comps)))
        if workers <= 1 or len(comps) <= 1:
            for c in comps:
                row = _locus_one(c)
                if row:
                    out[row[0]] = row[1]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(_locus_one, c) for c in comps]
                for fut in as_completed(futs):
                    try:
                        row = fut.result()
                    except Exception:
                        log.warning("fleet: locus telemetry worker failed site %s", site_id, exc_info=True)
                        continue
                    if row:
                        out[row[0]] = row[1]
    _site_cache[ck] = (now(), out)
    return out


def _stored_inverter_daily(db, inverter_id: int) -> list[dict]:
    """Read persisted per-inverter daily kWh (InverterDaily) for vendors captured
    via the extension (Fronius) that have no live API connection to pull through.
    Returns peer_analysis's expected shape: [{"date": "YYYY-MM-DD", "kwh": float}]
    ascending, last 14 days.
    """
    rows = db.execute(
        select(InverterDaily)
        .where(InverterDaily.inverter_id == inverter_id)
        .order_by(InverterDaily.day.desc())
        .limit(14)
    ).scalars().all()
    return [
        {"date": r.day.isoformat(), "kwh": r.kwh}
        for r in sorted(rows, key=lambda x: x.day)
    ]


def _persist_daily_series(db, tenant_id: str, inverter_id: int,
                          series: list[dict], *, source: str) -> int:
    """Snapshot a per-inverter daily kWh series into InverterDaily so the graph's
    history SURVIVES regardless of whether the vendor API answers next time.

    This is the heart of the API-independent history store: whenever build_fleet_tree
    sees daily readings for an inverter — from a LIVE vendor API (SolarEdge) or any
    other source — we upsert them here, keyed by (inverter_id, day). Idempotent:
    re-seeing a day keeps the LARGER kWh (a day's energy only climbs / settles up),
    so cached/partial reads never clobber a fuller value. Returns rows written.

    `series` is the [{"date": "YYYY-MM-DD"|date, "kwh": float}, ...] shape.
    """
    if not series:
        return 0
    # existing rows for this inverter, keyed by day, so we upsert in one pass
    existing = {
        r.day: r
        for r in db.execute(
            select(InverterDaily).where(InverterDaily.inverter_id == inverter_id)
        ).scalars().all()
    }
    written = 0
    for pt in series:
        raw_day = pt.get("date")
        kwh = pt.get("kwh")
        if raw_day is None or kwh is None:
            continue
        try:
            kwh = float(kwh)
        except (TypeError, ValueError):
            continue
        if kwh < 0:
            continue
        # accept ISO string or a date/datetime
        if isinstance(raw_day, str):
            try:
                day = _date.fromisoformat(raw_day[:10])
            except ValueError:
                continue
        elif isinstance(raw_day, datetime):
            day = raw_day.date()
        elif isinstance(raw_day, _date):
            day = raw_day
        else:
            continue
        row = existing.get(day)
        if row is None:
            db.add(InverterDaily(tenant_id=tenant_id, inverter_id=inverter_id,
                                 day=day, kwh=round(kwh, 3), source=source))
            written += 1
        elif kwh > (row.kwh or 0):
            row.kwh = round(kwh, 3)
            row.source = source
            row.uploaded_at = now()
            written += 1
    return written


def _merged_daily(db, inverter_id: int, live_series: list[dict]) -> list[dict]:
    """The graph's authoritative daily series: STORAGE is the source of truth, with
    any fresh live readings merged on top. Reading from storage (not the live API)
    is exactly what makes a graph never vanish when an API is slow/down/rate-limited.

    Merge rule per day: keep the LARGER kWh between stored and live (a day's energy
    only climbs). Returns ascending [{"date","kwh"}], last 14 days.
    """
    merged: dict[str, float] = {}
    for pt in _stored_inverter_daily(db, inverter_id):
        merged[pt["date"]] = float(pt["kwh"] or 0)
    for pt in (live_series or []):
        d = pt.get("date")
        if isinstance(d, datetime):
            d = d.date().isoformat()
        elif isinstance(d, _date):
            d = d.isoformat()
        elif isinstance(d, str):
            d = d[:10]
        else:
            continue
        k = pt.get("kwh")
        if k is None:
            continue
        try:
            k = float(k)
        except (TypeError, ValueError):
            continue
        merged[d] = max(merged.get(d, 0), k)
    out = [{"date": d, "kwh": round(v, 3)} for d, v in sorted(merged.items())]
    return out[-14:]


# ─────────────────────────── discovery / persistence ─────────────────────────

def _vendor_discovery_enabled() -> bool:
    """False when this environment must NEVER call a vendor API.

    Preprod/staging mirrors production's data (see scripts/refresh_preprod_data.sh)
    and must not reach out to vendors: the SolarEdge api_key rides inline in the
    connection config (so a second environment silently burns the quota'd key),
    SMA rotates its OAuth refresh token (a staging refresh invalidates PROD's
    token), and GMP/SmartHub sessions are cookie-bound + dedup'd. Prod leaves
    FLEET_VENDOR_DISCOVERY unset → discovery stays on, unchanged.
    """
    return (os.getenv("FLEET_VENDOR_DISCOVERY", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _persisted_inverters(db, tenant: Tenant) -> list[Inverter]:
    """The tenant's live (non-deleted) inverters, in the owner's arrangement."""
    return db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant.id, Inverter.deleted_at.is_(None)
        ).order_by(Inverter.array_id, Inverter.position)
    ).scalars().all()


def _insert_inverter_race_safe(db, iv: Inverter) -> Inverter:
    """Insert an Inverter the caller believes is missing (SELECT-then-INSERT).

    Concurrent fleet-tree / discover calls can both pass the empty-existing read
    and race the INSERT — the loser hits uq_inverter_tenant_vendor_serial and
    used to 500 /v1/array-owners/fleet-tree (Sentry, locus serial 2294589).
    SAVEPOINT the insert; on IntegrityError re-read the winner's row.
    Mirrors array_owners capture path (dialect-agnostic: PG + sqlite).
    """
    try:
        with db.begin_nested():
            db.add(iv)
            db.flush()
        return iv
    except IntegrityError:
        return db.execute(
            select(Inverter).where(
                Inverter.tenant_id == iv.tenant_id,
                Inverter.vendor == iv.vendor,
                Inverter.serial == iv.serial,
            )
        ).scalar_one()


def discover_and_persist(db, tenant: Tenant, *, force_refresh: bool = False) -> list[Inverter]:
    """Walk every array's connection, pull live inventory, and upsert one
    persisted Inverter per real serial. IDEMPOTENT and owner-safe:

      * keyed by (tenant_id, vendor, serial)
      * NEW serials are created under the Array that owns their source site
        (the owner's starting point = the discovered grouping)
      * EXISTING rows refresh name/model/nameplate/last_seen but KEEP the owner's
        array_id + position (their arrangement is sacred)
      * undeleted if they reappear after a soft-delete

    Returns the tenant's live (non-deleted) inverters.
    """
    if not _vendor_discovery_enabled():
        # Vendor-silent environment (preprod): serve the mirrored fleet as-is.
        return _persisted_inverters(db, tenant)

    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
    ).scalars().all()

    existing = {
        (iv.vendor, iv.serial): iv
        for iv in db.execute(
            select(Inverter).where(Inverter.tenant_id == tenant.id)
        ).scalars().all()
    }

    # Next free position per owner array (avoid a maxpos SELECT per new serial).
    next_pos: dict[int, int] = defaultdict(int)
    for iv in existing.values():
        if iv.deleted_at is None and iv.array_id is not None:
            next_pos[iv.array_id] = max(next_pos[iv.array_id], (iv.position or 0) + 1)

    # Build unique site jobs first, then pull telemetry in parallel (DB apply stays
    # single-threaded). Previously each array waited on the previous site's full
    # N-inverter equipment fan-out — multi-site fleets paid that cliff end-to-end.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    jobs: list[tuple] = []  # (arr, conn, vendor, cfg, site_id)
    seen_site: set[tuple] = set()
    arr_by_site: dict[tuple, list] = defaultdict(list)
    for arr in arrays:
        conn = _resolve_connection(db, arr)
        if conn is None:
            continue
        vendor = conn.vendor
        cfg = conn.config or {}
        site_id = cfg.get("site_id")
        if not site_id:
            continue
        skey = ((vendor or ""), str(site_id))
        arr_by_site[skey].append((arr, conn, vendor, cfg, site_id))
        if skey in seen_site:
            continue
        seen_site.add(skey)
        jobs.append((skey, vendor, cfg, site_id))

    def _job_tel(job):
        skey, vendor, cfg, site_id = job
        try:
            return skey, _telemetry_for_site(vendor, cfg, site_id, force=force_refresh)
        except Exception:
            log.warning("fleet: discover telemetry failed for %s", skey, exc_info=True)
            return skey, {}

    tel_by_site: dict[tuple, dict] = {}
    if len(jobs) <= 1:
        for job in jobs:
            k, tel = _job_tel(job)
            tel_by_site[k] = tel
    else:
        with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
            futs = [pool.submit(_job_tel, job) for job in jobs]
            for fut in as_completed(futs):
                try:
                    k, tel = fut.result()
                except Exception:
                    continue
                tel_by_site[k] = tel

    # Apply telemetry → Inverter rows (one site's tel can feed multiple arrays that
    # share a connection; we seed NEW serials onto the first array that owns the site).
    for skey, owners in arr_by_site.items():
        tel = tel_by_site.get(skey) or {}
        if not tel:
            continue
        # Prefer the array that actually holds the connection as the discovery home.
        home_arr, home_conn, vendor, cfg, site_id = owners[0]
        for serial, m in tel.items():
            key = (vendor, str(serial))
            iv = existing.get(key)
            if iv is None:
                pos = next_pos[home_arr.id]
                next_pos[home_arr.id] = pos + 1
                candidate = Inverter(
                    tenant_id=tenant.id,
                    array_id=home_arr.id,                 # owner grouping starts = source
                    position=pos,
                    vendor=vendor, serial=str(serial),
                    source_site_id=str(site_id),
                    source_connection_id=getattr(home_conn, "id", None),
                    source_array_id=home_arr.id,
                )
                iv = _insert_inverter_race_safe(db, candidate)
                if iv is not candidate:
                    # Concurrent discover won — refresh source pointers only;
                    # never clobber owner array_id/position.
                    iv.source_site_id = str(site_id)
                    iv.source_connection_id = getattr(home_conn, "id", None)
                    iv.source_array_id = iv.source_array_id or home_arr.id
                    if iv.deleted_at is not None:
                        iv.deleted_at = None
                existing[key] = iv
            else:
                # Refresh source pointers in case the connection moved, but DO NOT
                # touch array_id/position (owner's layout).
                iv.source_site_id = str(site_id)
                iv.source_connection_id = getattr(home_conn, "id", None)
                iv.source_array_id = iv.source_array_id or home_arr.id
                if iv.deleted_at is not None:
                    iv.deleted_at = None
            # metadata refresh (cheap, safe). An OWNER-renamed inverter
            # (name_is_custom) is part of "their arrangement is sacred" — the
            # telemetry name must NOT clobber it, exactly like array_id/position
            # are never touched on a sync.
            if not getattr(iv, "name_is_custom", False):
                iv.name = m.get("name") or iv.name or str(serial)
            elif not iv.name:
                # custom flag set but somehow empty — fall back so it's never blank
                iv.name = m.get("name") or str(serial)
            iv.model = m.get("model") or iv.model
            if m.get("nameplate_kw") is not None:
                iv.nameplate_kw = m.get("nameplate_kw")
            iv.last_seen_at = now()
            # Stamp live power so the stored/lite fleet-tree path (instant first
            # paint, no vendor round-trips) still shows kW-now instead of blanks
            # until the slow live enrichment finishes. API-polled vendors used to
            # skip this column entirely; that made mode=stored look empty on power.
            lp = m.get("last_power_w")
            if lp is not None:
                try:
                    lp_f = float(lp)
                except (TypeError, ValueError):
                    lp_f = None
                if lp_f is not None and lp_f >= 0:
                    iv.last_power_w = lp_f
                    iv.last_power_at = now()
                    iv.last_power_estimated = False
            lr = m.get("last_report")
            if lr:
                # Best-effort: keep source clock when the vendor gave us one.
                # Store as naive UTC so it matches models.now() (naive utcnow) and
                # _live_power_w's subtraction never mixes aware/naive.
                try:
                    parsed = None
                    if isinstance(lr, str):
                        raw = lr.replace("Z", "+00:00")
                        parsed = datetime.fromisoformat(raw)
                    elif isinstance(lr, datetime):
                        parsed = lr
                    if parsed is not None:
                        if parsed.tzinfo is not None:
                            from datetime import timezone as _tz
                            parsed = parsed.astimezone(_tz.utc).replace(tzinfo=None)
                        iv.source_last_data_at = parsed
                except Exception:
                    pass

    db.commit()
    return _persisted_inverters(db, tenant)


# ─────────────────────────────── fleet tree ──────────────────────────────────

_ALERT_HEADLINE = {
    "fault": "Inverter fault — service drafted",
    "dead": "An inverter stopped earning",
    "comm_gap": "An inverter went quiet",
    "underperforming": "A money leak caught early",
    "ok": "All clear",
}
_ALERT_PRIORITY = {"fault": 4, "dead": 4, "comm_gap": 3, "underperforming": 2, "ok": 0}


# A vendor site is treated as having a SOURCE-side reporting outage when its
# freshest inverter last_report is older than this (it stopped sending data to
# its own monitoring portal — nothing we can fix; we just surface it honestly).
_SOURCE_STALE_HOURS = 6.0
# Extension-captured vendors (Fronius/SMA/Chint) rely on the user's browser
# session staying alive for hourly background recapture. last_power_at advances
# only when real production is captured (the sub-floor guard means near-zero
# nighttime readings don't stamp it), so the natural overnight gap can be 8–14h.
# Use a 26h threshold so a single missed night never triggers SOURCE OFFLINE;
# a genuine session expiry (no daytime capture for >26h) still fires the banner.
_EXT_SOURCE_STALE_HOURS = 26.0
_EXT_CAPTURED_VENDORS = {"fronius", "sma", "chint"}

# A telemetry/source timestamp older than this is GARBAGE, not a real "last reported"
# time — e.g. a 1970 Unix-epoch value from a missing/zero SMA gauge reading, which
# parses as a valid (but absurd) ISO date and otherwise reads as "20628 days ago" +
# a false SOURCE-OFFLINE banner. Every AO array + vendor portal is well post-2015, so
# anything older is treated as absent (callers fall back to a real timestamp or None).
_TS_SANITY_FLOOR_YEAR = 2015


def _sane_dt(dt):
    """Return dt only if it's a plausibly-real timestamp (year >= the sanity floor);
    None for a missing / epoch / garbage value so callers degrade honestly."""
    try:
        return dt if (dt is not None and dt.year >= _TS_SANITY_FLOOR_YEAR) else None
    except (AttributeError, TypeError):
        return None


def _source_status(inv_rows: list[dict], *, stale_hours: float = _SOURCE_STALE_HOURS) -> dict:
    """Array-level source-data freshness from the inverters' last_report.

    Returns {state, last_report, age_hours}:
      • "none"  — no live-capable feed at all (no last_report on any inverter).
      • "ok"    — freshest report within the staleness window.
      • "stale" — freshest report older than the window (the VENDOR/source has
                  not received data recently — a source-side outage, not ours).
    last_report is the most-recent inverter timestamp (ISO), age_hours its age.
    Pass stale_hours to override the threshold (extension vendors use 26h).
    """
    from datetime import datetime, timezone
    stamps: list[datetime] = []
    for r in inv_rows:
        lr = r.get("last_report")
        if not lr:
            continue
        try:
            dt = datetime.fromisoformat(str(lr).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.year < _TS_SANITY_FLOOR_YEAR:
                continue   # garbage epoch ts — not a real report (never "20628 days ago")
            stamps.append(dt)
        except (ValueError, TypeError):
            continue
    if not stamps:
        return {"state": "none", "last_report": None, "age_hours": None}
    freshest = max(stamps)
    age_h = round((datetime.now(timezone.utc) - freshest).total_seconds() / 3600.0, 1)
    state = "stale" if age_h >= stale_hours else "ok"
    return {"state": state, "last_report": freshest.isoformat(), "age_hours": age_h}


def _sync_recency(invs) -> dict:
    """How recently WE last captured this array (max last_seen_at across its inverters).

    Distinct from _source_status (the vendor SOURCE's own data age): last_seen_at advances
    on EVERY successful capture — including overnight, when there's no production and the
    source clock is frozen — so the UI can honestly show "Synced Xm ago" (our pipeline is
    alive) as a separate truth from how old the vendor's own data is. This is what makes
    the front-end freshness faithfully mirror the back end: a fresh auto-login/keep-warm
    capture moves THIS even when the source's LastImport can't move (panels asleep).
    Returns {synced_at: iso|None, age_min: float|None}.
    """
    from datetime import datetime, timezone
    stamps: list[datetime] = []
    for iv in invs:
        dt = _sane_dt(getattr(iv, "last_seen_at", None))
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        stamps.append(dt)
    if not stamps:
        return {"synced_at": None, "age_min": None}
    freshest = max(stamps)
    age_min = round((datetime.now(timezone.utc) - freshest).total_seconds() / 60.0, 1)
    return {"synced_at": freshest.isoformat(), "age_min": age_min}


def _flag_no_energy_register(inv_rows: list[dict]) -> None:
    """Stamp inv_rows[*]["no_energy_register"] in place for a unit whose vendor
    reports (or could report) LIVE POWER but has NO cumulative-energy history at
    all — the classic dead-metering case Bruce's Tannery #7 (S/N 191213319)
    exposed: SMA streams its live watts but its TotWhOut register is dead, so it
    has an empty daily series, window_kwh 0, peak None, and peer_index None. The
    14-day peer verdict can't grade it (defaults 'ok'), and because per-inverter
    power is split by TODAY'S energy share, a zero-energy unit gets a bogus 0/low
    live allocation — so grading its live power against peers is meaningless too.

    We mark it so EVERY surface can render an honest, distinct "no energy data /
    metering issue" state instead of the harsh Error/Offline it otherwise falls
    into (a producing inverter must never read as dead). This mirrors the digest's
    _nonreporting_inverters predicate exactly (api/jobs/morning_fleet_digest.py)
    so the email and the dashboard agree.

    Predicate (same as the digest): the unit has NO production data at all while a
    producing MAJORITY of its array cohort IS reporting — so an array-wide capture
    gap (every unit dark, already surfaced by the staleness banner) never flags
    every inverter. Requires >=2 inverters and >=2 producers, majority producing.
    """
    if len(inv_rows) < 2:
        return

    def _produced(r: dict) -> bool:
        return bool((r.get("window_kwh") or 0) > 0 or (r.get("peak_kwh") or 0) > 0)

    def _has_history(r: dict) -> bool:
        return any(p.get("kwh") is not None for p in (r.get("daily") or []))

    producing = sum(1 for r in inv_rows if _produced(r))
    # Only judge when a producing majority reports; else it's a whole-array gap.
    if producing < 2 or producing * 2 < len(inv_rows):
        return
    for r in inv_rows:
        # No cumulative energy anywhere in the window (empty series + no window/peak)
        # while its siblings produce → its energy register isn't reporting.
        r["no_energy_register"] = bool(not _produced(r) and not _has_history(r))


def _array_alert(inv_rows: list[dict]) -> dict:
    worst, worst_rank, bad = "ok", 0, 0
    for inv in inv_rows:
        st = inv.get("status") or "ok"
        r = _ALERT_PRIORITY.get(st, 0)
        if r >= 2:
            bad += 1
        if r > worst_rank:
            worst_rank, worst = r, st
    level = "critical" if worst_rank >= 4 else "warn" if worst_rank >= 2 else "ok"
    return {"level": level, "count": bad, "status": worst,
            "headline": _ALERT_HEADLINE.get(worst, "All clear")}


def _cross_tenant_live_by_serial(db, inverters: list) -> dict:
    """Best FRESH live-power reading per physical inverter, ACROSS ALL tenants.

    The same physical array can be captured into more than one tenant (e.g. an
    installer/owner each run the extension against the same Fronius Solar.web
    system; a system shared as a guest gets a delayed/reduced live feed). When one
    tenant's browser captures a bogus near-zero wattage for a device while another
    tenant captured a real value for the SAME device THIS SAME window, we'd rather
    show the real one than a misleading ~0.

    Join key = (vendor, serial). For Fronius the serial is the stable device GUID,
    so this matches the exact physical inverter — borrowing can only correct a
    bad/low reading, never invent or cross panels. We keep the MAX fresh reading
    (freshness-gated by _POWER_FRESH, positive only) so a stale or zero sibling
    never drags a good local reading down — it's a pure upward correction.

    Returns {(vendor, serial): watts}. Empty when no sibling capture beats local.
    """
    serials = {
        (str(iv.vendor or "").lower(), str(iv.serial))
        for iv in inverters if iv.serial
    }
    if not serials:
        return {}
    wanted_serials = {s for _v, s in serials}
    # Only borrow a sibling reading that is itself genuinely CURRENT (same live
    # window we'd trust our own capture in) — a 6h-old reading from another tenant is
    # not "this window," and importing it is how a dead array lit up as producing.
    # Also exclude demo/test/seed tenants so sample data never bleeds into a real
    # fleet (the same Fronius GUIDs live in ~10 demo tenants).
    fresh_after = now() - _POWER_LIVE_FRESH
    rows = db.execute(
        select(Inverter).where(
            Inverter.serial.in_(wanted_serials),
            Inverter.deleted_at.is_(None),
            Inverter.last_power_w.isnot(None),
            Inverter.last_power_at.isnot(None),
            Inverter.last_power_at >= fresh_after,
            ~Inverter.tenant_id.like("ten_demo%"),
            ~Inverter.tenant_id.like("%readonly%"),
        )
    ).scalars().all()
    best: dict[tuple, float] = {}
    for r in rows:
        key = (str(r.vendor or "").lower(), str(r.serial))
        if key not in serials:
            continue  # a serial we care about but a different vendor — skip
        w = float(r.last_power_w or 0.0)
        if w <= 0:
            continue
        if key not in best or w > best[key]:
            best[key] = w
    return best


def _report_is_stale(last_report) -> bool:
    """True when a vendor telemetry timestamp is older than the SOURCE-stale
    window (_SOURCE_STALE_HOURS) — i.e. the source stopped reporting, so any
    live value carried alongside it is frozen, not current. Uses the SAME
    threshold as _source_status so the live number and the SOURCE-OFFLINE banner
    can never disagree. A missing/unparseable timestamp is NOT treated as stale
    (return False) — we only suppress a live value when we can prove it's old."""
    if not last_report:
        return False
    from datetime import datetime, timezone
    try:
        if isinstance(last_report, datetime):
            dt = last_report
        else:
            s = str(last_report).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    return age_h > _SOURCE_STALE_HOURS


def _live_power_w(iv: Inverter, m: dict, *, daylight: bool = True,
                  borrow: dict | None = None):
    """The card's "Current kW". API-pulled vendors (SolarEdge) carry a live
    instantaneous power in their telemetry (m["last_power_w"]) — prefer it. For
    extension-captured vendors there is no live feed, so fall back to the power
    stamped at capture time, but ONLY while fresh (see _POWER_FRESH) so a capture
    from hours ago doesn't keep claiming the panels are producing right now.

    HONESTY GATE: never report a stale captured reading as live power when the
    sun is down. A 2pm capture must not read as "producing 17kW" at 9pm — that's
    the exact bug that contradicted SMA's own portal. A genuine live telemetry
    value (m["last_power_w"], freshly polled/pulled this request) is trusted as-is
    since it reflects the real instant; only the STORED capture fallback is
    daylight-gated.

    CROSS-TENANT BORROW: `borrow` maps (vendor, serial) -> the best fresh reading
    for this physical device across ALL tenants (see _cross_tenant_live_by_serial).
    When another tenant captured a higher real value for the SAME inverter this
    window, take it — this corrects the case where one browser's capture returns a
    bogus near-zero for a shared/guest Solar.web system while the owner's browser
    read the true wattage. Upward-only and serial-exact, so it can't fabricate.
    Still daylight-gated: at night every tenant reads ~0, so there's nothing to
    borrow.

    SOURCE-STALE GATE: a vendor live value (m["last_power_w"], e.g. SolarEdge's
    /overview currentPower) and its timestamp (m["last_report"], lastUpdateTime)
    come from the SAME response. When the source stopped reporting, currentPower
    FREEZES at its last value while lastUpdateTime ages — so a stale feed would
    otherwise read "producing 596 W" while the card simultaneously shows "SOURCE
    OFFLINE — last reported 8h ago" (the exact contradiction Ford hit on Cover
    Catamount). So we only trust m["last_power_w"] as LIVE when its own report is
    within the staleness window; a stale vendor reading is dropped (→ no live
    number), matching the honest SOURCE-OFFLINE banner."""
    pw = m.get("last_power_w")
    # Drop a vendor live reading whose own telemetry timestamp is stale — it's a
    # frozen value from a source that stopped reporting, not a live instant.
    if pw is not None and _report_is_stale(m.get("last_report")):
        pw = None
    base = pw if pw is not None else None
    # Stored per-inverter capture fallback. Extension-captured vendors (Chint,
    # Fronius, SMA) have NO live API feed — the real measured watts live in
    # iv.last_power_w, stamped at OUR capture time. Show it as "Current kW" when ALL:
    #   • own_recent — we captured it within _POWER_FRESH (a day); an older value is
    #     dropped so yesterday's reading never shows as "now".
    #   • src_live   — the SOURCE was still reporting when captured (within
    #     _SOURCE_LIVE_FRESH, the SAME window as the SOURCE-OFFLINE banner so the
    #     number and the banner can't disagree). Fronius's Solar.web LastImport lags
    #     1–2h behind its live feed even while producing, so this must be generous
    #     (Waterford: producing, LastImport 1.9h → SHOWN; West Chester: stopped 7h
    #     ago → blanked + bannered).
    #   • daylight   — never report a captured midday value as "producing" after dark
    #     (the SMA 9pm bug). _is_daylight is a real solar-elevation calc, so it won't
    #     blank a genuine daytime reading.
    # REGRESSION FIXED: this had collapsed to a 15-min "own_fresh" window with no
    # daylight tier, which blanked every extension array between its (hourly) recaptures
    # — Ford's "Waterford produces on Solar.web but shows no live feed here".
    _lpa = _sane_dt(getattr(iv, "last_power_at", None))
    _slda = _sane_dt(getattr(iv, "source_last_data_at", None))
    _lpw = getattr(iv, "last_power_w", None)
    def _age_ok(dt, window):
        if dt is None:
            return False
        try:
            # Guard mixed aware/naive (e.g. ISO-Z stamps vs models.now naive UTC).
            n = now()
            if getattr(dt, "tzinfo", None) is not None and getattr(n, "tzinfo", None) is None:
                dt = dt.replace(tzinfo=None)
            elif getattr(dt, "tzinfo", None) is None and getattr(n, "tzinfo", None) is not None:
                n = n.replace(tzinfo=None)
            return (n - dt) <= window
        except Exception:
            return False
    own_recent = _age_ok(_lpa, _POWER_FRESH)
    src_live = (_slda is None or _age_ok(_slda, _SOURCE_LIVE_FRESH))
    capture_live = own_recent and src_live and daylight
    if base is None and _lpw is not None and capture_live:
        base = _lpw
    # Cross-tenant upward correction — only while THIS owner's own reading is itself
    # live (recent capture + source reporting + daylight). Fixes a fresh-but-bogus
    # near-zero on a SHARED system (another tenant read the true wattage this window);
    # never resurrects a stale/zero array from an unrelated tenant's older reading.
    if borrow and iv.serial and capture_live:
        bw = borrow.get((str(iv.vendor or "").lower(), str(iv.serial)))
        if bw is not None and (base is None or bw > base):
            base = bw
    return _sane_live_power_w(base, _eff_nameplate_kw(iv, m))


# ── Two distinct data streams: VENDOR (inverter telemetry) vs UTILITY (meter) ──
# The sandbox slider switches between these. The system still INTEGRATES both
# (the blended `daily` is what dedup/Trends use); these split views just let the
# owner SEE each feed on its own. Classify DailyGeneration.source into one stream.
# DERIVED from the canonical registry (api.generation_sources) so this can't
# drift from forecasting / bill_to_daily again (audit #12). The VENDOR stream is
# inverter telemetry + the extension captures + operator-supplied independent
# production. It formerly hard-coded only solaredge/fronius/sma/chint and silently
# dropped enphase/solis/tigo/alsoenergy/locus — those now flow in from VENDORS.
# The utility-side reads (gmp_api/gmp_portal_scrape/smarthub) belong to the
# UTILITY stream below, not here, so they are excluded.
_VENDOR_SOURCES = (
    generation_sources.VENDOR_TELEMETRY_SOURCES
    | generation_sources.EXTENSION_SOURCES
    | {"csv", "manual"}                   # operator-supplied independent production
)
# NOTE: 'bill_prorate' is deliberately NOT here (audit #9). It's a monthly utility bill
# smeared flat across its days — an ESTIMATE, not a settled meter reading — so it must
# not blend into the "Utility meter · settled generation" stream as if it were one. It
# still appears in the BLENDED array-production view (_array_daily); it's just never
# mislabeled as settled-meter data.
# The UTILITY-meter STREAM (what the sandbox slider shows as the meter feed).
# Its real reads track the canonical registry's UTILITY_REAL_SOURCES so they can't
# drift, PLUS 'utility_meter' — the operator-entered per-day meter reading. This
# is a STREAM classifier, not the MEASURED/estimate distinction: 'utility_meter'
# is intentionally shown here as a utility feed. 'bill_prorate' is deliberately
# still NOT in either stream (it's a bill smear — see the note above _daily_stream
# and audit #9); it only appears in the BLENDED array-production view.
_UTILITY_SOURCES = (
    generation_sources.UTILITY_REAL_SOURCES | {"utility_meter"}
)


def _daily_stream(src: str | None) -> str:
    """Map a raw DailyGeneration.source onto 'vendor' | 'utility' | 'other'."""
    s = (src or "").strip().lower()
    if s in _VENDOR_SOURCES:
        return "vendor"
    if s in _UTILITY_SOURCES:
        return "utility"
    return "other"


def _array_daily(db, array_id: int, days: int = 14) -> list[dict]:
    """Array-level daily kWh (DailyGeneration), ascending, last `days`. This is
    the array's OWN production history — used by the front-end array graph as a
    fallback/primary when the vendor gives site-level history but no per-inverter
    series (e.g. Chint's weekETrend backfill). Returns [{"date","kwh"}].

    BLENDED view: at most one row per (array,day) exists in DailyGeneration, so
    this is already the integrated stream (dedup keeps the strongest source/day).
    """
    rows = db.execute(
        select(DailyGeneration)
        .where(DailyGeneration.array_id == array_id)
        .order_by(DailyGeneration.day.desc())
        .limit(days)
    ).scalars().all()
    return [
        {"date": r.day.isoformat(), "kwh": round(float(r.kwh or 0.0), 2),
         # Provenance so callers can tell a MEASURED day (extension/GMP/API pull,
         # CSV upload) from an ESTIMATE split out of a utility bill (bill_prorate)
         # — never render an estimate as if it were metered. See AO data-honesty audit.
         "source": r.source or "csv"}
        for r in sorted(rows, key=lambda x: x.day)
    ]


def _array_daily_split(db, array_id: int, days: int = 14) -> dict:
    """Per-array daily kWh split into the two SOURCE STREAMS the sandbox slider
    toggles between:
        vendor  — inverter telemetry (SolarEdge/Fronius/SMA/CHINT/extension) +
                  operator-supplied (csv/manual)
        utility — the utility meter's settled generation (GMP api/meter/smarthub,
                  bill-prorated). Includes GmpDailyGeneration (per-account meter
                  table) aggregated per day, in case it's richer than the array's
                  utility_meter DailyGeneration rows.
    Each value is ascending [{date,kwh}], last `days`. Returns
    {"vendor": [...], "utility": [...], "has_vendor": bool, "has_utility": bool}.
    """
    from .models import GmpDailyGeneration
    rows = db.execute(
        select(DailyGeneration)
        .where(DailyGeneration.array_id == array_id)
        .order_by(DailyGeneration.day.desc())
        .limit(days * 3)   # over-fetch; a day may belong to either stream
    ).scalars().all()

    vendor: dict[str, float] = {}
    utility: dict[str, float] = {}
    for r in rows:
        stream = _daily_stream(r.source)
        d = r.day.isoformat()
        kwh = round(float(r.kwh or 0.0), 2)
        if stream == "vendor":
            vendor[d] = kwh
        elif stream == "utility":
            utility[d] = kwh
        # 'other' is intentionally excluded from both named streams

    # Fold in the GMP per-account meter table (utility side) — sum per day across
    # the array's GMP accounts; only fills days the array's own utility rows miss.
    gmp_rows = db.execute(
        select(GmpDailyGeneration.day, GmpDailyGeneration.kwh)
        .where(GmpDailyGeneration.array_id == array_id)
    ).all()
    gmp_by_day: dict[str, float] = {}
    for day, kwh in gmp_rows:
        k = day.isoformat()
        gmp_by_day[k] = gmp_by_day.get(k, 0.0) + float(kwh or 0.0)
    for k, v in gmp_by_day.items():
        utility.setdefault(k, round(v, 2))

    def _tail(d: dict) -> list[dict]:
        items = sorted(d.items())[-days:]
        return [{"date": k, "kwh": v} for k, v in items]

    return {
        "vendor": _tail(vendor),
        "utility": _tail(utility),
        "has_vendor": bool(vendor),
        "has_utility": bool(utility),
    }


def _production_fallback_block(db, array_id: int, days: int = 14) -> dict:
    """Thin wrapper so fleet-tree columns always carry production_fallback."""
    from . import production_fallback as pf
    try:
        return pf.compute_production_fallback(db, array_id, days=days)
    except Exception:
        log.warning("production_fallback compute failed array=%s", array_id,
                    exc_info=True)
        return {
            "active": False,
            "source": None,
            "days_filled": 0,
            "vendor_last_day": None,
        }



# Fleet-local timezone for "what day is it" when judging health on COMPLETE days
# (stable_verdicts). Every Array Operator customer today is Vermont/GMP-territory,
# so Eastern is correct; make this per-tenant when a non-ET fleet onboards.
_FLEET_TZ = "America/New_York"


def build_fleet_tree(db, tenant: Tenant, *, force_refresh: bool = False,
                     stable_verdicts: bool = False,
                     stored_only: bool = False) -> dict:
    """Owner-grouped 3-tier tree. Inverters are read from the persisted table
    (owner's arrangement), telemetry pulled from each one's SOURCE site, then
    peer-analyzed WITHIN each owner array group — so a drag changes real cohorts.

    stable_verdicts=True is the E-MAIL path (morning digest / alert sweep): it
    judges each inverter's health on COMPLETE days only (drops today's partial
    dawn day) and uses a cohort-relative "gone quiet" test, so early-morning
    weather variability stops producing false "needs attention" alarms. The live
    dashboard leaves it False so it keeps showing the real-time picture.

    stored_only=True is the INSTANT first-paint path for the owner spreadsheet:
    skip vendor discovery + live equipment pulls, build entirely from persisted
    Inverter / InverterDaily / last_power_* rows. Frontend paints this in <1s,
    then upgrades with the live tree in the background. Without this, a cold
    SolarEdge site cache serially fetched ~190 equipment endpoints (~60–90s)
    before anything was usable.
    """
    if stored_only:
        inverters = _persisted_inverters(db, tenant)
    else:
        inverters = discover_and_persist(db, tenant, force_refresh=force_refresh)

    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
        .order_by(Array.id)
    ).scalars().all()
    array_by_id = {a.id: a for a in arrays}

    # Arrays that carry a UTILITY-METER account (created by the GMP/SmartHub generation
    # capture so an offtaker bill can bind to them). The vendor-data / inverter fleet is
    # a SEPARATE system from utility bills: a pure meter array (a UtilityAccount with NO
    # inverters) must NOT masquerade as a "vendor data" array. We keep it for billing but
    # exclude it from this inverter view below. An inverter array that ALSO has a linked
    # bill keeps its inverters and still shows (it's a real vendor array).
    _util_array_ids = set(db.execute(
        select(UtilityAccount.array_id).where(
            UtilityAccount.tenant_id == tenant.id,
            UtilityAccount.array_id.isnot(None),
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalars().all())
    # Array ids that have EVER had an inverter row (INCLUDING soft-deleted). A real
    # inverter array — even a flaky Chint one whose inverter count momentarily blips to
    # 0 — is always in here, so the meter-array filter below can NEVER hide it. Only an
    # array that has a utility account AND has never had any inverter is a pure meter array.
    _arr_ids = [a.id for a in arrays]
    _arrays_with_inverters = set(db.execute(
        select(Inverter.array_id).where(Inverter.array_id.in_(_arr_ids)).distinct()
    ).scalars().all()) if _arr_ids else set()

    # ── batched InverterConnection map (was N+1) ──────────────────────────────
    # _resolve_connection fired one SELECT per inverter in the loop below, so an
    # array with 20 inverters cost 20 round-trips per fleet-tree build. Load
    # every connection for this tenant's arrays once and resolve in-memory.
    _all_array_ids = list(array_by_id.keys())
    _conn_by_array: dict[int, InverterConnection] = {}
    if _all_array_ids:
        for c in db.execute(
            select(InverterConnection)
            .where(InverterConnection.array_id.in_(_all_array_ids))
        ).scalars().all():
            _conn_by_array.setdefault(c.array_id, c)

    def _conn_for(src_arr: Array):
        """In-memory equivalent of _resolve_connection: pre-loaded row, then the
        legacy SolarEdge-columns virtual connection fallback."""
        if src_arr is None:
            return None
        conn = _conn_by_array.get(src_arr.id)
        if conn is not None:
            return conn
        if src_arr.solaredge_api_key and src_arr.solaredge_site_id:
            from types import SimpleNamespace
            return SimpleNamespace(
                id=None, vendor="solaredge",
                config={"api_key": src_arr.solaredge_api_key,
                        "site_id": src_arr.solaredge_site_id},
                status="ok",
            )
        return None

    # Group persisted inverters by their OWNER array_id.
    by_array: dict[int, list[Inverter]] = defaultdict(list)
    for iv in inverters:
        by_array[iv.array_id].append(iv)

    # ── batched InverterDaily (was N+1) ───────────────────────────────────────
    # _merged_daily / _stored_inverter_daily used to SELECT per inverter inside
    # the hot loop. On a 190-inverter fleet that's 190 round-trips even in the
    # stored/lite path. Load recent days for every inverter once.
    # Match _stored_inverter_daily: last 14 ROWS per inverter (not a calendar
    # window) — a 20-day API outage must not blank the graph.
    _daily_by_iv: dict[int, list[dict]] = defaultdict(list)
    _iv_ids = [iv.id for iv in inverters if getattr(iv, "id", None) is not None]
    if _iv_ids:
        try:
            _drows = db.execute(
                select(InverterDaily)
                .where(InverterDaily.inverter_id.in_(_iv_ids))
                .order_by(InverterDaily.day.desc())
            ).scalars().all()
            _tmp: dict[int, list] = defaultdict(list)
            for r in _drows:
                bucket = _tmp[r.inverter_id]
                if len(bucket) >= 14:
                    continue
                bucket.append(r)
            for iid, rows in _tmp.items():
                _daily_by_iv[iid] = [
                    {"date": r.day.isoformat(), "kwh": r.kwh}
                    for r in sorted(rows, key=lambda x: x.day)
                ]
        except Exception:
            log.warning("fleet: batched InverterDaily load failed", exc_info=True)

    def _merged_daily_cached(inverter_id: int, live_series: list[dict]) -> list[dict]:
        """Same merge rule as _merged_daily, but against the preloaded store map."""
        merged: dict[str, float] = {}
        for pt in _daily_by_iv.get(inverter_id, []):
            merged[pt["date"]] = float(pt["kwh"] or 0)
        for pt in (live_series or []):
            d = pt.get("date")
            if isinstance(d, datetime):
                d = d.date().isoformat()
            elif isinstance(d, _date):
                d = d.isoformat()
            elif isinstance(d, str):
                d = d[:10]
            else:
                continue
            k = pt.get("kwh")
            if k is None:
                continue
            try:
                k = float(k)
            except (TypeError, ValueError):
                continue
            merged[d] = max(merged.get(d, 0), k)
        out = [{"date": d, "kwh": round(v, 3)} for d, v in sorted(merged.items())]
        return out[-14:]

    # Prefetch unique site telemetry OUTSIDE the inverter loop when live, so a
    # multi-array site is only pulled once and (with the parallel SE/Locus
    # workers) the whole fleet warms in one bounded burst. Sites themselves also
    # run concurrently — a 30-site SolarEdge book used to pay inventory+equipment
    # serially per site even after per-inverter fan-out.
    _tel_by_key: dict[tuple, dict] = {}
    if not stored_only:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        _site_jobs: list[tuple[tuple, str, dict, object]] = []
        _seen_sites: set[tuple] = set()
        for iv in inverters:
            src_arr = array_by_id.get(iv.source_array_id) or array_by_id.get(iv.array_id)
            conn = _conn_for(src_arr) if src_arr is not None else None
            if conn is None:
                continue
            site_id = (conn.config or {}).get("site_id")
            if not site_id:
                continue
            key = ((conn.vendor or iv.vendor or ""), str(site_id))
            if key in _seen_sites:
                continue
            _seen_sites.add(key)
            _site_jobs.append((key, conn.vendor or iv.vendor, conn.config or {}, site_id))

        def _pull_site(job):
            key, vendor, cfg, sid = job
            try:
                return key, _telemetry_for_site(vendor, cfg, sid, force=force_refresh)
            except Exception:
                log.warning("fleet: site telemetry failed for %s", key, exc_info=True)
                return key, {}

        if len(_site_jobs) <= 1:
            for job in _site_jobs:
                k, tel = _pull_site(job)
                _tel_by_key[k] = tel
        else:
            # Bound site-level concurrency; each site already fans out inverters.
            sw = min(6, len(_site_jobs))
            with ThreadPoolExecutor(max_workers=sw) as pool:
                futs = [pool.submit(_pull_site, job) for job in _site_jobs]
                for fut in as_completed(futs):
                    try:
                        k, tel = fut.result()
                    except Exception:
                        continue
                    _tel_by_key[k] = tel

    columns: list[dict] = []
    inv_total = 0
    # Regional (central-VT) sun-up default, computed once per build — the
    # board-wide summary flag and the fallback for arrays with no stored
    # location. Arrays WITH a lat/long get their own per-site verdict below
    # (_solar_state_for), so a distant array is never gated on Vermont's sun.
    regional_elev = _solar_elevation_now()
    daylight = (
        True if regional_elev is None
        else regional_elev > _DAYLIGHT_MIN_ELEVATION_DEG
    )
    _dl_cache: dict = {}
    # Cross-tenant live-power borrow map (vendor,serial) -> best fresh wattage
    # across ALL tenants. Lets a tenant whose browser captured a bogus near-zero
    # for a SHARED physical system show the real reading another tenant captured
    # for the same device this window. Computed once per build (upward-only).
    borrow_live = _cross_tenant_live_by_serial(db, inverters)
    for arr in arrays:
        ivs = sorted(by_array.get(arr.id, []), key=lambda x: (x.position, x.id))

        # Exclude pure UTILITY-METER arrays — a linked utility account AND never any
        # inverter (created by the GMP/SmartHub generation capture so an offtaker bill
        # can bind to them) — from the inverter/vendor fleet: they belong to the utility-
        # bills system, not "vendor data". A real inverter array (incl. one momentarily
        # at 0 inverters, like flaky Chint) is in _arrays_with_inverters and is NEVER
        # hidden. Kept in the DB for offtaker billing; just not shown in this view.
        if arr.id in _util_array_ids and arr.id not in _arrays_with_inverters:
            continue

        # THIS array's sun: its own coordinates when stored, else regional.
        arr_elev, arr_daylight = _solar_state_for(
            arr, regional_elev, daylight, _dl_cache
        )

        # Pull telemetry per source site (cached), then build peer-units for THIS
        # owner group (cohort = the inverters the owner placed under this array).
        units = []
        meta_by_serial = {}
        for iv in ivs:
            conn_vendor = iv.vendor
            # find the source connection's creds
            src_arr = array_by_id.get(iv.source_array_id) or arr
            conn = _conn_for(src_arr)
            tel_map = {}
            if not stored_only and conn is not None and (conn.config or {}).get("site_id"):
                key = ((conn.vendor or conn_vendor or ""), str(conn.config["site_id"]))
                tel_map = _tel_by_key.get(key) or {}
                if not tel_map and key not in _tel_by_key:
                    # Fallback if prefetch missed this site.
                    tel_map = _telemetry_for_site(
                        conn_vendor, conn.config or {}, conn.config["site_id"],
                        force=force_refresh,
                    )
            m = tel_map.get(iv.serial, {})
            # --- API-INDEPENDENT HISTORY STORE ---
            # 1) Whatever daily readings we just saw LIVE (e.g. SolarEdge's API),
            #    snapshot them into InverterDaily so the graph survives the next
            #    time that API is slow/down/off-peak. (Extension vendors already
            #    persisted their readings at capture time.)
            live_daily = m.get("daily") or []
            if live_daily and not stored_only:
                try:
                    _persist_daily_series(db, tenant.id, iv.id, live_daily,
                                          source=f"{conn_vendor or 'api'}_live")
                except Exception:
                    log.warning("fleet: failed to persist daily for inv %s", iv.id, exc_info=True)
            # 2) The graph's series is now STORAGE-authoritative (stored history with
            #    any fresh live readings merged on top) — never a bare live read that
            #    can vanish. Falls back gracefully to whatever live gave us.
            merged = _merged_daily_cached(iv.id, live_daily)
            m = dict(m)
            m["daily"] = merged if merged else live_daily
            # Extension vendors often lack live-API last_report — fall back to
            # source_last_data_at / last_power_at so dead units (e.g. Danville #54
            # silent since Jul 1 while peers still report) trip comm_gap/dead.
            _lr = m.get("last_report")
            if not _lr:
                _src = _sane_dt(getattr(iv, "source_last_data_at", None))
                _cap = _sane_dt(getattr(iv, "last_power_at", None))
                _pick = _src or _cap
                if _pick is not None:
                    _lr = _pick.isoformat()
            m["last_report"] = _lr
            # stored/lite path: seed last_power from the Inverter row so kW-now
            # paints without a live equipment call.
            if m.get("last_power_w") is None and getattr(iv, "last_power_w", None) is not None:
                m["last_power_w"] = iv.last_power_w
            meta_by_serial[iv.serial] = m
            units.append({
                "id": iv.serial,
                "nameplate_kw": _eff_nameplate_kw(iv, m),
                "daily": m.get("daily", []),
                "error_code": m.get("error_code"),
                "last_report": _lr,
                # Owner-confirmed structural underperformance: the verdict engine
                # re-baselines these against expected_low_baseline instead of the
                # cohort floor (see peer_analysis.analyze_cohort).
                "expected_low": bool(getattr(iv, "expected_low", False)),
                "expected_low_baseline": getattr(iv, "expected_low_baseline", None),
                "expected_low_reason": getattr(iv, "expected_low_reason", None),
            })

        analyzed = peer_analysis.analyze_cohort(
            units,
            complete_days_only=stable_verdicts,
            tz_name=_FLEET_TZ if stable_verdicts else None,
        ) if units else {"units": []}
        an_by_id = {u["id"]: u for u in analyzed["units"]}

        inv_rows = []
        _inv_today_iso = local_today().isoformat()   # per-inverter "today" match key
        for iv in ivs:
            u = an_by_id.get(iv.serial, {})
            m = meta_by_serial.get(iv.serial, {})
            # Per-inverter daily kWh series (last ~14 days, ascending) drives the
            # card's output graph + the real Min/Max daily-output figures. SolarEdge
            # gives 7 days of equipment telemetry; extension-captured vendors store
            # their own InverterDaily rows (read above into m["daily"]).
            daily = [
                {"date": d.get("date"), "kwh": round(float(d.get("kwh") or 0.0), 2)}
                for d in (m.get("daily") or [])
                if d.get("kwh") is not None
            ]
            kwh_vals = [d["kwh"] for d in daily]
            min_kwh = round(min(kwh_vals), 2) if kwh_vals else None
            peak_kwh = round(max(kwh_vals), 2) if kwh_vals else None
            inv_rows.append({
                "inverter_id": iv.id,
                "sn": iv.serial,
                "name": iv.name or m.get("name") or iv.serial,
                "model": iv.model or m.get("model"),
                "vendor": iv.vendor,
                "nameplate_kw": _eff_nameplate_kw(iv, m),
                "peer_index": u.get("peer_index"),
                "status": u.get("status", "ok"),
                # Dead-energy-register flag (default off; set by _flag_no_energy_register
                # below once the whole cohort is known). True = live power but no
                # cumulative-energy history, so it can't be peer-graded — surfaces
                # render an honest "no energy data" state, never Error/Offline.
                "no_energy_register": False,
                "diagnosis": u.get("diagnosis"),
                "window_kwh": u.get("window_kwh"),
                # Owner-confirmed structural underperformance (shading/obstruction).
                # Carried so every surface renders the calm "expected lower" state
                # instead of a red "underperforming", and so the agent can reason
                # about it. expected_low_breach = it dropped BELOW its baseline (a
                # real new problem) and IS flagged underperforming despite the mark.
                "expected_low": bool(getattr(iv, "expected_low", False)),
                "expected_low_reason": getattr(iv, "expected_low_reason", None),
                "expected_low_baseline": getattr(iv, "expected_low_baseline", None),
                "expected_low_breach": bool(u.get("expected_low_breach")),
                # This inverter's TODAY kWh (the last daily point IF it's today), so the
                # spreadsheet's Today column can show per-inverter output, not just the
                # array total. None when today hasn't been captured yet (no misleading 0).
                "produced_today_kwh": next(
                    (d["kwh"] for d in reversed(daily)
                     if str(d.get("date")) == _inv_today_iso and (d.get("kwh") or 0) > 0),
                    None,
                ),
                "daily": daily,                       # ascending [{date,kwh}] for the sparkline
                "min_kwh": min_kwh,                   # lowest daily output in the window (real)
                "peak_kwh": peak_kwh,                 # highest daily output in the window (real)
                "last_mode": m.get("last_mode"),
                "current_power_w": _live_power_w(iv, m, daylight=arr_daylight, borrow=borrow_live),
                # Per-inverter live-power provenance, for the live-anomaly detectors
                # (inverter_alert_sweep). power_age_hours = how long ago WE captured
                # THIS device's power (last_power_at); power_estimated = the value is
                # a site-total split fill, not a real per-device reading. A "dark
                # right now" verdict must rest on a fresh, real per-device reading —
                # never a stale one and never a fill — else a partial Fronius capture
                # fabricates dark alerts on healthy inverters (Bruce's Chester, Jul-03).
                "power_age_hours": (
                    round((now() - _sane_dt(iv.last_power_at)).total_seconds() / 3600.0, 2)
                    if _sane_dt(iv.last_power_at) else None
                ),
                "power_estimated": bool(getattr(iv, "last_power_estimated", False)),
                # Freshness basis. For extension vendors prefer the SOURCE's own
                # last-data time (source_last_data_at) — the real "is this current?"
                # signal (Fronius LastImport / SMA reading ts) — falling back to OUR
                # capture time (last_power_at) only when no source ts was captured
                # (older rows, or SMA before it ships its timestamp).
                "last_report": (
                    u.get("last_report") or m.get("last_report")
                    or (_sane_dt(iv.source_last_data_at).isoformat()
                        if _sane_dt(iv.source_last_data_at) and iv.vendor in _EXT_CAPTURED_VENDORS
                        else (_sane_dt(iv.last_power_at).isoformat()
                              if _sane_dt(iv.last_power_at) and iv.vendor in _EXT_CAPTURED_VENDORS
                              else None))
                ),
                # True when last_report above is the SOURCE's REAL timestamp (not the
                # capture-time proxy) — so a stale state can read as a genuine outage.
                # _sane_dt guards a garbage (1970-epoch) gauge ts from claiming this.
                "has_src_ts": bool(_sane_dt(iv.source_last_data_at) and iv.vendor in _EXT_CAPTURED_VENDORS),
                "source_array_id": iv.source_array_id,
                "moved": iv.source_array_id is not None and iv.source_array_id != iv.array_id,
                "origin_url": _portal_link(iv.vendor, iv.source_site_id),
                "origin_label": _VENDOR_LABEL.get((iv.vendor or "").lower()) or (iv.vendor or None),
            })
        inv_total += len(inv_rows)

        # Flag any dead-energy-register unit (live power but no cumulative energy,
        # e.g. Tannery #7) so every surface renders an honest "no energy data"
        # state instead of a harsh Error/Offline. Mirrors the digest predicate.
        _flag_no_energy_register(inv_rows)

        # vendor mix for the array chip. Derived from actual Inverter rows when
        # any exist — but a FRESHLY-connected array (discover/connect-account
        # just ran; the poller hasn't created its first Inverter row yet) has
        # none, and would otherwise report vendor=None. That's not "unknown" —
        # we know the vendor from InverterConnection — so fall back to it.
        # Found 2026-07-08 verifying the Fronius account-connect flow live: the
        # frontend was masking this gap with a hardcoded "solaredge" default
        # (now removed as its own honesty bug), which showed "Open in
        # SolarEdge" for a brand-new Fronius connection.
        vendors = sorted({iv.vendor for iv in ivs})
        if not vendors:
            _conn = _conn_by_array.get(arr.id)
            if _conn is not None:
                vendors = [_conn.vendor]

        # Distinct origin-site deep links among this array's inverters.
        seen: dict[tuple, dict] = {}
        for iv in ivs:
            url = _portal_link(iv.vendor, iv.source_site_id)
            if not url:
                continue
            key = (iv.vendor, iv.source_site_id or "")
            if key in seen:
                continue
            seen[key] = {"vendor": iv.vendor,
                         "label": _VENDOR_LABEL.get((iv.vendor or "").lower()) or iv.vendor,
                         "site_id": iv.source_site_id, "url": url}
        origin_links = [seen[k] for k in sorted(seen, key=lambda t: (str(t[0] or ""), str(t[1] or "")))]

        # Source-data freshness for this array. The vendor portal (e.g. SolarEdge)
        # reports each inverter's last_report timestamp; when a site stops sending
        # data to its own vendor, last_report goes stale. We surface that as a
        # SOURCE outage (the vendor isn't receiving data) — distinct from any
        # problem on our side. Computed from the freshest inverter last_report.
        # Extension-captured vendors use a 26h threshold (vs 6h for API-polled)
        # because their last_report proxy (last_power_at) only advances during
        # production — a normal overnight gap must not trigger SOURCE OFFLINE.
        _arr_vendors = {iv.vendor for iv in ivs}
        _is_ext_only = bool(_arr_vendors) and _arr_vendors.issubset(_EXT_CAPTURED_VENDORS)
        src_status = _source_status(inv_rows, stale_hours=_SOURCE_STALE_HOURS)
        # "SOURCE OFFLINE" only means something for vendors we actively POLL
        # (SolarEdge), where last_report is the SOURCE's own telemetry clock — a
        # gap there genuinely means the site stopped reporting to its vendor.
        # Extension-captured vendors (Fronius/SMA/Chint) are different: we only
        # have data when the owner's browser captures it, so last_report is OUR
        # capture time and a gap reflects how recently they logged in — NOT a
        # source outage. We can't pull them on a schedule, so claiming "offline"
        # from a capture gap fired falsely on every array the owner hadn't
        # re-opened lately. Married to how we actually pull: for capture-only
        # vendors a stale gap is "unpolled" (no outage banner), never "stale".
        # ...UNLESS we now have the source's OWN timestamp (source_last_data_at): then
        # a stale state is a REAL source outage (the inverter stopped reporting to its
        # vendor), so keep "stale" and show the honest outage banner like SolarEdge.
        # Only the capture-time-proxy gap stays "unpolled".
        _has_src_ts = any(r.get("has_src_ts") for r in inv_rows)
        if _is_ext_only and src_status.get("state") == "stale" and not _has_src_ts:
            src_status = {**src_status, "state": "unpolled"}

        # ARRAY-LEVEL live power (server-computed, authoritative). Sum the
        # per-inverter live readings so the card no longer has to aggregate
        # client-side (which broke on a stale SPA bundle) and every surface reads
        # ONE number. None when NO inverter has a live value (so the card can show
        # an honest "no live feed" rather than a fake 0).
        _live_vals = [
            i["current_power_w"] for i in inv_rows
            if i.get("current_power_w") is not None
        ]
        array_power_w = round(sum(_live_vals), 1) if _live_vals else None

        # HONEST "did it produce today" signal — the antidote to the whack-a-mole
        # where a near-zero/stale LIVE reading makes a healthy array read "IDLE /
        # not producing" even though it generated energy all day (Bruce's
        # Waterford: 10 inverters live at 3 W each = 30 W "idle" while it logged
        # 591 kWh today). The card uses this to show "produced today · live feed
        # updating" instead of a bald "not producing right now" when live≈0 but
        # the array clearly worked today. Read from the array's own daily history
        # (today's row), independent of the flaky instantaneous feed.
        # Fleet-LOCAL day key — DailyGeneration/InverterDaily days are local
        # (US/Eastern) production days; a UTC key made "produced today" point at
        # an empty tomorrow-row every evening after ~8pm ET. Must move in
        # lockstep with the capture write key (models.local_today).
        _today_iso = local_today().isoformat()
        _daily_rows = _array_daily(db, arr.id)
        _today_row = next(
            (r for r in _daily_rows if r.get("date") == _today_iso), None
        )
        _today_kwh = _today_row["kwh"] if _today_row else None
        # Provenance for "produced today": the array-level row's own source, or
        # "live" when we fall back to summing per-inverter telemetry below.
        _today_source = _today_row.get("source") if _today_row else None
        # SolarEdge (and any API-polled vendor) writes the array-level DailyGeneration
        # row only on the nightly pull, so today's array total is blank intraday — while
        # the extension vendors (SMA/Fronius/Chint) write a daily row the moment they
        # capture. Each SolarEdge inverter's LIVE telemetry already carries today's kWh,
        # so when the array-level today row is missing, sum the per-inverter series for
        # today. Display-only (no DailyGeneration write) so billing/history stay sourced
        # from the nightly pull. Fixes Bruce: "SolarEdge total kWh for the day not showing
        # like the other vendors."
        if not (_today_kwh and _today_kwh > 0):
            _inv_today = [
                d["kwh"]
                for row in inv_rows
                for d in (row.get("daily") or [])
                if d.get("date") == _today_iso and d.get("kwh") is not None
            ]
            if _inv_today:
                _today_kwh = round(sum(_inv_today), 2)
                _today_source = "live"
        produced_today_kwh = _today_kwh if (_today_kwh and _today_kwh > 0) else None
        # Only attribute a source when we actually have a value to show.
        produced_today_source = _today_source if produced_today_kwh is not None else None
        produced_today_is_estimated = produced_today_source == "bill_prorate"

        columns.append({
            "array_id": arr.id,
            "array_name": arr.name,
            # Operator-assigned portfolio/group label (Analysis-tab fleet
            # hierarchy). None until the owner groups this site.
            "portfolio_name": arr.portfolio_name,
            # Operator O&M note shown in the Analysis Sites "Reminder" column.
            "reminder": arr.reminder,
            "vendor": vendors[0] if len(vendors) == 1 else None,
            "vendors": vendors,
            "inverter_source": "live" if ivs else None,
            "inverter_count": len(inv_rows),
            # Server-computed array live power (W) = sum of per-inverter live
            # readings; None when no inverter has a live value. Authoritative so
            # the card need not aggregate client-side.
            "current_power_w": array_power_w,
            # Today's generated kWh from the array's own daily history (the source
            # of truth that the flaky instantaneous live feed is NOT). Lets the
            # card show "produced today" instead of "not producing" when live≈0.
            "produced_today_kwh": produced_today_kwh,
            # Provenance for produced_today_kwh: "csv"/"gmp"/"smarthub"/vendor =
            # measured; "bill_prorate" = estimated from a utility bill; "live" =
            # summed per-inverter telemetry. is_estimated lets the card render an
            # estimated day distinctly instead of as a metered reading.
            "produced_today_source": produced_today_source,
            "produced_today_is_estimated": produced_today_is_estimated,
            "alert": _array_alert(inv_rows),
            "inverters": inv_rows,
            "origin_links": origin_links,
            # Array-level production history (DailyGeneration). Drives the array
            # graph when the vendor gives site-level history but no per-inverter
            # series (Chint weekETrend backfill); ascending [{date,kwh}].
            "daily": _array_daily(db, arr.id),
            # The SAME data split into the two source streams the sandbox slider
            # toggles between — VENDOR (inverter telemetry) vs UTILITY (meter).
            # System still integrates both; this just lets the owner view each on
            # its own. {vendor:[...], utility:[...], has_vendor, has_utility}.
            "daily_split": _array_daily_split(db, arr.id),
            # Vendor-offline continuity: when inverter feed is dead but the
            # utility meter still has days, the UI draws utility with a
            # provenance chip (never relabels as vendor). See production_fallback.
            "production_fallback": _production_fallback_block(db, arr.id),
            # Sun-up flag for the card "Sleeping" night state. The card gates
            # "Sleeping" on (is_daylight==False AND output==0) so a daytime fault
            # that zeroes output never reads as "asleep". Per-array when the
            # array has a stored lat/long, else regional central-VT.
            "is_daylight": arr_daylight,
            # Degrees above horizon at this array (NOAA approx). Soft night is
            # elev ≤ -2°; live peer-compare alerts only fire when elev ≥
            # LIVE_COMPARE_MIN_ELEVATION_DEG (dusk/dawn shoulder otherwise).
            "solar_elevation_deg": (
                None if arr_elev is None else round(float(arr_elev), 2)
            ),
            "live_compare_ok": _live_compare_ok_elev(arr_elev),
            # {"state": ok|stale|dark|none, "last_report": iso|None, "age_hours": float|None}
            # — surfaced on the card so a vendor-side reporting gap reads as a
            # SOURCE outage, not an app failure.
            "source_status": src_status,
            # {"synced_at": iso|None, "age_min": float|None} — how recently WE captured
            # (max last_seen_at). Advances on every successful capture incl. overnight, so
            # the UI shows "Synced Xm ago" (our pipeline is alive) DISTINCT from the
            # vendor source's own data age above. The two clocks together are the honest,
            # legible freshness mirror.
            "sync_status": _sync_recency(ivs),
        })

    attention = sum(c["alert"]["count"] for c in columns)
    # Commit the daily history we snapshotted into InverterDaily during the build
    # (persist-on-read). Never let a storage hiccup break the tree the owner sees.
    # stored/lite path never writes live daily, so skip the commit.
    if not stored_only:
        try:
            db.commit()
        except Exception:
            log.warning("fleet: daily-history commit failed", exc_info=True)
            db.rollback()
    return {
        "generated_at": now().replace(microsecond=0).isoformat() + "Z",
        "tiers": ["alerts", "arrays", "inverters"],
        "mode": "stored" if stored_only else "live",
        "columns": columns,
        "summary": {
            "arrays_total": len(columns),
            "inverters_total": inv_total,
            "attention": attention,
            # Board-wide sun-up flag (regional default). The card layer ANDs this
            # with per-inverter output==0 to pick the calm "Sleeping" state.
            "is_daylight": daylight,
        },
    }


# ─────────────────────────────── mutations ───────────────────────────────────

class FleetError(Exception):
    """Raised for invalid owner mutations (bad ids, cross-tenant, etc.)."""


def reassign_inverter(db, tenant: Tenant, inverter_id: int, target_array_id: int,
                      position: Optional[int] = None) -> Inverter:
    """Move an inverter to a different array (the owner's drag). Telemetry source
    is untouched — only the owner grouping changes. Re-sequences positions."""
    iv = db.get(Inverter, inverter_id)
    if iv is None or iv.tenant_id != tenant.id or iv.deleted_at is not None:
        raise FleetError("Inverter not found")
    target = db.get(Array, target_array_id)
    if target is None or target.tenant_id != tenant.id or target.deleted_at is not None:
        raise FleetError("Target array not found")

    iv.array_id = target_array_id
    # place at end unless a position is given
    if position is None:
        maxpos = db.execute(
            select(Inverter.position).where(
                Inverter.tenant_id == tenant.id, Inverter.array_id == target_array_id,
                Inverter.deleted_at.is_(None), Inverter.id != iv.id,
            ).order_by(Inverter.position.desc())
        ).scalars().first()
        iv.position = (maxpos or 0) + 1
    else:
        iv.position = int(position)
    db.commit()
    db.refresh(iv)
    return iv


def rename_array(db, tenant: Tenant, array_id: int, name: str) -> Array:
    """Rename an owner array (the inline edit in the Sandbox / Spreadsheet view).

    Tenant-scoped (array must belong to `tenant`, else FleetError → 404). The name
    is trimmed; empty is rejected. Array names are unique per tenant
    (uq_array_per_tenant), so a clash with ANOTHER of this tenant's arrays raises
    FleetError (route → 409) — mirroring api.account.update_array's rule. Renaming
    to the SAME name is a no-op (returns the row unchanged)."""
    arr = db.get(Array, array_id)
    if arr is None or arr.tenant_id != tenant.id or arr.deleted_at is not None:
        raise FleetError("Array not found")
    new_name = (name or "").strip()
    if not new_name:
        raise FleetError("Name cannot be empty")
    if new_name == arr.name:
        return arr                                  # no-op
    # Only a LIVE array reserves a name — a soft-deleted one no longer blocks
    # renaming to that name (uniqueness is a partial index over live rows).
    clash = db.execute(
        select(Array).where(
            Array.tenant_id == tenant.id,
            Array.name == new_name,
            Array.id != arr.id,
            Array.deleted_at.is_(None),
        )
    ).scalars().first()
    if clash is not None:
        raise FleetError("Another array already has that name")
    arr.name = new_name
    db.commit()
    db.refresh(arr)
    return arr


def rename_inverter(db, tenant: Tenant, inverter_id: int, name: str) -> Inverter:
    """Rename an owner inverter (the inline edit in either dashboard view).

    Tenant-scoped (inverter must belong to `tenant`, else FleetError → 404). The
    name is trimmed; empty is rejected. NO uniqueness check — inverters may share
    a name across arrays (e.g. "Inverter 1" under several sites). Sets
    name_is_custom so a later telemetry sync (discover_and_persist) never
    overwrites the owner's chosen name."""
    iv = db.get(Inverter, inverter_id)
    if iv is None or iv.tenant_id != tenant.id or iv.deleted_at is not None:
        raise FleetError("Inverter not found")
    new_name = (name or "").strip()
    if not new_name:
        raise FleetError("Name cannot be empty")
    iv.name = new_name
    iv.name_is_custom = True
    db.commit()
    db.refresh(iv)
    return iv


def current_peer_index(db, tenant: Tenant, inverter_id: int) -> float | None:
    """The inverter's STABLE 14-day peer_index right now (same verdict the dashboard
    and emails use). Used to seed an expected-low baseline. None when it can't be
    peer-graded yet (solo cohort, or no captured history)."""
    tree = build_fleet_tree(db, tenant, stable_verdicts=True)
    for col in tree.get("columns", []):
        for r in col.get("inverters", []):
            if r.get("inverter_id") == inverter_id:
                return r.get("peer_index")
    return None


def set_expected_low(db, tenant: Tenant, inverter_id: int, *, expected_low: bool,
                     reason: str | None = None, baseline: float | None = None,
                     set_by: str = "owner") -> Inverter:
    """Mark (or clear) an inverter as OWNER-CONFIRMED expected-low — it runs
    permanently below its peers for a fixed physical reason (afternoon shade from a
    neighbour's tree, a chimney, a poor roof face). When set, we record its CURRENT
    stable peer_index as the baseline and the verdict engine re-judges it against
    THAT level, not the cohort floor (peer_analysis.analyze_cohort) — so it reads
    calm while it holds, but still flags + alerts if it drops beneath the baseline
    (a genuine new fault on top of the shading). Tenant-scoped."""
    iv = db.get(Inverter, inverter_id)
    if iv is None or iv.tenant_id != tenant.id or iv.deleted_at is not None:
        raise FleetError("Inverter not found")
    if expected_low:
        if baseline is None:
            baseline = current_peer_index(db, tenant, inverter_id)
        if baseline is None:
            raise FleetError(
                "Not enough peer history yet to set a baseline for this inverter — it "
                "needs a few days of data alongside at least one neighbour first."
            )
        iv.expected_low = True
        iv.expected_low_reason = ((reason or "").strip()[:240] or None)
        iv.expected_low_baseline = float(baseline)
        iv.expected_low_set_at = now()
        iv.expected_low_set_by = (set_by or "owner")[:40]
    else:
        iv.expected_low = False
        iv.expected_low_reason = None
        iv.expected_low_baseline = None
        iv.expected_low_set_at = None
        iv.expected_low_set_by = None
    db.commit()
    db.refresh(iv)
    return iv


def reorder_within_array(db, tenant: Tenant, array_id: int, ordered_ids: list[int]) -> None:
    """Persist the order of inverters within one array (drag-to-reorder)."""
    arr = db.get(Array, array_id)
    if arr is None or arr.tenant_id != tenant.id:
        raise FleetError("Array not found")
    pos = 1
    for iid in ordered_ids:
        iv = db.get(Inverter, iid)
        if iv is not None and iv.tenant_id == tenant.id and iv.array_id == array_id:
            iv.position = pos
            pos += 1
    db.commit()


def create_array(db, tenant: Tenant, name: str) -> Array:
    """Create a new owner-defined array (empty group to drag inverters into).
    No utility/connection — purely an owner grouping that inverters reference.

    Array names are unique per tenant among LIVE rows (partial index
    uq_array_per_tenant_live), so if a LIVE array already has the requested name
    we auto-suffix (" 2", " 3", …) rather than 500. A soft-deleted array of the
    same name is revived (only when no live one exists) instead of orphaned."""
    nm = (name or "").strip() or "New array"

    # A live array owns the name → keep the new group distinct by auto-suffixing.
    live = db.execute(
        select(Array).where(
            Array.tenant_id == tenant.id, Array.name == nm,
            Array.deleted_at.is_(None),
        )
    ).scalars().first()
    if live is None:
        # No live array by this name — revive a soft-deleted same-name array if
        # one exists (preserves history) rather than spawning a duplicate row.
        ghost = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id, Array.name == nm,
                Array.deleted_at.is_not(None),
            ).limit(1)
        ).scalars().first()
        if ghost is not None:
            ghost.deleted_at = None
            db.commit()
            db.refresh(ghost)
            return ghost
    else:
        base = nm
        for i in range(2, 100):
            cand = f"{base} {i}"
            clash = db.execute(
                select(Array).where(
                    Array.tenant_id == tenant.id, Array.name == cand,
                    Array.deleted_at.is_(None),
                )
            ).scalars().first()
            if clash is None:
                nm = cand
                break

    arr = Array(tenant_id=tenant.id, name=nm, fuel_type="solar")
    db.add(arr)
    db.commit()
    db.refresh(arr)
    return arr


def delete_array(db, tenant: Tenant, array_id: int) -> Array:
    """Soft-delete an owner array and its inverters (the owner's "remove card").

    SOFT-delete only — sets `deleted_at` on the Array AND every Inverter that
    references it, so the array vanishes from build_fleet_tree (which filters
    `Array.deleted_at.is_(None)`) and its inverters don't dangle pointing at a
    dead array. NEVER hard-deletes (an undo / restore can revive the rows).

    Ownership: the array must belong to `tenant`; otherwise raises FleetError
    (which the route turns into a 404), so a cross-tenant id leaks nothing.
    Idempotent: an already-deleted array is treated as not-found.

    AO arrays have client_id=None and AO billing is per-kWh metered (not
    per-array), so this deliberately does NOT touch Stripe / subscription
    reconcile — unlike api.account.delete_array for operator client arrays.
    """
    arr = db.get(Array, array_id)
    if arr is None or arr.tenant_id != tenant.id or arr.deleted_at is not None:
        raise FleetError("Array not found")

    ts = now()
    arr.deleted_at = ts
    invs = db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant.id,
            Inverter.array_id == array_id,
            Inverter.deleted_at.is_(None),
        )
    ).scalars().all()
    for iv in invs:
        iv.deleted_at = ts
    db.commit()
    db.refresh(arr)
    return arr


def restore_array(db, tenant: Tenant, array_id: int) -> Array:
    """Un-delete (restore) a soft-deleted owner array and the inverters that were
    deleted ALONGSIDE it (same deleted_at timestamp).

    The inverse of delete_array: clears `deleted_at` on the Array and on exactly the
    Inverter rows that shared the array's deletion timestamp — so inverters that were
    already removed BEFORE the array was deleted stay removed, and we don't revive
    stragglers. Powers the sandbox "Undo delete". Ownership-checked (cross-tenant or
    unknown id raises FleetError → 404). Idempotent: a not-currently-deleted array
    is treated as not-found.
    """
    arr = db.get(Array, array_id)
    if arr is None or arr.tenant_id != tenant.id or arr.deleted_at is None:
        raise FleetError("Array not found")

    ts = arr.deleted_at
    arr.deleted_at = None
    invs = db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant.id,
            Inverter.array_id == array_id,
            Inverter.deleted_at == ts,
        )
    ).scalars().all()
    for iv in invs:
        iv.deleted_at = None
    db.commit()
    db.refresh(arr)
    return arr


def delete_inverter(db, tenant: Tenant, inverter_id: int) -> Inverter:
    """Soft-delete a SINGLE inverter (the owner's right-click "Delete inverter").

    SOFT-delete only — sets `deleted_at` on the one Inverter row so it vanishes
    from build_fleet_tree (which filters `Inverter.deleted_at.is_(None)`) while
    leaving its parent array and siblings untouched. NEVER hard-deletes, so an
    undo / restore can revive the row. Ownership-checked: the inverter must
    belong to `tenant`, else FleetError (route → 404), so a cross-tenant id
    leaks nothing. Idempotent: an already-deleted inverter is treated as
    not-found.

    AO billing is per-kWh metered (not per-inverter), so this deliberately does
    NOT touch Stripe — same as delete_array.
    """
    iv = db.get(Inverter, inverter_id)
    if iv is None or iv.tenant_id != tenant.id or iv.deleted_at is not None:
        raise FleetError("Inverter not found")
    iv.deleted_at = now()
    db.commit()
    db.refresh(iv)
    return iv


def restore_inverter(db, tenant: Tenant, inverter_id: int) -> Inverter:
    """Un-delete a soft-deleted inverter (the inverse of delete_inverter).

    Clears `deleted_at` so the inverter reappears in the fleet tree under its
    array. Powers the sandbox "Undo delete" for a single inverter.
    Ownership-checked (cross-tenant or unknown id → FleetError → 404).
    Idempotent: a not-currently-deleted inverter is treated as not-found.

    Guards against a dangling revive: if the inverter's parent array is itself
    soft-deleted, the inverter stays hidden in the tree (the array filter wins),
    which is correct — restoring the array brings its inverters back.
    """
    iv = db.get(Inverter, inverter_id)
    if iv is None or iv.tenant_id != tenant.id or iv.deleted_at is None:
        raise FleetError("Inverter not found")
    iv.deleted_at = None
    db.commit()
    db.refresh(iv)
    return iv


def reset_layout(db, tenant: Tenant) -> int:
    """Snap every inverter back to its discovered (source) array grouping.
    Returns count reset. Empty owner-created arrays are left in place."""
    invs = db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant.id, Inverter.deleted_at.is_(None)
        )
    ).scalars().all()
    n = 0
    for iv in invs:
        if iv.source_array_id and iv.array_id != iv.source_array_id:
            iv.array_id = iv.source_array_id
            n += 1
    db.commit()
    return n
