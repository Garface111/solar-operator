"""Array Owners (EnergyAgent) overview API.

Powers the dashboard ArrayOverview screen: per-array live power, today/month/
lifetime generation, a dollar-value model, and a health badge. See
docs/plans/ARRAY_OWNERS_API_CONTRACT.md — both sides build against that doc.

GET  /v1/array-owners/overview
    Aggregates DailyGeneration + (cached) live SolarEdge power for every array
    the tenant owns.

POST /v1/array-owners/arrays/{array_id}/solaredge
    Connect a SolarEdge site to an array. Validates the key with a live
    overview call before saving.

Auth: standard tenant bearer (api.app.tenant_from_bearer, imported lazily to
avoid a circular import — api.app imports many of this module's siblings).
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, time as dtime
from types import SimpleNamespace
from typing import Optional

import httpx  # noqa: F401 — kept so tests can monkeypatch array_owners.httpx.get
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from . import inverters
from .adapters import gmp as gmp_adapter
from .db import SessionLocal
from .inverters import VENDORS, InverterAuthError, InverterError, InverterScopeError
from .inverters import peer_analysis
from .models import Array, Bill, DailyGeneration, InverterConnection, Tenant, UtilityAccount, now
from .models import Inverter, InverterDaily
from .rates import REC_PRICE_USD_PER_MWH, get_energy_rate

log = logging.getLogger(__name__)

router = APIRouter()

# An array is "stale" once its newest DailyGeneration row is older than this.
STALE_DAYS = 3

# ── live power cache ──────────────────────────────────────────────────────────
# Vendor live reads are rate-limited (SolarEdge ~300 req/day per token), so we
# cache each connection's live result for 5 minutes. Keyed by a stable
# (vendor, config) signature -> (fetched_at, live_dict|None).
_CACHE_TTL = timedelta(minutes=5)
_overview_cache: dict[str, tuple[datetime, Optional[dict]]] = {}


def _cache_key(vendor: str, config: dict) -> str:
    return vendor + ":" + json.dumps(config, sort_keys=True, default=str)


def _tenant_from_bearer(authorization: str | None) -> Tenant:
    """Auth for dashboard calls: the SPA sends a short-lived SESSION token
    (api.account login flow), not the raw tenant key. Accept the session
    token first; fall back to the tenant-key bearer so programmatic/API
    callers (and tests) keep working.

    NOTE: this is the CAPTURE/dashboard resolver — it is intentionally more
    permissive than app.tenant_from_bearer about INACTIVE tenants. The session
    path already lets inactive (e.g. paused-no-card) tenants through so they can
    view read-only; capture must match that so a paused trial keeps its inverter
    data flowing (only report DELIVERY + premium features gate on active, in the
    scheduler). The strict app.tenant_from_bearer hard-403s ANY inactive tenant,
    which silently blocked every capture the moment a 14-day trial auto-paused —
    see _capture_tenant_by_key for the capture-tolerant key path.
    """
    from .account import tenant_from_session

    try:
        return tenant_from_session(authorization)
    except HTTPException:
        pass
    return _capture_tenant_by_key(authorization)


# Statuses that are paused/ended-but-RECOVERABLE: the tenant kept its data and
# can resume by adding a card. Capture stays allowed for these (data keeps
# flowing); only a HARD-cancelled or never-existent tenant is refused.
_CAPTURE_RECOVERABLE_STATUSES = {"paused_no_card", "trialing", "comped", "active", None}


def _capture_tenant_by_key(authorization: str | None) -> Tenant:
    """Resolve a tenant from its raw tenant-key bearer for the CAPTURE path.

    Unlike app.tenant_from_bearer (which hard-403s any inactive tenant), this
    allows an INACTIVE tenant through when its status is recoverable (e.g.
    paused-no-card after a trial). A genuinely CANCELLED tenant — the user chose
    to leave / payment hard-failed — is refused with a clear, actionable 402 so
    the extension can show "add a card to resume" instead of a cryptic 403.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    key = authorization.split(" ", 1)[1].strip()
    with SessionLocal() as db:
        t = db.execute(select(Tenant).where(Tenant.tenant_key == key)).scalar_one_or_none()
        if t is None:
            raise HTTPException(403, "Invalid tenant key")
        if not t.active and t.subscription_status not in _CAPTURE_RECOVERABLE_STATUSES:
            # Hard-cancelled → actionable 402 (not a silent 403).
            raise HTTPException(
                402,
                detail={
                    "error": "subscription-cancelled",
                    "message": "This account's subscription has ended. Add a "
                               "payment method to resume syncing your data.",
                    "cta_url": "/account",
                },
            )
        return t


# ── inverter connection resolution ────────────────────────────────────────────

def _resolve_connection(db, arr: Array):
    """Return the array's inverter connection, or None.

    Prefers a real InverterConnection row. Falls back to a virtual
    {vendor: "solaredge"} connection synthesized from the legacy
    Array.solaredge_api_key/solaredge_site_id columns when no row exists, so
    arrays connected before the InverterConnection table keep working.
    """
    conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == arr.id)
    ).scalar_one_or_none()
    if conn is not None:
        return conn
    if arr.solaredge_api_key and arr.solaredge_site_id:
        return SimpleNamespace(
            vendor="solaredge",
            config={"api_key": arr.solaredge_api_key, "site_id": arr.solaredge_site_id},
            status="ok",
        )
    return None


def _cached_fetch_live(vendor: str, config: dict) -> Optional[dict]:
    """Vendor fetch_live with a short-TTL cache. Raises InverterError on failure."""
    key = _cache_key(vendor, config)
    cached = _overview_cache.get(key)
    if cached is not None and (now() - cached[0]) < _CACHE_TTL:
        return cached[1]
    live = inverters.fetch_live(vendor, config)
    _overview_cache[key] = (now(), live)
    return live


def _connect_inverter(db, arr: Array, vendor: str, config: dict) -> dict:
    """Validate `config` against the vendor's live API, then upsert the
    connection. Raises InverterError/InverterAuthError on bad credentials —
    nothing is persisted in that case (validate runs first).

    Returns the vendor's validate() result (includes at least "site_name").
    """
    result = inverters.validate(vendor, config)  # raises before any write

    conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == arr.id)
    ).scalar_one_or_none()
    if conn is None:
        conn = InverterConnection(array_id=arr.id, vendor=vendor, config=config, status="ok")
        db.add(conn)
    else:
        conn.vendor = vendor
        conn.config = config
        conn.status = "ok"
        conn.last_error = None

    # Backward compat: mirror SolarEdge creds onto the legacy columns so the
    # daily-pull virtual-connection path and any legacy readers keep working.
    if vendor == "solaredge":
        arr.solaredge_api_key = str(config.get("api_key") or "").strip()
        arr.solaredge_site_id = int(config["site_id"])

    db.commit()
    # Self-healing: pull the vendor's FULL multi-year daily history in the
    # background so this array shows past years in Trends within minutes, not
    # only the ~90 days the nightly pull reaches. Best-effort; the scheduled
    # healer retries if this connection isn't stamped.
    _trigger_history_backfill(db, arr.id)
    return result


def _trigger_history_backfill(db, array_id: int) -> None:
    """Fire the on-connect deep-history backfill for an array's connection."""
    try:
        conn = db.execute(
            select(InverterConnection).where(InverterConnection.array_id == array_id)
        ).scalar_one_or_none()
        if conn is not None and conn.id is not None:
            from .jobs.inverter_history import backfill_connection_history_async
            backfill_connection_history_async(conn.id)
    except Exception:  # noqa: BLE001 — never let backfill scheduling break a connect
        log.warning("history backfill trigger failed for array %s", array_id, exc_info=True)


# ── aggregation helpers ───────────────────────────────────────────────────────

def _array_provider(db, array_id: int) -> str | None:
    """Return the utility provider code for an array (first live account)."""
    return db.execute(
        select(UtilityAccount.provider)
        .where(
            UtilityAccount.array_id == array_id,
            UtilityAccount.deleted_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()


def _value_model(
    today_kwh: float, month_kwh: float, lifetime_kwh: float, rate: float
) -> dict:
    """Compute the dollar-value block per the contract value model.

    energy = kwh * rate; REC value is floored MWh for lifetime, but pro-rated
    (no floor, "estimated") for today/month since a partial period hasn't
    minted a whole certificate yet.
    """
    rec = REC_PRICE_USD_PER_MWH
    today_usd = today_kwh * rate + (today_kwh / 1000.0) * rec
    month_usd = month_kwh * rate + (month_kwh / 1000.0) * rec
    life_energy = lifetime_kwh * rate
    life_rec = math.floor(lifetime_kwh / 1000.0) * rec
    return {
        "today_usd": round(today_usd, 2),
        "month_usd": round(month_usd, 2),
        "lifetime_usd": round(life_energy + life_rec, 2),
        "breakdown": {
            "energy_rate_usd_per_kwh": rate,
            "rec_usd_per_mwh": rec,
            "energy_usd": round(life_energy, 2),
            "rec_usd": round(life_rec, 2),
        },
    }


def _health(
    has_live_source: bool,
    last_day: date | None,
    overview_ok: bool | None,
    today: date,
) -> dict:
    """Classify an array's reporting health per the contract rules.

    Precedence: no_source > offline > stale > ok.
    overview_ok is None when no live source was attempted.
    """
    days_since = (today - last_day).days if last_day is not None else None

    if not has_live_source and last_day is None:
        status, message = "no_source", "No live source or generation data yet"
    elif has_live_source and overview_ok is False:
        status, message = "offline", "Live source unreachable"
    elif days_since is not None and days_since > STALE_DAYS:
        status, message = "stale", f"No data for {days_since} days"
    else:
        status, message = "ok", "Reporting normally"

    return {
        "status": status,
        "last_data_day": last_day.isoformat() if last_day else None,
        "days_since_data": days_since,
        "message": message,
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/v1/array-owners/overview")
def array_owners_overview(authorization: str | None = Header(default=None)) -> dict:
    """Per-array live power, generation totals, value, and health for a tenant."""
    tenant = _tenant_from_bearer(authorization)

    today = date.today()
    month_start = today.replace(day=1)
    # Peer analysis compares each array against its cohort over a rolling window.
    window_start = today - timedelta(days=peer_analysis.WINDOW_DAYS)

    arrays_out: list[dict] = []
    tot_power = 0.0
    tot_today = tot_month = tot_life = 0.0
    tot_today_usd = tot_month_usd = tot_life_usd = 0.0

    # Cohort = the arrays under one Client (the account/fleet, e.g. Bruce's 7
    # arrays on one SolarEdge login). Arrays with no client share a per-tenant
    # default cohort. We collect each array's daily-kWh window here and run the
    # peer-relative analysis after the main loop, then attach a `peer` block.
    peer_inputs_by_client: dict[object, list[dict]] = defaultdict(list)
    out_by_array_id: dict[int, dict] = {}

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array)
            .where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
            .order_by(Array.id)
        ).scalars().all()

        for arr in arrays:
            today_kwh = db.execute(
                select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0)).where(
                    DailyGeneration.array_id == arr.id,
                    DailyGeneration.day == today,
                )
            ).scalar_one()
            has_today = db.execute(
                select(func.count())
                .select_from(DailyGeneration)
                .where(
                    DailyGeneration.array_id == arr.id,
                    DailyGeneration.day == today,
                )
            ).scalar_one() > 0

            month_kwh = db.execute(
                select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0)).where(
                    DailyGeneration.array_id == arr.id,
                    DailyGeneration.day >= month_start,
                )
            ).scalar_one()
            lifetime_kwh, last_day = db.execute(
                select(
                    func.coalesce(func.sum(DailyGeneration.kwh), 0.0),
                    func.max(DailyGeneration.day),
                ).where(DailyGeneration.array_id == arr.id)
            ).one()

            # Daily series over the peer window (ascending) — the raw signal the
            # cohort analysis runs on.
            window_rows = db.execute(
                select(DailyGeneration.day, DailyGeneration.kwh)
                .where(
                    DailyGeneration.array_id == arr.id,
                    DailyGeneration.day >= window_start,
                )
                .order_by(DailyGeneration.day)
            ).all()
            daily_series = [
                {"date": d.isoformat(), "kwh": float(k or 0.0)} for d, k in window_rows
            ]

            conn = _resolve_connection(db, arr)
            module = VENDORS.get(conn.vendor) if conn is not None else None
            has_live_source = bool(
                module is not None and getattr(module, "SUPPORTS_LIVE", False)
            )

            live: dict | None = None
            overview_ok: bool | None = None
            if has_live_source:
                try:
                    live_raw = _cached_fetch_live(conn.vendor, conn.config)
                    power_w = (live_raw or {}).get("current_power_w")
                    live = {
                        "source": conn.vendor,
                        "current_power_w": power_w,
                        "as_of": (live_raw or {}).get("as_of"),
                    }
                    overview_ok = True
                    if power_w is not None:
                        tot_power += power_w
                except InverterError:
                    overview_ok = False
                    live = {
                        "source": conn.vendor,
                        "current_power_w": None,
                        "as_of": None,
                    }

            rate = get_energy_rate(_array_provider(db, arr.id))
            value = _value_model(today_kwh, month_kwh, lifetime_kwh, rate)
            health = _health(has_live_source, last_day, overview_ok, today)

            entry = {
                "array_id": arr.id,
                "name": arr.name,
                "client_name": arr.client.name if arr.client else None,
                "fuel_type": arr.fuel_type,
                "live": live,
                "today": {"kwh": round(today_kwh, 3)} if has_today else None,
                "month": {"kwh": round(month_kwh, 3)},
                "lifetime": {"kwh": round(lifetime_kwh, 3)},
                "value": value,
                "health": health,
                # Daily kWh over the peer window (ascending) for the owner
                # dashboard's sparkline. Kept lightweight (date+kwh only).
                "_daily": daily_series,
            }
            arrays_out.append(entry)
            out_by_array_id[arr.id] = entry

            # Collect cohort input. error_code/last_report are left None at the
            # array level: we have no per-inverter fault codes, and daily-grain
            # staleness is already covered by `health` (STALE_DAYS), so the
            # array-level peer status resolves to ok | dead | underperforming —
            # the comparative "is it pulling its weight" signal. Per-inverter
            # capture will later light up the fault/comm_gap paths natively.
            peer_inputs_by_client[arr.client_id].append({
                "id": arr.id,
                "nameplate_kw": None,   # inferred from observed peak in-module
                "daily": daily_series,
                "error_code": None,
                "last_report": None,
            })

            tot_today += today_kwh
            tot_month += month_kwh
            tot_life += lifetime_kwh
            tot_today_usd += value["today_usd"]
            tot_month_usd += value["month_usd"]
            tot_life_usd += value["lifetime_usd"]

    # ── peer-relative cohort pass ─────────────────────────────────────────────
    peer_attention = 0
    peer_loss = 0.0
    cohorts_with_peers = 0
    for _client_id, units in peer_inputs_by_client.items():
        result = peer_analysis.analyze_cohort(units)
        if result["summary"]["peer_analysis_available"]:
            cohorts_with_peers += 1
        peer_attention += result["summary"]["units_attention"]
        peer_loss += result["summary"]["estimated_loss_kwh_window"]
        for u in result["units"]:
            entry = out_by_array_id.get(u["id"])
            if entry is None:
                continue
            entry["peer"] = {
                "peer_index": u["peer_index"],
                "status": u["status"],
                "diagnosis": u["diagnosis"],
                "window_kwh": u["window_kwh"],
                "cohort_size": result["cohort_size"],
                "peer_analysis_available": result["summary"]["peer_analysis_available"],
            }

    return {
        "generated_at": now().replace(microsecond=0).isoformat() + "Z",
        "arrays": arrays_out,
        "totals": {
            "current_power_w": round(tot_power, 1),
            "today_kwh": round(tot_today, 3),
            "month_kwh": round(tot_month, 3),
            "lifetime_kwh": round(tot_life, 3),
            "today_usd": round(tot_today_usd, 2),
            "month_usd": round(tot_month_usd, 2),
            "lifetime_usd": round(tot_life_usd, 2),
        },
        "peer_summary": {
            "arrays_attention": peer_attention,
            "arrays_total": len(arrays_out),
            "estimated_loss_kwh_window": round(peer_loss, 1),
            "cohorts_with_peer_signal": cohorts_with_peers,
            "window_days": peer_analysis.WINDOW_DAYS,
        },
    }


_MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Canonical data-source families for the Production Analytics "where this data
# comes from" attribution. Maps every DailyGeneration.source value (and the GMP
# daily sponge, which has no DailyGeneration row) onto a stable display family.
# This is AO's edge over single-ecosystem portals: one fleet, every vendor,
# attributed honestly. `key` is stable for the frontend; `label` is user-facing.
_SOURCE_FAMILY = {
    # utility meter feeds (settled generation) → GMP
    "gmp_api": ("gmp", "GMP (utility meter)"),
    "gmp_portal_scrape": ("gmp", "GMP (utility meter)"),
    "utility_meter": ("gmp", "GMP (utility meter)"),
    "smarthub": ("gmp", "GMP (utility meter)"),
    # inverter telemetry vendors
    "solaredge": ("solaredge", "SolarEdge"),
    "fronius": ("fronius", "Fronius"),
    "sma": ("sma", "SMA"),
    "chint": ("chint", "CHINT"),
    "extension_pull": ("inverter", "Inverter (extension)"),
    "extension_pull_corrected": ("inverter", "Inverter (extension)"),
    # operator-supplied
    "csv": ("csv", "CSV upload"),
    "manual": ("manual", "Manual entry"),
    "bill_prorate": ("bill", "Bill (prorated)"),
}
# Display order for the attribution legend (the five named vendors lead).
_SOURCE_ORDER = ["gmp", "solaredge", "fronius", "sma", "chint",
                 "inverter", "csv", "manual", "bill", "other"]
_SOURCE_LABELS = {
    "gmp": "GMP (utility meter)", "solaredge": "SolarEdge", "fronius": "Fronius",
    "sma": "SMA", "chint": "CHINT", "inverter": "Inverter (extension)",
    "csv": "CSV upload", "manual": "Manual entry", "bill": "Bill (prorated)",
    "other": "Other",
}


def _source_family(src: str | None) -> str:
    """Map a raw DailyGeneration.source onto a canonical display family key."""
    if not src:
        return "other"
    fam = _SOURCE_FAMILY.get(src.strip().lower())
    return fam[0] if fam else "other"


@router.get("/v1/array-owners/fleet-trends")
def array_owners_fleet_trends(
    authorization: str | None = Header(default=None),
    array_id: int | None = Query(default=None,
        description="Scope the whole trends payload to ONE owned array. "
                    "by_array still lists the full fleet so the filter can switch."),
) -> dict:
    """PORTFOLIO-WIDE multi-year production trends — the owner's macro view.

    Aggregates DailyGeneration kWh across EVERY array the tenant owns, grouped
    by calendar (year, month), so the Trends tab can draw one line per year
    (Jan–Dec) plus a seasonal year-over-year comparison for the whole fleet.
    This is Paul's "macro-level tab for multi-year trend lines" — the fleet
    total, not a single array. Also returns a per-array breakdown so the owner
    can drill in. Derived entirely from real generation telemetry; an owner with
    thin history gets empty collections (never a 500).

    Shape:
      {
        "years": [2024, 2025, 2026],
        "monthly_by_year": {"2025": [{"month": 1, "kwh": ...}, ...], ...},
        "seasonal_yoy": [{"month": 1, "label": "Jan",
                          "by_year": {"2025": ..., "2026": ...},
                          "latest_delta_pct": 4.8}, ...],
        "ttm_kwh": ..., "ttm_savings_usd": ...,
        "lifetime_kwh": ...,
        "by_array": [{"array_id", "name", "lifetime_kwh", "years": [...]}],
      }
    """
    tenant = _tenant_from_bearer(authorization)
    today = date.today()

    # (year, month) → kWh across the whole fleet, and per-array lifetime/years.
    fleet_ym: dict[tuple[int, int], float] = defaultdict(float)
    fleet_daily: dict[date, float] = defaultdict(float)   # last-30-days bar graph
    # Per data-source family: (year,month)→kWh, day→kWh, lifetime→kWh. Drives the
    # Production Analytics vendor-attribution layer (AO's multi-vendor edge).
    fleet_ym_src: dict[str, dict[tuple[int, int], float]] = defaultdict(lambda: defaultdict(float))
    fleet_daily_src: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    src_lifetime: dict[str, float] = defaultdict(float)
    capacity_kw = 0.0          # summed inverter nameplate over the SCOPED set
    capacity_known_arrays = 0  # how many scoped arrays had any nameplate data
    by_array: dict[int, dict] = {}
    rate_blended = 0.0
    rate_n = 0

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array)
            .where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None),
                   Array.excluded.is_(False))
            .order_by(Array.id)
        ).scalars().all()

        # Optional per-array scope: the aggregation below runs over `scoped`,
        # but `by_array` is always built from the FULL fleet so the filter
        # dropdown can switch between arrays. A bad/unowned id → 404.
        scoped = arrays
        if array_id is not None:
            scoped = [a for a in arrays if a.id == array_id]
            if not scoped:
                raise HTTPException(404, "array not found")

        from .reports import gmp_daily_read as _gdr

        for arr in scoped:
            # Per-day kWh for this array, merged across BOTH sources:
            #   • DailyGeneration  — CSV-upload / billing-meter table
            #   • gmp_daily_generation — the GMP API daily sponge (via the read
            #     contract; summed across the array's GMP meters)
            # Prefer the CSV value on any day both have (avoid double-count); fall
            # back to the GMP value to fill gaps. Result feeds fleet totals,
            # month×year, the 30-day daily bars, and per-array lifetime.
            per_day: dict = {}
            per_day_src: dict = {}   # day → canonical source family
            rows = db.execute(
                select(DailyGeneration.day, DailyGeneration.kwh,
                       DailyGeneration.source).where(
                    DailyGeneration.array_id == arr.id,
                )
            ).all()
            for d, kwh, src in rows:
                if d is None or kwh is None:
                    continue
                per_day[d] = float(kwh)
                per_day_src[d] = _source_family(src)
            # GMP daily sponge — only fills days the CSV table doesn't already cover.
            try:
                for pt in _gdr.get_daily_series(arr.id, db=db):
                    d = pt["day"]
                    if d is not None and d not in per_day:
                        per_day[d] = float(pt["kwh"] or 0.0)
                        per_day_src[d] = "gmp"   # sponge is always the GMP meter
            except Exception:  # noqa: BLE001 — never let a read-contract hiccup sink trends
                pass

            arr_ym: dict[tuple[int, int], float] = defaultdict(float)
            arr_life = 0.0
            for d, k in per_day.items():
                fam = per_day_src.get(d, "other")
                fleet_ym[(d.year, d.month)] += k
                fleet_daily[d] += k
                arr_ym[(d.year, d.month)] += k
                arr_life += k
                fleet_ym_src[fam][(d.year, d.month)] += k
                fleet_daily_src[fam][d] += k
                src_lifetime[fam] += k
            # System size (kWp) — sum live inverter nameplate so Analytics can show
            # specific yield (kWh/kWp) like SolarEdge/SMA. Honest: arrays with no
            # nameplate on record simply don't contribute (frontend prompts to add).
            try:
                nps = db.execute(
                    select(Inverter.nameplate_kw).where(
                        Inverter.array_id == arr.id,
                        Inverter.deleted_at.is_(None),
                        Inverter.nameplate_kw.isnot(None),
                    )
                ).scalars().all()
                arr_kw = sum(float(x) for x in nps if x)
                if arr_kw > 0:
                    capacity_kw += arr_kw
                    capacity_known_arrays += 1
            except Exception:  # noqa: BLE001
                pass
            # rate blended over the SCOPED set, so EST. VALUE matches the kWh shown
            try:
                rate_blended += get_energy_rate(_array_provider(db, arr.id))
                rate_n += 1
            except Exception:  # noqa: BLE001
                pass

        # by_array always reflects the FULL fleet (so the filter dropdown lists
        # every array regardless of the current scope). Computed with the same
        # two-source merge as the aggregation, but only for the lifetime/years
        # summary each row needs.
        for arr in arrays:
            per_day: dict = {}
            for d, kwh in db.execute(
                select(DailyGeneration.day, DailyGeneration.kwh).where(
                    DailyGeneration.array_id == arr.id)
            ).all():
                if d is not None and kwh is not None:
                    per_day[d] = float(kwh)
            try:
                for pt in _gdr.get_daily_series(arr.id, db=db):
                    dd = pt["day"]
                    if dd is not None and dd not in per_day:
                        per_day[dd] = float(pt["kwh"] or 0.0)
            except Exception:  # noqa: BLE001
                pass
            life = round(sum(per_day.values()), 1)
            by_array[arr.id] = {
                "array_id": arr.id,
                "name": arr.name,
                "lifetime_kwh": life,
                "years": sorted({d.year for d in per_day.keys()}),
            }

    years = sorted({y for (y, _m) in fleet_ym.keys()})
    rate = (rate_blended / rate_n) if rate_n else 0.0

    monthly_by_year: dict[str, list[dict]] = {}
    for y in years:
        pts = [
            {"month": m, "kwh": round(fleet_ym[(y, m)], 1)}
            for m in range(1, 13) if (y, m) in fleet_ym
        ]
        monthly_by_year[str(y)] = pts

    # Seasonal YoY: per calendar month, each year's fleet total + latest delta%.
    seasonal_yoy: list[dict] = []
    for m in range(1, 13):
        by_year = {
            str(y): round(fleet_ym[(y, m)], 1)
            for y in years if (y, m) in fleet_ym
        }
        if not by_year:
            continue
        present = sorted(int(y) for y in by_year.keys())
        latest_delta_pct = None
        if len(present) >= 2:
            latest, prior = present[-1], present[-2]
            pv = by_year[str(prior)]
            if pv:
                latest_delta_pct = round(
                    100.0 * (by_year[str(latest)] - pv) / pv, 1)
        seasonal_yoy.append({
            "month": m, "label": _MONTH_LABELS[m - 1],
            "by_year": by_year, "latest_delta_pct": latest_delta_pct,
        })

    # Trailing twelve months (rolling from today) across the fleet.
    ttm_kwh = 0.0
    for off in range(12):
        yy, mm = today.year, today.month - off
        while mm <= 0:
            mm += 12
            yy -= 1
        ttm_kwh += fleet_ym.get((yy, mm), 0.0)
    lifetime_kwh = round(sum(fleet_ym.values()), 1)

    # Recent daily series for the fleet bar graph — the 30-day window ending at
    # the most recent day with data (so the chart is full even if today's pull
    # hasn't landed). Contiguous days; days with no generation render as 0 bars.
    daily_recent: list[dict] = []
    daily_series: list[dict] = []   # longer (≤365d) window for Analytics Day/Week nav
    if fleet_daily:
        last_day = max(fleet_daily.keys())
        from datetime import timedelta as _td
        window_start = last_day - _td(days=29)
        d = window_start
        while d <= last_day:
            daily_recent.append({"day": d.isoformat(),
                                 "kwh": round(fleet_daily.get(d, 0.0), 1)})
            d += _td(days=1)
        # Extended daily series: from the earliest day with data, capped at the
        # most recent 365 days, so Analytics can navigate weeks/months of bars.
        first_day = min(fleet_daily.keys())
        ext_start = max(first_day, last_day - _td(days=364))
        d = ext_start
        while d <= last_day:
            daily_series.append({"day": d.isoformat(),
                                 "kwh": round(fleet_daily.get(d, 0.0), 1)})
            d += _td(days=1)

    # ── Data-source attribution (AO's multi-vendor edge) ────────────────────
    # For each canonical source family present, report lifetime kWh, share of
    # fleet lifetime, and a per-month breakdown. None of GMP/SolarEdge/Fronius/
    # SMA/CHINT can show this — each only sees its own ecosystem; AO sees them
    # side by side, attributed to the feed each kWh actually came from.
    total_life = sum(src_lifetime.values()) or 0.0
    source_breakdown: list[dict] = []
    present_fams = [f for f in _SOURCE_ORDER if f in src_lifetime and src_lifetime[f] > 0]
    # include any family not in the canonical order (defensive)
    present_fams += [f for f in src_lifetime if f not in present_fams and src_lifetime[f] > 0]
    for fam in present_fams:
        # monthly per year for this source
        fam_by_year: dict[str, list[dict]] = {}
        for y in years:
            pts = [{"month": m, "kwh": round(fleet_ym_src[fam].get((y, m), 0.0), 1)}
                   for m in range(1, 13) if (y, m) in fleet_ym_src[fam]]
            if pts:
                fam_by_year[str(y)] = pts
        lt = round(src_lifetime[fam], 1)
        source_breakdown.append({
            "key": fam,
            "label": _SOURCE_LABELS.get(fam, fam.title()),
            "lifetime_kwh": lt,
            "share_pct": round(100.0 * lt / total_life, 1) if total_life else 0.0,
            "monthly_by_year": fam_by_year,
        })

    # System size + specific yield (kWh/kWp) — SolarEdge/SMA show this. Honest:
    # only computed from inverter nameplate we actually hold; null when unknown
    # so the frontend can prompt the owner to add system size rather than guess.
    cap_kw = round(capacity_kw, 2) if capacity_kw > 0 else None
    specific_yield_ttm = (round(ttm_kwh / capacity_kw, 1)
                          if capacity_kw > 0 else None)

    # Environmental impact — EPA Greenhouse Gas Equivalencies (2024 factors),
    # applied to LIFETIME clean kWh. Factors are published + cited so this is
    # provenance-backed, not fabricated.  https://www.epa.gov/energy/greenhouse-gases-equivalencies-calculator-calculations-and-references
    _CO2_LB_PER_KWH = 1.5634          # avoided CO2 (lb) per kWh (US grid avg)
    _TREE_CO2_LB_YR = 48.0            # CO2 (lb) sequestered by 1 tree-seedling over 10y / 10 ≈ annual
    _CAR_CO2_LB_YR = 11015.0         # avg passenger vehicle CO2 (lb) per year
    _HOME_KWH_YR = 10500.0           # avg US home electricity use (kWh) per year
    co2_lb = lifetime_kwh * _CO2_LB_PER_KWH
    environmental = {
        "co2_avoided_lb": round(co2_lb, 0),
        "co2_avoided_tonnes": round(co2_lb * 0.000453592, 2),
        "trees_equiv": round(co2_lb / _TREE_CO2_LB_YR, 0),
        "cars_year_equiv": round(co2_lb / _CAR_CO2_LB_YR, 1),
        "homes_year_equiv": round(lifetime_kwh / _HOME_KWH_YR, 1),
        "basis": "EPA GHG equivalencies (2024); applied to lifetime clean kWh",
    } if lifetime_kwh > 0 else None

    return {
        "years": years,
        "monthly_by_year": monthly_by_year,
        "seasonal_yoy": seasonal_yoy,
        "ttm_kwh": round(ttm_kwh, 1),
        "ttm_savings_usd": round(ttm_kwh * rate, 2) if rate else None,
        "blended_rate_usd_per_kwh": round(rate, 4) if rate else None,
        "lifetime_kwh": lifetime_kwh,
        "daily_recent": daily_recent,
        "daily_series": daily_series,
        "source_breakdown": source_breakdown,
        "capacity_kw": cap_kw,
        "capacity_known_arrays": capacity_known_arrays,
        "specific_yield_ttm_kwh_per_kwp": specific_yield_ttm,
        "environmental": environmental,
        "selected_array_id": array_id,
        "by_array": sorted(by_array.values(),
                           key=lambda a: -a["lifetime_kwh"]),
    }


