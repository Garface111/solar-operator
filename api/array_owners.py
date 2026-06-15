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
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Optional

import httpx  # noqa: F401 — kept so tests can monkeypatch array_owners.httpx.get
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from . import inverters
from .db import SessionLocal
from .inverters import VENDORS, InverterAuthError, InverterError, InverterScopeError
from .inverters import peer_analysis
from .models import Array, DailyGeneration, InverterConnection, Tenant, UtilityAccount, now
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
    """
    from .account import tenant_from_session

    try:
        return tenant_from_session(authorization)
    except HTTPException:
        pass
    from .app import tenant_from_bearer

    return tenant_from_bearer(authorization)


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
    return result


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
                if name.lower() in names_lower:
                    # Two sites share a name (or it collides with an array we
                    # won't reuse) — disambiguate so uq_array_per_tenant holds.
                    name = f"{site_name} ({sid})"
                new_arr = Array(
                    tenant_id=tenant.id, name=name, client_id=None, fuel_type="solar",
                )
                db.add(new_arr)
                db.flush()
                _attach_solaredge(db, new_arr, api_key, sid)
                used.add(new_arr.id)
                names_lower.add(name.lower())
                arr_by_id[new_arr.id] = new_arr
                entry["array_id"] = new_arr.id
                entry["name"] = new_arr.name
                created.append(entry)

            connected.append(entry)

        db.commit()

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
                if name.lower() in names_lower:
                    # Disambiguate so uq_array_per_tenant holds.
                    name = f"{site_name} ({sid})"
                new_arr = Array(
                    tenant_id=tenant.id, name=name, client_id=None, fuel_type="solar",
                )
                db.add(new_arr)
                db.flush()
                _attach_locus(db, new_arr, creds, sid)
                used.add(new_arr.id)
                names_lower.add(name.lower())
                arr_by_id[new_arr.id] = new_arr
                entry["array_id"] = new_arr.id
                entry["name"] = new_arr.name
                created.append(entry)

            connected.append(entry)

        db.commit()

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

class CaptureInverter(BaseModel):
    serial: str                       # vendor device id (Fronius deviceId GUID)
    name: Optional[str] = None
    model: Optional[str] = None
    nameplate_kw: Optional[float] = None
    energy_today_kwh: Optional[float] = None
    peak_power_kw: Optional[float] = None


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
    inverters: list[CaptureInverter] = []


class InverterCaptureBody(BaseModel):
    # Vendors that ship readings via the extension rather than a pullable key.
    # Constrained so a bad/cred-bearing vendor can't sneak in through this path.
    provider: str
    sites: list[CaptureSite]


# Vendors allowed to ingest readings this way (no usable backend API key path).
_CAPTURE_VENDORS = {"fronius", "chint"}


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
        existing = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id, Array.deleted_at.is_(None)
            )
        ).scalars().all()
        by_name = {a.name.strip().lower(): a for a in existing}

        for site in body.sites:
            site_name = (site.name or "").strip() or f"Fronius {site.site_id}"
            arr = by_name.get(site_name.lower())
            created = False
            if arr is None:
                arr = Array(
                    tenant_id=tenant.id, name=site_name,
                    client_id=None, fuel_type="solar",
                )
                db.add(arr)
                db.flush()
                by_name[site_name.lower()] = arr
                created = True

            # NOTE: nameplate hinting via Inverter rows is a follow-up — the
            # Array is the owner-facing grouping, and the value/peer model
            # consumes the kWh reading recorded below.

            recorded_kwh = None
            if site.energy_today_kwh is not None and site.energy_today_kwh >= 0:
                recorded_kwh = float(site.energy_today_kwh)
                row = db.execute(
                    select(DailyGeneration).where(
                        DailyGeneration.array_id == arr.id,
                        DailyGeneration.day == today,
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = DailyGeneration(
                        tenant_id=tenant.id, array_id=arr.id, day=today,
                        kwh=recorded_kwh, source="extension_pull",
                    )
                    db.add(row)
                else:
                    # Solar.web's EnergyTodayInkWh climbs through the day — take
                    # the max so an early-morning re-capture can't lower it.
                    row.kwh = max(row.kwh, recorded_kwh)
                    row.source = "extension_pull"
                    row.uploaded_at = now()

            # ── Per-inverter persistence (the sandbox comb) ──────────────────
            # When the capture drilled into a system's analysis chart, we get one
            # entry per real inverter. Persist each as an Inverter row (idempotent
            # by tenant+vendor+serial, owner arrangement preserved) and store its
            # day's kWh in InverterDaily so build_fleet_tree can peer-analyze the
            # real comb — no API connection needed.
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

                if ci.energy_today_kwh is not None and ci.energy_today_kwh >= 0:
                    ikwh = float(ci.energy_today_kwh)
                    drow = db.execute(
                        select(InverterDaily).where(
                            InverterDaily.inverter_id == iv.id,
                            InverterDaily.day == today,
                        )
                    ).scalar_one_or_none()
                    if drow is None:
                        db.add(InverterDaily(
                            tenant_id=tenant.id, inverter_id=iv.id, day=today,
                            kwh=ikwh, source="extension_pull",
                        ))
                    else:
                        drow.kwh = max(drow.kwh, ikwh)  # climbs through the day
                        drow.uploaded_at = now()
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
