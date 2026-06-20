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
from collections import defaultdict
from datetime import timedelta, date as _date, datetime
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .models import Array, DailyGeneration, Inverter, InverterConnection, InverterDaily, Tenant, now
from .inverters import peer_analysis

log = logging.getLogger(__name__)

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


def _is_daylight(lat: float | None = None, lon: float | None = None,
                 when: datetime | None = None) -> bool:
    """True when the sun is up at the given location (UTC `when`, default now).
    Falls back to the central-Vermont regional default when no per-array
    coordinates are known (current state — no lat/long stored yet)."""
    import datetime as _dt
    w = when or _dt.datetime.now(_dt.timezone.utc)
    # the helper does its own UTC math; ensure naive UTC for timetuple/hour reads
    if w.tzinfo is not None:
        w = w.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    try:
        elev = _solar_elevation_deg(w, lat if lat is not None else _VT_LAT,
                                    lon if lon is not None else _VT_LON)
    except Exception:
        return True   # never let a sun-calc error hide a real card — default to "day"
    return elev > _DAYLIGHT_MIN_ELEVATION_DEG


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

def _telemetry_for_site(vendor: str, api_key: str, site_id, *, force: bool = False) -> dict:
    """Return {serial: {name, model, nameplate_kw, daily, error_code, last_report,
    last_mode, last_power_w}} for one source site. Cached 10 min. SolarEdge only
    today; other vendors return {} until their per-inverter capture lands."""
    ck = f"{vendor}:{site_id}"
    if not force:
        hit = _site_cache.get(ck)
        if hit and (now() - hit[0]) < _SITE_TTL:
            return hit[1]

    out: dict[str, dict] = {}
    if vendor == "solaredge":
        from .adapters import solaredge as _se
        try:
            inv = _se.fetch_inventory(api_key, int(site_id))
        except _se.SolarEdgeError as exc:
            log.warning("fleet: inventory fetch failed for site %s: %s", site_id, exc)
            return _site_cache.get(ck, (None, {}))[1] if ck in _site_cache else {}
        for it in inv:
            sn = it.get("sn")
            if not sn:
                continue
            try:
                tel = _se.fetch_inverter_telemetry(api_key, int(site_id), sn, days_back=7)
            except _se.SolarEdgeError:
                tel = {"daily": [], "error_code": None, "last_report": None,
                       "last_mode": None, "last_power_w": None}
            out[str(sn)] = {
                "name": it.get("name"), "model": it.get("model"),
                "nameplate_kw": it.get("nameplate_kw"),
                "daily": tel["daily"], "error_code": tel["error_code"],
                "last_report": tel["last_report"], "last_mode": tel.get("last_mode"),
                "last_power_w": tel.get("last_power_w"),
            }
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
    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
    ).scalars().all()

    existing = {
        (iv.vendor, iv.serial): iv
        for iv in db.execute(
            select(Inverter).where(Inverter.tenant_id == tenant.id)
        ).scalars().all()
    }

    for arr in arrays:
        conn = _resolve_connection(db, arr)
        if conn is None:
            continue
        vendor = conn.vendor
        cfg = conn.config or {}
        api_key, site_id = cfg.get("api_key"), cfg.get("site_id")
        if not (api_key and site_id):
            continue
        tel = _telemetry_for_site(vendor, api_key, site_id, force=force_refresh)
        for serial, m in tel.items():
            key = (vendor, str(serial))
            iv = existing.get(key)
            if iv is None:
                # Find the next position under this (source) array.
                maxpos = db.execute(
                    select(Inverter.position).where(
                        Inverter.tenant_id == tenant.id,
                        Inverter.array_id == arr.id,
                        Inverter.deleted_at.is_(None),
                    ).order_by(Inverter.position.desc())
                ).scalars().first()
                iv = Inverter(
                    tenant_id=tenant.id,
                    array_id=arr.id,                 # owner grouping starts = source
                    position=(maxpos or 0) + 1,
                    vendor=vendor, serial=str(serial),
                    source_site_id=str(site_id),
                    source_connection_id=getattr(conn, "id", None),
                    source_array_id=arr.id,
                )
                db.add(iv)
                existing[key] = iv
            else:
                # Refresh source pointers in case the connection moved, but DO NOT
                # touch array_id/position (owner's layout).
                iv.source_site_id = str(site_id)
                iv.source_connection_id = getattr(conn, "id", None)
                iv.source_array_id = iv.source_array_id or arr.id
                if iv.deleted_at is not None:
                    iv.deleted_at = None
            # metadata refresh (cheap, safe)
            iv.name = m.get("name") or iv.name or str(serial)
            iv.model = m.get("model") or iv.model
            if m.get("nameplate_kw") is not None:
                iv.nameplate_kw = m.get("nameplate_kw")
            iv.last_seen_at = now()

    db.commit()
    return db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant.id, Inverter.deleted_at.is_(None)
        ).order_by(Inverter.array_id, Inverter.position)
    ).scalars().all()


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