@router.get("/v1/array-owners/fleet-audit")
def array_owners_fleet_audit(
    authorization: str | None = Header(default=None),
) -> dict:
    """PORTFOLIO-WIDE settlement audit — the auditor's owner-facing view.

    Runs the production-vs-settlement reconciliation engine across EVERY array
    the tenant owns and rolls the per-array verdicts into a fleet summary the
    Audit tab renders: how many arrays reconcile cleanly, how many show an
    unconfirmed gap, how many can't be audited yet (and why), plus the dollars
    flagged. Read-only; never fabricates — an array with no production feed comes
    back honestly as 'insufficient_data', not a fake leak.

    Shape:
      {
        "summary": {
          "total": int, "auditable": int, "ok": int, "leak": int,
          "leak_unconfirmed": int, "incomplete_monitoring": int,
          "insufficient_data": int, "have_settlement": int,
          "have_production": int, "dollars_flagged": float,
          "coverage_pct": float          # auditable / total
        },
        "arrays": [ {
            "array_id", "name", "status", "classification",
            "settlement_kwh", "production_kwh", "coverage_ratio",
            "variance_pct", "dollars_at_risk", "independent_feed",
            "n_bills", "headline", "detail"
        }, ... ]   # sorted: leaks first, then unconfirmed, then by $ desc
      }
    """
    tenant = _tenant_from_bearer(authorization)
    from .reconciliation import reconcile_array

    # Human-facing one-liners per status (the slick card copy).
    HEADLINE = {
        "leak": "Money leak confirmed",
        "leak_unconfirmed": "Possible gap — confirm with monitoring",
        "ok": "Reconciles cleanly",
        "incomplete_monitoring": "Partial monitoring",
        "insufficient_data": "Not enough data to audit yet",
    }
    RANK = {"leak": 0, "leak_unconfirmed": 1, "incomplete_monitoring": 2,
            "ok": 3, "insufficient_data": 4}

    rows: list[dict] = []
    summary = {
        "total": 0, "auditable": 0, "ok": 0, "leak": 0,
        "leak_unconfirmed": 0, "incomplete_monitoring": 0,
        "insufficient_data": 0, "have_settlement": 0, "have_production": 0,
        "dollars_flagged": 0.0,
    }

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array)
            .where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None),
                   Array.excluded.is_(False))
            .order_by(Array.id)
        ).scalars().all()

        for arr in arrays:
            summary["total"] += 1
            try:
                r = reconcile_array(db, arr.id)
            except Exception:
                # A single array's failure must not 500 the whole audit.
                summary["insufficient_data"] += 1
                rows.append({
                    "array_id": arr.id, "name": arr.name,
                    "status": "insufficient_data",
                    "classification": "single_site",
                    "settlement_kwh": 0.0, "production_kwh": 0.0,
                    "coverage_ratio": None, "variance_pct": None,
                    "dollars_at_risk": 0.0, "independent_feed": False,
                    "n_bills": 0,
                    "headline": HEADLINE["insufficient_data"],
                    "detail": "Audit could not run for this array.",
                })
                continue

            st = r.status
            if st in summary:
                summary[st] += 1
            if st in ("ok", "leak"):
                summary["auditable"] += 1
            if r.settlement_kwh > 0:
                summary["have_settlement"] += 1
            if r.production_kwh > 0:
                summary["have_production"] += 1
            if st in ("leak", "leak_unconfirmed"):
                summary["dollars_flagged"] += r.dollars_at_risk or 0.0

            rows.append({
                "array_id": arr.id, "name": arr.name,
                "status": st, "classification": r.classification,
                "settlement_kwh": r.settlement_kwh,
                "production_kwh": r.production_kwh,
                "coverage_ratio": r.coverage_ratio,
                "variance_pct": r.variance_pct,
                "dollars_at_risk": r.dollars_at_risk,
                "independent_feed": bool(r.gates.get("independent_feed")),
                "n_bills": r.n_bills,
                "headline": HEADLINE.get(st, st),
                "detail": (r.notes[-1] if r.notes else ""),
            })

    summary["dollars_flagged"] = round(summary["dollars_flagged"], 2)
    summary["coverage_pct"] = (
        round(100.0 * summary["auditable"] / summary["total"], 1)
        if summary["total"] else 0.0
    )
    rows.sort(key=lambda x: (RANK.get(x["status"], 9),
                             -(x["dollars_at_risk"] or 0)))
    return {"summary": summary, "arrays": rows}


# ── fleet tree (sandbox) ──────────────────────────────────────────────────────
# Per-inverter equipment reads are heavier (inventory + 1 telemetry call per
# inverter), so cache the assembled tree per array for 10 minutes to respect the
# 300 req/day SolarEdge budget. Keyed by (site_id) -> (fetched_at, inverters).
_TREE_TTL = timedelta(minutes=10)
_tree_cache: dict[str, tuple[datetime, list[dict]]] = {}


def _se_inverters_for(api_key: str, site_id: int) -> list[dict]:
    """Real per-inverter rows for one SolarEdge site, peer-analyzed within the
    site. Returns [] on any SolarEdge failure (caller decides how to render).
    Cached 10 min by site_id."""
    ck = f"se:{site_id}"
    hit = _tree_cache.get(ck)
    if hit and (now() - hit[0]) < _TREE_TTL:
        return hit[1]

    from .adapters import solaredge as _se
    try:
        inv = _se.fetch_inventory(api_key, site_id)
    except _se.SolarEdgeError:
        return []

    units = []
    meta = {}
    for it in inv:
        sn = it.get("sn")
        if not sn:
            continue
        try:
            tel = _se.fetch_inverter_telemetry(api_key, site_id, sn, days_back=7)
        except _se.SolarEdgeError:
            tel = {"daily": [], "error_code": None, "last_report": None,
                   "last_mode": None, "last_power_w": None}
        units.append({
            "id": sn,
            "nameplate_kw": it.get("nameplate_kw"),
            "daily": tel["daily"],
            "error_code": tel["error_code"],
            "last_report": tel["last_report"],
        })
        meta[sn] = {"name": it.get("name"), "model": it.get("model"),
                    "nameplate_kw": it.get("nameplate_kw"),
                    "last_mode": tel.get("last_mode"),
                    "last_power_w": tel.get("last_power_w")}

    analyzed = peer_analysis.analyze_cohort(units) if units else {"units": []}
    rows: list[dict] = []
    for u in analyzed["units"]:
        m = meta.get(u["id"], {})
        rows.append({
            "sn": u["id"],
            "name": m.get("name") or u["id"],
            "model": m.get("model"),
            "nameplate_kw": m.get("nameplate_kw"),
            "peer_index": u.get("peer_index"),
            "status": u.get("status"),
            "diagnosis": u.get("diagnosis"),
            "window_kwh": u.get("window_kwh"),
            "last_mode": m.get("last_mode"),
            "current_power_w": m.get("last_power_w"),
            "last_report": u.get("last_report"),
        })
    _tree_cache[ck] = (now(), rows)
    return rows


_ALERT_PRIORITY = {"fault": 4, "dead": 4, "comm_gap": 3, "underperforming": 2, "ok": 0}


def _array_alert(inverters: list[dict], array_status: str | None) -> dict:
    """Roll the worst inverter state (plus the array-level peer status) into one
    Alert node: {level: ok|info|warn|critical, count, headline}."""
    worst = array_status or "ok"
    worst_rank = _ALERT_PRIORITY.get(worst, 0)
    bad = 0
    for inv in inverters:
        st = inv.get("status") or "ok"
        r = _ALERT_PRIORITY.get(st, 0)
        if r >= 2:
            bad += 1
        if r > worst_rank:
            worst_rank, worst = r, st

    level = ("critical" if worst_rank >= 4 else
             "warn" if worst_rank >= 2 else
             "ok")
    headline = {
        "fault": "Inverter fault — service drafted",
        "dead": "An inverter stopped earning",
        "comm_gap": "An inverter went quiet",
        "underperforming": "A money leak caught early",
        "ok": "All clear",
    }.get(worst, "All clear")
    return {"level": level, "count": bad, "status": worst, "headline": headline}


@router.get("/v1/array-owners/fleet-tree")
def fleet_tree(force: int = 0, authorization: str | None = Header(default=None)) -> dict:
    """Owner-grouped three-tier sandbox structure — the REAL integrated model:

        Alert  (per array, rolled-up worst state)
          └─ Array  (an OWNER-DEFINED group)
               └─ Inverters  (persisted, owner-arranged; telemetry by source)

    Inverters are persisted `Inverter` rows the owner can drag between arrays.
    Each inverter's telemetry is pulled from its fixed SOURCE site; peer analysis
    runs within each OWNER group — so moving an inverter genuinely changes its
    cohort. Pass ?force=1 to bypass the 10-min telemetry cache.

    See api/inverter_fleet.py for the model rationale (owners reproduce the model
    in their head; the vendor's site grouping is just the starting point).
    """
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    with SessionLocal() as db:
        return inverter_fleet.build_fleet_tree(db, tenant, force_refresh=bool(force))


class ReassignInverterBody(BaseModel):
    inverter_id: int
    target_array_id: int
    position: Optional[int] = None


@router.post("/v1/array-owners/inverters/reassign")
def reassign_inverter_ep(body: ReassignInverterBody,
                         authorization: str | None = Header(default=None)) -> dict:
    """Move an inverter into a different array (the owner's drag, persisted).
    Telemetry source is untouched — only the owner grouping changes."""
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            iv = inverter_fleet.reassign_inverter(
                db, tenant, body.inverter_id, body.target_array_id, body.position
            )
        except inverter_fleet.FleetError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "inverter_id": iv.id, "array_id": iv.array_id,
                "position": iv.position}


class ReorderInvertersBody(BaseModel):
    array_id: int
    ordered_inverter_ids: list[int]


@router.post("/v1/array-owners/inverters/reorder")
def reorder_inverters_ep(body: ReorderInvertersBody,
                         authorization: str | None = Header(default=None)) -> dict:
    """Persist inverter order within one array (drag-to-reorder)."""
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            inverter_fleet.reorder_within_array(
                db, tenant, body.array_id, body.ordered_inverter_ids
            )
        except inverter_fleet.FleetError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}


class CreateArrayBody(BaseModel):
    name: str


@router.post("/v1/array-owners/arrays")
def create_array_ep(body: CreateArrayBody,
                    authorization: str | None = Header(default=None)) -> dict:
    """Create a new owner-defined array (an empty group to drag inverters into)."""
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    with SessionLocal() as db:
        arr = inverter_fleet.create_array(db, tenant, body.name)
        return {"ok": True, "array_id": arr.id, "array_name": arr.name}


@router.get("/v1/array-owners/utility-accounts")
def list_utility_accounts_ep(authorization: str | None = Header(default=None)) -> dict:
    """The tenant's captured GMP/utility accounts and which array each is linked
    to (if any). Powers the 'link a captured account to an array' UI — the manual
    bridge for the multi-meter case (e.g. Starlake = 3 GMP accounts → 1 array)
    that name-matching can't auto-resolve. Also reports how many bills each
    account carries, so the owner knows which links will light up history."""
    from .models import UtilityAccount, Array, Bill
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        accts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant.id,
                UtilityAccount.deleted_at.is_(None),
            ).order_by(UtilityAccount.provider, UtilityAccount.account_number)
        ).scalars().all()
        out = []
        for a in accts:
            arr = db.get(Array, a.array_id) if a.array_id else None
            nbills = db.execute(
                select(func.count(Bill.id)).where(Bill.account_id == a.id)
            ).scalar() or 0
            out.append({
                "account_id": a.id, "provider": a.provider,
                "account_number": a.account_number, "nickname": a.nickname,
                "linked_array_id": a.array_id,
                "linked_array_name": arr.name if arr else None,
                "bill_count": int(nbills),
            })
        arrays = [{"array_id": ar.id, "name": ar.name} for ar in db.execute(
            select(Array).where(Array.tenant_id == tenant.id,
                                Array.deleted_at.is_(None)).order_by(Array.name)
        ).scalars().all()]
        return {"ok": True, "accounts": out, "arrays": arrays}


class LinkAccountBody(BaseModel):
    account_id: int
    array_id: int | None = None   # None = unlink


@router.get("/v1/array-owners/onboarding-status")
def onboarding_status_ep(authorization: str | None = Header(default=None)) -> dict:
    """Is the owner's onboarding actually COMPLETE? The product needs a REAL
    data source to do its job (audit / reconcile / bill). This drives the
    finish-setup (#gmpGate) banner.

    "Connected" means ANY working source — GMP, a VEC/WEC utility account, an
    inverter connection (SolarEdge/Fronius/SMA/Chint, incl. legacy
    Array.solaredge_site_id), or any stored DailyGeneration. The banner is named
    after GMP but must NOT nag an owner who connected another way (Ford's AO
    tenant: 19 arrays via SolarEdge, 0 GMP → banner was stuck on).

    Returns:
      gmp_connected     — a GMP session/account has been captured for this tenant
      connected         — ANY data source is connected (drives the banner)
      has_gmp_accounts  — at least one GMP UtilityAccount exists
      has_inverter      — an inverter connection (or legacy SolarEdge) exists
      has_utility_accounts — any utility account (any provider) exists
      linked_arrays     — # of arrays with a GMP account linked
      unlinked_accounts — # of captured GMP accounts not yet linked to an array
      arrays_total      — # of (active) arrays
      complete          — True once ANY source is connected (banner hides)
      next_step         — machine key for the UI: 'connect_gmp' | 'link_accounts' | 'done'
    """
    from .models import UtilityAccount, Array, UtilitySession, InverterConnection, DailyGeneration
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        gmp_sessions = db.execute(
            select(func.count(UtilitySession.id)).where(
                UtilitySession.tenant_id == tenant.id,
                UtilitySession.provider == "gmp",
            )
        ).scalar() or 0
        gmp_accts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant.id,
                UtilityAccount.provider == "gmp",
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all()
        linked = sum(1 for a in gmp_accts if a.array_id is not None)
        unlinked = sum(1 for a in gmp_accts if a.array_id is None)
        arrays_total = db.execute(
            select(func.count(Array.id)).where(
                Array.tenant_id == tenant.id, Array.deleted_at.is_(None),
            )
        ).scalar() or 0

        gmp_connected = gmp_sessions > 0 or len(gmp_accts) > 0

        # The setup gate is about whether the product has a REAL data source to
        # work with — NOT specifically GMP. Array Operator owners connect via
        # SolarEdge/Fronius/SMA/Chint inverters and VEC/WEC meters too; demanding
        # GMP nagged owners who were already fully connected another way (Ford's
        # AO tenant: 19 arrays via SolarEdge, 0 GMP → banner stuck on). So
        # "connected" = ANY working source:
        #   • GMP session/account, OR
        #   • any non-GMP utility account (vec/wec…), OR
        #   • an inverter connection (or a legacy Array.solaredge_site_id), OR
        #   • any stored DailyGeneration row (data is actually flowing).
        any_utility_acct = db.execute(
            select(func.count(UtilityAccount.id)).where(
                UtilityAccount.tenant_id == tenant.id,
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalar() or 0
        inverter_conns = db.execute(
            select(func.count(InverterConnection.id))
            .select_from(InverterConnection)
            .join(Array, InverterConnection.array_id == Array.id)
            .where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
        ).scalar() or 0
        legacy_solaredge = db.execute(
            select(func.count(Array.id)).where(
                Array.tenant_id == tenant.id, Array.deleted_at.is_(None),
                Array.solaredge_site_id.is_not(None),
            )
        ).scalar() or 0
        daily_rows = db.execute(
            select(func.count(DailyGeneration.id)).where(
                DailyGeneration.tenant_id == tenant.id,
            )
        ).scalar() or 0
        inverter_connected = (inverter_conns > 0) or (legacy_solaredge > 0)
        connected = bool(
            gmp_connected or any_utility_acct > 0 or inverter_connected or daily_rows > 0
        )

        # complete (drives the finish-setup banner): hidden once ANY source is
        # connected. next_step keeps the GMP-specific guidance for the common
        # path but no longer holds the gate up for non-GMP owners.
        complete = connected
        if not connected:
            next_step = "connect_gmp"
        elif gmp_connected and linked == 0:
            next_step = "link_accounts"
        else:
            next_step = "done"
        return {
            "ok": True,
            "gmp_connected": gmp_connected,
            "connected": connected,
            "has_gmp_accounts": len(gmp_accts) > 0,
            "has_inverter": inverter_connected,
            "has_utility_accounts": any_utility_acct > 0,
            "linked_arrays": linked,
            "unlinked_accounts": unlinked,
            "arrays_total": int(arrays_total),
            "complete": complete,
            "next_step": next_step,
        }


@router.post("/v1/array-owners/utility-accounts/link")
def link_utility_account_ep(body: LinkAccountBody,
                            authorization: str | None = Header(default=None)) -> dict:
    """Link (or unlink) a captured GMP account to an existing array — the manual
    bridge so one array can aggregate several GMP meters. Once linked, the
    account's captured bills flow into that array's daily stream (via the
    bill→daily transform) and its trends/audit/reconcile light up. Tenant-scoped:
    both the account and the target array must belong to the caller."""
    from .models import UtilityAccount, Array
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        acct = db.get(UtilityAccount, body.account_id)
        if acct is None or acct.tenant_id != tenant.id or acct.deleted_at is not None:
            raise HTTPException(404, "utility account not found")
        if body.array_id is not None:
            arr = db.get(Array, body.array_id)
            if arr is None or arr.tenant_id != tenant.id or arr.deleted_at is not None:
                raise HTTPException(404, "array not found")
            acct.array_id = arr.id
        else:
            acct.array_id = None
        db.commit()
        return {"ok": True, "account_id": acct.id, "linked_array_id": acct.array_id}


# ── Inverter down/underperformance email alerts ──────────────────────────────
class AlertSettingsBody(BaseModel):
    """All optional → only provided fields are updated."""
    enabled: bool | None = None
    email: str | None = None
    threshold_pct: int | None = None      # 10–95 sensitivity (alert under this % of peers)
    grace_hours: int | None = None        # 0–168 hours an inverter must stay down before emailing


def _alert_settings_dict(tenant) -> dict:
    return {
        "enabled": bool(getattr(tenant, "inverter_alerts_enabled", False)),
        "email": getattr(tenant, "inverter_alert_email", None) or tenant.contact_email,
        "email_is_default": not getattr(tenant, "inverter_alert_email", None),
        "threshold_pct": int(getattr(tenant, "inverter_alert_threshold_pct", 50) or 50),
        "grace_hours": int(getattr(tenant, "inverter_alert_grace_hours", 12) or 12),
    }


@router.get("/v1/array-owners/alert-settings")
def get_alert_settings(authorization: str | None = Header(default=None)) -> dict:
    """The owner's inverter-alert preferences (used by the sandbox settings panel)."""
    tenant = _tenant_from_bearer(authorization)
    return _alert_settings_dict(tenant)


@router.put("/v1/array-owners/alert-settings")
def put_alert_settings(body: AlertSettingsBody,
                       authorization: str | None = Header(default=None)) -> dict:
    """Update inverter-alert preferences. Validates the email + clamps the
    threshold (10–95%) and grace window (0–168h)."""
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    if body.email is not None:
        e = body.email.strip()
        if e and ("@" not in e or " " in e or "." not in e.split("@")[-1]):
            raise HTTPException(400, "email must be a valid email address")
    with SessionLocal() as db:
        t = db.get(type(tenant), tenant.id)
        if t is None:
            raise HTTPException(404, "Tenant not found")
        if body.enabled is not None:
            t.inverter_alerts_enabled = bool(body.enabled)
        if body.email is not None:
            t.inverter_alert_email = body.email.strip() or None
        if body.threshold_pct is not None:
            t.inverter_alert_threshold_pct = max(10, min(95, int(body.threshold_pct)))
        if body.grace_hours is not None:
            t.inverter_alert_grace_hours = max(0, min(168, int(body.grace_hours)))
        db.commit()
        db.refresh(t)
        return _alert_settings_dict(t)


@router.delete("/v1/array-owners/arrays/{array_id}")
def delete_array_ep(array_id: int,
                    authorization: str | None = Header(default=None)) -> dict:
    """Soft-delete an owner array (the owner's "remove card") and its inverters.

    SOFT-delete only: sets `deleted_at` on the Array and its Inverter rows so the
    array disappears from GET /v1/array-owners/fleet-tree immediately (that tree
    filters Array.deleted_at.is_(None)). 404 if the array isn't found or belongs
    to another tenant. The shared read-only DEMO tenant can never delete (403).
    AO billing is per-kWh metered, so this does NOT touch Stripe.
    """
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            arr = inverter_fleet.delete_array(db, tenant, array_id)
        except inverter_fleet.FleetError:
            raise HTTPException(404, "Array not found")
        return {"ok": True, "array_id": arr.id}


@router.post("/v1/array-owners/arrays/{array_id}/restore")
def restore_array_ep(array_id: int,
                     authorization: str | None = Header(default=None)) -> dict:
    """Un-delete a soft-deleted owner array + the inverters removed with it.

    The inverse of DELETE /v1/array-owners/arrays/{array_id} — powers the sandbox
    "Undo delete". Clears `deleted_at` so the array reappears in the fleet tree.
    404 if the array isn't found, isn't currently deleted, or belongs to another
    tenant. The shared read-only DEMO tenant can never mutate (403).
    """
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            arr = inverter_fleet.restore_array(db, tenant, array_id)
        except inverter_fleet.FleetError:
            raise HTTPException(404, "Array not found")
        return {"ok": True, "array_id": arr.id, "array_name": arr.name}


@router.delete("/v1/array-owners/inverters/{inverter_id}")
def delete_inverter_ep(inverter_id: int,
                       authorization: str | None = Header(default=None)) -> dict:
    """Soft-delete a single inverter (the owner's right-click "Delete inverter").

    SOFT-delete only: sets `deleted_at` on the one Inverter row so it disappears
    from GET /v1/array-owners/fleet-tree immediately (that tree filters
    Inverter.deleted_at.is_(None)) while its parent array and siblings stay put.
    404 if the inverter isn't found or belongs to another tenant. The shared
    read-only DEMO tenant can never delete (403). AO billing is per-kWh metered,
    so this does NOT touch Stripe.
    """
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            iv = inverter_fleet.delete_inverter(db, tenant, inverter_id)
        except inverter_fleet.FleetError:
            raise HTTPException(404, "Inverter not found")
        return {"ok": True, "inverter_id": iv.id}


@router.post("/v1/array-owners/inverters/{inverter_id}/restore")
def restore_inverter_ep(inverter_id: int,
                        authorization: str | None = Header(default=None)) -> dict:
    """Un-delete a soft-deleted inverter — powers the sandbox "Undo delete".

    The inverse of DELETE /v1/array-owners/inverters/{inverter_id}: clears
    `deleted_at` so the inverter reappears in the fleet tree under its array.
    404 if the inverter isn't found, isn't currently deleted, or belongs to
    another tenant. The shared read-only DEMO tenant can never mutate (403).
    """
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            iv = inverter_fleet.restore_inverter(db, tenant, inverter_id)
        except inverter_fleet.FleetError:
            raise HTTPException(404, "Inverter not found")
        return {"ok": True, "inverter_id": iv.id}


@router.post("/v1/array-owners/layout/reset")
def reset_layout_ep(authorization: str | None = Header(default=None)) -> dict:
    """Snap every inverter back to its discovered (source) array grouping."""
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    with SessionLocal() as db:
        n = inverter_fleet.reset_layout(db, tenant)
        return {"ok": True, "reset": n}


@router.get("/v1/array-owners/inverter-vendors")
def inverter_vendors(authorization: str | None = Header(default=None)) -> list[dict]:
    """The connect-form spec the dashboard renders: one entry per vendor.

    Each entry: {code, label, fields:[{name,label,secret}], available, note}.
    Chint comes back available=false with its manual-CSV note.
    """
    _tenant_from_bearer(authorization)
    return inverters.vendor_catalog()


class InverterConnectBody(BaseModel):
    vendor: str
    config: dict


@router.post("/v1/array-owners/arrays/{array_id}/inverter")
def connect_inverter(
    array_id: int,
    body: InverterConnectBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Connect an inverter source of any vendor to an array.

    Dispatches validate() for the vendor; on success upserts the
    InverterConnection (status="ok"). Credential/validation errors return 400
    and persist nothing.
    """
    tenant = _tenant_from_bearer(authorization)

    vendor = (body.vendor or "").strip().lower()
    if vendor not in VENDORS:
        raise HTTPException(400, f"Unknown inverter vendor: {vendor!r}")

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant.id:
            raise HTTPException(404, "Array not found")

        try:
            result = _connect_inverter(db, arr, vendor, dict(body.config or {}))
        except InverterAuthError as exc:
            raise HTTPException(400, str(exc))
        except InverterError as exc:
            raise HTTPException(400, str(exc))

    return {"ok": True, "site_name": result.get("site_name")}


class SolarEdgeConnectBody(BaseModel):
    api_key: str
    site_id: int


@router.post("/v1/array-owners/arrays/{array_id}/solaredge")
def connect_solaredge(
    array_id: int,
    body: SolarEdgeConnectBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Legacy SolarEdge connect — thin shim over the inverter framework.

    Kept for backward compat (existing frontend + tests). Forwards to the
    vendor-agnostic connect path and preserves the original response shape
    (site_name + peak_power_kw + site_id). A rejected key (401/403) or
    unreachable site returns 400 and persists nothing.
    """
    tenant = _tenant_from_bearer(authorization)

    api_key = (body.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")

    config = {"api_key": api_key, "site_id": body.site_id}

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant.id:
            raise HTTPException(404, "Array not found")

        try:
            result = _connect_inverter(db, arr, "solaredge", config)
        except InverterAuthError as exc:
            raise HTTPException(400, str(exc))
        except InverterError as exc:
            raise HTTPException(400, f"SolarEdge error: {exc}")

    return {
        "ok": True,
        "site_name": result.get("site_name"),
        "peak_power_kw": result.get("peak_power_kw"),
        "site_id": body.site_id,
    }


# ── account-level SolarEdge discovery ("paste one credential, attach all") ─────

def _attach_solaredge(db, arr: Array, api_key: str, site_id: int) -> InverterConnection:
    """Upsert a solaredge InverterConnection on `arr` WITHOUT a per-site validate
    call — the account-level key was already proven by discover(), so we don't
    burn one SolarEdge request per site. Mirrors the creds onto the legacy
    columns so the daily pull + virtual-connection path keep working.
    """
    config = {"api_key": api_key, "site_id": int(site_id)}
    conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == arr.id)
    ).scalar_one_or_none()
    if conn is None:
        conn = InverterConnection(
            array_id=arr.id, vendor="solaredge", config=config, status="ok"
        )
        db.add(conn)
    else:
        conn.vendor = "solaredge"
        conn.config = config
        conn.status = "ok"
        conn.last_error = None
    arr.solaredge_api_key = api_key
    arr.solaredge_site_id = int(site_id)
    return conn


class SolarEdgeDiscoverBody(BaseModel):
    api_key: str


@router.post("/v1/array-owners/solaredge/discover")
def solaredge_discover(
    body: SolarEdgeDiscoverBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Preview step: list every SolarEdge site an account-level key can read.

    Saves NOTHING — the dashboard shows the sites as checkboxes before the
    operator commits. A site-level key (403) or bad key (401) comes back as a
    400 with a clear, actionable message; a SolarEdge 5xx comes back as a 502.
    """
    _tenant_from_bearer(authorization)

    api_key = (body.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")

    try:
        sites = inverters.solaredge.discover_sites(api_key)
    except InverterScopeError as exc:
        raise HTTPException(400, str(exc))
    except InverterAuthError as exc:
        raise HTTPException(400, str(exc))
    except InverterError as exc:
        raise HTTPException(502, f"SolarEdge error: {exc}")

    return {
        "ok": True,
        "sites": sites,
        "message": None if sites else "No sites found on this SolarEdge account.",
    }


# ── public pre-signup preview ("paste your key, see your REAL arrays") ─────────
# An UNAUTHENTICATED, rate-limited endpoint so a prospective owner can paste an
# account-level SolarEdge key on the marketing site and instantly see their own
# sites + an estimated annual value — BEFORE creating an account. Saves nothing.
# Rate-limited per client IP to keep the open endpoint from being abused as a
# free SolarEdge-key oracle / scraping proxy.

# crude in-memory sliding-window limiter (per-process; Railway runs one web
# replica today). {ip: [monotonic_ts, ...]} pruned on each call.
_PREVIEW_HITS: dict[str, list[float]] = {}
_PREVIEW_WINDOW_S = 300.0   # 5-minute window
_PREVIEW_MAX = 8            # ≤8 preview attempts per IP per 5 min

# Typical fixed-tilt PV capacity factor (annual kWh ≈ kW_dc × 8760 × CF). 0.14
# is a conservative US/VT-ish blended figure for a quick pre-signup estimate;
# the dashboard shows exact measured value once live.
_EST_CAPACITY_FACTOR = 0.14


def _client_ip(request: Request) -> str:
    """Best-effort client IP behind Railway's proxy (X-Forwarded-For first hop)."""
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _preview_rate_ok(ip: str) -> bool:
    import time
    now_ts = time.monotonic()
    hits = [t for t in _PREVIEW_HITS.get(ip, []) if now_ts - t < _PREVIEW_WINDOW_S]
    if len(hits) >= _PREVIEW_MAX:
        _PREVIEW_HITS[ip] = hits
        return False
    hits.append(now_ts)
    _PREVIEW_HITS[ip] = hits
    return True


def _estimate_annual_value(peak_power_kw: float) -> dict:
    """Rough annual $ value for a site of `peak_power_kw`, for the pre-signup
    teaser. energy = kWh × default rate; REC = floored MWh × REC price. Clearly
    an estimate — the dashboard pins the real number once connected."""
    kw = max(float(peak_power_kw or 0.0), 0.0)
    annual_kwh = kw * 8760.0 * _EST_CAPACITY_FACTOR
    energy_usd = annual_kwh * get_energy_rate(None)
    rec_usd = math.floor(annual_kwh / 1000.0) * REC_PRICE_USD_PER_MWH
    return {
        "annual_kwh": round(annual_kwh),
        "annual_value_usd": round(energy_usd + rec_usd),
    }


class PublicPreviewBody(BaseModel):
    # Back-compat: a bare api_key means SolarEdge. New callers send vendor +
    # config (the per-vendor credential field dict the connect form collects).
    api_key: Optional[str] = None
    vendor: Optional[str] = None
    config: Optional[dict] = None


# Per-vendor friendly copy for the two recoverable failures (bad creds / scope).
_PREVIEW_AUTH_MSG = {
    "solaredge": "That key didn't work — make sure it's an active account-level API key.",
    "locus": "Those Locus credentials didn't work — double-check the client ID/secret and your SolarNOC login.",
    "fronius": "Those Solar.web keys didn't work — check the Access Key ID/Value and PV System ID.",
    "sma": "Those SMA credentials didn't work — check the client ID/secret and Plant/System ID.",
}
_PREVIEW_SCOPE_MSG = {
    "solaredge": "That's a site-level key — it can't list your whole account. Paste an "
                 "account-level key (SolarEdge Admin → Site Access → API Access) to see "
                 "every array at once.",
    "locus": "Those credentials can't list the partner's sites. Add your Partner ID, or "
             "enter a single Site ID to preview just that array.",
}


def _normalize_preview_site(vendor: str, raw: dict) -> dict:
    """Coerce a vendor's site/validate result into the UI's site shape +
    attach a value estimate when a peak_power_kw is known."""
    # peak: SolarEdge/Locus discovery already give peak_power_kw; Fronius
    # validate() gives peak_power in Wp (→kW); SMA gives none.
    kw = raw.get("peak_power_kw")
    if kw is None and raw.get("peak_power") is not None:
        try:
            kw = float(raw["peak_power"]) / 1000.0  # Solar.web Wp → kW
        except (TypeError, ValueError):
            kw = None
    site = {
        "site_id": raw.get("site_id") or raw.get("id") or "",
        "name": raw.get("name") or raw.get("site_name") or "",
        "peak_power_kw": round(kw, 2) if kw else None,
        "status": raw.get("status") or "",
    }
    if kw:
        site.update(_estimate_annual_value(kw))
    else:
        site.update({"annual_kwh": None, "annual_value_usd": None})
    return site


def _preview_sites_for_vendor(vendor: str, config: dict) -> list[dict]:
    """List the sites a credential can see for `vendor`, normalized for the UI.

    SolarEdge + Locus enumerate the whole account/partner (discover_sites);
    Fronius + SMA have no discovery, so we validate the one system the owner
    named and return it as a single site. Raises the InverterError family on
    auth/scope/transport failures (the caller turns those into friendly copy).
    """
    mod = inverters.VENDORS.get(vendor)
    if mod is None or not getattr(mod, "AVAILABLE", True):
        raise InverterError(f"{vendor} can't be previewed.")

    if vendor == "solaredge":
        key = (config.get("api_key") or config.get("apiKey") or "").strip()
        if not key:
            raise InverterError("api_key is required")
        raw = mod.discover_sites(key)
        return [_normalize_preview_site(vendor, s) for s in raw]

    if vendor == "locus":
        # Account-wide discovery needs a partner_id; otherwise preview the one
        # named site via validate().
        if (config.get("partner_id") or "").strip():
            raw = mod.discover_sites(config)
            return [_normalize_preview_site(vendor, s) for s in raw]
        return [_normalize_preview_site(vendor, mod.validate(config))]

    # Fronius / SMA: single-system validate.
    return [_normalize_preview_site(vendor, mod.validate(config))]


@router.post("/v1/array-owners/public/preview")
def public_solaredge_preview(body: PublicPreviewBody, request: Request) -> dict:
    """UNAUTHENTICATED pre-signup preview for ANY supported vendor: list the
    real sites a credential can read + a rough annual value, so a prospective
    owner sees THEIR arrays before signing up. Saves nothing. Rate-limited per IP.

    Accepts either {api_key} (legacy → SolarEdge) or {vendor, config}. Returns
    {ok, vendor, sites:[{site_id,name,peak_power_kw,annual_kwh,annual_value_usd}],
    totals, message}. Recoverable failures (bad creds, scope, empty) come back
    as ok:false + friendly message; rate limit → 429; vendor 5xx → 502."""
    ip = _client_ip(request)
    if not _preview_rate_ok(ip):
        raise HTTPException(429, "Too many preview attempts — give it a few minutes and try again.")

    # Resolve vendor + config (back-compat: a bare api_key is SolarEdge).
    vendor = (body.vendor or "").strip().lower() or "solaredge"
    config = dict(body.config or {})
    if body.api_key and not config.get("api_key"):
        config["api_key"] = body.api_key
    if vendor not in inverters.VENDORS:
        return {"ok": False, "vendor": vendor, "sites": [],
                "message": "We don't support that inverter brand yet."}
    if not getattr(inverters.VENDORS[vendor], "AVAILABLE", True):
        # e.g. Chint/CPS — no API; the UI shouldn't offer it, but be defensive.
        return {"ok": False, "vendor": vendor, "sites": [],
                "message": f"{inverters.VENDORS[vendor].LABEL} doesn't offer a live "
                           "connection yet — you can add it by CSV after signing up."}

    # Required fields present? (cheap pre-check so we return friendly copy, not 500)
    needs = {
        "solaredge": ["api_key"],
        "locus": ["client_id", "client_secret", "username", "password"],
        "fronius": ["access_key_id", "access_key_value", "pv_system_id"],
        "sma": ["client_id", "client_secret", "system_id"],
    }.get(vendor, [])
    missing = [n for n in needs if not str(config.get(n) or "").strip()]
    if vendor == "solaredge" and not str(config.get("api_key") or config.get("apiKey") or "").strip():
        missing = ["api_key"]
    if missing:
        return {"ok": False, "vendor": vendor, "sites": [],
                "message": "Fill in your credentials first."}

    try:
        sites = _preview_sites_for_vendor(vendor, config)
    except InverterScopeError:
        return {"ok": False, "vendor": vendor, "sites": [], "scope": "site",
                "message": _PREVIEW_SCOPE_MSG.get(vendor, _PREVIEW_AUTH_MSG.get(vendor, "Those credentials lack access."))}
    except InverterAuthError:
        return {"ok": False, "vendor": vendor, "sites": [],
                "message": _PREVIEW_AUTH_MSG.get(vendor, "Those credentials didn't work.")}
    except InverterError as exc:
        raise HTTPException(502, f"{inverters.VENDORS[vendor].LABEL} is unreachable right now: {exc}")

    total_kw = 0.0
    total_val = 0.0
    any_value = False
    for s in sites:
        if s.get("peak_power_kw"):
            total_kw += float(s["peak_power_kw"])
        if s.get("annual_value_usd"):
            total_val += float(s["annual_value_usd"])
            any_value = True

    label = inverters.VENDORS[vendor].LABEL
    return {
        "ok": True,
        "vendor": vendor,
        "sites": sites,
        "totals": {
            "count": len(sites),
            "peak_power_kw": round(total_kw, 1),
            # None when we couldn't estimate any value (e.g. SMA gives no peak) —
            # the UI then shows the arrays without the dollar hero.
            "annual_value_usd": round(total_val) if any_value else None,
        },
        "message": None if sites else f"We reached {label} but found no sites on it.",
    }


class SolarEdgeConnectAccountBody(BaseModel):
    api_key: str
    # When omitted, every discovered site is connected.
    site_ids: Optional[list[int]] = None


@router.post("/v1/array-owners/solaredge/connect-account")
def solaredge_connect_account(
    body: SolarEdgeConnectAccountBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Attach every (or a chosen subset of) SolarEdge site on an account-level
    key to the tenant's arrays in one shot.

    Per site: match an existing array by (1) its InverterConnection site_id,
    (2) the legacy Array.solaredge_site_id, or (3) an EXACT case-insensitive
    name match — otherwise create a fresh Array (solar, no client). Idempotent:
    re-running updates the same arrays instead of duplicating them.

    Returns {connected, created, matched} so the UI can celebrate specifics.
    """
    tenant = _tenant_from_bearer(authorization)

    api_key = (body.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")

    requested: set[int] | None = None
    if body.site_ids is not None:
        requested = {int(s) for s in body.site_ids}

    # 1. Discover the account's sites (or fall back for a site-level key).
    try:
        discovered = inverters.solaredge.discover_sites(api_key)
    except InverterScopeError:
        # Site-level key — can't enumerate. If the caller named explicit sites we
        # can still validate each by id; otherwise ask for an account-level key.
        if not requested:
            raise HTTPException(
                400,
                "This is a site-level SolarEdge key, which can't list your "
                "sites. Enter the site ID manually, or paste an account-level "
                "API key (SolarEdge Admin → Site Access → API Access) to attach "
                "every array at once.",
            )
        discovered = []
        for sid in sorted(requested):
            try:
                details = inverters.solaredge.validate(
                    {"api_key": api_key, "site_id": sid}
                )
            except InverterAuthError as exc:
                raise HTTPException(400, str(exc))
            except InverterError as exc:
                raise HTTPException(502, f"SolarEdge error: {exc}")
            discovered.append({
                "site_id": sid,
                "name": details.get("site_name") or f"SolarEdge site {sid}",
                "peak_power_kw": details.get("peak_power_kw"),
                "status": "",
            })
    except InverterAuthError as exc:
        raise HTTPException(400, str(exc))
    except InverterError as exc:
        raise HTTPException(502, f"SolarEdge error: {exc}")

    # 2. Narrow to the requested subset (account-level path).
    if requested is not None:
        discovered = [s for s in discovered if int(s["site_id"]) in requested]

    if not discovered:
        return {
            "ok": True,
            "connected": [], "created": [], "matched": [],
            "message": "No SolarEdge sites to connect.",
        }

    # 3. Attach each site to an array (match existing or create new).
    connected: list[dict] = []
    created: list[dict] = []
    matched: list[dict] = []

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id, Array.deleted_at.is_(None)
            )
        ).scalars().all()
        conns = {
            c.array_id: c for c in db.execute(
                select(InverterConnection).where(
                    InverterConnection.array_id.in_([a.id for a in arrays] or [0])
                )
            ).scalars().all()
        }

        by_site_id: dict[int, list[int]] = defaultdict(list)
        by_name: dict[str, list[int]] = defaultdict(list)
        names_lower: set[str] = set()
        # uq_array_per_tenant spans soft-deleted rows, so the new-array collision
        # guard must check ALL names (live + soft-deleted), else a site colliding
        # with a deleted array's name slips through → INSERT → UniqueViolation 500.
        all_names_lower: set[str] = {
            n.strip().lower() for (n,) in db.execute(
                select(Array.name).where(Array.tenant_id == tenant.id)
            ).all()
        }
        arr_by_id = {a.id: a for a in arrays}
        for a in arrays:
            key = a.name.strip().lower()
            names_lower.add(key)
            by_name[key].append(a.id)
            sid = None
            c = conns.get(a.id)
            if c is not None and c.vendor == "solaredge":
                sid = (c.config or {}).get("site_id")
            elif a.solaredge_site_id:
                sid = a.solaredge_site_id
            if sid is not None:
                try:
                    by_site_id[int(sid)].append(a.id)
                except (TypeError, ValueError):
                    pass

        used: set[int] = set()

        for site in discovered:
            sid = int(site["site_id"])
            site_name = (site.get("name") or "").strip() or f"SolarEdge site {sid}"
            entry = {
                "array_id": None,
                "name": site_name,
                "site_id": sid,
                "peak_power_kw": site.get("peak_power_kw"),
            }

            claimants = [aid for aid in by_site_id.get(sid, []) if aid not in used]
            if len(claimants) > 1:
                # Pre-existing data integrity problem — don't silently pick one.
                raise HTTPException(
                    409,
                    f"Two arrays already claim SolarEdge site {sid} "
                    f"(arrays {sorted(claimants)}). Resolve the duplicate before "
                    "connecting this account.",
                )

            target = None
            if claimants:
                target = arr_by_id[claimants[0]]
            else:
                name_hits = [
                    aid for aid in by_name.get(site_name.lower(), [])
                    if aid not in used
                ]
                # Exact, unambiguous name match only — never guess fuzzily, and
                # never collapse two same-named sites onto one array.
                if len(name_hits) == 1:
                    target = arr_by_id[name_hits[0]]

            if target is not None:
                _attach_solaredge(db, target, api_key, sid)
                used.add(target.id)
                entry["array_id"] = target.id
                entry["name"] = target.name
                matched.append(entry)
            else:
                name = site_name
                if name.lower() in all_names_lower:
                    # Two sites share a name OR it collides with an array we won't
                    # reuse — INCLUDING a soft-deleted array, whose name still
                    # reserves the uq_array_per_tenant slot. Disambiguate so the
                    # INSERT can't hit the unique constraint (→ 500).
                    name = f"{site_name} ({sid})"
                new_arr = Array(
                    tenant_id=tenant.id, name=name, client_id=None, fuel_type="solar",
                )
                db.add(new_arr)
                db.flush()
                _attach_solaredge(db, new_arr, api_key, sid)
                used.add(new_arr.id)
                names_lower.add(name.lower())
                all_names_lower.add(name.lower())
                arr_by_id[new_arr.id] = new_arr
                entry["array_id"] = new_arr.id
                entry["name"] = new_arr.name
                created.append(entry)

            connected.append(entry)

        db.commit()

        # Self-healing: kick off the deep multi-year history backfill for every
        # array we just attached so past years populate in Trends within minutes
        # (the nightly pull only reaches ~90 days). Best-effort; the scheduled
        # healer covers any that don't stamp.
        for _e in connected:
            if _e.get("array_id"):
                _trigger_history_backfill(db, _e["array_id"])

    return {
        "ok": True,
        "connected": connected,
        "created": created,
        "matched": matched,
        "message": (
            f"{len(connected)} arrays connected — "
            f"{len(created)} new, {len(matched)} matched."
        ),
    }


# ── account-level Locus discovery ("paste one credential, attach all") ─────────

def _attach_locus(db, arr: Array, creds: dict, site_id: int) -> InverterConnection:
    """Upsert a locus InverterConnection on `arr` WITHOUT a per-site validate
    call — the partner credential was already proven by discover(), so we don't
    burn one Locus request per site. There is NO legacy-column mirroring for
    locus (the solaredge_* columns are SolarEdge-only).
    """
    config = {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "username": creds["username"],
        "password": creds["password"],
        "site_id": int(site_id),
    }
    conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == arr.id)
    ).scalar_one_or_none()
    if conn is None:
        conn = InverterConnection(
            array_id=arr.id, vendor="locus", config=config, status="ok"
        )
        db.add(conn)
    else:
        conn.vendor = "locus"
        conn.config = config
        conn.status = "ok"
        conn.last_error = None
    return conn


class LocusDiscoverBody(BaseModel):
    client_id: str
    client_secret: str
    username: str
    password: str
    partner_id: int


@router.post("/v1/array-owners/locus/discover")
def locus_discover(
    body: LocusDiscoverBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Preview step: list every Locus site under the partner the credential reads.

    Saves NOTHING — the dashboard shows the sites as checkboxes before the
    operator commits. Bad credentials (401) or a forbidden partner (403) come
    back as a 400 with a clear message; a Locus 5xx comes back as a 502.
    """
    _tenant_from_bearer(authorization)

    creds = {
        "client_id": body.client_id,
        "client_secret": body.client_secret,
        "username": body.username,
        "password": body.password,
        "partner_id": body.partner_id,
    }

    try:
        sites = inverters.locus.discover_sites(creds)
    except InverterScopeError as exc:
        raise HTTPException(400, str(exc))
    except InverterAuthError as exc:
        raise HTTPException(400, str(exc))
    except InverterError as exc:
        raise HTTPException(502, f"Locus error: {exc}")

    return {
        "ok": True,
        "sites": sites,
        "message": None if sites else "No sites found under this Locus partner.",
    }


class LocusConnectAccountBody(BaseModel):
    client_id: str
    client_secret: str
    username: str
    password: str
    partner_id: int
    # When omitted, every discovered site is connected.
    site_ids: Optional[list[int]] = None


@router.post("/v1/array-owners/locus/connect-account")
def locus_connect_account(
    body: LocusConnectAccountBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Attach every (or a chosen subset of) Locus site under a partner credential
    to the tenant's arrays in one shot.

    Per site: match an existing array by (1) its locus InverterConnection
    site_id, or (2) an EXACT case-insensitive name match — otherwise create a
    fresh Array (solar, no client). Idempotent: re-running updates the same
    arrays instead of duplicating them.

    Returns {connected, created, matched} so the UI can celebrate specifics.
    """
    tenant = _tenant_from_bearer(authorization)

    creds = {
        "client_id": body.client_id,
        "client_secret": body.client_secret,
        "username": body.username,
        "password": body.password,
        "partner_id": body.partner_id,
    }

    requested: set[int] | None = None
    if body.site_ids is not None:
        requested = {int(s) for s in body.site_ids}

    # 1. Discover the partner's sites.
    try:
        discovered = inverters.locus.discover_sites(creds)
    except InverterScopeError as exc:
        raise HTTPException(400, str(exc))
    except InverterAuthError as exc:
        raise HTTPException(400, str(exc))
    except InverterError as exc:
        raise HTTPException(502, f"Locus error: {exc}")

    # 2. Narrow to the requested subset.
    if requested is not None:
        discovered = [s for s in discovered if int(s["site_id"]) in requested]

    if not discovered:
        return {
            "ok": True,
            "connected": [], "created": [], "matched": [],
            "message": "No Locus sites to connect.",
        }

    # 3. Attach each site to an array (match existing or create new).
    connected: list[dict] = []
    created: list[dict] = []
    matched: list[dict] = []

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id, Array.deleted_at.is_(None)
            )
        ).scalars().all()
        conns = {
            c.array_id: c for c in db.execute(
                select(InverterConnection).where(
                    InverterConnection.array_id.in_([a.id for a in arrays] or [0])
                )
            ).scalars().all()
        }

        by_site_id: dict[int, list[int]] = defaultdict(list)
        by_name: dict[str, list[int]] = defaultdict(list)
        names_lower: set[str] = set()
        # uq_array_per_tenant spans soft-deleted rows — the new-array collision
        # guard must check ALL names (live + soft-deleted) or a site colliding with
        # a deleted array's name slips through → INSERT → UniqueViolation 500.
        all_names_lower: set[str] = {
            n.strip().lower() for (n,) in db.execute(
                select(Array.name).where(Array.tenant_id == tenant.id)
            ).all()
        }
        arr_by_id = {a.id: a for a in arrays}
        for a in arrays:
            key = a.name.strip().lower()
            names_lower.add(key)
            by_name[key].append(a.id)
            c = conns.get(a.id)
            if c is not None and c.vendor == "locus":
                sid = (c.config or {}).get("site_id")
                if sid is not None:
                    try:
                        by_site_id[int(sid)].append(a.id)
                    except (TypeError, ValueError):
                        pass

        used: set[int] = set()

        for site in discovered:
            sid = int(site["site_id"])
            site_name = (site.get("name") or "").strip() or f"Locus site {sid}"
            entry = {
                "array_id": None,
                "name": site_name,
                "site_id": sid,
                "peak_power_kw": site.get("peak_power_kw"),
            }

            claimants = [aid for aid in by_site_id.get(sid, []) if aid not in used]
            if len(claimants) > 1:
                # Pre-existing data integrity problem — don't silently pick one.
                raise HTTPException(
                    409,
                    f"Two arrays already claim Locus site {sid} "
                    f"(arrays {sorted(claimants)}). Resolve the duplicate before "
                    "connecting this account.",
                )

            target = None
            if claimants:
                target = arr_by_id[claimants[0]]
            else:
                name_hits = [
                    aid for aid in by_name.get(site_name.lower(), [])
                    if aid not in used
                ]
                # Exact, unambiguous name match only — never guess fuzzily.
                if len(name_hits) == 1:
                    target = arr_by_id[name_hits[0]]

            if target is not None:
                _attach_locus(db, target, creds, sid)
                used.add(target.id)
                entry["array_id"] = target.id
                entry["name"] = target.name
                matched.append(entry)
            else:
                name = site_name
                if name.lower() in all_names_lower:
                    # Disambiguate so uq_array_per_tenant holds (includes
                    # soft-deleted arrays, whose names still reserve the slot).
                    name = f"{site_name} ({sid})"
                new_arr = Array(
                    tenant_id=tenant.id, name=name, client_id=None, fuel_type="solar",
                )
                db.add(new_arr)
                db.flush()
                _attach_locus(db, new_arr, creds, sid)
                used.add(new_arr.id)
                names_lower.add(name.lower())
                all_names_lower.add(name.lower())
                arr_by_id[new_arr.id] = new_arr
                entry["array_id"] = new_arr.id
                entry["name"] = new_arr.name
                created.append(entry)

            connected.append(entry)

        db.commit()

        # Self-healing: kick off the deep multi-year history backfill for every
        # array we just attached so past years populate in Trends within minutes
        # (the nightly pull only reaches ~90 days). Best-effort; the scheduled
        # healer covers any that don't stamp.
        for _e in connected:
            if _e.get("array_id"):
                _trigger_history_backfill(db, _e["array_id"])

    return {
        "ok": True,
        "connected": connected,
        "created": created,
        "matched": matched,
        "message": (
            f"{len(connected)} arrays connected — "
            f"{len(created)} new, {len(matched)} matched."
        ),
    }


# ── single-system connect (Fronius / SMA / any one-system vendor) ──────────────
# Fronius and SMA have no account-level discovery (one credential = one system),
# so "attach all" doesn't apply. This endpoint validates the one named system
# and attaches it to a matched-or-created array — the one-click post-signup
# attach for those vendors, mirroring connect-account's match/create behavior.

class ConnectSingleBody(BaseModel):
    vendor: str
    config: dict
    # Optional friendly name for a freshly-created array; defaults to the
    # vendor's validated site name, then the system id.
    name: Optional[str] = None


@router.post("/v1/array-owners/connect-single")
def connect_single(
    body: ConnectSingleBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Validate a single-system vendor credential and attach it to ONE array
    (matched by exact name, else created). For Fronius / SMA / Locus-single —
    vendors with no account-level enumeration. Idempotent by array name.

    Returns {ok, array_id, name, created, site_name, vendor}. Bad credentials
    return 400 and persist nothing (validate runs before any write)."""
    tenant = _tenant_from_bearer(authorization)

    vendor = (body.vendor or "").strip().lower()
    mod = VENDORS.get(vendor)
    if mod is None:
        raise HTTPException(400, f"Unknown inverter vendor: {vendor!r}")
    if not getattr(mod, "AVAILABLE", True):
        raise HTTPException(400, f"{mod.LABEL} can't be connected via API.")

    config = dict(body.config or {})

    # Validate FIRST (raises before any DB write) — the real credential check,
    # and it gives us the site name for matching/creation.
    try:
        result = inverters.validate(vendor, config)
    except InverterAuthError as exc:
        raise HTTPException(400, str(exc))
    except InverterError as exc:
        raise HTTPException(400, str(exc))

    site_name = (body.name or result.get("site_name") or "").strip()
    if not site_name:
        sysid = config.get("system_id") or config.get("pv_system_id") or config.get("site_id") or "site"
        site_name = f"{mod.LABEL} {sysid}"

    created = False
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id, Array.deleted_at.is_(None)
            )
        ).scalars().all()
        names_lower = {a.name.strip().lower(): a for a in arrays}

        target = names_lower.get(site_name.lower())
        if target is None:
            target = Array(
                tenant_id=tenant.id, name=site_name, client_id=None, fuel_type="solar",
            )
            db.add(target)
            db.flush()
            created = True

        # _connect_inverter re-validates (cheap second call) and upserts — keeps
        # the write path identical to the per-array connect endpoint.
        try:
            _connect_inverter(db, target, vendor, config)
        except InverterAuthError as exc:
            raise HTTPException(400, str(exc))
        except InverterError as exc:
            raise HTTPException(400, str(exc))

        array_id = target.id
        array_name = target.name

    return {
        "ok": True,
        "vendor": vendor,
        "array_id": array_id,
        "name": array_name,
        "created": created,
        "site_name": result.get("site_name"),
        "message": f"{mod.LABEL} system connected to “{array_name}”.",
    }


# ── extension-based capture (readings ingest) ─────────────────────────────────
# Some vendors have NO usable API key for us (Fronius's Solar.web Query API is a
# paid business API not offered in the USA; Chint/CPS has no public API at all).
# For those, the EnergyAgent browser extension reads the owner's live numbers
# straight from the portal they're already logged into, and POSTs them here.
# Unlike connect-single (which stores a credential and lets the backend pull),
# this endpoint ingests the READINGS themselves: one Array per captured system,
# and today's energy as a DailyGeneration row so the existing /overview value +
# peer model lights up with zero further wiring.

class CaptureDaily(BaseModel):
    date: str                         # ISO date YYYY-MM-DD
    kwh: Optional[float] = None


class CaptureInverter(BaseModel):
    serial: str                       # vendor device id (Fronius deviceId GUID)
    name: Optional[str] = None
    model: Optional[str] = None
    nameplate_kw: Optional[float] = None
    energy_today_kwh: Optional[float] = None
    peak_power_kw: Optional[float] = None
    # Live instantaneous AC power in WATTS, when the portal exposes it PER
    # inverter (Chint's commDevice.currentPower does; Fronius only gives a
    # site-level reading, so its inverters leave this None and the backend
    # allocates the site total across them by energy share — see ingest).
    current_power_w: Optional[float] = None
    # Optional PER-INVERTER daily-kWh history → persisted to InverterDaily so the
    # per-inverter SPARKLINE renders real history on connect (needs >=2 days),
    # not just "no history yet". Distinct from CaptureSite.daily (array-level →
    # DailyGeneration, drives the array graph). Vendors that expose per-device
    # history (Fronius devwork curves, SMA per-device measurements) populate this;
    # Chint has no per-inverter history so it stays empty.
    daily: list[CaptureDaily] = []


class CaptureSite(BaseModel):
    site_id: str
    name: Optional[str] = None
    peak_power_kw: Optional[float] = None
    inverter_count: Optional[int] = None
    energy_today_kwh: Optional[float] = None
    kwh_per_kwp: Optional[float] = None
    current_power_w: Optional[float] = None
    error_count_today: Optional[int] = 0
    online: Optional[bool] = None
    status: Optional[str] = None
    last_report: Optional[str] = None
    last_report_disp: Optional[str] = None
    # Optional site-level daily-kWh history for instant graph backfill on connect
    # (Chint weekETrend → ~7 days). Site-level only; never split per inverter.
    daily: list[CaptureDaily] = []
    inverters: list[CaptureInverter] = []


class InverterCaptureBody(BaseModel):
    # Vendors that ship readings via the extension rather than a pullable key.
    # Constrained so a bad/cred-bearing vendor can't sneak in through this path.
    provider: str
    sites: list[CaptureSite]


# Vendors allowed to ingest readings this way (no usable backend API key path).
_CAPTURE_VENDORS = {"fronius", "chint", "sma"}

# Absolute daily-kWh sanity ceilings — used ONLY when a capture reports no capacity
# signal at all (no site peak_power_kw AND no inverter nameplate). Without them the
# plausibility guard is disabled (ceiling=None => everything passes) and a cumulative/
# lifetime value reaches the per-kWh meter UNBOUNDED (the no-capacity case behind the
# 677,533 kWh -> ~$4k phantom-bill class). Set FAR above any Array Operator-plausible
# single day so they only ever catch lifetime junk, never real generation — and the
# tighter capacity x 24h ceiling always wins when a real peak/nameplate is present:
#   array    = 100,000 kWh/day  (a >4 MW array at realistic peak-sun-hours — no
#                                browser-extension owner runs that scale)
#   inverter = 10,000 kWh/day   (a >400 kW single inverter; AO uses string inverters)
# A drop is always logged, so a (vanishingly unlikely) false-drop is visible, never
# silent — and under-billing from a drop is far safer than over-billing from a leak.
_ABS_ARRAY_DAILY_KWH_CEILING = 100_000.0
_ABS_INVERTER_DAILY_KWH_CEILING = 10_000.0


@router.post("/v1/array-owners/inverter-capture")
def inverter_capture(
    body: InverterCaptureBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Ingest live inverter readings captured by the extension from a portal the
    owner is logged into (Fronius Solar.web today; extensible to other no-key
    vendors). Dual-auth (session token OR tenant key). For each captured system:
    match an Array by exact name (else create one), record today's energy as a
    DailyGeneration row (source="extension_pull"), and stamp nameplate when the
    portal gave us one. Returns a per-site summary the onboarding page can show.

    Idempotent: re-capturing the same day upserts the DailyGeneration row and
    re-matches the same Array by name, so repeated captures never duplicate.
    """
    tenant = _tenant_from_bearer(authorization)

    provider = (body.provider or "").strip().lower()
    if provider not in _CAPTURE_VENDORS:
        raise HTTPException(
            400,
            f"Vendor {provider!r} is not an extension-capture vendor "
            f"(allowed: {', '.join(sorted(_CAPTURE_VENDORS))}).",
        )
    if not body.sites:
        raise HTTPException(400, "No sites in capture payload.")

    today = now().date()
    results: list[dict] = []

    with SessionLocal() as db:
        # Match by name across ALL arrays, including soft-deleted ones:
        # uq_array_per_tenant spans (tenant_id, name) with no deleted_at, so a
        # soft-deleted array still owns its name. Filtering deleted rows out here
        # made us INSERT a colliding name → UniqueViolation on every retry.
        existing = db.execute(
            select(Array).where(Array.tenant_id == tenant.id)
        ).scalars().all()
        by_name = {a.name.strip().lower(): a for a in existing}
        by_id = {a.id: a for a in existing}

        # STABLE SITE ANCHOR (fixes "inverters in the wrong array after a rename").
        # The Fronius site name is MUTABLE — the owner can rename the array in AO,
        # and the portal name can differ from ours. Matching a captured site to an
        # array by NAME alone meant a rename produced a PHANTOM duplicate array:
        # the site's DailyGeneration + any newly-appearing inverter routed to the
        # phantom while the existing inverters (matched globally by serial) stayed
        # put — splitting a device from its data and its siblings.
        #
        # The durable anchor is the vendor's stable site id (Fronius PvSystemId),
        # which we already persist on every inverter as source_site_id, tied to the
        # array that originally surfaced it (source_array_id). Build site_id→array
        # from those rows so a captured site re-binds to the SAME array regardless
        # of name. First capture (no inverter rows yet) still falls back to name.
        by_site_id: dict[str, Array] = {}
        for iv in db.execute(
            select(Inverter).where(
                Inverter.tenant_id == tenant.id,
                Inverter.vendor == provider,
                Inverter.source_site_id.isnot(None),
                Inverter.deleted_at.is_(None),
            )
        ).scalars().all():
            sid = str(iv.source_site_id).strip()
            if not sid or sid in by_site_id:
                continue
            anchor = (by_id.get(iv.source_array_id) if iv.source_array_id else None) \
                or (by_id.get(iv.array_id) if iv.array_id else None)
            if anchor is not None:
                by_site_id[sid] = anchor

        for site in body.sites:
            site_name = (site.name or "").strip() or f"Fronius {site.site_id}"
            site_key = str(site.site_id).strip()
            # 1) Stable site-id anchor (rename-proof). 2) name. 3) create.
            arr = (by_site_id.get(site_key) if site_key else None) \
                or by_name.get(site_name.lower())
            created = False
            if arr is None:
                arr = Array(
                    tenant_id=tenant.id, name=site_name,
                    client_id=None, fuel_type="solar",
                )
                db.add(arr)
                db.flush()
                by_name[site_name.lower()] = arr
                by_id[arr.id] = arr
                if site_key:
                    by_site_id[site_key] = arr
                created = True
            else:
                if arr.deleted_at is not None:
                    # Re-capturing a previously-deleted array reactivates it (the
                    # constraint kept its name reserved). Mirrors the Inverter
                    # undelete below — live data is flowing again.
                    arr.deleted_at = None
                # Remember this site_id→array binding for the rest of this batch
                # (and so a same-name-different-site collision can't steal it).
                if site_key and site_key not in by_site_id:
                    by_site_id[site_key] = arr

            # NOTE: nameplate hinting via Inverter rows is a follow-up — the
            # Array is the owner-facing grouping, and the value/peer model
            # consumes the kWh reading recorded below.

            # Array-level daily kWh → DailyGeneration, driving BOTH today's
            # reading and the instant graph-history backfill. Build ONE deduped
            # {day: kwh} map (today's energy_today_kwh + any site.daily history,
            # max-wins), load existing rows ONCE, then update-or-insert. Robust
            # against a re-add of an existing array, duplicate dates in
            # site.daily, AND today appearing in BOTH the today-row and the
            # history (SMA's daily series includes today) — the old two-block
            # SELECT-then-INSERT raised UniqueViolation on uq_daily_array_day at
            # db.commit() in exactly that case (Sentry PYTHON-FASTAPI-3).
            recorded_kwh = None
            if site.energy_today_kwh is not None and site.energy_today_kwh >= 0:
                recorded_kwh = float(site.energy_today_kwh)

            # PHYSICAL-PLAUSIBILITY GUARD (billing-critical). A capture parse
            # glitch once landed a cumulative/lifetime value (677,533 kWh) in a
            # single DAILY slot for a 144 kW array, which is ~34× the physical
            # max — and since Array Operator bills per-kWh, that one row alone
            # would have invoiced ~$4k. A daily kWh value can NEVER exceed the
            # array's rated power running flat-out for 24h. Compute that ceiling
            # from the site's peak power (or the sum of its inverters' nameplates)
            # and DROP any day above it rather than poison the meter. The ceiling
            # is deliberately generous (24h @ full nameplate ≈ 4-5× a real sunny
            # day) so it only ever catches unit-error/cumulative garbage, never a
            # legitimately strong production day.
            peak_kw = site.peak_power_kw
            if not peak_kw:
                _np = sum(
                    (iv.nameplate_kw or 0) for iv in (site.inverters or [])
                ) or 0
                peak_kw = _np or None
            # Capacity-based ceiling (preferred). When the portal reports NEITHER a
            # peak power NOR any inverter nameplate, fall back to an absolute sanity
            # ceiling so a cumulative/lifetime value can't slip through UNBOUNDED into
            # the per-kWh meter — the no-capacity gap behind the 677,533 kWh -> ~$4k
            # phantom-bill class. The fallback sits far above any AO-plausible single
            # day, so it only ever catches lifetime junk, never real generation.
            day_ceiling = (peak_kw * 24.0) if peak_kw else _ABS_ARRAY_DAILY_KWH_CEILING

            def _plausible(kwh: float) -> bool:
                return kwh <= day_ceiling

            want_arr: dict = {}
            if recorded_kwh is not None and _plausible(recorded_kwh):
                want_arr[today] = recorded_kwh
            for pt in (site.daily or []):
                if pt.kwh is None or pt.kwh < 0:
                    continue
                try:
                    d = date.fromisoformat(str(pt.date)[:10])
                except (TypeError, ValueError):
                    continue
                v = float(pt.kwh)
                if not _plausible(v):
                    log.warning(
                        "inverter-capture: dropping implausible daily kWh %.0f for "
                        "array %s day %s (ceiling %.0f = %.1f kW × 24h) — likely a "
                        "cumulative/unit-error capture glitch, not real generation",
                        v, arr.id, d, day_ceiling or 0, peak_kw or 0,
                    )
                    continue
                if d not in want_arr or v > want_arr[d]:   # max-wins on dup dates
                    want_arr[d] = v
            backfilled_days = 0
            if want_arr:
                existing_d = {
                    r.day: r for r in db.execute(
                        select(DailyGeneration).where(
                            DailyGeneration.array_id == arr.id,
                            DailyGeneration.day.in_(list(want_arr.keys())),
                        )
                    ).scalars().all()
                }
                for d, v in want_arr.items():
                    drow = existing_d.get(d)
                    if drow is None:
                        db.add(DailyGeneration(
                            tenant_id=tenant.id, array_id=arr.id, day=d,
                            kwh=v, source="extension_pull",
                        ))
                        backfilled_days += 1
                    elif v > (drow.kwh or 0):   # climbs through the day / never regresses
                        drow.kwh = v
                        drow.source = "extension_pull"
                        drow.uploaded_at = now()
                        backfilled_days += 1

            # When the capture drilled into a system's analysis chart, we get one
            # entry per real inverter. Persist each as an Inverter row (idempotent
            # by tenant+vendor+serial, owner arrangement preserved) and store its
            # day's kWh in InverterDaily so build_fleet_tree can peer-analyze the
            # real comb — no API connection needed.
            # Basis for allocating the site's live instantaneous power across its
            # inverters (the portal exposes only a site-level "now" reading, not a
            # per-inverter one). We split that REAL aggregate by each inverter's
            # share of TODAY's energy so the per-inverter values sum to the measured
            # site total — a principled split, never an invented number. Falls back
            # to nameplate share, then an even split, only if no energy is reported;
            # stamps nothing when the site didn't report live power (card shows "—").
            site_power_w = (
                float(site.current_power_w)
                if site.current_power_w is not None and site.current_power_w >= 0
                else None
            )
            _site_invs = [c for c in (site.inverters or []) if str(c.serial or "").strip()]
            _energy_sum = sum(
                float(c.energy_today_kwh) for c in _site_invs
                if c.energy_today_kwh and c.energy_today_kwh > 0
            )
            _np_sum = sum(float(c.nameplate_kw) for c in _site_invs if c.nameplate_kw)

            inv_persisted = 0
            for ci in (site.inverters or []):
                serial = str(ci.serial or "").strip()
                if not serial:
                    continue
                iv = db.execute(
                    select(Inverter).where(
                        Inverter.tenant_id == tenant.id,
                        Inverter.vendor == provider,
                        Inverter.serial == serial,
                    )
                ).scalar_one_or_none()
                if iv is None:
                    maxpos = db.execute(
                        select(Inverter.position).where(
                            Inverter.tenant_id == tenant.id,
                            Inverter.array_id == arr.id,
                            Inverter.deleted_at.is_(None),
                        ).order_by(Inverter.position.desc())
                    ).scalars().first()
                    iv = Inverter(
                        tenant_id=tenant.id, array_id=arr.id,
                        position=(maxpos or 0) + 1,
                        vendor=provider, serial=serial,
                        source_site_id=site.site_id, source_array_id=arr.id,
                    )
                    db.add(iv)
                    db.flush()
                else:
                    # Refresh source pointer; NEVER clobber owner array_id/position.
                    iv.source_site_id = site.site_id
                    if iv.source_array_id is None:
                        iv.source_array_id = arr.id
                    if iv.deleted_at is not None:
                        iv.deleted_at = None
                iv.name = ci.name or iv.name or serial
                iv.model = ci.model or iv.model
                if ci.nameplate_kw is not None:
                    iv.nameplate_kw = ci.nameplate_kw
                iv.last_seen_at = now()

                # Live current power. PREFER the inverter's OWN reading when the
                # portal exposed it per device (Chint commDevice.currentPower; and
                # Fronius's per-device devwork chart latest point, when fresh) —
                # that's the real measured value, not a derived split. Only when
                # the inverter carries no per-unit reading do we fall back to
                # allocating the site's instantaneous total by energy share
                # (Fronius's site-level GetActualValues, used when the per-device
                # devwork point is stale/absent — e.g. at night).
                if ci.current_power_w is not None and ci.current_power_w >= 0:
                    iv.last_power_w = round(float(ci.current_power_w), 1)
                    iv.last_power_at = now()
                elif site_power_w is not None and _site_invs:
                    if _energy_sum > 0:
                        e = float(ci.energy_today_kwh) if (ci.energy_today_kwh and ci.energy_today_kwh > 0) else 0.0
                        iv.last_power_w = round(site_power_w * (e / _energy_sum), 1)
                    elif _np_sum > 0 and ci.nameplate_kw:
                        iv.last_power_w = round(site_power_w * (float(ci.nameplate_kw) / _np_sum), 1)
                    else:
                        iv.last_power_w = round(site_power_w / len(_site_invs), 1)
                    iv.last_power_at = now()

                # Per-inverter daily kWh → InverterDaily, driving BOTH today's
                # reading and the per-inverter SPARKLINE history (needs >=2 days,
                # else "no history yet"). Build ONE deduped {day: kwh} map (today's
                # energy_today_kwh + any ci.daily history, max-wins), then load
                # existing rows ONCE and update-or-insert. This is robust against a
                # re-capture of an already-linked account, duplicate dates in
                # ci.daily, and today appearing in both — the SELECT-then-INSERT-
                # per-row version raised UniqueViolation on uq_inverter_daily_inv_day
                # (Sentry PYTHON-FASTAPI-3) exactly in those cases.
                # PHYSICAL-PLAUSIBILITY GUARD (same class as the array-level one):
                # a per-inverter daily kWh can never exceed its nameplate × 24h.
                # The same Fronius cumulative-value glitch that poisoned the
                # array meter also wrote ~36,000 kWh into each 7.6 kW inverter's
                # daily slot (cap ≈ 182). Drop anything above the ceiling so the
                # sparkline + peer-analysis engine never see impossible spikes.
                # Prefer the captured nameplate, fall back to the persisted row's.
                inv_np = ci.nameplate_kw or iv.nameplate_kw
                # Capacity-or-absolute fallback (same logic as the array level): an
                # inverter daily kWh with no known nameplate still can't exceed an
                # absolute single-inverter sanity ceiling, so a cumulative value never
                # poisons the sparkline/peer-analysis even when nameplate is missing.
                inv_ceiling = (float(inv_np) * 24.0) if inv_np else _ABS_INVERTER_DAILY_KWH_CEILING

                def _inv_plausible(kwh: float) -> bool:
                    return kwh <= inv_ceiling

                want: dict = {}
                if (ci.energy_today_kwh is not None and ci.energy_today_kwh >= 0
                        and _inv_plausible(float(ci.energy_today_kwh))):
                    want[today] = float(ci.energy_today_kwh)
                for pt in (ci.daily or []):
                    if pt.kwh is None or pt.kwh < 0:
                        continue
                    try:
                        dd = date.fromisoformat(str(pt.date)[:10])
                    except (TypeError, ValueError):
                        continue
                    v = float(pt.kwh)
                    if not _inv_plausible(v):
                        log.warning(
                            "inverter-capture: dropping implausible per-inverter "
                            "daily kWh %.0f for inverter %s day %s (ceiling %.0f = "
                            "%.1f kW × 24h) — capture glitch, not real generation",
                            v, iv.id, dd, inv_ceiling or 0, float(inv_np or 0),
                        )
                        continue
                    if dd not in want or v > want[dd]:   # max-wins on dup dates
                        want[dd] = v
                if want:
                    existing = {
                        r.day: r for r in db.execute(
                            select(InverterDaily).where(
                                InverterDaily.inverter_id == iv.id,
                                InverterDaily.day.in_(list(want.keys())),
                            )
                        ).scalars().all()
                    }
                    for dd, v in want.items():
                        row = existing.get(dd)
                        if row is None:
                            db.add(InverterDaily(
                                tenant_id=tenant.id, inverter_id=iv.id, day=dd,
                                kwh=v, source="extension_pull",
                            ))
                        elif v > (row.kwh or 0):         # climbs through the day / never regresses
                            row.kwh = v
                            row.uploaded_at = now()
                inv_persisted += 1

            results.append({
                "site_id": site.site_id,
                "array_id": arr.id,
                "name": arr.name,
                "created": created,
                "recorded_today_kwh": recorded_kwh,
                "current_power_w": site.current_power_w,
                "status": site.status,
                "error_count_today": site.error_count_today or 0,
                "inverters_persisted": inv_persisted,
            })

        db.commit()

    fault_count = sum(1 for r in results if (r["error_count_today"] or 0) > 0)
    return {
        "ok": True,
        "provider": provider,
        "sites_captured": len(results),
        "arrays_created": sum(1 for r in results if r["created"]),
        "inverters_persisted": sum(r["inverters_persisted"] for r in results),
        "faults_detected": fault_count,
        "sites": results,
    }


# ── Utility-meter capture (GMP solar generation via the extension) ────────────
# DISTINCT from inverter-capture: utilities are ADAPTERS, not inverter VENDORS.
# The extension reads the owner's GMP usage *summary* (kWh the array produced /
# sent to grid) and POSTs it here. We persist it as DailyGeneration rows with
# source="utility_meter" so the Array Operator value/peer model lights up even
# for owners whose only production signal is their utility meter.

class UtilityMeterDaily(BaseModel):
    date: str                          # ISO date (YYYY-MM-DD or full timestamp)
    generated_kwh: Optional[float] = None


class UtilityMeterAccount(BaseModel):
    account_number: str
    nickname: Optional[str] = None
    summary: dict = {}                 # raw GMP /usage/{acct}/summary body
    daily: list[UtilityMeterDaily] = []  # optional per-day generation series


class UtilityMeterCaptureBody(BaseModel):
    provider: str
    accounts: list[UtilityMeterAccount]


# Utilities allowed to ingest GENERATION via the meter-capture path. Kept
# separate from _CAPTURE_VENDORS (inverter vendors) — utilities are adapters.
#   gmp = Green Mountain Power (bespoke REST API, carries a GMP-style summary).
#   vec = Vermont Electric Coop, wec = Washington Electric Coop (both NISC
#   SmartHub — the extension supplies daily[] generation directly, no GMP summary).
_UTILITY_CAPTURE_VENDORS = {"gmp", "vec", "wec"}

# Human label per utility for the default array name when no nickname is given.
_UTILITY_LABEL = {"gmp": "GMP", "vec": "VEC", "wec": "WEC"}


def _meter_day(value: str | None):
    """Parse an ISO date/timestamp string into a date, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


@router.post("/v1/array-owners/utility-meter-capture")
def utility_meter_capture(
    body: UtilityMeterCaptureBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Ingest SOLAR GENERATION read from a utility meter (GMP today) by the
    extension. Dual-auth (session token OR tenant key), same as inverter_capture.

    For each account: match an Array by name (nickname, else "GMP <account>"),
    else create one. Then record generation as DailyGeneration rows with
    source="utility_meter":
      • If daily[] is supplied, upsert one row per day (idempotent per
        (array, day), max-kWh — same rule as inverter_capture).
      • Otherwise persist the billing-period total as ONE representative row
        keyed on the period_end date.

    An account with isNetMetered=false / zero generation is VALID — it just has
    no solar. We still record it (created/matched) but flag has_generation=false
    so the UI can honestly tell the owner "this account has no solar production".

    Idempotent: re-capturing re-matches the same Array by name and upserts the
    same DailyGeneration rows, so repeated captures never duplicate.
    """
    tenant = _tenant_from_bearer(authorization)

    provider = (body.provider or "").strip().lower()
    if provider not in _UTILITY_CAPTURE_VENDORS:
        raise HTTPException(
            400,
            f"Provider {provider!r} is not a utility-meter capture provider "
            f"(allowed: {', '.join(sorted(_UTILITY_CAPTURE_VENDORS))}).",
        )
    if not body.accounts:
        raise HTTPException(400, "No accounts in capture payload.")

    with SessionLocal() as db:
        results = _persist_meter_accounts(db, tenant, provider, body.accounts)
        db.commit()

    return {
        "ok": True,
        "provider": provider,
        "accounts_captured": len(results),
        "arrays_created": sum(1 for r in results if r["created"]),
        "accounts_with_generation": sum(1 for r in results if r["has_generation"]),
        "accounts": results,
    }


def _persist_meter_accounts(
    db,
    tenant: Tenant,
    provider: str,
    accounts: list[UtilityMeterAccount],
) -> list[dict]:
    """Shared per-account persistence for utility-meter GENERATION capture.

    Used by the extension-push path (utility_meter_capture). For each account:
    match an Array by name (nickname, else "<LABEL> <account>"), else create — but
    ONLY when the account actually has solar generation. Records generation as
    DailyGeneration rows with source="utility_meter" (idempotent per
    (array, day), max-kWh). Does NOT commit — the caller owns the transaction.

    Returns one result dict per account with the same shape across both paths.
    """
    results: list[dict] = []

    # Match by name across ALL arrays, INCLUDING soft-deleted ones. The unique
    # constraint uq_array_per_tenant spans (tenant_id, name) with NO deleted_at
    # predicate, so a soft-deleted array still RESERVES its name. Filtering
    # deleted rows out here meant a re-capture of a previously-deleted array tried
    # to INSERT a colliding name → psycopg2 UniqueViolation → 500 ("couldn't grab
    # your GMP account"). Include them and revive on reuse (mirrors the Fronius
    # inverter-capture path).
    existing = db.execute(
        select(Array).where(Array.tenant_id == tenant.id)
    ).scalars().all()
    by_name = {a.name.strip().lower(): a for a in existing}

    # Match by the array's LINKED utility account number too — far more stable
    # than the display name. NEPOOL arrays are created by the bill-capture path
    # with a UtilityAccount (e.g. VEC acct 6578300) and a service-address name;
    # the meter-capture nickname (addr1, city, state — no zip) won't string-match
    # that name, so without this an account-number match the generation pull
    # would spawn a DUPLICATE array. Map account_number → its array.
    by_acct_number: dict[str, Array] = {}
    array_ids = [a.id for a in existing]
    if array_ids:
        for ua in db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant.id,
                UtilityAccount.array_id.in_(array_ids),
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all():
            arr_for_acct = next((a for a in existing if a.id == ua.array_id), None)
            if ua.account_number and arr_for_acct is not None:
                by_acct_number[str(ua.account_number).strip()] = arr_for_acct

    for acct in accounts:
            # GMP accounts carry a GMP-style /usage summary; SmartHub utilities
            # (vec/wec) supply daily[] generation directly with no summary. Only
            # parse the GMP summary for gmp so a missing/foreign summary is a no-op.
            parsed = (
                gmp_adapter.parse_usage_summary(acct.summary or {})
                if provider == "gmp" else {}
            )
            acct_num = (acct.account_number or parsed.get("account_number") or "").strip()
            label = _UTILITY_LABEL.get(provider, provider.upper())
            name = (acct.nickname or "").strip() or f"{label} {acct_num or 'account'}"

            # Determine generation BEFORE creating anything. Bruce has 48 GMP
            # accounts but only a handful are solar arrays — the rest are homes/
            # pumps/meters with zero generation. We must NOT spawn an empty array
            # for every non-solar account (that would bury the real arrays). So an
            # account only gets an Array when it actually has production: either a
            # per-day generation series, OR a positive billing-period total.
            daily_rows = [
                d for d in (acct.daily or [])
                if d.generated_kwh is not None and d.generated_kwh >= 0
            ]
            period_gen = parsed.get("kwh_generated")
            period_end = _meter_day(parsed.get("period_end"))
            has_any_generation = bool(
                any(d.generated_kwh and d.generated_kwh > 0 for d in daily_rows)
                or (period_gen is not None and period_gen > 0 and period_end is not None)
            )

            if not has_any_generation:
                # Record the account honestly as no-solar — but create NO array and
                # write NO rows. The UI uses has_generation=false to tell the owner
                # "this GMP account has no solar production."
                results.append({
                    "account_number": acct_num,
                    "array_id": None,
                    "name": name,
                    "created": False,
                    "kwh_recorded": 0.0,
                    "days_written": 0,
                    "is_net_metered": bool(parsed.get("is_net_metered")),
                    "has_generation": False,
                })
                continue

            arr = by_acct_number.get(acct_num) or by_name.get(name.lower())
            created = False
            if arr is None:
                arr = Array(
                    tenant_id=tenant.id, name=name,
                    client_id=None, fuel_type="solar",
                )
                db.add(arr)
                db.flush()
                by_name[name.lower()] = arr
                if acct_num:
                    by_acct_number[acct_num] = arr
                created = True
            elif arr.deleted_at is not None:
                # Reusing a soft-deleted array (its name kept the constraint slot)
                # — reactivate it rather than colliding. Generation is flowing again.
                arr.deleted_at = None

            # ── Link a UtilityAccount + Bill so offtakers can bind to this bill ──
            # The offtaker invoice generator lists GMP UtilityAccounts and bills
            # them EXCLUSIVELY from Bill.kwh_generated. This capture path used to
            # write only DailyGeneration, so a "connected" GMP account never
            # appeared in the add-offtaker picker and had no bill to invoice from.
            # Upsert the UtilityAccount (idempotent on tenant+provider+account_number)
            # and, when the GMP summary carries a billing period + generation,
            # upsert a Bill so the offtaker dropdown populates and invoices have
            # real utility-bill kWh. Only for accounts WITH generation (we already
            # 'continue'd past the no-solar ones above).
            if acct_num:
                ua = db.execute(
                    select(UtilityAccount).where(
                        UtilityAccount.tenant_id == tenant.id,
                        UtilityAccount.provider == provider,
                        UtilityAccount.account_number == acct_num,
                    )
                ).scalar_one_or_none()
                if ua is None:
                    ua = UtilityAccount(
                        tenant_id=tenant.id, provider=provider,
                        account_number=acct_num, array_id=arr.id,
                        nickname=(acct.nickname or "").strip() or None,
                    )
                    db.add(ua)
                    db.flush()
                else:
                    if ua.deleted_at is not None:
                        ua.deleted_at = None
                    # Keep it linked to the array (don't steal an existing manual link).
                    if ua.array_id is None:
                        ua.array_id = arr.id
                    if not ua.nickname and (acct.nickname or "").strip():
                        ua.nickname = (acct.nickname or "").strip()
                    ua.last_seen = now()

                # Bill from the GMP billing-period summary (the "paper bill" total).
                if (period_gen is not None and period_gen > 0
                        and period_end is not None):
                    period_start = _meter_day(parsed.get("period_start"))
                    existing_bill = db.execute(
                        select(Bill).where(
                            Bill.account_id == ua.id,
                            Bill.period_end.isnot(None),
                        ).order_by(Bill.period_end.desc())
                    ).scalars().all()
                    # Match the same billing period by period_end (one Bill/period).
                    bill = next(
                        (b for b in existing_bill
                         if b.period_end and b.period_end.date() == period_end),
                        None,
                    )
                    bdays = ((period_end - period_start).days
                             if period_start else None)
                    if bill is None:
                        db.add(Bill(
                            tenant_id=tenant.id, account_id=ua.id,
                            period_start=(datetime.combine(period_start, dtime.min)
                                          if period_start else None),
                            period_end=datetime.combine(period_end, dtime.min),
                            bill_date=datetime.combine(period_end, dtime.min),
                            billing_days=bdays,
                            kwh_generated=int(round(float(period_gen))),
                            kwh_sent_to_grid=parsed.get("kwh_sent_to_grid"),
                            is_net_metered=bool(parsed.get("is_net_metered")),
                            parse_status="parsed",
                        ))
                    else:
                        # Never lower a captured generation figure (climbs only).
                        newg = int(round(float(period_gen)))
                        if bill.kwh_generated is None or newg > bill.kwh_generated:
                            bill.kwh_generated = newg
                        if bill.period_start is None and period_start is not None:
                            bill.period_start = datetime.combine(period_start, dtime.min)

            kwh_recorded = 0.0
            days_written = 0

            # ── Per-day series (preferred when present) ──────────────────────
            if daily_rows:
                for d in daily_rows:
                    day = _meter_day(d.date)
                    if day is None or d.generated_kwh is None or d.generated_kwh < 0:
                        continue
                    dk = float(d.generated_kwh)
                    row = db.execute(
                        select(DailyGeneration).where(
                            DailyGeneration.array_id == arr.id,
                            DailyGeneration.day == day,
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        db.add(DailyGeneration(
                            tenant_id=tenant.id, array_id=arr.id, day=day,
                            kwh=dk, source="utility_meter",
                        ))
                    else:
                        # Same idempotency as inverter_capture: never lower a row.
                        row.kwh = max(row.kwh, dk)
                        row.source = "utility_meter"
                        row.uploaded_at = now()
                    kwh_recorded += dk
                    days_written += 1
            else:
                # ── Billing-period total → one representative row ────────────
                gen = period_gen
                if gen is not None and gen > 0 and period_end is not None:
                    gk = float(gen)
                    row = db.execute(
                        select(DailyGeneration).where(
                            DailyGeneration.array_id == arr.id,
                            DailyGeneration.day == period_end,
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        db.add(DailyGeneration(
                            tenant_id=tenant.id, array_id=arr.id, day=period_end,
                            kwh=gk, source="utility_meter",
                        ))
                    else:
                        row.kwh = max(row.kwh, gk)
                        row.source = "utility_meter"
                        row.uploaded_at = now()
                    kwh_recorded = gk
                    days_written = 1

            has_generation = kwh_recorded > 0
            results.append({
                "account_number": acct_num,
                "array_id": arr.id,
                "name": arr.name,
                "created": created,
                "kwh_recorded": kwh_recorded,
                "days_written": days_written,
                "is_net_metered": bool(parsed.get("is_net_metered")),
                "has_generation": has_generation,
            })

    return results