def _source_status(inv_rows: list[dict]) -> dict:
    """Array-level source-data freshness from the inverters' last_report.

    Returns {state, last_report, age_hours}:
      • "none"  — no live-capable feed at all (no last_report on any inverter).
      • "ok"    — freshest report within the staleness window.
      • "stale" — freshest report older than the window (the VENDOR/source has
                  not received data recently — a source-side outage, not ours).
    last_report is the most-recent inverter timestamp (ISO), age_hours its age.
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
            stamps.append(dt)
        except (ValueError, TypeError):
            continue
    if not stamps:
        return {"state": "none", "last_report": None, "age_hours": None}
    freshest = max(stamps)
    age_h = round((datetime.now(timezone.utc) - freshest).total_seconds() / 3600.0, 1)
    state = "stale" if age_h >= _SOURCE_STALE_HOURS else "ok"
    return {"state": state, "last_report": freshest.isoformat(), "age_hours": age_h}


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
    fresh_after = now() - _POWER_FRESH
    rows = db.execute(
        select(Inverter).where(
            Inverter.serial.in_(wanted_serials),
            Inverter.deleted_at.is_(None),
            Inverter.last_power_w.isnot(None),
            Inverter.last_power_at.isnot(None),
            Inverter.last_power_at >= fresh_after,
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
    if base is None and daylight and (
        iv.last_power_w is not None and iv.last_power_at is not None
        and (now() - iv.last_power_at) <= _POWER_FRESH
    ):
        base = iv.last_power_w
    # Cross-tenant upward correction (daylight only — nothing to borrow at night).
    if daylight and borrow and iv.serial:
        bw = borrow.get((str(iv.vendor or "").lower(), str(iv.serial)))
        if bw is not None and (base is None or bw > base):
            return bw
    if base is None and pw is None and not daylight:
        return None
    return base


# ── Two distinct data streams: VENDOR (inverter telemetry) vs UTILITY (meter) ──
# The sandbox slider switches between these. The system still INTEGRATES both
# (the blended `daily` is what dedup/Trends use); these split views just let the
# owner SEE each feed on its own. Classify DailyGeneration.source into one stream.
_VENDOR_SOURCES = {
    "solaredge", "fronius", "sma", "chint",
    "extension_pull", "extension_pull_corrected",
    "csv", "manual",                      # operator-supplied independent production
}
_UTILITY_SOURCES = {
    "gmp_api", "gmp_portal_scrape", "utility_meter", "smarthub", "bill_prorate",
}


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
        {"date": r.day.isoformat(), "kwh": round(float(r.kwh or 0.0), 2)}
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



def build_fleet_tree(db, tenant: Tenant, *, force_refresh: bool = False) -> dict:
    """Owner-grouped 3-tier tree. Inverters are read from the persisted table
    (owner's arrangement), telemetry pulled from each one's SOURCE site, then
    peer-analyzed WITHIN each owner array group — so a drag changes real cohorts.
    """
    inverters = discover_and_persist(db, tenant, force_refresh=force_refresh)

    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
        .order_by(Array.id)
    ).scalars().all()
    array_by_id = {a.id: a for a in arrays}

    # Group persisted inverters by their OWNER array_id.
    by_array: dict[int, list[Inverter]] = defaultdict(list)
    for iv in inverters:
        by_array[iv.array_id].append(iv)

    columns: list[dict] = []
    inv_total = 0
    # Compute sun-up ONCE per fleet build (regional default; all arrays share it
    # until per-array lat/long exists). The card uses this for the "Sleeping" state.
    daylight = _is_daylight()
    # Cross-tenant live-power borrow map (vendor,serial) -> best fresh wattage
    # across ALL tenants. Lets a tenant whose browser captured a bogus near-zero
    # for a SHARED physical system show the real reading another tenant captured
    # for the same device this window. Computed once per build (upward-only).
    borrow_live = _cross_tenant_live_by_serial(db, inverters)
    for arr in arrays:
        ivs = sorted(by_array.get(arr.id, []), key=lambda x: (x.position, x.id))

        # Pull telemetry per source site (cached), then build peer-units for THIS
        # owner group (cohort = the inverters the owner placed under this array).
        units = []
        meta_by_serial = {}
        for iv in ivs:
            conn_vendor = iv.vendor
            # find the source connection's creds
            src_arr = array_by_id.get(iv.source_array_id) or arr
            conn = _resolve_connection(db, src_arr)
            tel_map = {}
            if conn is not None and (conn.config or {}).get("api_key") and (conn.config or {}).get("site_id"):
                tel_map = _telemetry_for_site(conn_vendor, conn.config["api_key"],
                                              conn.config["site_id"], force=force_refresh)
            m = tel_map.get(iv.serial, {})
            # --- API-INDEPENDENT HISTORY STORE ---
            # 1) Whatever daily readings we just saw LIVE (e.g. SolarEdge's API),
            #    snapshot them into InverterDaily so the graph survives the next
            #    time that API is slow/down/off-peak. (Extension vendors already
            #    persisted their readings at capture time.)
            live_daily = m.get("daily") or []
            if live_daily:
                try:
                    _persist_daily_series(db, tenant.id, iv.id, live_daily,
                                          source=f"{conn_vendor or 'api'}_live")
                except Exception:
                    log.warning("fleet: failed to persist daily for inv %s", iv.id, exc_info=True)
            # 2) The graph's series is now STORAGE-authoritative (stored history with
            #    any fresh live readings merged on top) — never a bare live read that
            #    can vanish. Falls back gracefully to whatever live gave us.
            merged = _merged_daily(db, iv.id, live_daily)
            m = dict(m)
            m["daily"] = merged if merged else live_daily
            meta_by_serial[iv.serial] = m
            units.append({
                "id": iv.serial,
                "nameplate_kw": iv.nameplate_kw if iv.nameplate_kw is not None else m.get("nameplate_kw"),
                "daily": m.get("daily", []),
                "error_code": m.get("error_code"),
                "last_report": m.get("last_report"),
            })

        analyzed = peer_analysis.analyze_cohort(units) if units else {"units": []}
        an_by_id = {u["id"]: u for u in analyzed["units"]}

        inv_rows = []
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
                "nameplate_kw": iv.nameplate_kw if iv.nameplate_kw is not None else m.get("nameplate_kw"),
                "peer_index": u.get("peer_index"),
                "status": u.get("status", "ok"),
                "diagnosis": u.get("diagnosis"),
                "window_kwh": u.get("window_kwh"),
                "daily": daily,                       # ascending [{date,kwh}] for the sparkline
                "min_kwh": min_kwh,                   # lowest daily output in the window (real)
                "peak_kwh": peak_kwh,                 # highest daily output in the window (real)
                "last_mode": m.get("last_mode"),
                "current_power_w": _live_power_w(iv, m, daylight=daylight, borrow=borrow_live),
                "last_report": u.get("last_report") or m.get("last_report"),
                "source_array_id": iv.source_array_id,
                "moved": iv.source_array_id is not None and iv.source_array_id != iv.array_id,
                "origin_url": _portal_link(iv.vendor, iv.source_site_id),
                "origin_label": _VENDOR_LABEL.get((iv.vendor or "").lower()) or (iv.vendor or None),
            })
        inv_total += len(inv_rows)

        # vendor mix for the array chip
        vendors = sorted({iv.vendor for iv in ivs})

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
        src_status = _source_status(inv_rows)

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
        _today_iso = now().date().isoformat()
        _daily_rows = _array_daily(db, arr.id)
        _today_kwh = next(
            (r["kwh"] for r in _daily_rows if r.get("date") == _today_iso), None
        )
        produced_today_kwh = _today_kwh if (_today_kwh and _today_kwh > 0) else None

        columns.append({
            "array_id": arr.id,
            "array_name": arr.name,
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
            # Sun-up flag for the card "Sleeping" night state. The card gates
            # "Sleeping" on (is_daylight==False AND output==0) so a daytime fault
            # that zeroes output never reads as "asleep". Regional (central VT)
            # until a per-array lat/long exists; recomputed each fetch.
            "is_daylight": daylight,
            # {"state": ok|stale|dark|none, "last_report": iso|None, "age_hours": float|None}
            # — surfaced on the card so a vendor-side reporting gap reads as a
            # SOURCE outage, not an app failure.
            "source_status": src_status,
        })

    attention = sum(c["alert"]["count"] for c in columns)
    # Commit the daily history we snapshotted into InverterDaily during the build
    # (persist-on-read). Never let a storage hiccup break the tree the owner sees.
    try:
        db.commit()
    except Exception:
        log.warning("fleet: daily-history commit failed", exc_info=True)
        db.rollback()
    return {
        "generated_at": now().replace(microsecond=0).isoformat() + "Z",
        "tiers": ["alerts", "arrays", "inverters"],
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

    Array names are unique per tenant (uq_array_per_tenant), so if the requested
    name collides we auto-suffix (" 2", " 3", …) rather than 500. Also revives a
    soft-deleted array of the same name instead of colliding with its row."""
    nm = (name or "").strip() or "New array"

    # Revive a soft-deleted same-name array if one exists (the unique constraint
    # spans deleted rows too, so we can't just insert a duplicate).
    existing = db.execute(
        select(Array).where(Array.tenant_id == tenant.id, Array.name == nm)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.deleted_at is not None:
            existing.deleted_at = None
            db.commit()
            db.refresh(existing)
            return existing
        # live array already has this name — auto-suffix to keep it unique
        base = nm
        for i in range(2, 100):
            cand = f"{base} {i}"
            clash = db.execute(
                select(Array).where(Array.tenant_id == tenant.id, Array.name == cand)
            ).scalar_one_or_none()
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
