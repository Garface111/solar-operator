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
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, time as dtime
from types import SimpleNamespace
from typing import Optional

import httpx  # noqa: F401 — kept so tests can monkeypatch array_owners.httpx.get
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from . import generation_sources
from . import inverters
from . import ratelimit
from .adapters import gmp as gmp_adapter
from .adapters import is_smarthub_provider
from .db import SessionLocal
from .inverters import VENDORS, InverterAuthError, InverterError, InverterScopeError
from .inverters import peer_analysis
from .models import Array, Bill, DailyGeneration, InverterConnection, Tenant, UtilityAccount, UtilitySession, now, local_today
from .models import Inverter, InverterDaily, SmaConsent, FleetForecastSnapshot
from .rates import REC_PRICE_USD_PER_MWH, get_energy_rate

log = logging.getLogger(__name__)


# Devices a monitoring portal lists ALONGSIDE inverters but which are NOT inverters
# — the SMA Energy Data Manager (model "EDMM-10" / names like "…Datamanager"),
# Sunny Home Manager, energy meters, gateways, cluster controllers. They never
# produce, so capturing one would add a phantom permanently-0 kW "inverter".
_NON_INVERTER_RE = re.compile(
    r"\bedmm\b|data\s*manager|datamanager|home\s*manager|energy\s*meter|\bmeter\b"
    r"|webconnect|gateway|cluster\s*controller",
    re.I,
)


def _is_non_inverter_device(name, model) -> bool:
    """True when a captured 'inverter' is really a logger/meter/gateway (see above).
    Defense-in-depth: the extension filters these at capture, but this also catches
    payloads from older extension versions and any future portal that lists them."""
    return bool(_NON_INVERTER_RE.search(f"{name or ''} {model or ''}"))


def _safe_create_array(db, tenant_id, name, **kw):
    """Create an Array, surviving a concurrent capture that inserted the same
    (tenant_id, name) first. The extension fires several capture POSTs per login,
    so two requests can both miss an existing array and race the INSERT — the
    partial unique index uq_array_per_tenant_live then 500s the loser. Roll back
    ONLY the failed insert (a SAVEPOINT, not the whole capture) and reuse the
    LIVE row that won. Returns (array, created)."""
    arr = Array(tenant_id=tenant_id, name=name, **kw)
    try:
        with db.begin_nested():          # SAVEPOINT around just this insert
            db.add(arr)
            db.flush()
        return arr, True
    except IntegrityError:
        try:
            db.expunge(arr)              # drop the rolled-back instance, if still attached
        except Exception:
            pass
        # The index only spans LIVE rows, so the winner of the race is a live
        # array — grab it. (Fall back to any same-name row and revive it.)
        won = db.execute(
            select(Array).where(
                Array.tenant_id == tenant_id, Array.name == name,
                Array.deleted_at.is_(None),
            )
        ).scalars().first()
        if won is None:
            won = db.execute(
                select(Array).where(Array.tenant_id == tenant_id, Array.name == name)
            ).scalars().first()
        if won is None:
            raise
        if won.deleted_at is not None:
            won.deleted_at = None
        return won, False

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


# ── extension popup status ────────────────────────────────────────────────────

@router.get("/v1/array-owners/extension-status")
def extension_status(authorization: str | None = Header(default=None)) -> dict:
    """Lightweight product + live-stat summary for the EnergyAgent extension popup.

    Auth matches every other array-owner endpoint (_tenant_from_bearer): a SPA
    SESSION bearer resolves first, falling back to the raw tenant_key the
    extension popup sends. It previously accepted ONLY the tenant key, so a
    valid session bearer got a misleading 403 "Invalid tenant key" — the one
    array-owner endpoint that rejected the dashboard's own credential. The
    key path still uses the capture-tolerant resolver, so a
    paused-but-recoverable tenant keeps showing its numbers in the popup.

    LINKED dual-product install: when that tenant is cross-product LINKED
    (Tenant.linked_tenant_id → api.tenant_link), this reports BOTH products with
    per-product stats from BOTH tenants — so the popup of a single install honestly
    shows the NEPOOL clients/reports AND the AO arrays/inverters it feeds. When the
    tenant is unlinked, it carries exactly one product entry (unchanged behavior).

    Everything here is read from existing rows — nothing is fabricated. AO stats
    reuse the cached fleet-tree summary (arrays / inverters / flagged) plus a
    cheap DailyGeneration sum for today's kWh; NEPOOL stats are simple client +
    report-delivery counts. `last_capture` is the newest touch across BOTH tenants.

    Shape:
      {
        "product": "array_operator" | "nepool",   # the bearer tenant's own product
        "products": ["array_operator", "nepool"],  # 1 entry, or 2 when linked
        "linked": bool,                            # cross-product sibling linked
        "company_name": str,
        "connected": bool,                         # subscription not hard-dead
        "array_operator": {                        # present iff an AO tenant is in scope
            "arrays": int, "inverters": int, "flagged": int,
            "kwh_today": float, "offtakers": int
        },
        "nepool": {                                # present iff a NEPOOL tenant is in scope
            "clients": int, "arrays": int,
            "last_report_at": iso|None
        },
        "last_capture": {"provider": str, "at": iso} | None
      }
    """
    tenant = _tenant_from_bearer(authorization)
    product = (tenant.product or "nepool").strip() or "nepool"

    out: dict = {
        "product": product,
        "products": [product],
        "linked": False,
        "company_name": tenant.company_name or tenant.name or "",
        "connected": tenant.active
        or (tenant.subscription_status in _CAPTURE_RECOVERABLE_STATUSES),
        # Read-only demo/test tenant: the extension must NEVER stay paired to one
        # (its /v1/sync captures are refused), so the popup surfaces it and the
        # SPAs refuse to auto-pair it. Real captures can only feed a real account.
        "is_demo": bool(getattr(tenant, "is_demo", False)),
        "last_capture": None,
    }

    with SessionLocal() as db:
        # Resolve the validated cross-product sibling (if linked) so we report
        # BOTH products. get_linked_sibling re-validates the link, so a dangling
        # link is safely ignored (single-product behavior).
        from .tenant_link import get_linked_sibling
        sibling = get_linked_sibling(db, tenant)

        scope = [tenant] + ([sibling] if sibling is not None else [])
        if sibling is not None:
            out["linked"] = True
            sib_product = (sibling.product or "nepool").strip() or "nepool"
            if sib_product not in out["products"]:
                out["products"].append(sib_product)
            # Connected if EITHER tenant is live — one install, both products.
            out["connected"] = out["connected"] or bool(
                sibling.active or sibling.subscription_status in _CAPTURE_RECOVERABLE_STATUSES)

        # ── Last capture across ALL in-scope tenants (newest wins) ──
        cands: list[tuple[datetime, str]] = []
        for t in scope:
            cands.extend(_last_capture_candidates(db, t))
        if cands:
            at, prov = max(cands, key=lambda c: c[0])
            out["last_capture"] = {"provider": prov, "at": at.isoformat()}

        # ── Per-product stat blocks, computed for whichever tenant owns each
        # product. With a link this fills BOTH blocks from the two tenants. ──
        for t in scope:
            t_product = (t.product or "nepool").strip() or "nepool"
            if t_product == "array_operator":
                out["array_operator"] = _ao_status_stats(db, t)
            else:
                out["nepool"] = _nepool_status_stats(db, t)

    return out


def _last_capture_candidates(db, tenant: Tenant) -> list[tuple[datetime, str]]:
    """(timestamp, provider) of this tenant's freshest utility-account touch and
    freshest inverter reading — the caller takes the global max across tenants."""
    cands: list[tuple[datetime, str]] = []
    last_acct = db.execute(
        select(UtilityAccount)
        .where(UtilityAccount.tenant_id == tenant.id,
               UtilityAccount.deleted_at.is_(None))
        .order_by(UtilityAccount.last_seen.desc())
    ).scalars().first()
    last_inv = db.execute(
        select(Inverter)
        .where(Inverter.tenant_id == tenant.id,
               Inverter.deleted_at.is_(None),
               Inverter.last_power_at.is_not(None))
        .order_by(Inverter.last_power_at.desc())
    ).scalars().first()
    if last_acct is not None and last_acct.last_seen:
        cands.append((last_acct.last_seen, last_acct.provider or "utility"))
    if last_inv is not None and last_inv.last_power_at:
        cands.append((last_inv.last_power_at, last_inv.vendor or "inverter"))
    return cands


def _ao_status_stats(db, tenant: Tenant) -> dict:
    """Array Operator popup stats for ONE tenant: arrays / inverters / flagged
    (from the cached fleet tree, fleet-tree-failure-tolerant), today's measured
    kWh (excl. bill_prorate), and active offtakers."""
    today = local_today()   # rows are keyed by the fleet-LOCAL day, so read likewise
    arrays = inverters_total = flagged = 0
    try:
        from . import inverter_fleet
        tree = inverter_fleet.build_fleet_tree(db, tenant, stable_verdicts=True)
        summ = tree.get("summary", {})
        arrays = int(summ.get("arrays_total") or 0)
        inverters_total = int(summ.get("inverters_total") or 0)
        flagged = int(summ.get("attention") or 0)
    except Exception:
        log.warning("extension-status: fleet tree failed", exc_info=True)
        arrays = db.execute(
            select(func.count(Array.id)).where(
                Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False))
        ).scalar() or 0
        inverters_total = db.execute(
            select(func.count(Inverter.id)).where(
                Inverter.tenant_id == tenant.id,
                Inverter.deleted_at.is_(None))
        ).scalar() or 0

    # Today's measured kWh — EXCLUDE bill_prorate (a smeared utility-bill estimate,
    # never a real measurement; see the data-honesty audit).
    kwh_today = db.execute(
        select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0)).where(
            DailyGeneration.tenant_id == tenant.id,
            DailyGeneration.day == today,
            DailyGeneration.source != "bill_prorate")
    ).scalar() or 0.0

    offtakers = 0
    try:
        from .models import BillingReportSubscription
        offtakers = db.execute(
            select(func.count(BillingReportSubscription.id)).where(
                BillingReportSubscription.tenant_id == tenant.id,
                BillingReportSubscription.deleted_at.is_(None),
                BillingReportSubscription.enabled.is_(True))
        ).scalar() or 0
    except Exception:
        offtakers = 0

    return {
        "arrays": arrays,
        "inverters": inverters_total,
        "flagged": flagged,
        "kwh_today": round(float(kwh_today), 1),
        "offtakers": int(offtakers),
    }


def _nepool_status_stats(db, tenant: Tenant) -> dict:
    """NEPOOL popup stats for ONE tenant: active clients, arrays, freshest sent
    report."""
    from .models import Client, ReportDelivery
    clients = db.execute(
        select(func.count(Client.id)).where(
            Client.tenant_id == tenant.id,
            Client.deleted_at.is_(None),
            Client.active.is_(True))
    ).scalar() or 0
    arrays = db.execute(
        select(func.count(Array.id)).where(
            Array.tenant_id == tenant.id,
            Array.deleted_at.is_(None))
    ).scalar() or 0
    last_rep = db.execute(
        select(ReportDelivery.sent_at).where(
            ReportDelivery.tenant_id == tenant.id,
            ReportDelivery.status == "sent")
        .order_by(ReportDelivery.sent_at.desc())
    ).scalars().first()
    return {
        "clients": int(clients),
        "arrays": int(arrays),
        "last_report_at": last_rep.isoformat() if last_rep else None,
    }


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

    # Stamp site peakPower onto the connection so the weather model has a
    # nameplate before inventory/fleet creates Inverter rows (SolarEdge).
    cfg = dict(config or {})
    peak = result.get("peak_power_kw")
    if peak is None:
        peak = result.get("peak_kw")
    try:
        if peak is not None and float(peak) > 0:
            cfg["peak_power_kw"] = float(peak)
    except (TypeError, ValueError):
        pass

    conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == arr.id)
    ).scalar_one_or_none()
    if conn is None:
        conn = InverterConnection(array_id=arr.id, vendor=vendor, config=cfg, status="ok")
        db.add(conn)
    else:
        conn.vendor = vendor
        # Keep a prior peak if this vendor's validate didn't return one.
        if "peak_power_kw" not in cfg:
            prev = (conn.config or {}).get("peak_power_kw")
            if prev is not None:
                try:
                    if float(prev) > 0:
                        cfg["peak_power_kw"] = float(prev)
                except (TypeError, ValueError):
                    pass
        conn.config = cfg
        conn.status = "ok"
        conn.last_error = None

    # Backward compat: mirror SolarEdge creds onto the legacy columns so the
    # daily-pull virtual-connection path and any legacy readers keep working.
    if vendor == "solaredge":
        arr.solaredge_api_key = str(cfg.get("api_key") or "").strip()
        arr.solaredge_site_id = int(cfg["site_id"])

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

    # DailyGeneration.day is a fleet-LOCAL (US/Eastern) day — read with the same
    # key or the today/month buckets go stale/empty every evening after ~8pm ET.
    today = local_today()
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
            # Eager-load each array's client so the per-array `arr.client.name`
            # read below doesn't fire a lazy SELECT per row (one batched IN query
            # instead of N). This endpoint runs on every dashboard load.
            .options(selectinload(Array.client))
            .where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
            .order_by(Array.id)
        ).scalars().all()
        array_ids = [a.id for a in arrays]

        # ── batched DailyGeneration aggregates (was N+1) ──────────────────────
        # Previously this loop fired 5 DailyGeneration queries PER array (today,
        # has_today, month, lifetime/last_day, window series), so a 20-array
        # tenant cost 100+ round-trips on every dashboard load. Compute all of it
        # up front with a handful of GROUP BY array_id queries, then look each
        # array up in-memory below.
        today_by_array: dict[int, float] = {}
        month_by_array: dict[int, float] = {}
        life_by_array: dict[int, tuple[float, object]] = {}
        series_by_array: dict[int, list[dict]] = defaultdict(list)
        if array_ids:
            for aid, kwh in db.execute(
                select(DailyGeneration.array_id, func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
                .where(DailyGeneration.array_id.in_(array_ids), DailyGeneration.day == today)
                .group_by(DailyGeneration.array_id)
            ).all():
                today_by_array[aid] = float(kwh or 0.0)
            for aid, kwh in db.execute(
                select(DailyGeneration.array_id, func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
                .where(DailyGeneration.array_id.in_(array_ids), DailyGeneration.day >= month_start)
                .group_by(DailyGeneration.array_id)
            ).all():
                month_by_array[aid] = float(kwh or 0.0)
            for aid, kwh, last in db.execute(
                select(
                    DailyGeneration.array_id,
                    func.coalesce(func.sum(DailyGeneration.kwh), 0.0),
                    func.max(DailyGeneration.day),
                )
                .where(DailyGeneration.array_id.in_(array_ids))
                .group_by(DailyGeneration.array_id)
            ).all():
                life_by_array[aid] = (float(kwh or 0.0), last)
            for aid, d, k, src in db.execute(
                select(DailyGeneration.array_id, DailyGeneration.day,
                       DailyGeneration.kwh, DailyGeneration.source)
                .where(DailyGeneration.array_id.in_(array_ids), DailyGeneration.day >= window_start)
                .order_by(DailyGeneration.array_id, DailyGeneration.day)
            ).all():
                series_by_array[aid].append(
                    {"date": d.isoformat(), "kwh": float(k or 0.0), "source": src or "csv"})

        # ── data-honesty: which arrays carry ESTIMATED (bill_prorate) kWh ─────
        # The kWh aggregates above SUM across every DailyGeneration source, so a
        # value can silently blend metered rows with an estimate split out of a
        # utility bill. Flag the estimate so the card never shows it as measured.
        # One grouped query: arrays with any bill_prorate row today, and in the
        # whole window (covers month/lifetime cards). (See AO data-honesty audit.)
        est_today: set[int] = set()
        est_window: set[int] = set()
        if array_ids:
            for aid, day in db.execute(
                select(DailyGeneration.array_id, DailyGeneration.day)
                .where(DailyGeneration.array_id.in_(array_ids),
                       DailyGeneration.source == "bill_prorate")
                .group_by(DailyGeneration.array_id, DailyGeneration.day)
            ).all():
                est_window.add(aid)
                if day == today:
                    est_today.add(aid)

        # ── batched InverterConnection + provider lookups (was N+1) ───────────
        # _resolve_connection and _array_provider each fired one SELECT per array
        # inside the loop below. Pre-load both with a single IN query so a
        # 20-array tenant doesn't pay 40 extra round-trips per dashboard load.
        conn_by_array: dict[int, InverterConnection] = {}
        provider_by_array: dict[int, str] = {}
        if array_ids:
            for c in db.execute(
                select(InverterConnection)
                .where(InverterConnection.array_id.in_(array_ids))
            ).scalars().all():
                # First connection wins (mirrors the single-row resolve below);
                # an array realistically has one inverter connection.
                conn_by_array.setdefault(c.array_id, c)
            for aid, provider in db.execute(
                select(UtilityAccount.array_id, UtilityAccount.provider)
                .where(UtilityAccount.array_id.in_(array_ids),
                       UtilityAccount.deleted_at.is_(None))
                .order_by(UtilityAccount.id)
            ).all():
                provider_by_array.setdefault(aid, provider)

        # Cache energy rates by provider so identical providers don't re-derive.
        _rate_cache: dict[object, float] = {}

        for arr in arrays:
            today_kwh = today_by_array.get(arr.id, 0.0)
            # has_today: a row exists for today (distinct from "summed to 0").
            has_today = arr.id in today_by_array
            month_kwh = month_by_array.get(arr.id, 0.0)
            lifetime_kwh, last_day = life_by_array.get(arr.id, (0.0, None))

            # Daily series over the peer window (ascending) — the raw signal the
            # cohort analysis runs on.
            daily_series = series_by_array.get(arr.id, [])

            # Resolve from the pre-loaded map; fall back to the legacy
            # SolarEdge-columns virtual connection when no row exists (same
            # rule as _resolve_connection, without the per-array query).
            conn = conn_by_array.get(arr.id)
            if conn is None and arr.solaredge_api_key and arr.solaredge_site_id:
                conn = SimpleNamespace(
                    vendor="solaredge",
                    config={"api_key": arr.solaredge_api_key,
                            "site_id": arr.solaredge_site_id},
                    status="ok",
                )
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

            provider = provider_by_array.get(arr.id)
            if provider not in _rate_cache:
                _rate_cache[provider] = get_energy_rate(provider)
            rate = _rate_cache[provider]
            value = _value_model(today_kwh, month_kwh, lifetime_kwh, rate)
            health = _health(has_live_source, last_day, overview_ok, today)

            # Vendor-offline continuity flag (utility stands in for dead inverter feed).
            try:
                from . import production_fallback as _pf
                _pfall = _pf.compute_production_fallback(db, arr.id)
            except Exception:
                _pfall = {
                    "active": False, "source": None,
                    "days_filled": 0, "vendor_last_day": None,
                }

            entry = {
                "array_id": arr.id,
                "name": arr.name,
                "client_name": arr.client.name if arr.client else None,
                "fuel_type": arr.fuel_type,
                "live": live,
                # is_estimated: today's figure includes a bill_prorate row (split
                # from a utility bill), not a metered reading — UI marks it.
                "today": ({"kwh": round(today_kwh, 3),
                           "is_estimated": arr.id in est_today} if has_today else None),
                "month": {"kwh": round(month_kwh, 3)},
                "lifetime": {"kwh": round(lifetime_kwh, 3)},
                # True when ANY kWh in the window (month/lifetime cards) is an
                # estimate split from a utility bill rather than metered data.
                "has_estimated": arr.id in est_window,
                "value": value,
                "health": health,
                # Daily kWh over the peer window (ascending) for the owner
                # dashboard's sparkline. Kept lightweight (date+kwh only).
                "_daily": daily_series,
                # {active, source, days_filled, vendor_last_day} — UI chip + graph
                "production_fallback": _pfall,
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
    "locus": ("locus", "Locus Energy"),
    "extension_pull": ("inverter", "Inverter (extension)"),
    "extension_pull_corrected": ("inverter", "Inverter (extension)"),
    # operator-supplied
    "csv": ("csv", "CSV upload"),
    "manual": ("manual", "Manual entry"),
    "bill_prorate": ("bill", "Bill (prorated)"),
}
# Display order for the attribution legend (the five named vendors lead).
_SOURCE_ORDER = ["gmp", "solaredge", "fronius", "sma", "chint", "locus",
                 "inverter", "csv", "manual", "bill", "other"]
_SOURCE_LABELS = {
    "gmp": "GMP (utility meter)", "solaredge": "SolarEdge", "fronius": "Fronius",
    "sma": "SMA", "chint": "CHINT", "locus": "Locus Energy",
    "inverter": "Inverter (extension)",
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
    today = local_today()   # TTM window anchored on the fleet-local day

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
            kwh_by_year: dict[int, float] = {}
            kwh_by_ym: dict[tuple[int, int], float] = {}
            for d, kwh in per_day.items():
                kwh_by_year[d.year] = kwh_by_year.get(d.year, 0.0) + float(kwh or 0)
                ym = (d.year, d.month)
                kwh_by_ym[ym] = kwh_by_ym.get(ym, 0.0) + float(kwh or 0)
            ytd_year = today.year
            ytd_kwh = round(sum(
                v for d, v in per_day.items() if d.year == ytd_year
            ), 1)
            # Trailing 12 calendar months (same window as fleet ttm_kwh) so the
            # year matrix can compare an apples-to-apples rolling year against
            # full prior calendar years (YTD alone understates the current year).
            arr_ttm = 0.0
            for off in range(12):
                yy, mm = today.year, today.month - off
                while mm <= 0:
                    mm += 12
                    yy -= 1
                arr_ttm += kwh_by_ym.get((yy, mm), 0.0)
            prior_year = today.year - 1
            prior_year_kwh = round(kwh_by_year.get(prior_year, 0.0), 1)
            ttm_vs_prior_pct = None
            if prior_year_kwh > 0 and arr_ttm > 0:
                ttm_vs_prior_pct = round(
                    100.0 * (arr_ttm - prior_year_kwh) / prior_year_kwh, 1)
            by_array[arr.id] = {
                "array_id": arr.id,
                "name": arr.name,
                "lifetime_kwh": life,
                "years": sorted({d.year for d in per_day.keys()}),
                "kwh_by_year": {
                    str(y): round(v, 1) for y, v in sorted(kwh_by_year.items())
                },
                "kwh_ytd": ytd_kwh,
                "ytd_year": ytd_year,
                "kwh_ttm": round(arr_ttm, 1),
                "kwh_prior_year": prior_year_kwh if prior_year_kwh > 0 else None,
                "prior_year": prior_year,
                "ttm_vs_prior_year_pct": ttm_vs_prior_pct,
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


# ── Master Array Data Pack (Paul 2026-07) ─────────────────────────────────────
# Per-array mega spreadsheet: all utility bills + daily + monthly + YoY/TTM.
# Transmit path today = download (and zip of all arrays). Email schedule later.


@router.get("/v1/array-owners/arrays/{array_id}/master-data.xlsx")
def download_array_master_data(
    array_id: int,
    authorization: str | None = Header(default=None),
):
    """Download the Master Data Pack for one array (xlsx).

    Sheets: Meta · Bills · Monthly · Daily · YoY (calendar years + trailing 12 mo).
    Built live from Bill + DailyGeneration — never a frozen snapshot.
    """
    tenant = _tenant_from_bearer(authorization)
    from .reports.array_master_pack import (
        array_master_filename, build_array_master_workbook,
    )
    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant.id or arr.deleted_at is not None:
            raise HTTPException(404, "Array not found")
        name = arr.name or f"array-{array_id}"
        blob = build_array_master_workbook(tenant.id, array_id, db=db)
    if not blob:
        raise HTTPException(404, "Array not found")
    fname = array_master_filename(name, array_id)
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/v1/array-owners/master-data.zip")
def download_fleet_master_data_zip(
    authorization: str | None = Header(default=None),
):
    """Zip of one Master Data Pack per live array for this tenant."""
    tenant = _tenant_from_bearer(authorization)
    from .reports.array_master_pack import build_fleet_master_zip
    from .models import local_today as _lt
    blob, n = build_fleet_master_zip(tenant.id)
    if n == 0:
        raise HTTPException(404, "No arrays to export")
    fname = f"fleet-master-data-{_lt().isoformat()}.zip"
    return Response(
        content=blob,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
            "X-Array-Count": str(n),
        },
    )


@router.get("/v1/array-owners/fleet-tree")
def fleet_tree(force: int = 0, mode: str = "live",
               authorization: str | None = Header(default=None)) -> dict:
    """Owner-grouped three-tier sandbox structure — the REAL integrated model:

        Alert  (per array, rolled-up worst state)
          └─ Array  (an OWNER-DEFINED group)
               └─ Inverters  (persisted, owner-arranged; telemetry by source)

    Inverters are persisted `Inverter` rows the owner can drag between arrays.
    Each inverter's telemetry is pulled from its fixed SOURCE site; peer analysis
    runs within each OWNER group — so moving an inverter genuinely changes its
    cohort. Pass ?force=1 to bypass the 10-min telemetry cache.

    mode=stored|lite|fast|db — INSTANT first-paint path: DB only, no vendor API.
    The owner spreadsheet loads this first so provider groups stream into the DOM
    immediately, then upgrades with mode=live (default) in the background.

    See api/inverter_fleet.py for the model rationale (owners reproduce the model
    in their head; the vendor's site grouping is just the starting point).
    """
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    _mode = (mode or "live").strip().lower()
    stored_only = _mode in ("stored", "lite", "fast", "db")
    with SessionLocal() as db:
        # stable_verdicts: judge inverter HEALTH on complete days + per-active-day
        # peer comparison (immune to dawn weather + capture-gap days that fake
        # "underperforming"), the SAME logic the email digest/alerts use — so the
        # app and the emails never disagree about which inverters need attention.
        # Live elements (current kW, daylight, live-dark overlay) are computed
        # independently of the verdict, so the dashboard stays real-time.
        return inverter_fleet.build_fleet_tree(
            db, tenant, force_refresh=bool(force),
            stable_verdicts=True, stored_only=stored_only,
        )


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


class RenameBody(BaseModel):
    name: str


@router.post("/v1/array-owners/arrays/{array_id}/name")
def rename_array_ep(array_id: int, body: RenameBody,
                    authorization: str | None = Header(default=None)) -> dict:
    """Rename an owner array (the inline edit in the Sandbox / Spreadsheet view).
    Persists to the backend so BOTH dashboard views and a reload see the new name.
    Name clash with another of this tenant's arrays → 409; empty → 400."""
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            a = inverter_fleet.rename_array(db, tenant, array_id, body.name)
        except inverter_fleet.FleetError as exc:
            msg = str(exc)
            status = 409 if "already has that name" in msg else 400
            raise HTTPException(status, msg)
        return {"ok": True, "array_id": a.id, "name": a.name}


class PortfolioBody(BaseModel):
    portfolio_name: str | None = None


@router.post("/v1/array-owners/arrays/{array_id}/portfolio")
def set_array_portfolio_ep(array_id: int, body: PortfolioBody,
                           authorization: str | None = Header(default=None)) -> dict:
    """Assign (or clear) an array's portfolio/group label — the Analysis-tab
    fleet hierarchy. Tenant-scoped. Empty/whitespace clears it (NULL = Unassigned).
    Trimmed and capped to the column width (80)."""
    from .models import Array
    tenant = _tenant_from_bearer(authorization)
    name = (body.portfolio_name or "").strip()[:80] or None
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(
                Array.id == array_id,
                Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if arr is None:
            raise HTTPException(404, "Array not found")
        arr.portfolio_name = name
        db.commit()
        return {"ok": True, "array_id": arr.id, "portfolio_name": arr.portfolio_name}


class ReminderBody(BaseModel):
    reminder: str | None = None


@router.post("/v1/array-owners/arrays/{array_id}/reminder")
def set_array_reminder_ep(array_id: int, body: ReminderBody,
                          authorization: str | None = Header(default=None)) -> dict:
    """Assign/clear an array's O&M reminder note (Analysis Sites "Reminder" column).
    Tenant-scoped; empty clears (NULL)."""
    tenant = _tenant_from_bearer(authorization)
    note = (body.reminder or "").strip()[:2000] or None
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(Array.id == array_id, Array.tenant_id == tenant.id,
                                Array.deleted_at.is_(None))
        ).scalar_one_or_none()
        if arr is None:
            raise HTTPException(404, "Array not found")
        arr.reminder = note
        db.commit()
        return {"ok": True, "array_id": arr.id, "reminder": arr.reminder}


# ── Files: per-site document storage (Analysis-tab "Files") ──────────────────
MAX_SITE_FILE_BYTES = 10 * 1024 * 1024   # 10 MB per document


class FileUploadBody(BaseModel):
    array_id: int
    filename: str
    mime: str | None = None
    data_b64: str


@router.get("/v1/array-owners/files")
def list_site_files_ep(authorization: str | None = Header(default=None)) -> dict:
    from .models import SiteFile
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        rows = db.execute(
            select(SiteFile, Array.name).join(Array, SiteFile.array_id == Array.id)
            .where(SiteFile.tenant_id == tenant.id, SiteFile.deleted_at.is_(None))
            .order_by(SiteFile.uploaded_at.desc())
        ).all()
        files = [{
            "id": f.id, "array_id": f.array_id, "array_name": aname,
            "filename": f.filename, "mime": f.mime, "size": f.size,
            "uploaded_at": f.uploaded_at.isoformat() if f.uploaded_at else None,
        } for (f, aname) in rows]
        return {"files": files}


@router.post("/v1/array-owners/files")
def upload_site_file_ep(body: FileUploadBody,
                        authorization: str | None = Header(default=None)) -> dict:
    import base64
    from .models import SiteFile
    tenant = _tenant_from_bearer(authorization)
    try:
        raw = base64.b64decode((body.data_b64 or "").split(",")[-1])
    except Exception:
        raise HTTPException(400, "Invalid file data")
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_SITE_FILE_BYTES:
        raise HTTPException(413, "File too large (max 10 MB)")
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(Array.id == body.array_id, Array.tenant_id == tenant.id,
                                Array.deleted_at.is_(None))
        ).scalar_one_or_none()
        if arr is None:
            raise HTTPException(404, "Array not found")
        f = SiteFile(tenant_id=tenant.id, array_id=arr.id,
                     filename=(body.filename or "file")[:255], mime=(body.mime or None),
                     size=len(raw), data=raw)
        db.add(f)
        db.commit()
        db.refresh(f)
        return {"ok": True, "file": {"id": f.id, "array_id": f.array_id,
                "filename": f.filename, "mime": f.mime, "size": f.size,
                "uploaded_at": f.uploaded_at.isoformat()}}


@router.get("/v1/array-owners/files/{file_id}/download")
def download_site_file_ep(file_id: int,
                          authorization: str | None = Header(default=None)):
    from fastapi import Response
    from .models import SiteFile
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        f = db.execute(
            select(SiteFile).where(SiteFile.id == file_id, SiteFile.tenant_id == tenant.id,
                                   SiteFile.deleted_at.is_(None))
        ).scalar_one_or_none()
        if f is None or f.data is None:
            raise HTTPException(404, "File not found")
        safe = (f.filename or "file").replace('"', "")
        return Response(content=f.data, media_type=(f.mime or "application/octet-stream"),
                        headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@router.delete("/v1/array-owners/files/{file_id}")
def delete_site_file_ep(file_id: int,
                        authorization: str | None = Header(default=None)) -> dict:
    from .models import SiteFile
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        f = db.execute(
            select(SiteFile).where(SiteFile.id == file_id, SiteFile.tenant_id == tenant.id,
                                   SiteFile.deleted_at.is_(None))
        ).scalar_one_or_none()
        if f is None:
            raise HTTPException(404, "File not found")
        f.deleted_at = now()
        db.commit()
        return {"ok": True}


# ── Event log: ticketed alerts with an O&M lifecycle (Analysis-tab "Event log") ──
_EVENT_FROM_STATUS = {
    "dead": ("Inverter stopped", "critical"),
    "fault": ("Fault reported", "critical"),
    "underperforming": ("Underperforming", "warning"),
    "comm_gap": ("Gone quiet", "warning"),
}


class AlertEventPatchBody(BaseModel):
    status: str | None = None
    note: str | None = None


def _sync_alert_events(db, tenant) -> None:
    """Upsert an OPEN AlertEvent for each currently-firing inverter alert so the
    Event log reflects live faults. Idempotent (dedup on tenant+array+inverter+title);
    an already-tracked event is left as-is (operator owns its lifecycle)."""
    from . import inverter_fleet
    from .models import AlertEvent
    try:
        tree = inverter_fleet.build_fleet_tree(db, tenant, stable_verdicts=True)
    except Exception:
        return
    created = False
    for col in tree.get("columns", []):
        aid = col.get("array_id")
        aname = col.get("array_name")
        for inv in col.get("inverters", []):
            spec = _EVENT_FROM_STATUS.get(inv.get("status"))
            if not spec:
                continue
            title, sev = spec
            ref = inv.get("name") or (str(inv.get("inverter_id")) if inv.get("inverter_id") is not None else None)
            existing = db.execute(
                select(AlertEvent).where(
                    AlertEvent.tenant_id == tenant.id,
                    AlertEvent.array_id == aid,
                    AlertEvent.inverter_ref == ref,
                    AlertEvent.title == title,
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(AlertEvent(tenant_id=tenant.id, array_id=aid, array_name=aname,
                                  inverter_ref=ref, title=title, severity=sev,
                                  status="open", note=inv.get("diagnosis")))
                created = True
    if created:
        try:
            db.commit()
        except Exception:
            db.rollback()


@router.get("/v1/array-owners/alert-events")
def list_alert_events_ep(authorization: str | None = Header(default=None)) -> dict:
    from .models import AlertEvent
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        _sync_alert_events(db, tenant)
        rows = db.execute(
            select(AlertEvent).where(AlertEvent.tenant_id == tenant.id)
            .order_by(AlertEvent.created_at.desc())
        ).scalars().all()
        events = [{
            "id": e.id, "array_id": e.array_id, "array_name": e.array_name,
            "inverter_name": e.inverter_ref, "title": e.title,
            "severity": e.severity, "status": e.status, "note": e.note,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        } for e in rows]
        return {"events": events}


@router.patch("/v1/array-owners/alert-events/{event_id}")
def patch_alert_event_ep(event_id: int, body: AlertEventPatchBody,
                         authorization: str | None = Header(default=None)) -> dict:
    from .models import AlertEvent
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        e = db.execute(
            select(AlertEvent).where(AlertEvent.id == event_id,
                                     AlertEvent.tenant_id == tenant.id)
        ).scalar_one_or_none()
        if e is None:
            raise HTTPException(404, "Event not found")
        if body.status in ("open", "ack", "resolved"):
            e.status = body.status
        if body.note is not None:
            e.note = body.note.strip()[:2000] or None
        db.commit()
        return {"ok": True, "event": {"id": e.id, "status": e.status, "note": e.note}}


@router.post("/v1/array-owners/inverters/{inverter_id}/name")
def rename_inverter_ep(inverter_id: int, body: RenameBody,
                       authorization: str | None = Header(default=None)) -> dict:
    """Rename an owner inverter (the inline edit in either dashboard view).
    Persists + marks the name owner-set so a telemetry sync never overwrites it.
    Empty → 400. No uniqueness check (inverters may share names across arrays)."""
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            iv = inverter_fleet.rename_inverter(db, tenant, inverter_id, body.name)
        except inverter_fleet.FleetError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "inverter_id": iv.id, "name": iv.name}


class ExpectedLowBody(BaseModel):
    # True = mark this inverter as expected-low (structural shading/obstruction);
    # False = clear the mark and return it to normal peer grading.
    expected_low: bool
    reason: str | None = None       # e.g. "Afternoon shade from neighbour's maple"
    baseline: float | None = None   # optional peer_index to hold to; else computed


@router.post("/v1/array-owners/inverters/{inverter_id}/expected-low")
def set_expected_low_ep(inverter_id: int, body: ExpectedLowBody,
                        authorization: str | None = Header(default=None)) -> dict:
    """Mark/clear an inverter as OWNER-CONFIRMED expected-low. When marked, the
    verdict engine re-baselines it (holds it to its recorded level instead of the
    cohort floor) so a permanently-shaded unit stops reading "underperforming" —
    but still flags + alerts if it drops BELOW that baseline. The Energy Agent sets
    this after confirming the cause with the owner; the dashboard exposes it as a
    manual toggle too."""
    tenant = _tenant_from_bearer(authorization)
    from . import inverter_fleet
    with SessionLocal() as db:
        try:
            iv = inverter_fleet.set_expected_low(
                db, tenant, inverter_id,
                expected_low=body.expected_low, reason=body.reason,
                baseline=body.baseline, set_by="owner",
            )
        except inverter_fleet.FleetError as exc:
            raise HTTPException(400, str(exc))
        return {
            "ok": True, "inverter_id": iv.id,
            "expected_low": iv.expected_low,
            "expected_low_reason": iv.expected_low_reason,
            "expected_low_baseline": iv.expected_low_baseline,
        }


@router.get("/v1/array-owners/inverters/{inverter_id}/outages")
def inverter_outages_ep(inverter_id: int,
                        days: int = Query(default=180, ge=1, le=730),
                        authorization: str | None = Header(default=None)) -> dict:
    """The OUTAGE LOG for one inverter — every episode where it stopped producing,
    when it started, and the specific vendor fault code (or, failing that, our
    honestly-labelled best guess) for why.

    Powers the "Outage log" section of the inverter detail overlay. Read-only.
    404s for an inverter that is not this tenant's — indistinguishable from one that
    does not exist, so an id from another tenant leaks nothing.

    See api/inverter_outage_log.py for the cause-attribution ladder and the honesty
    rules (night is never an outage; absent is not zero; estimates are labelled).
    """
    from . import inverter_outage_log
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        log_data = inverter_outage_log.build_outage_log(db, tenant, inverter_id, days=days)
        if log_data is None:
            raise HTTPException(404, "Inverter not found")
        return log_data


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
    # When True, skip separate alert emails — the morning fleet digest covers them.
    via_digest: bool | None = None


def _alert_settings_dict(tenant) -> dict:
    return {
        "enabled": bool(getattr(tenant, "inverter_alerts_enabled", False)),
        "email": getattr(tenant, "inverter_alert_email", None) or tenant.contact_email,
        "email_is_default": not getattr(tenant, "inverter_alert_email", None),
        "threshold_pct": int(getattr(tenant, "inverter_alert_threshold_pct", 50) or 50),
        "grace_hours": int(getattr(tenant, "inverter_alert_grace_hours", 12) or 12),
        "via_digest": bool(getattr(tenant, "inverter_alerts_via_digest", False)),
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
    # email may be a comma/semicolon-separated list (send alerts to the whole team).
    _recips = None
    if body.email is not None:
        _recips = [e.strip() for e in body.email.replace(";", ",").split(",") if e.strip()]
        for e in _recips:
            if "@" not in e or " " in e or "." not in e.split("@")[-1]:
                raise HTTPException(400, f"'{e}' is not a valid email address")
    with SessionLocal() as db:
        t = db.get(type(tenant), tenant.id)
        if t is None:
            raise HTTPException(404, "Tenant not found")
        if body.enabled is not None:
            t.inverter_alerts_enabled = bool(body.enabled)
        if body.email is not None:
            t.inverter_alert_email = ", ".join(_recips) or None
        if body.threshold_pct is not None:
            t.inverter_alert_threshold_pct = max(10, min(95, int(body.threshold_pct)))
        if body.grace_hours is not None:
            t.inverter_alert_grace_hours = max(0, min(168, int(body.grace_hours)))
        if body.via_digest is not None:
            t.inverter_alerts_via_digest = bool(body.via_digest)
        db.commit()
        db.refresh(t)
        return _alert_settings_dict(t)


@router.post("/v1/array-owners/alert-settings/test")
def test_alert_settings(authorization: str | None = Header(default=None)) -> dict:
    """Send a test inverter-alert email to the configured recipient(s) NOW, so the
    owner can confirm delivery and see the format — independent of the enabled flag."""
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    raw = getattr(tenant, "inverter_alert_email", None) or tenant.contact_email
    to = [e.strip() for e in str(raw or "").replace(";", ",").split(",") if e.strip()]
    if not to:
        raise HTTPException(400, "Add a recipient email first, then send a test.")
    from . import notify
    subject = "✅ Test alert — your Array Operator monitoring is set up"
    html = (
        "<p>This is a <strong>test alert</strong> from Array Operator.</p>"
        "<p>Your inverter monitoring is working. When a real inverter goes dark or "
        "drops below its neighbors — and stays there past your grace window — "
        "we’ll email you here with the site, the inverter, and what to do.</p>"
        "<p>No action needed — you can change who gets alerts and how sensitive "
        "they are from the alerts widget in Array Operator.</p>"
    )
    text = ("Test alert from Array Operator. Your inverter monitoring is working — "
            "we’ll email you here when a real inverter needs attention. No action needed.")
    ok = notify._send_via_resend(to=to, subject=subject, html=html, text=text,
                                 product="array_operator")
    if not ok:
        raise HTTPException(502, "Couldn't send the test email right now — try again shortly.")
    return {"ok": True, "sent_to": to}


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


@router.get("/v1/array-owners/solaredge/keys")
def list_solaredge_keys(authorization: str | None = Header(default=None)) -> dict:
    """The tenant's connected SolarEdge monitoring API keys, so the owner can SEE
    what's linked in the credential vault (Ford 2026-07-10). Grouped by key (one key
    can cover many sites). Returns the full key (their OWN read-only key — for a
    reveal/copy toggle in the UI), a masked form, and the arrays it powers.

    Also returns a tenant-wide `last_synced_at` — the freshest DailyGeneration row
    with source="solaredge" across this tenant's SolarEdge arrays (see
    jobs/solaredge_pull.py) — so the Auto-refresh Credential Vault's LIVE data
    board can show a SolarEdge status row alongside the harvester-backed vendors
    (Ford 2026-07-12: "add its status up here"). SolarEdge has no login-based
    health record (it's a plain API key, not a PortalCredential the harvester
    logs into), so DailyGeneration.uploaded_at is the best honest freshness
    signal actually available. No fabricated ok/error flag: if nothing's synced
    yet, `last_synced_at` is simply None — the board reads that as "not yet
    synced," not a lie."""
    tenant = _tenant_from_bearer(authorization)
    from .models import Array, DailyGeneration
    from sqlalchemy import func
    by_key: dict[str, list[str]] = {}
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tenant.id,
                                Array.deleted_at.is_(None))
        ).scalars().all()
        array_ids = []
        for a in arrays:
            k = (getattr(a, "solaredge_api_key", None) or "").strip()
            if k:
                by_key.setdefault(k, []).append(a.name or f"Array {a.id}")
                array_ids.append(a.id)
        last_synced_at = None
        if array_ids:
            last_synced_at = db.execute(
                select(func.max(DailyGeneration.uploaded_at)).where(
                    DailyGeneration.array_id.in_(array_ids),
                    DailyGeneration.source == "solaredge",
                )
            ).scalar()
    keys = []
    for k, names in by_key.items():
        masked = ("••••" + k[-4:]) if len(k) >= 4 else ("•" * len(k))
        uniq = sorted(set(names))
        keys.append({"key": k, "masked": masked, "arrays": uniq, "array_count": len(uniq)})
    keys.sort(key=lambda x: (-x["array_count"], x["masked"]))
    return {
        "ok": True, "keys": keys,
        "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
    }


# Human labels for linked-source board rows (inverter vendors + common utilities).
_LINKED_LABELS = {
    "solaredge": "SolarEdge",
    "alsoenergy": "AlsoEnergy (PowerTrack)",
    "locus": "Locus Energy",
    "fronius": "Fronius (Solar.web)",
    "sma": "SMA (Sunny Portal)",
    "chint": "Chint",
    "enphase": "Enphase",
    "solis": "Solis",
    "tigo": "Tigo",
    "gmp": "Green Mountain Power (GMP)",
    "vec": "Vermont Electric Cooperative (SmartHub)",
    "wec": "Washington Electric Coop (SmartHub)",
    "eversource": "Eversource Energy",
    "eversource_ma": "Eversource Energy (MA)",
    "eversource_ct": "Eversource Energy (CT)",
    "cmp": "Central Maine Power",
}


@router.get("/v1/array-owners/linked-sources")
def list_linked_sources(authorization: str | None = Header(default=None)) -> dict:
    """Every data source linked to this tenant — inverter vendors (InverterConnection
    + legacy SolarEdge columns) and utilities (UtilityAccount / UtilitySession).

    Powers the Auto-refresh LIVE board so API-key vendors (SolarEdge, AlsoEnergy,
    Locus, …) and bill-linked utilities always appear next to harvester vault
    logins. Does NOT return secrets — only code, label, counts, and best-effort
    last_synced_at from DailyGeneration / account touch times.
    """
    tenant = _tenant_from_bearer(authorization)
    from .models import Array, DailyGeneration, UtilityAccount, UtilitySession
    from sqlalchemy import func

    inv_by: dict[str, dict] = {}  # code -> {arrays:set, last}
    util_by: dict[str, dict] = {}

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id, Array.deleted_at.is_(None),
            )
        ).scalars().all()
        arr_by_id = {a.id: a for a in arrays}
        array_ids = list(arr_by_id.keys())

        # Inverter connections (AlsoEnergy, Fronius, SMA, Locus, SolarEdge, …)
        if array_ids:
            conns = db.execute(
                select(InverterConnection).where(
                    InverterConnection.array_id.in_(array_ids)
                )
            ).scalars().all()
            for c in conns:
                code = (c.vendor or "").strip().lower()
                if not code:
                    continue
                bucket = inv_by.setdefault(code, {"arrays": set(), "last": None})
                a = arr_by_id.get(c.array_id)
                if a is not None:
                    bucket["arrays"].add(a.name or f"Array {a.id}")

        # Legacy SolarEdge columns without an InverterConnection row
        for a in arrays:
            k = (getattr(a, "solaredge_api_key", None) or "").strip()
            sid = getattr(a, "solaredge_site_id", None)
            if k or sid:
                bucket = inv_by.setdefault("solaredge", {"arrays": set(), "last": None})
                bucket["arrays"].add(a.name or f"Array {a.id}")

        # Freshest DailyGeneration per vendor source for those arrays
        if array_ids:
            for code in list(inv_by.keys()):
                # DailyGeneration.source is usually the vendor code for inverter pulls
                last = db.execute(
                    select(func.max(DailyGeneration.uploaded_at)).where(
                        DailyGeneration.array_id.in_(array_ids),
                        DailyGeneration.source == code,
                    )
                ).scalar()
                if last is None and code == "solaredge":
                    # older rows sometimes used slightly different source tags
                    last = db.execute(
                        select(func.max(DailyGeneration.uploaded_at)).where(
                            DailyGeneration.array_id.in_(array_ids),
                            DailyGeneration.source.in_(("solaredge", "se", "solaredge_api")),
                        )
                    ).scalar()
                inv_by[code]["last"] = last

        # Utility accounts
        accts = db.execute(
            select(UtilityAccount).where(UtilityAccount.tenant_id == tenant.id)
        ).scalars().all()
        for acct in accts:
            code = (acct.provider or "").strip().lower()
            if not code:
                continue
            bucket = util_by.setdefault(code, {"accounts": set(), "last": None})
            label = (acct.nickname or acct.account_number or "").strip()
            if label:
                bucket["accounts"].add(label)
            else:
                bucket["accounts"].add(code)
            touched = getattr(acct, "last_seen", None) or getattr(acct, "updated_at", None)
            if touched and (bucket["last"] is None or touched > bucket["last"]):
                bucket["last"] = touched

        # Utility sessions (JWT / cookie capture) even if account row is sparse
        sessions = db.execute(
            select(UtilitySession).where(UtilitySession.tenant_id == tenant.id)
        ).scalars().all()
        for sess in sessions:
            code = (sess.provider or "").strip().lower()
            if not code:
                continue
            bucket = util_by.setdefault(code, {"accounts": set(), "last": None})
            if not bucket["accounts"]:
                bucket["accounts"].add(code)
            cap = getattr(sess, "captured_at", None)
            if cap and (bucket["last"] is None or cap > bucket["last"]):
                bucket["last"] = cap

        # Utility-meter DailyGeneration (source=utility_meter) doesn't map cleanly
        # per provider; skip.

    sources = []
    for code, b in inv_by.items():
        n = len(b["arrays"])
        last = b["last"]
        sources.append({
            "code": code,
            "kind": "inverter",
            "label": _LINKED_LABELS.get(code) or code.replace("_", " ").title(),
            "count": n,
            "detail": f"{n} array{'' if n == 1 else 's'}",
            "last_synced_at": last.isoformat() if last else None,
        })
    for code, b in util_by.items():
        n = len(b["accounts"])
        last = b["last"]
        sources.append({
            "code": code,
            "kind": "utility",
            "label": _LINKED_LABELS.get(code) or code.replace("_", " ").title(),
            "count": n,
            "detail": f"{n} account{'' if n == 1 else 's'}",
            "last_synced_at": last.isoformat() if last else None,
        })
    sources.sort(key=lambda s: (0 if s["kind"] == "inverter" else 1, s["label"].lower()))
    return {"ok": True, "sources": sources, "count": len(sources)}


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


class LocusConnectBody(BaseModel):
    username: str
    password: str
    site_id: int
    partner_id: str | None = None
    timezone: str | None = None


@router.post("/v1/array-owners/arrays/{array_id}/locus")
def connect_locus(
    array_id: int,
    body: LocusConnectBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Connect a Locus Energy (SolarNOC) site to an array — thin shim over the
    vendor-agnostic inverter framework (same path SolarEdge uses). Validates the
    credentials + site by pulling site details before persisting; a rejected
    login (401/403) or unreachable site returns 400 and saves nothing.

    Auth is the SolarNOC portal username + password (the API is fronted by AWS
    Cognito) — no API app client_id/secret needed.
    """
    tenant = _tenant_from_bearer(authorization)

    config = {
        "username": (body.username or "").strip(),
        "password": body.password or "",
        "site_id": int(body.site_id),
    }
    if not all((config["username"], config["password"])):
        raise HTTPException(400, "username and password are both required")
    if body.partner_id:
        config["partner_id"] = body.partner_id.strip()
    if body.timezone:
        config["timezone"] = body.timezone.strip()

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant.id:
            raise HTTPException(404, "Array not found")
        try:
            result = _connect_inverter(db, arr, "locus", config)
        except InverterAuthError as exc:
            raise HTTPException(400, str(exc))
        except InverterError as exc:
            raise HTTPException(400, f"Locus error: {exc}")

    return {
        "ok": True,
        "site_name": result.get("site_name"),
        "site_id": int(body.site_id),
    }


# ── account-level SolarEdge discovery ("paste one credential, attach all") ─────

def _attach_solaredge(
    db, arr: Array, api_key: str, site_id: int, *, peak_power_kw: float | None = None,
) -> InverterConnection:
    """Upsert a solaredge InverterConnection on `arr` WITHOUT a per-site validate
    call — the account-level key was already proven by discover(), so we don't
    burn one SolarEdge request per site. Mirrors the creds onto the legacy
    columns so the daily pull + virtual-connection path keep working.

    `peak_power_kw` is SolarEdge's site peakPower (kWp) from /sites/list or
    /site/{id}/details. Storing it on the connection lets the weather model run
    even before inventory/fleet sync creates Inverter rows (Ford 2026-07-20:
    Cover/Starlake showed "not modeled yet" with location but zero nameplate).
    """
    config: dict = {"api_key": api_key, "site_id": int(site_id)}
    try:
        pk = float(peak_power_kw) if peak_power_kw is not None else None
    except (TypeError, ValueError):
        pk = None
    if pk is not None and pk > 0:
        config["peak_power_kw"] = pk
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
        # Preserve a previously stamped peak when rediscover omits it.
        if pk is None or pk <= 0:
            prev = (conn.config or {}).get("peak_power_kw")
            if prev is not None:
                try:
                    if float(prev) > 0:
                        config["peak_power_kw"] = float(prev)
                except (TypeError, ValueError):
                    pass
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
    _tenant = _tenant_from_bearer(authorization)
    _guard_vendor_discover(_tenant.id, "solaredge")

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

# Typical fixed-tilt PV capacity factor (annual kWh ≈ kW_dc × 8760 × CF). 0.14
# is a conservative US/VT-ish blended figure for a quick pre-signup estimate;
# the dashboard shows exact measured value once live.
_EST_CAPACITY_FACTOR = 0.14


def _guard_preview_oracle(request: Request) -> None:
    """Throttle the UNAUTHENTICATED vendor-credential preview oracle.

    `/public/preview` validates arbitrary vendor credentials with no auth, so
    it's a credential-validation + site-enumeration oracle that can also burn an
    operator's SolarEdge/Locus API budget. A per-IP cap alone is bypassable by
    rotating the client-supplied X-Forwarded-For header, so we layer an
    UNSPOOFABLE global ceiling on top: even with IP rotation the endpoint can't
    be hammered past the global cap. (Mirrors ratelimit.enforce's pytest
    exemption so the shared-IP test suite doesn't trip it.)
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    ip = ratelimit.client_ip(request)
    if not ratelimit.allow("vendor_preview_ip", ip, max_hits=6, window_s=300):
        raise HTTPException(429, "Too many preview attempts — give it a few minutes and try again.")
    if not ratelimit.allow("vendor_preview_global", "all", max_hits=40, window_s=300):
        raise HTTPException(429, "The preview service is busy right now — please try again in a few minutes.")


def _guard_vendor_discover(tenant_id: str, vendor: str) -> None:
    """Bound an AUTHENTICATED tenant's vendor-discovery calls so a compromised
    session/key can't drain the operator's vendor API budget, plus a global
    ceiling across all tenants. Keyed on the validated tenant id (unspoofable)."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    if not ratelimit.allow(f"{vendor}_discover_tenant", tenant_id, max_hits=30, window_s=300):
        raise HTTPException(429, "Too many discovery attempts — give it a few minutes and try again.")
    if not ratelimit.allow("vendor_discover_global", "all", max_hits=120, window_s=300):
        raise HTTPException(429, "Discovery is busy right now — please try again in a few minutes.")


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
    "locus": "That SolarNOC login didn't work — double-check the username and password "
             "you use at the Locus SolarNOC / AlsoEnergy portal.",
    "fronius": "Those Solar.web keys didn't work — check the Access Key ID and "
               "Access Key Value (Solar.web → Settings → REST API → CREATE NEW KEY).",
    "sma": "Those SMA credentials didn't work — check the client ID/secret and Plant/System ID.",
}
_PREVIEW_SCOPE_MSG = {
    "solaredge": "That's a site-level key — it can't list your whole account. Paste an "
                 "account-level key (SolarEdge Admin → Site Access → API Access) to see "
                 "every array at once.",
    "locus": "That login is valid but can't list the partner's sites. Enter a single "
             "Site ID to preview just that array.",
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
        # One SolarNOC login lists every site under the partner (the partner id
        # is read from the login itself — no partner_id input). A named site_id
        # still previews just that array.
        if str(config.get("site_id") or "").strip():
            return [_normalize_preview_site(vendor, mod.validate(config))]
        raw = mod.discover_sites(config)
        return [_normalize_preview_site(vendor, s) for s in raw]

    if vendor == "fronius":
        # Full-account cascade: the Solar.web AccessKey lists EVERY system on
        # the account (discover_systems, grounded live 2026-07-04) — same
        # "paste one credential, see all your arrays" as SolarEdge. A named
        # pv_system_id still previews just that system.
        if str(config.get("pv_system_id") or "").strip():
            return [_normalize_preview_site(vendor, mod.validate(config))]
        raw = mod.discover_systems(config)
        return [_normalize_preview_site(vendor, {**s, "site_id": s["pv_system_id"]})
                for s in raw]

    if vendor == "alsoenergy":
        # PowerTrack username+password lists every site on the login (no API
        # key / client_id). A named site_id still previews just that site.
        if str(config.get("site_id") or "").strip():
            return [_normalize_preview_site(vendor, mod.validate(config))]
        raw = mod.discover_sites(config)
        return [_normalize_preview_site(vendor, s) for s in raw]

    # SMA: single-system validate (account cascade arrives with the OAuth app).
    return [_normalize_preview_site(vendor, mod.validate(config))]


@router.post("/v1/array-owners/public/preview")
def public_solaredge_preview(body: PublicPreviewBody, request: Request) -> dict:
    """UNAUTHENTICATED pre-signup preview for ANY supported vendor: list the
    real sites a credential can read + a rough annual value, so a prospective
    owner sees THEIR arrays before signing up. Saves nothing. Rate-limited per IP.

    Accepts either {api_key} (legacy → SolarEdge) or {vendor, config}. Returns
    {ok, vendor, sites:[{site_id,name,peak_power_kw,annual_kwh,annual_value_usd}],
    totals, message}. Recoverable failures (bad creds, scope, empty, vendor
    unreachable) come back as ok:false + friendly message; rate limit → 429.
    Vendor 5xx/network never raise HTTP 502 here — that was Sentry noise and
    leaked upstream HTML into the client detail."""
    _guard_preview_oracle(request)

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
        # site_id optional: without it we list every site under the login.
        "locus": ["username", "password"],
        # pv_system_id optional: without it we list the whole account.
        "fronius": ["access_key_id", "access_key_value"],
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
    except InverterError:
        # Upstream 5xx/network is expected flakiness, not an app bug. Do NOT raise
        # HTTPException(502) — Sentry captures those as errors, and the old path
        # appended raw vendor HTML (CDN 502 pages) into the detail string.
        label = inverters.VENDORS[vendor].LABEL
        return {
            "ok": False,
            "vendor": vendor,
            "sites": [],
            "message": f"{label} is unreachable right now — try again in a few minutes.",
        }

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

            _peak = site.get("peak_power_kw")
            if target is not None:
                _attach_solaredge(db, target, api_key, sid, peak_power_kw=_peak)
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
                _attach_solaredge(db, new_arr, api_key, sid, peak_power_kw=_peak)
                used.add(new_arr.id)
                names_lower.add(name.lower())
                all_names_lower.add(name.lower())
                arr_by_id[new_arr.id] = new_arr
                entry["array_id"] = new_arr.id
                entry["name"] = new_arr.name
                created.append(entry)

            connected.append(entry)

            # Geocode the SolarEdge site address onto the array so the weather
            # model can run (SolarEdge's /sites/list gives a real address). Fills
            # only when the array has no location yet.
            _tgt = arr_by_id.get(entry["array_id"])
            if _tgt is not None and site.get("address"):
                _set_array_location(db, _tgt, address=site.get("address"),
                                    source_label="vendor:solaredge")

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
            f"{len(connected)} arrays connected: "
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
    username: str
    password: str
    # Optional: normally derived from the login; override only for a sub-partner.
    partner_id: Optional[int] = None


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
    _tenant = _tenant_from_bearer(authorization)
    _guard_vendor_discover(_tenant.id, "locus")

    creds = {"username": body.username, "password": body.password}
    if body.partner_id is not None:
        creds["partner_id"] = body.partner_id

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
    username: str
    password: str
    # Optional: normally derived from the login; override only for a sub-partner.
    partner_id: Optional[int] = None
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

    username = (body.username or "").strip()
    password = body.password or ""
    if not username or not password:
        raise HTTPException(400, "username and password are required")
    creds = {"username": username, "password": password}
    if body.partner_id is not None:
        creds["partner_id"] = body.partner_id

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
            f"{len(connected)} arrays connected: "
            f"{len(created)} new, {len(matched)} matched."
        ),
    }


# ── account-level Fronius discovery ("paste one key, attach all") ──────────────
# The Solar.web Query API's /pvsystems lists EVERY system the AccessKey can
# read (grounded live 2026-07-04 via scripts/verify_inverter_apis), so Fronius
# gets the same one-credential cascade as SolarEdge/Locus. pv_system_id is a
# STRING (UUID) — never coerce to int.

def _attach_fronius(db, arr: Array, keys: dict, pv_system_id: str) -> InverterConnection:
    """Upsert a fronius InverterConnection on `arr` WITHOUT a per-system
    validate call — the account key was already proven by discover_systems().
    No legacy-column mirroring (solaredge_* columns are SolarEdge-only)."""
    config = {
        "access_key_id": keys["access_key_id"],
        "access_key_value": keys["access_key_value"],
        "pv_system_id": str(pv_system_id),
    }
    conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == arr.id)
    ).scalar_one_or_none()
    if conn is None:
        conn = InverterConnection(
            array_id=arr.id, vendor="fronius", config=config, status="ok"
        )
        db.add(conn)
    else:
        conn.vendor = "fronius"
        conn.config = config
        conn.status = "ok"
        conn.last_error = None
    return conn


class FroniusDiscoverBody(BaseModel):
    access_key_id: str
    access_key_value: str


@router.post("/v1/array-owners/fronius/discover")
def fronius_discover(
    body: FroniusDiscoverBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Preview step: list every PV system this Solar.web AccessKey can read.

    Saves NOTHING — the dashboard shows the systems as checkboxes before the
    owner commits. A bad key (401/403) comes back as a 400 with a clear
    message; a Solar.web 5xx comes back as a 502.
    """
    _tenant = _tenant_from_bearer(authorization)
    _guard_vendor_discover(_tenant.id, "fronius")

    keys = {
        "access_key_id": (body.access_key_id or "").strip(),
        "access_key_value": (body.access_key_value or "").strip(),
    }
    if not (keys["access_key_id"] and keys["access_key_value"]):
        raise HTTPException(400, "access_key_id and access_key_value are required")

    try:
        systems = inverters.fronius.discover_systems(keys)
    except InverterAuthError as exc:
        raise HTTPException(400, str(exc))
    except InverterError as exc:
        raise HTTPException(502, f"Solar.web error: {exc}")

    return {
        "ok": True,
        "systems": systems,
        "message": None if systems else (
            "This AccessKey authenticates but has no PV systems attached. "
            "Generate the key from the Solar.web account that owns your "
            "systems (Settings → REST API → CREATE NEW KEY)."
        ),
    }


class FroniusConnectAccountBody(BaseModel):
    access_key_id: str
    access_key_value: str
    # When omitted, every discovered system is connected. STRING ids (UUIDs).
    pv_system_ids: Optional[list[str]] = None


@router.post("/v1/array-owners/fronius/connect-account")
def fronius_connect_account(
    body: FroniusConnectAccountBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Attach every (or a chosen subset of) Solar.web PV system on an
    AccessKey to the tenant's arrays in one shot.

    Per system: match an existing array by (1) its fronius InverterConnection
    pv_system_id, or (2) an EXACT case-insensitive name match — otherwise
    create a fresh Array (solar, no client). Idempotent: re-running updates
    the same arrays instead of duplicating them.

    Returns {connected, created, matched} so the UI can celebrate specifics.
    """
    tenant = _tenant_from_bearer(authorization)

    keys = {
        "access_key_id": (body.access_key_id or "").strip(),
        "access_key_value": (body.access_key_value or "").strip(),
    }
    if not (keys["access_key_id"] and keys["access_key_value"]):
        raise HTTPException(400, "access_key_id and access_key_value are required")

    requested: set[str] | None = None
    if body.pv_system_ids is not None:
        requested = {str(s) for s in body.pv_system_ids}

    # 1. Discover the key's systems.
    try:
        discovered = inverters.fronius.discover_systems(keys)
    except InverterAuthError as exc:
        raise HTTPException(400, str(exc))
    except InverterError as exc:
        raise HTTPException(502, f"Solar.web error: {exc}")

    # 2. Narrow to the requested subset.
    if requested is not None:
        discovered = [s for s in discovered if str(s["pv_system_id"]) in requested]

    if not discovered:
        return {
            "ok": True,
            "connected": [], "created": [], "matched": [],
            "message": "No Fronius PV systems to connect.",
        }

    # 3. Attach each system to an array (match existing or create new).
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

        by_system_id: dict[str, list[int]] = defaultdict(list)
        by_name: dict[str, list[int]] = defaultdict(list)
        names_lower: set[str] = set()
        # uq_array_per_tenant spans soft-deleted rows — the new-array collision
        # guard must check ALL names (live + soft-deleted) or a system colliding
        # with a deleted array's name slips through → INSERT → UniqueViolation.
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
            if c is not None and c.vendor == "fronius":
                sid = (c.config or {}).get("pv_system_id")
                if sid:
                    by_system_id[str(sid)].append(a.id)

        used: set[int] = set()

        for system in discovered:
            sid = str(system["pv_system_id"])
            sys_name = (system.get("name") or "").strip() or f"PV system {sid}"
            entry = {
                "array_id": None,
                "name": sys_name,
                "pv_system_id": sid,
                "peak_power_kw": system.get("peak_power_kw"),
            }

            claimants = [aid for aid in by_system_id.get(sid, []) if aid not in used]
            if len(claimants) > 1:
                # Pre-existing data integrity problem — don't silently pick one.
                raise HTTPException(
                    409,
                    f"Two arrays already claim Fronius system {sid} "
                    f"(arrays {sorted(claimants)}). Resolve the duplicate before "
                    "connecting this account.",
                )

            target = None
            if claimants:
                target = arr_by_id[claimants[0]]
            else:
                name_hits = [
                    aid for aid in by_name.get(sys_name.lower(), [])
                    if aid not in used
                ]
                # Exact, unambiguous name match only — never guess fuzzily.
                if len(name_hits) == 1:
                    target = arr_by_id[name_hits[0]]

            if target is not None:
                _attach_fronius(db, target, keys, sid)
                used.add(target.id)
                entry["array_id"] = target.id
                entry["name"] = target.name
                matched.append(entry)
            else:
                name = sys_name
                if name.lower() in all_names_lower:
                    # Disambiguate so uq_array_per_tenant holds (includes
                    # soft-deleted arrays, whose names still reserve the slot).
                    name = f"{sys_name} ({sid[:8]})"
                new_arr = Array(
                    tenant_id=tenant.id, name=name, client_id=None, fuel_type="solar",
                )
                db.add(new_arr)
                db.flush()
                _attach_fronius(db, new_arr, keys, sid)
                used.add(new_arr.id)
                names_lower.add(name.lower())
                all_names_lower.add(name.lower())
                arr_by_id[new_arr.id] = new_arr
                entry["array_id"] = new_arr.id
                entry["name"] = new_arr.name
                created.append(entry)

            connected.append(entry)

            # Geocode the Solar.web address onto the array so the weather model
            # can run. Fills only when the array has no location yet.
            _tgt = arr_by_id.get(entry["array_id"])
            if _tgt is not None and system.get("address"):
                _set_array_location(db, _tgt, address=system.get("address"),
                                    source_label="vendor:fronius")

        db.commit()

        # Self-healing: deep multi-year history backfill for every array we just
        # attached so past years populate in Trends within minutes. Best-effort;
        # the scheduled healer covers any that don't stamp.
        for _e in connected:
            if _e.get("array_id"):
                _trigger_history_backfill(db, _e["array_id"])

    return {
        "ok": True,
        "connected": connected,
        "created": created,
        "matched": matched,
        "message": (
            f"{len(connected)} arrays connected: "
            f"{len(created)} new, {len(matched)} matched."
        ),
    }


# ── AlsoEnergy (PowerTrack) — one login, every site ───────────────────────────
# Clean REST API (api.alsoenergy.com OAuth password grant). NOT extension scrape:
# username+password mint a bearer and GET /Sites lists the whole account. Same
# "paste one credential, attach all arrays" shape as SolarEdge/Fronius/Locus.

class AlsoEnergyConnectAccountBody(BaseModel):
    username: str
    password: str
    # When omitted, every discovered site is connected.
    site_ids: Optional[list[int]] = None


def _attach_alsoenergy(db, arr: Array, creds: dict, site_id: int) -> None:
    """Upsert an AlsoEnergy InverterConnection for one site on `arr`."""
    config = {
        "username": creds["username"],
        "password": creds["password"],
        "site_id": int(site_id),
    }
    _connect_inverter(db, arr, "alsoenergy", config)


@router.post("/v1/array-owners/alsoenergy/connect-account")
def alsoenergy_connect_account(
    body: AlsoEnergyConnectAccountBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Attach every (or a chosen subset of) AlsoEnergy / PowerTrack sites under
    one portal login to the tenant's arrays.

    Per site: match an existing array by (1) its alsoenergy InverterConnection
    site_id, or (2) an EXACT case-insensitive name match — otherwise create a
    fresh Array (solar, no client). Idempotent: re-running updates the same
    arrays instead of duplicating them.

    Returns {connected, created, matched} so the UI can celebrate specifics.
    """
    tenant = _tenant_from_bearer(authorization)

    username = (body.username or "").strip()
    password = body.password or ""
    if not username or not password:
        raise HTTPException(400, "username and password are required")

    creds = {"username": username, "password": password}

    requested: set[int] | None = None
    if body.site_ids is not None:
        requested = {int(s) for s in body.site_ids}

    try:
        discovered = inverters.alsoenergy.discover_sites(creds)
    except InverterScopeError as exc:
        raise HTTPException(400, str(exc))
    except InverterAuthError as exc:
        raise HTTPException(400, str(exc))
    except InverterError as exc:
        raise HTTPException(502, f"AlsoEnergy error: {exc}")

    if requested is not None:
        discovered = [s for s in discovered if int(s["site_id"]) in requested]

    if not discovered:
        return {
            "ok": True,
            "connected": [], "created": [], "matched": [],
            "message": "No AlsoEnergy sites to connect.",
        }

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
            if c is not None and c.vendor == "alsoenergy":
                sid = (c.config or {}).get("site_id")
                if sid is not None:
                    try:
                        by_site_id[int(sid)].append(a.id)
                    except (TypeError, ValueError):
                        pass

        used: set[int] = set()

        for site in discovered:
            sid = int(site["site_id"])
            site_name = (site.get("name") or "").strip() or f"AlsoEnergy site {sid}"
            entry = {
                "array_id": None,
                "name": site_name,
                "site_id": sid,
                "peak_power_kw": site.get("peak_power_kw"),
            }

            claimants = [aid for aid in by_site_id.get(sid, []) if aid not in used]
            if len(claimants) > 1:
                raise HTTPException(
                    409,
                    f"Two arrays already claim AlsoEnergy site {sid} "
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
                if len(name_hits) == 1:
                    target = arr_by_id[name_hits[0]]

            if target is not None:
                _attach_alsoenergy(db, target, creds, sid)
                used.add(target.id)
                entry["array_id"] = target.id
                entry["name"] = target.name
                matched.append(entry)
            else:
                name = site_name
                if name.lower() in all_names_lower:
                    name = f"{site_name} ({sid})"
                new_arr = Array(
                    tenant_id=tenant.id, name=name, client_id=None, fuel_type="solar",
                )
                db.add(new_arr)
                db.flush()
                _attach_alsoenergy(db, new_arr, creds, sid)
                used.add(new_arr.id)
                names_lower.add(name.lower())
                all_names_lower.add(name.lower())
                arr_by_id[new_arr.id] = new_arr
                entry["array_id"] = new_arr.id
                entry["name"] = new_arr.name
                created.append(entry)

            connected.append(entry)

        db.commit()

        for _e in connected:
            if _e.get("array_id"):
                _trigger_history_backfill(db, _e["array_id"])

    return {
        "ok": True,
        "connected": connected,
        "created": created,
        "matched": matched,
        "message": (
            f"{len(connected)} arrays connected: "
            f"{len(created)} new, {len(matched)} matched."
        ),
    }


# ── single-system connect (Fronius / SMA / any one-system vendor) ──────────────
# SMA has no account-level discovery here yet (per-system creds + owner consent);
# Fronius NOW HAS the full cascade above (/fronius/discover + connect-account) —
# AlsoEnergy has /alsoenergy/connect-account (username+password → every site).
# this endpoint remains as the manual one-system path for SMA and for a Fronius
# key the owner wants attached to a single named system. It validates the one
# named system and attaches it to a matched-or-created array, mirroring
# connect-account's match/create behavior.

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
    # inverter (Chint's commDevice.currentPower; Fronius's REALTIME endpoint
    # GetActualPvSystemData → series[].data[].custom.power). When absent the
    # backend allocates the site total across inverters by energy share (ingest).
    current_power_w: Optional[float] = None
    # The SOURCE's own timestamp for current_power_w (Fronius realtime
    # FormatedDateTimeStamp). Drives source_last_data_at so the freshness/“source
    # offline” signal reflects the real feed, not our capture time. Falls back to
    # the site's last_report when absent.
    last_report: Optional[str] = None
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
    # Site location captured from the vendor portal so the weather model can run
    # without a utility service address. Either coordinates OR a geocodable address;
    # the ingest fills Array.latitude/longitude (geocoding the address if needed).
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    address: Optional[str] = None
    # Optional site-level daily-kWh history for instant graph backfill on connect
    # (Chint weekETrend → ~7 days). Site-level only; never split per inverter.
    daily: list[CaptureDaily] = []
    inverters: list[CaptureInverter] = []


class InverterCaptureBody(BaseModel):
    # Vendors that ship readings via the extension rather than a pullable key.
    # Constrained so a bad/cred-bearing vendor can't sneak in through this path.
    provider: str
    sites: list[CaptureSite]


def _parse_src_ts(s):
    """Parse a SOURCE last-data timestamp (ISO string from the extension) into a
    naive-UTC datetime, matching how last_power_at is stored. Returns None on a
    missing/garbage value — we never fabricate a source time."""
    if not s:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    # Reject a garbage epoch/pre-2015 value (e.g. a missing/zero SMA gauge reading
    # that serialized as 1970-01-01) — storing it would surface as "20628 days ago"
    # + a false SOURCE-OFFLINE banner. Never fabricate a source time; treat it as absent.
    if dt.year < 2015:
        return None
    return dt


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

# When per-inverter integrated energy is < this fraction of site EnergyToday,
# re-allocate the site total across inverters (Fronius Waterford sparkline bug).
_FRONIUS_INV_SUM_SITE_RATIO = 0.50


def _rebalance_fronius_inverter_dailies(
    db,
    *,
    tenant_id: str,
    array_id: int,
    site_days: dict,
    inv_rows: list,
) -> int:
    """If sum(InverterDaily) << site DailyGeneration for a day, allocate site kWh.

    Returns number of inverter-day rows written/updated.
    """
    if not site_days or not inv_rows:
        return 0
    inv_ids = [iv.id for iv in inv_rows]
    # Nameplate weights (heal missing nameplate from model string)
    from .inverter_fleet import _nameplate_from_model
    weights: list[tuple] = []
    for iv in inv_rows:
        np = iv.nameplate_kw
        if not np and (iv.model or iv.name):
            np = _nameplate_from_model("fronius", iv.model or iv.name or "")
            if np and not iv.nameplate_kw:
                iv.nameplate_kw = np
        weights.append((iv, float(np) if np and np > 0 else 1.0))
    wsum = sum(w for _, w in weights) or float(len(weights))
    n_fixed = 0
    for day, site_kwh in site_days.items():
        if not site_kwh or site_kwh <= 0:
            continue
        existing = {
            r.inverter_id: r for r in db.execute(
                select(InverterDaily).where(
                    InverterDaily.inverter_id.in_(inv_ids),
                    InverterDaily.day == day,
                )
            ).scalars().all()
        }
        inv_sum = sum(float(r.kwh or 0) for r in existing.values())
        if inv_sum >= float(site_kwh) * _FRONIUS_INV_SUM_SITE_RATIO:
            continue  # inverter series already agrees with site
        # Allocate site total by nameplate share
        for iv, w in weights:
            share = float(site_kwh) * (w / wsum)
            # Cap at nameplate×24
            cap = (float(iv.nameplate_kw) * 24.0) if iv.nameplate_kw else _ABS_INVERTER_DAILY_KWH_CEILING
            share = min(share, cap)
            share = round(share, 3)
            row = existing.get(iv.id)
            if row is None:
                _insert_inverter_daily_race_safe(
                    db, tenant_id=tenant_id, inverter_id=iv.id,
                    day=day, kwh=share)
                n_fixed += 1
            elif share > float(row.kwh or 0):
                row.kwh = share
                row.uploaded_at = now()
                n_fixed += 1
        if n_fixed:
            log.info(
                "inverter-capture: rebalanced Fronius array=%s day=%s site=%.1f "
                "inv_sum_was=%.1f → allocated by nameplate across %d units",
                array_id, day, site_kwh, inv_sum, len(weights),
            )
    return n_fixed


# ── race-safe daily upserts (2026-07-04, Anna's uq_daily_array_day 500) ────────
# The capture handler reads existing (array, day) rows and then inserts the
# missing ones — safe within one request, but TWO CONCURRENT captures for the
# same array (the 6-min live loop overlapping a manual sync, or two open tabs)
# can both pass that read before either commits: the loser's INSERT then hits
# uq_daily_array_day and 500s the whole capture (seen live: tenant ten_anna_800,
# array 2416). These helpers make the insert race-safe: try the INSERT inside a
# SAVEPOINT; if a concurrent request won, roll back to the savepoint, re-read
# the row the winner wrote, and apply the same max-wins update the fresh-read
# path would have applied. Dialect-agnostic (savepoints work on PG + sqlite).

def _insert_daily_generation_race_safe(db, *, tenant_id: str, array_id: int,
                                       day: date, kwh: float,
                                       source: str = "extension_pull") -> bool:
    """Insert an array-day generation row the caller believes is missing.
    Returns True when the value landed (insert OR losing-the-race update).

    `source` defaults to extension_pull (inverter-capture). utility-meter-capture
    passes source='utility_meter' / 'bill_prorate' so a concurrent winner's row
    is refreshed with the same rules as the non-race update path (never clobber
    a measured reading; bill_prorate only fills None/bill_prorate gaps).
    """
    try:
        with db.begin_nested():
            db.add(DailyGeneration(tenant_id=tenant_id, array_id=array_id,
                                   day=day, kwh=kwh, source=source))
            db.flush()
        return True
    except IntegrityError:
        row = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == array_id,
                DailyGeneration.day == day,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        if source == "extension_pull":
            if kwh > (row.kwh or 0):
                row.kwh = kwh
                row.source = source
                row.uploaded_at = now()
                return True
            return False
        if source == "utility_meter":
            # Mirrors _persist_meter_accounts non-race branch.
            if not generation_sources.is_measured(row.source):
                row.kwh = kwh
                row.source = source
                row.uploaded_at = now()
                return True
            return False
        if source == "bill_prorate":
            if row.source is None or row.source == "bill_prorate":
                row.kwh = kwh
                row.source = source
                row.uploaded_at = now()
                return True
            return False
        return False


def _insert_inverter_daily_race_safe(db, *, tenant_id: str, inverter_id: int,
                                     day: date, kwh: float) -> bool:
    """Same race-safe insert for per-inverter days (uq_inverter_daily_inv_day)."""
    try:
        with db.begin_nested():
            db.add(InverterDaily(tenant_id=tenant_id, inverter_id=inverter_id,
                                 day=day, kwh=kwh, source="extension_pull"))
            db.flush()
        return True
    except IntegrityError:
        row = db.execute(
            select(InverterDaily).where(
                InverterDaily.inverter_id == inverter_id,
                InverterDaily.day == day,
            )
        ).scalar_one_or_none()
        if row is not None and kwh > (row.kwh or 0):
            row.kwh = kwh
            row.uploaded_at = now()
            return True
        return False


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

    out = _inverter_capture_for_tenant(tenant, provider, body)

    # FAN-OUT (feature-flagged, default OFF): if this tenant is cross-product
    # LINKED, replay the SAME inverter payload into the sibling via the SAME
    # write-logic — identical dup-safe helpers, so both products' tenants get the
    # arrays + daily rows from one install. Sibling failure never breaks this
    # capture (primary already committed inside _inverter_capture_for_tenant).
    from .capture_fanout import fanout
    fanout(tenant, lambda sib: _inverter_capture_for_tenant(sib, provider, body))

    return out


def _inverter_capture_for_tenant(tenant: Tenant, provider: str, body: "InverterCaptureBody") -> dict:
    """The inverter-capture write-body, parameterized on the target tenant so it
    serves BOTH the primary capture and the linked-sibling fan-out (capture_fanout
    replays the same payload through this exact function). Opens its own session,
    matches/creates arrays, upserts DailyGeneration / Inverter / InverterDaily
    with the dup-safe helpers, commits, and returns the per-site summary."""
    # PRODUCT GUARD (Ford 2026-07-16): inverter/vendor telemetry (Fronius/Chint/SMA)
    # belongs to ARRAY OPERATOR only. NEPOOL Operator builds its Client→Array graph
    # exclusively from UTILITY data (GMP/SmartHub bills via /v1/sync +
    # utility-meter-capture). This write-body creates Array rows from vendor sites
    # (see _safe_create_array below), so if a nepool tenant ever receives an
    # inverter capture — extension OR the Cloud-Capture harvester scraping a stored
    # inverter credential — it would pollute NEPOOL's arrays with vendor-derived
    # solar arrays. Refuse the vendor→array write for non-AO tenants. Guarding the
    # shared write-body (not just the endpoint) also covers the sibling fan-out.
    from .stripe_helpers import is_array_operator  # noqa: PLC0415
    if not is_array_operator(getattr(tenant, "product", None)):
        log.info(
            "inverter-capture: REFUSED vendor->array write for non-AO tenant=%s "
            "product=%s provider=%s sites=%d (NEPOOL uses utility data only)",
            tenant.id, getattr(tenant, "product", None), provider, len(body.sites),
        )
        return {"ok": True, "skipped": "not_array_operator",
                "results": [], "sites": []}
    # Observability: which tenant this capture wrote to + per-site device counts.
    # Makes "captured but nothing shows" trivially diagnosable (is it the wrong
    # tenant? a 0-device payload?). Counts only — no serials/PII.
    log.info(
        "inverter-capture: tenant=%s provider=%s sites=%d per_site_devices=%s",
        tenant.id, provider, len(body.sites),
        [(str(s.site_id), (s.name or "")[:24],
          len([c for c in (s.inverters or []) if str(c.serial or "").strip()]))
         for s in body.sites],
    )

    # FLEET-LOCAL day bucket (billing-critical). energy_today_kwh is the
    # portal's LOCAL (US/Eastern) day total; keying it by utcnow().date()
    # wrote every 8pm–midnight ET capture into TOMORROW's slot, and the
    # climb-only upsert below then kept yesterday's larger total over a
    # cloudier real today — double-counted kWh straight into the Stripe
    # per-kWh meter. See models.local_today.
    today = local_today()
    results: list[dict] = []

    with SessionLocal() as db:
        # Match by name across ALL arrays, including soft-deleted ones:
        # Match by name across ALL arrays (a soft-deleted twin can be revived on
        # reuse). Names are unique only among LIVE rows now (partial index
        # uq_array_per_tenant_live), so a live + soft-deleted array can share a
        # name — always prefer the LIVE one so it isn't shadowed by a ghost.
        existing = db.execute(
            select(Array).where(Array.tenant_id == tenant.id)
        ).scalars().all()
        by_name = {}
        for _a in existing:
            _k = _a.name.strip().lower()
            if _k not in by_name or (by_name[_k].deleted_at is not None
                                     and _a.deleted_at is None):
                by_name[_k] = _a
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
                arr, created = _safe_create_array(
                    db, tenant.id, site_name, client_id=None, fuel_type="solar")
                by_name[site_name.lower()] = arr
                by_id[arr.id] = arr
                if site_key:
                    by_site_id[site_key] = arr
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

            # CROSS-VENDOR SPLIT (fixes "my CHINT inverters never show up"). A
            # capture can name-match an EXISTING array that already holds ANOTHER
            # vendor's inverters — e.g. a CHINT site called "Londonderry" matching a
            # SolarEdge "Londonderry" array — and merge into it, burying this
            # vendor's inverters inside a foreign array so the owner never sees them
            # as their own. If the matched array already holds OTHER-vendor
            # inverters, peel THIS vendor into its own "<name> (<Vendor>)" array and
            # move any of this vendor's inverters already stranded in the mixed array
            # over to it — healing the bad merge on the next capture. Single-vendor
            # arrays (the norm) never trigger this.
            if arr is not None and not created:
                _other = db.execute(
                    select(Inverter.id).where(
                        Inverter.array_id == arr.id,
                        Inverter.deleted_at.is_(None),
                        Inverter.vendor != provider,
                    ).limit(1)
                ).first()
                if _other is not None:
                    _vname = f"{arr.name} ({provider.capitalize()})"
                    _varr = by_name.get(_vname.strip().lower())
                    if _varr is None:
                        _varr, _ = _safe_create_array(
                            db, tenant.id, _vname,
                            client_id=arr.client_id,
                            fuel_type=getattr(arr, "fuel_type", None) or "solar")
                        by_name[_vname.strip().lower()] = _varr
                        by_id[_varr.id] = _varr
                    elif _varr.deleted_at is not None:
                        _varr.deleted_at = None
                    for _iv in db.execute(
                        select(Inverter).where(
                            Inverter.array_id == arr.id,
                            Inverter.deleted_at.is_(None),
                            Inverter.vendor == provider,
                        )
                    ).scalars().all():
                        _iv.array_id = _varr.id
                        _iv.source_array_id = _varr.id
                    db.flush()
                    if site_key:
                        by_site_id[site_key] = _varr
                    arr = _varr

            # Vendor-captured site location → the weather model can run without a
            # utility service address (the whole reason inverter-onboarded Chint/
            # Fronius/SMA/SolarEdge arrays read "not modeled yet"). Fills only when
            # the array has no location yet, so a manual override always wins.
            if site.latitude is not None or site.longitude is not None or site.address:
                _set_array_location(db, arr, lat=site.latitude, lng=site.longitude,
                                    address=site.address, source_label="vendor:" + provider)

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
                        # Race-safe: a CONCURRENT capture may insert this
                        # (array, day) between our read and this insert — the
                        # helper falls back to the winner's row with the same
                        # max-wins update instead of 500ing the whole capture.
                        if _insert_daily_generation_race_safe(
                                db, tenant_id=tenant.id, array_id=arr.id,
                                day=d, kwh=v):
                            backfilled_days += 1
                    elif v > (drow.kwh or 0):   # climbs through the day / never regresses
                        drow.kwh = v
                        drow.source = "extension_pull"
                        drow.uploaded_at = now()
                        backfilled_days += 1
            db.flush()  # surface this site's daily rows so a sibling site on the SAME array updates them, never racing a duplicate (uq_daily_array_day)

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
                # Never persist a non-inverter device (SMA Data Manager, meter, …)
                # as an inverter — it would show as a permanently-0 kW phantom.
                if _is_non_inverter_device(getattr(ci, "name", None), getattr(ci, "model", None)):
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
                    try:
                        with db.begin_nested():   # SAVEPOINT around just this insert
                            db.add(iv)
                            db.flush()
                    except IntegrityError:
                        # A concurrent capture (the 6-min live loop overlapping a
                        # manual sync, or two open tabs) inserted this
                        # (tenant, vendor, serial) first, hitting
                        # uq_inverter_tenant_vendor_serial and 500-ing the whole
                        # capture. Roll back to the savepoint, re-read the winner's
                        # row and keep going — mirrors _insert_daily_generation_race_safe.
                        iv = db.execute(
                            select(Inverter).where(
                                Inverter.tenant_id == tenant.id,
                                Inverter.vendor == provider,
                                Inverter.serial == serial,
                            )
                        ).scalar_one()
                        if iv.deleted_at is not None:
                            iv.deleted_at = None
                        if iv.source_array_id is None:
                            iv.source_array_id = arr.id
                else:
                    # Refresh source pointer; NEVER clobber a VALID owner grouping.
                    iv.source_site_id = site.site_id
                    if iv.source_array_id is None:
                        iv.source_array_id = arr.id
                    if iv.deleted_at is not None:
                        iv.deleted_at = None
                    # HEAL AN ORPHANED OWNER LINK. If this inverter's array is gone —
                    # soft-deleted, missing, or in a DIFFERENT tenant (possible from
                    # earlier cross-tenant test data) — its telemetry is live but it
                    # never shows in the sandbox because the array it points at isn't
                    # rendered. Re-link it to THIS capture's live array so it reappears.
                    # A live, in-tenant array is left untouched (respect the owner's
                    # deliberate drag-to-regroup).
                    _cur = db.get(Array, iv.array_id) if iv.array_id else None
                    if _cur is None or _cur.deleted_at is not None or _cur.tenant_id != tenant.id:
                        _maxpos = db.execute(
                            select(Inverter.position).where(
                                Inverter.tenant_id == tenant.id,
                                Inverter.array_id == arr.id,
                                Inverter.deleted_at.is_(None),
                            ).order_by(Inverter.position.desc())
                        ).scalars().first()
                        iv.array_id = arr.id
                        iv.source_array_id = arr.id
                        iv.position = (_maxpos or 0) + 1
                iv.name = ci.name or iv.name or serial
                iv.model = ci.model or iv.model
                if ci.nameplate_kw is not None:
                    iv.nameplate_kw = ci.nameplate_kw
                iv.last_seen_at = now()
                # SOURCE's own last-data timestamp (Fronius LastImport / SMA reading ts):
                # WHEN the inverter last reported to its vendor portal — the real freshness
                # signal, kept distinct from our capture time (last_power_at). Prefer a
                # per-inverter ts if the device carried one, else the site's. Only ever
                # advances on a genuine source timestamp; never guessed.
                _src_ts = _parse_src_ts(getattr(ci, "last_report", None)) or _parse_src_ts(site.last_report)
                if _src_ts is not None:
                    iv.source_last_data_at = _src_ts

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
                    # A genuine per-device reading — trustworthy for live anomaly
                    # detection (this device really reported this wattage).
                    iv.last_power_estimated = False
                elif site_power_w is not None and _site_invs:
                    if _energy_sum > 0:
                        e = float(ci.energy_today_kwh) if (ci.energy_today_kwh and ci.energy_today_kwh > 0) else 0.0
                        allocated = round(site_power_w * (e / _energy_sum), 1)
                    elif _np_sum > 0 and ci.nameplate_kw:
                        allocated = round(site_power_w * (float(ci.nameplate_kw) / _np_sum), 1)
                    else:
                        allocated = round(site_power_w / len(_site_invs), 1)
                    # Skip sub-floor allocations: a per-inverter allocation below the
                    # frontend's production floor (max(25W, nameplate×1%)) would stamp
                    # a "producing" value that the card renders as OFFLINE. A genuine
                    # low reading (clouds, startup) will record correctly on the next
                    # capture; meanwhile leaving last_power_w null is more honest than
                    # writing 4W that permanently says "offline" until the 24h window expires.
                    _inv_floor = max(25.0, float(iv.nameplate_kw or ci.nameplate_kw or 0) * 10)
                    if allocated >= _inv_floor:
                        iv.last_power_w = allocated
                        iv.last_power_at = now()
                        # This device exposed no per-unit reading — the value is a
                        # split of the site total, NOT a measurement of THIS inverter.
                        # Mark it so the live-anomaly detectors never treat it as
                        # evidence (a partial Fronius capture that fills some units
                        # this way must not fabricate a "dark" verdict on the rest).
                        iv.last_power_estimated = True

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
                            _insert_inverter_daily_race_safe(
                                db, tenant_id=tenant.id, inverter_id=iv.id,
                                day=dd, kwh=v)
                        elif v > (row.kwh or 0):         # climbs through the day / never regresses
                            row.kwh = v
                            row.uploaded_at = now()
                inv_persisted += 1

            # ── Site/inverter energy rebalance (Fronius Waterford class) ─────
            # Site DailyGeneration often comes from Solar.web EnergyTodayInkWh
            # (authoritative kWh). Per-inverter InverterDaily comes from integrating
            # the devwork power chart — which freezes low on morning-only harvests
            # and used to miss watts→kW on history backfill. Result: array "Today"
            # looks right (hundreds of kWh) while every Primo sparkline is a flat
            # ~5 kWh stub. When the inverter sum is far below the site day, allocate
            # the site total across inverters by nameplate so sparklines match
            # reality. Never invent energy above the site total; never shrink a
            # higher already-correct inverter sum.
            if provider == "fronius" and want_arr:
                _db_invs = db.execute(
                    select(Inverter).where(
                        Inverter.array_id == arr.id,
                        Inverter.deleted_at.is_(None),
                        Inverter.vendor == "fronius",
                    )
                ).scalars().all()
                if _db_invs:
                    _rebalance_fronius_inverter_dailies(
                        db, tenant_id=tenant.id, array_id=arr.id,
                        site_days=want_arr, inv_rows=_db_invs,
                    )

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
    auth: Optional[dict] = None   # {apiToken, apiTokenExpires?, refreshToken?} → UtilitySession


# Utilities allowed to ingest GENERATION via the meter-capture path. Kept
# separate from _CAPTURE_VENDORS (inverter vendors) — utilities are adapters.
#   gmp = Green Mountain Power (bespoke REST API, carries a GMP-style summary).
#   vec = Vermont Electric Coop, wec = Washington Electric Coop (both NISC
#   SmartHub — the extension supplies daily[] generation directly, no GMP summary).
#   eversource* = Eversource Energy CT/MA/NH (bespoke MyAccount; Cloud Capture).
#   cmp = Central Maine Power (Avangrid portal; Cloud Capture).
_UTILITY_CAPTURE_VENDORS = {
    "gmp", "vec", "wec",
    "eversource", "eversource_ma", "eversource_ct",
    "cmp",
}

# Human label per utility for the default array name when no nickname is given.
_UTILITY_LABEL = {
    "gmp": "GMP", "vec": "VEC", "wec": "WEC",
    "eversource": "Eversource",
    "eversource_ma": "Eversource",
    "eversource_ct": "Eversource",
    "cmp": "CMP",
}


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

    out = _utility_meter_capture_for_tenant(tenant, provider, body)

    # FAN-OUT (feature-flagged, default OFF): replay the SAME utility-meter
    # payload (generation rows + the auth session that powers autonomous bill
    # pulls) into the cross-product LINKED sibling, via the SAME write-logic.
    # Both NEPOOL and AO consume utility generation/bills, so one install feeds
    # both. Sibling failure never breaks this capture.
    from .capture_fanout import fanout
    fanout(tenant, lambda sib: _utility_meter_capture_for_tenant(sib, provider, body))

    return out


def _utility_meter_capture_for_tenant(
    tenant: Tenant, provider: str, body: "UtilityMeterCaptureBody"
) -> dict:
    """The utility-meter-capture write-body, parameterized on the target tenant so
    it serves BOTH the primary capture and the linked-sibling fan-out. Persists
    generation, upserts the UtilitySession, and kicks the same best-effort bill
    absorb — identical for primary and sibling."""
    # Observability: record whether the client forwarded auth, so a "bills won't
    # pull" report can be diagnosed without guessing (old extension / stale page
    # JS both silently drop auth → no UtilitySession → no bill pull).
    _auth_present = body.auth is not None
    _auth_has_token = bool(body.auth and body.auth.get("apiToken"))
    log.info(
        "meter-capture: tenant=%s provider=%s accounts=%d auth_present=%s has_token=%s",
        tenant.id, provider, len(body.accounts), _auth_present, _auth_has_token,
    )

    with SessionLocal() as db:
        results = _persist_meter_accounts(db, tenant, provider, body.accounts)

        # Store the auth session when the extension passes it so the scheduler
        # can pull GMP bills autonomously. Without this, accounts exist but no
        # UtilitySession is ever stored (the AO capture path never called /v1/sync).
        session_stored = False
        if body.auth and body.auth.get("apiToken"):
            from .sessions import session_customer_number
            api_token = body.auth["apiToken"]
            refresh_token = body.auth.get("refreshToken")
            expires_at = None
            raw_expires = body.auth.get("apiTokenExpires")
            if raw_expires:
                try:
                    expires_at = datetime.fromisoformat(
                        str(raw_expires).replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except Exception:
                    pass
            acct_dicts = [
                {"customer_number": a.summary.get("customerNumber")}
                for a in body.accounts
            ]
            session_cust = session_customer_number(acct_dicts)
            cust_predicate = (
                UtilitySession.customer_number == session_cust
                if session_cust is not None
                else UtilitySession.customer_number.is_(None)
            )
            existing = db.execute(
                select(UtilitySession)
                .where(
                    UtilitySession.tenant_id == tenant.id,
                    UtilitySession.provider == provider,
                    cust_predicate,
                )
                .order_by(UtilitySession.captured_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                existing.api_token = api_token
                existing.refresh_token = refresh_token
                existing.expires_at = expires_at
                existing.captured_at = now()
                existing.refresh_failures = 0
            else:
                db.add(UtilitySession(
                    tenant_id=tenant.id,
                    provider=provider,
                    customer_number=session_cust,
                    api_token=api_token,
                    refresh_token=refresh_token,
                    expires_at=expires_at,
                ))
            session_stored = True

        db.commit()

    # With a fresh session on file, kick off an immediate bill absorb so the
    # owner's bills appear within minutes instead of waiting up to 6h for the
    # scheduler tick. This is the SAME data-sponge /v1/sync fires on GMP capture:
    # it absorbs the FULL energy record (consumption, cost, credits) for EVERY
    # enabled account — solar or not — so non-solar GMP accounts get bills too.
    # Best-effort, daemon thread, never blocks or fails the capture response.
    if session_stored:
        try:
            from threading import Thread
            _tid = tenant.id
            _prov = provider
            def _bg_absorb(tid: str, prov: str):
                try:
                    if prov == "gmp":
                        from .sponge import absorb_history
                        absorb_history(tid, "gmp")
                    else:
                        from .worker import pull_bills_for_tenant
                        pull_bills_for_tenant(tid)
                except Exception:
                    log.exception("post-meter-capture bill absorb failed for %s", tid)
            Thread(target=_bg_absorb, args=(_tid, _prov), daemon=True).start()
        except Exception:
            log.exception("failed to kick off post-meter-capture bill absorb")

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
    # Names are unique only among LIVE rows now (partial index), so a live +
    # soft-deleted array can share a name — always prefer the LIVE one so it
    # isn't shadowed by a ghost.
    existing = db.execute(
        select(Array).where(Array.tenant_id == tenant.id)
    ).scalars().all()
    by_name = {}
    for _a in existing:
        _k = _a.name.strip().lower()
        if _k not in by_name or (by_name[_k].deleted_at is not None
                                 and _a.deleted_at is None):
            by_name[_k] = _a

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

            # The "no array for non-solar accounts" gate is GMP-SPECIFIC: a GMP login
            # carries dozens of homes/pumps/meters with no solar, and we must not bury
            # the real arrays under empty ones. A SmartHub (VEC/WEC) connect is the
            # operator's OWN solar account — and VEC net-metering "credit" accounts
            # routinely read 0 kWh in SmartHub's usage explorer yet are a real array
            # the operator needs to bill an offtaker from. So for SmartHub we STILL
            # create the bindable UtilityAccount + Array even with no generation yet
            # (it appears in the offtaker picker immediately; generation attaches later
            # via the SmartHub pull and the offtaker invoice WAITS for it). Only GMP
            # skips a no-generation account. (Was: every provider skipped → a linked
            # VEC account never reached the picker — Ford: "I don't see Glover.")
            if not has_any_generation and not is_smarthub_provider(provider):
                # No solar production → create NO Array, NO Bill, NO generation rows,
                # so the inverter dashboard isn't buried under empty non-solar arrays
                # (a GMP login carries dozens of homes/pumps/meters). BUT still create
                # a bindable UtilityAccount so EVERY GMP account the owner has appears
                # in the offtaker "choose utility account" dropdown — Ford, for Bruce:
                # "pull ALL the utility bills so I can see them in the dropdown." A
                # non-solar account just has no bill to invoice from yet (the picker
                # shows "no bill on file yet"; the offtaker invoice WAITS until
                # generation/bills land — it never mis-bills). This decouples dropdown
                # visibility (UtilityAccount) from dashboard arrays (Array): the gate
                # used to skip the UtilityAccount too, so non-solar accounts vanished.
                ua_id = None
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
                            account_number=acct_num, array_id=None,
                            nickname=(acct.nickname or "").strip() or None,
                        )
                        db.add(ua)
                        db.flush()
                    else:
                        # Never steal an existing array link (a once-solar account
                        # that momentarily reads 0) — only revive + refresh metadata.
                        if ua.deleted_at is not None:
                            ua.deleted_at = None
                        if not ua.nickname and (acct.nickname or "").strip():
                            ua.nickname = (acct.nickname or "").strip()
                        # Throttled. This function does NOT commit — the caller
                        # owns the transaction — so every row it stamps stays
                        # locked for the whole ingest, which is what starved
                        # POST /v1/sync into LockNotAvailable 500s.
                        ua.touch_last_seen()
                    ua_id = ua.id
                results.append({
                    "account_number": acct_num,
                    "array_id": None,
                    "utility_account_id": ua_id,
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
                arr, created = _safe_create_array(
                    db, tenant.id, name, client_id=None, fuel_type="solar")
                by_name[name.lower()] = arr
                if acct_num:
                    by_acct_number[acct_num] = arr
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
                    ua.touch_last_seen()   # throttled — see UtilityAccount

                # Bill from the GMP billing-period summary (the "paper bill" total).
                # ONLY when the period has CLOSED. The GMP usage summary reports the
                # CURRENT, OPEN cycle — period_end is the NEXT bill date (in the
                # FUTURE) and period_gen is only the generation accrued SO FAR.
                # Recording that as a settled paper bill is the root of the
                # future-dated bills + prorate rows (Bruce's 83-vs-803): a partial
                # total smeared across a full, partly-future window. Wait for close.
                if (period_gen is not None and period_gen > 0
                        and period_end is not None
                        and period_end <= now().date()):
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
                    # The meter capture is an EXTENSION path (the GMP server pull is
                    # flaky and SmartHub has none), so keep this account's offtaker
                    # generation-spreadsheet rows current here too — gated + idempotent
                    # + best-effort, mirroring worker._tracker_append_for_account.
                    from .billing import sheet_tracker as _sheet_tracker_mc
                    _sheet_tracker_mc.maybe_append_for_account(db, tenant.id, ua.id)

            kwh_recorded = 0.0
            days_written = 0

            # Ingest plausibility ceiling: the array's nameplate kW x 24h. A daily
            # kWh above this is physically impossible — almost always a cumulative or
            # billing-PERIOD total leaked into a single day. AO bills per-kWh, so we
            # must never ingest such a value (the generation watchdog flags it after).
            _np = db.execute(
                select(func.coalesce(func.sum(Inverter.nameplate_kw), 0.0)).where(
                    Inverter.array_id == arr.id, Inverter.deleted_at.is_(None)
                )
            ).scalar() or 0.0
            _cap = (float(_np) * 24.0) if _np and _np > 0 else None

            # ── Per-day series (preferred when present) ──────────────────────
            # Write rules live in production_fallback.apply_utility_day:
            #   • empty / estimate day → write utility_meter
            #   • measured utility → climb only
            #   • measured vendor with positive kWh → never overwrite
            #   • measured vendor ZERO + vendor feed DEAD → gap-fill with utility
            #     (source stays utility_meter — never relabeled as the vendor)
            if daily_rows:
                from . import production_fallback as _pf
                for d in daily_rows:
                    day = _meter_day(d.date)
                    if day is None or d.generated_kwh is None or d.generated_kwh < 0:
                        continue
                    dk = float(d.generated_kwh)
                    if _cap is not None and dk > _cap:
                        log.warning("utility_meter: skip implausible daily %.0f kWh "
                                    "(cap %.0f) array=%s day=%s", dk, _cap, arr.id, day)
                        continue
                    action = _pf.apply_utility_day(
                        db,
                        tenant_id=tenant.id,
                        array_id=arr.id,
                        day=day,
                        utility_kwh=dk,
                        utility_source="utility_meter",
                        insert_fn=_insert_daily_generation_race_safe,
                    )
                    if action in ("inserted", "updated", "gap_filled"):
                        kwh_recorded += dk
                        days_written += 1
            else:
                # ── Billing-period TOTAL: prorate across the period's days ───────
                # NEVER write a whole period's kWh into one day — that is a physically
                # impossible daily value that over-invoices a per-kWh plan and trips the
                # watchdog. Spread it evenly across the billing period so each day is
                # plausible and the period sum is preserved. If the span is unknown,
                # skip the daily write (the Bill row above already holds the total for
                # offtaker billing).
                gen = period_gen
                _pstart = _meter_day(parsed.get("period_start")) if isinstance(parsed, dict) else None
                if (gen is not None and gen > 0 and period_end is not None
                        and _pstart is not None and period_end > _pstart
                        and period_end <= now().date()):   # closed cycles only
                    n_days = (period_end - _pstart).days + 1
                    per_day = float(gen) / n_days
                    # Never estimate today / the future / the trailing 2-day guard —
                    # those days belong to the real inverter / GMP-API pull. Clamp the
                    # write window so a bill running past today (or a mis-parsed
                    # future-dated bill) can't fabricate present/future production and
                    # mask the authoritative reading. Mirrors bill_to_daily's guard.
                    _cutoff = now().date() - timedelta(days=2)
                    _write_end = min(period_end, _cutoff)
                    if _pstart > _cutoff:
                        log.info("utility_meter: bill window starts in guard/future zone "
                                 "(%s) — left in Bill only (array=%s)", _pstart, arr.id)
                    elif _cap is None or per_day <= _cap:
                        _wrote = 0
                        day = _pstart
                        while day <= _write_end:
                            row = db.execute(
                                select(DailyGeneration).where(
                                    DailyGeneration.array_id == arr.id,
                                    DailyGeneration.day == day,
                                )
                            ).scalar_one_or_none()
                            if row is None:
                                if _insert_daily_generation_race_safe(
                                        db, tenant_id=tenant.id, array_id=arr.id,
                                        day=day, kwh=per_day, source="bill_prorate"):
                                    _wrote += 1
                            elif row.source is None or row.source == "bill_prorate":
                                # Only fill/refresh a gap or our own earlier estimate.
                                # NEVER overwrite, RAISE, or relabel a real metered
                                # reading (solaredge/fronius/gmp_api/…) or a finer
                                # utility_meter day with this coarse bill smear — that
                                # is exactly the over-invoice bug (audit #9). Mirrors
                                # bill_to_daily's guard.
                                row.kwh = per_day
                                row.source = "bill_prorate"
                                row.uploaded_at = now()
                                _wrote += 1
                            # else: real / utility_meter reading owns this day — skip.
                            day += timedelta(days=1)
                        kwh_recorded = per_day * _wrote
                        days_written = _wrote
                    else:
                        log.warning("utility_meter: skip implausible prorated %.0f kWh/day "
                                    "(cap %.0f) array=%s", per_day, _cap, arr.id)
                else:
                    log.info("utility_meter: period total %.0f kWh with no usable day "
                             "span — left in Bill only (array=%s)", float(gen or 0), arr.id)

            db.flush()  # flush this account's daily rows before the next account (sibling accounts on the SAME array update, never race a duplicate)
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


# ═══════════════════════════════════════════════════════════════════════════════
# Predicted-vs-actual production forecast (feat 2026-06-30)
# ═══════════════════════════════════════════════════════════════════════════════
# Read endpoint behind the dashboard's "Production vs target" card. It answers
# "given the ACTUAL sunlight on THIS array's roof, how much should it have made,
# vs what it really made?" — the weather-aware, absolute basis peer-analysis and
# the static-CF card can't provide. All the model's inputs come back with the
# number so the UI can show the full math (Ford: "extreme clarity about how the
# model is working"). See api/forecasting.py for the model + validation.

def _array_nameplate_kw(db, arr) -> float:
    """Effective installed AC capacity for an array = Σ its inverters' nameplate,
    falling back to the rating parsed from the model code for nameplate-less
    vendors (Chint/SolarEdge), mirroring inverter_fleet._eff_nameplate_kw so the
    forecast nameplate matches what the dashboard shows.

    When no inverter rows exist yet (or none carry a parseable model) — common
    right after SolarEdge connect, before fleet inventory sync — fall back to:
      1. InverterConnection.config.peak_power_kw (stamped from SE site peakPower)
      2. A ``NN kW`` token in the array name (e.g. "Starlake 45kW SolarEdge")
    so weather modeling is not blocked on a second round-trip.
    """
    from .inverter_fleet import _nameplate_from_model
    ivs = db.execute(
        select(Inverter).where(
            Inverter.array_id == arr.id, Inverter.deleted_at.is_(None)
        )
    ).scalars().all()
    total = 0.0
    for iv in ivs:
        npk = iv.nameplate_kw
        if npk is None:
            npk = _nameplate_from_model(iv.vendor, iv.model)
        total += float(npk or 0.0)
    if total > 0:
        return total

    # Site-level peakPower from SolarEdge (or other vendors that stamp it).
    try:
        conn = db.execute(
            select(InverterConnection).where(InverterConnection.array_id == arr.id)
        ).scalar_one_or_none()
        if conn is not None:
            pk = (conn.config or {}).get("peak_power_kw")
            if pk is None:
                pk = (conn.config or {}).get("peak_kw")
            if pk is not None and float(pk) > 0:
                return float(pk)
    except Exception:
        pass

    # Last resort: capacity embedded in the array display name.
    import re as _re
    m = _re.search(r"(\d+(?:\.\d+)?)\s*kW\b", str(arr.name or ""), _re.I)
    if m:
        try:
            kw = float(m.group(1))
            if 0 < kw <= 10000:
                return kw
        except (TypeError, ValueError):
            pass
    return 0.0


def _utility_oneline(db, arr) -> str | None:
    """Best linked utility service address as a geocodable one-liner (GMP preferred)."""
    from . import forecasting
    accts = db.execute(
        select(UtilityAccount).where(
            UtilityAccount.array_id == arr.id,
            UtilityAccount.deleted_at.is_(None),
            UtilityAccount.service_address.isnot(None),
        )
    ).scalars().all()
    for ac in sorted(accts, key=lambda a: 0 if a.provider == "gmp" else 1):
        oneline = forecasting.address_to_oneline(ac.service_address)
        if oneline:
            return oneline
    return None


def _place_from_array_name(name: str | None) -> str | None:
    """Strip capacity/vendor noise from an array name → 'Town, VT' for geocoding.

    'Londonderry 150 kW SolarEdge (416160)' → 'Londonderry, VT'
    'Chester 150kW' → 'Chester, VT'
    'Tannery Brook 140kW' → 'Tannery Brook, VT'
    """
    import re
    if not name:
        return None
    s = str(name)
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s)
    s = re.sub(r"\s+\d[\d.]*\s*(kW|kw|MW|mw)?\b", " ", s)
    s = re.sub(r"\b(SolarEdge|Fronius|SMA|Chint|CPS|Locus|Enphase)\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -_,")
    if not s or len(s) < 2:
        return None
    # Default state for this product's fleet (VT community solar). Skip if already
    # carries a state or looks like a pure serial/id.
    if re.fullmatch(r"[\dA-Za-z_-]{1,6}", s):
        return None
    if not re.search(r",\s*[A-Za-z]{2}\b", s):
        s = s + ", VT"
    return s


def _vendor_site_address(db, arr) -> str | None:
    """Pull a street address from the vendor portal when we have API access.

    SolarEdge /sites/{id}/details carries location.address/city/state — the
    same data we already capture on connect, but older arrays may have lat/lng
    from a name-geocode without a displayable address string.
    """
    try:
        if arr.solaredge_api_key and arr.solaredge_site_id:
            from .adapters import solaredge as se
            det = se.site_details(arr.solaredge_api_key, int(arr.solaredge_site_id))
            addr = (det or {}).get("address") or ""
            if addr and str(addr).strip():
                return str(addr).strip()
    except Exception:
        log.info("vendor address lookup failed for array %s", getattr(arr, "id", "?"),
                 exc_info=True)
    return None


def _ensure_array_geocoded(db, arr) -> bool:
    """Lazily geocode an array from its linked UtilityAccount.service_address and
    CACHE lat/lng on the Array row. Idempotent: returns True (and does nothing) if
    already geocoded. Returns False when there's no address to geocode or the
    geocoders couldn't resolve it (caller then shows an honest unavailable state).
    Commits on a successful first geocode so the lookup is paid exactly once.

    Also backfills a missing geocoded_address LABEL when we already have coords
    (so the Analysis model editor isn't blank for sites that only have lat/lng).
    """
    from . import forecasting
    dirty = False
    if arr.latitude is not None and arr.longitude is not None:
        # Already located — fill a blank display label from cheap local sources
        # only (utility bill / place-from-name). Vendor API is reserved for the
        # explicit model-autofill path so forecast compute never fans out to SE.
        if not (arr.geocoded_address or "").strip():
            label = (_utility_oneline(db, arr)
                     or _place_from_array_name(arr.name))
            if label:
                arr.geocoded_address = label[:500]
                dirty = True
        if dirty:
            try:
                db.commit()
            except Exception:
                db.rollback()
        return True
    # Utility bill first (best precision), then place-from-name. Vendor portal
    # addresses are applied by autofill_fleet_model (explicit, rate-limit-aware).
    oneline = _utility_oneline(db, arr) or _place_from_array_name(arr.name)
    if not oneline:
        return False
    geo = forecasting.geocode_oneline(oneline)
    if not geo:
        return False
    arr.latitude = geo["lat"]
    arr.longitude = geo["lng"]
    arr.geocode_source = geo["source"]
    arr.geocoded_address = geo.get("matched") or oneline
    arr.geocoded_at = now()
    try:
        db.commit()
    except Exception:
        db.rollback()
        log.warning("forecast: geocode cache commit failed for array %s", arr.id, exc_info=True)
    return arr.latitude is not None


def autofill_fleet_model(db, tenant) -> dict:
    """Propagate known location/geometry into every array for the model editor.

    Sources (in order, never overwrite an operator-set value):
      • Address: utility service_address → SolarEdge site details → place-from-name
      • Tilt: leave NULL (UI shows assumed ≈ latitude) unless we already have one
      • Facing / PR: industry defaults already applied at forecast time; not written

    Returns counts so the UI can say "filled 6 addresses from utility bills".
    """
    from . import forecasting
    arrays = db.execute(
        select(Array).where(
            Array.tenant_id == tenant.id,
            Array.deleted_at.is_(None),
            Array.excluded.is_(False),
            exists().where(Inverter.array_id == Array.id),
        )
    ).scalars().all()
    filled_addr = 0
    located = 0
    for arr in arrays:
        had_loc = arr.latitude is not None and arr.longitude is not None
        had_label = bool((arr.geocoded_address or "").strip())

        # Prefer vendor street address when we still lack a label (or any location).
        if not had_label:
            vendor_addr = _vendor_site_address(db, arr)
            if vendor_addr:
                if had_loc:
                    arr.geocoded_address = vendor_addr[:500]
                    if not arr.geocode_source:
                        arr.geocode_source = "vendor:solaredge"
                    filled_addr += 1
                    had_label = True
                else:
                    # Geocode the vendor address to locate the array.
                    geo = forecasting.geocode_oneline(vendor_addr)
                    if geo:
                        arr.latitude = geo["lat"]
                        arr.longitude = geo["lng"]
                        arr.geocode_source = "vendor:solaredge"
                        arr.geocoded_address = (geo.get("matched") or vendor_addr)[:500]
                        arr.geocoded_at = now()
                        located += 1
                        filled_addr += 1
                        had_loc = True
                        had_label = True

        ok = _ensure_array_geocoded(db, arr)
        if ok and not had_loc:
            located += 1
        try:
            db.refresh(arr)
        except Exception:
            pass
        if not had_label and (arr.geocoded_address or "").strip():
            filled_addr += 1
        elif not had_label and not (arr.geocoded_address or "").strip():
            # Last resort: store the place-from-name even if geocode failed, so the
            # editor isn't blank — user can correct one character and re-save.
            guess = _place_from_array_name(arr.name)
            if guess:
                arr.geocoded_address = guess[:500]
                if not arr.geocode_source:
                    arr.geocode_source = "name-guess"
                filled_addr += 1
    try:
        db.commit()
    except Exception:
        db.rollback()
        log.warning("model autofill commit failed t=%s", tenant.id, exc_info=True)
    return {
        "ok": True,
        "arrays": len(arrays),
        "addresses_filled": filled_addr,
        "newly_located": located,
        "defaults": {
            "tilt": "≈ site latitude (assumed)",
            "facing": "South (0°)",
            "performance_ratio": forecasting.DEFAULT_PR,
        },
    }


def _set_array_location(db, arr, *, lat=None, lng=None, address=None,
                        source_label: str = "vendor") -> bool:
    """Give an array a location from vendor-captured data — coordinates directly,
    or by geocoding a vendor-supplied address. Fills ONLY when the array has no
    location yet (never overwrites a manual override or a utility-address geocode).
    Does NOT commit — callers batch it. Returns True if a location was set."""
    from . import forecasting
    if arr is None or (arr.latitude is not None and arr.longitude is not None):
        return False
    try:
        if lat is not None and lng is not None:
            latf, lngf = float(lat), float(lng)
            if -90 <= latf <= 90 and -180 <= lngf <= 180 and not (latf == 0 and lngf == 0):
                arr.latitude, arr.longitude = latf, lngf
                arr.geocode_source = source_label[:24]
                arr.geocoded_address = (str(address) if address else source_label + " coordinates")[:500]
                arr.geocoded_at = now()
                return True
    except (TypeError, ValueError):
        pass
    if address:
        geo = forecasting.geocode_oneline(str(address))
        if geo:
            arr.latitude, arr.longitude = geo["lat"], geo["lng"]
            arr.geocode_source = (source_label + "-geo")[:24]
            arr.geocoded_address = (geo.get("matched") or str(address))[:500]
            arr.geocoded_at = now()
            return True
    return False


# An array-day reading is CONTRADICTED by its own inverters when it falls below
# this fraction of the same day's per-inverter telemetry sum. A site total can
# never truly be less than the sum of its parts; the margin only absorbs
# capture-timing skew between the site-level and per-inverter reads.
_INV_SUM_AGREE_FRACTION = 0.85


def _clean_actual_by_day(db, arr, start: date, end: date) -> dict:
    """{iso_day: kwh} of REAL measured single-day generation in [start,end].
    Excludes bill_prorate + utility_meter (monthly-bill smears) so a daily
    predicted-vs-actual isn't distorted by a 2000-kWh monthly meter total landing
    on one day. See forecasting.MEASURED_DAILY_SOURCES.

    DATA-HONESTY CROSS-CHECK (C9 — Bruce's "Timberworks made 180 kWh vs 930
    expected — 19%" clearest-day spotlight, 2026-07-03). Each array-day value is
    cross-checked against the SUM of the array's own per-inverter InverterDaily
    rows for that day: a site total can never be LESS than the sum of its own
    inverters' measured readings. Prod ground truth: an SMA capture glitch stored
    ONE inverter's daily series (179.59 kWh) as the whole 150 kW array's day while
    the same day's 7 InverterDaily rows summed to 1,111.4 (true site day 1,275.9,
    confirmed by Bruce's GMP + SMA statements) — the spotlight then presented a
    false catastrophic deficit and inflated "energy at risk". Resolution per day:
      * streams agree (DailyGeneration ≥ 85% of the inverter sum) → keep the
        LARGER reading (the most-complete measured source);
      * DailyGeneration contradicted + EVERY live inverter reported that day →
        use the inverter sum (complete sibling telemetry beats a provably-partial
        array row);
      * DailyGeneration contradicted + only SOME inverters reported → the sum is
        just a lower bound; DROP the day entirely — fewer correct datapoints beat
        a wrong scary one (never present knowingly-partial data as truth).
    Days with no InverterDaily siblings keep today's behavior unchanged."""
    from . import forecasting
    rows = db.execute(
        select(DailyGeneration).where(
            DailyGeneration.array_id == arr.id,
            DailyGeneration.day >= start,
            DailyGeneration.day <= end,
        )
    ).scalars().all()
    out: dict[str, float] = {}
    for r in rows:
        if (r.source or "").lower() not in forecasting.MEASURED_DAILY_SOURCES:
            continue
        if r.kwh is None or r.kwh < 0:
            continue
        # last-writer-wins per day (upsert means at most one row anyway)
        out[r.day.isoformat()] = float(r.kwh)
    if not out:
        return out

    # Per-inverter telemetry cross-check (measured sources only, live inverters).
    inv_ids = db.execute(
        select(Inverter.id).where(
            Inverter.array_id == arr.id,
            Inverter.deleted_at.is_(None),
        )
    ).scalars().all()
    if not inv_ids:
        return out
    inv_rows = db.execute(
        select(InverterDaily).where(
            InverterDaily.inverter_id.in_(inv_ids),
            InverterDaily.day >= start,
            InverterDaily.day <= end,
        )
    ).scalars().all()
    inv_sum: dict[str, float] = {}
    inv_count: dict[str, int] = {}
    for r in inv_rows:
        if (r.source or "").lower() not in forecasting.MEASURED_DAILY_SOURCES:
            continue
        if r.kwh is None or r.kwh < 0:
            continue
        iso = r.day.isoformat()
        inv_sum[iso] = inv_sum.get(iso, 0.0) + float(r.kwh)
        inv_count[iso] = inv_count.get(iso, 0) + 1

    for iso, dg in list(out.items()):
        sib = inv_sum.get(iso)
        if sib is None or sib <= 0:
            continue                       # nothing to cross-check against
        if dg >= sib * _INV_SUM_AGREE_FRACTION:
            out[iso] = max(dg, sib)        # consistent — most-complete wins
        elif inv_count.get(iso, 0) >= len(inv_ids):
            # Array row provably partial; COMPLETE sibling telemetry replaces it.
            log.warning(
                "forecast actual: array %s day %s DailyGeneration %.1f kWh contradicted "
                "by its own %d-inverter sum %.1f kWh — using the inverter sum "
                "(single-inverter/partial array row, see C9 Timberworks)",
                arr.id, iso, dg, inv_count.get(iso, 0), sib,
            )
            out[iso] = sib
        else:
            # Contradicted AND the sibling sum itself is incomplete — no honest
            # number exists for this day; drop it from the comparison.
            log.warning(
                "forecast actual: array %s day %s DailyGeneration %.1f kWh contradicted "
                "by a PARTIAL inverter sum %.1f kWh (%d/%d inverters) — day excluded "
                "from predicted-vs-actual rather than shown as a false deficit",
                arr.id, iso, dg, sib, inv_count.get(iso, 0), len(inv_ids),
            )
            del out[iso]
    return out


@router.get("/v1/array-owners/forecast")
def array_forecast_ep(array_id: int = Query(...),
                      window_days: int = Query(14, ge=3, le=30),
                      authorization: str | None = Header(default=None)) -> dict:
    """Predicted-vs-actual production for ONE array over a rolling window.

    Resolves the array's location once (lazy geocode of its utility-account
    address, cached on the Array row), pulls Open-Meteo plane-of-array irradiance
    for the array's real tilt/azimuth, and compares the modeled expected AC kWh to
    the array's clean measured daily generation. Returns the number AND every
    input that produced it (location, irradiance source + value, tilt/azimuth and
    whether they're assumed, nameplate, performance ratio, the days counted,
    confidence) so the dashboard can show the full math.

    available=False with a `reason` when we genuinely can't model it
    (ungeocodable, no nameplate, irradiance feed down) — never a fabricated guess.
    """
    from . import forecasting
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(
                Array.id == array_id,
                Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if arr is None:
            raise HTTPException(404, "Array not found")

        nameplate = _array_nameplate_kw(db, arr)
        if nameplate <= 0:
            return {"array_id": arr.id, "array_name": arr.name,
                    **forecasting.Forecast(False, reason="no_nameplate").to_dict()}

        # Operator-entered expected specific yield (kWh/kW per DAY). When set,
        # "expected" = ratio × nameplate (flat) and a location is NOT required —
        # the ratio basis works for inverter-only arrays with no address.
        expected_ratio = arr.expected_kwh_per_kw_day
        has_loc = _ensure_array_geocoded(db, arr)
        if expected_ratio is None and not has_loc:
            return {"array_id": arr.id, "array_name": arr.name,
                    **forecasting.Forecast(False, reason="no_location").to_dict()}

        # Geometry: operator override (geometry_source='manual') or our labeled
        # default (tilt ≈ latitude, due south). PR: operator override or DEFAULT_PR.
        tilt_assumed = arr.tilt_deg is None
        az_assumed = arr.azimuth_deg is None
        tilt = arr.tilt_deg if arr.tilt_deg is not None else (
            forecasting.default_tilt_deg(arr.latitude) if has_loc else 0.0)
        az = arr.azimuth_deg if arr.azimuth_deg is not None else forecasting.DEFAULT_AZIMUTH_DEG
        pr_assumed = arr.performance_ratio is None
        pr = (float(arr.performance_ratio)
              if arr.performance_ratio is not None else forecasting.DEFAULT_PR)

        today = now().date()
        end = today - timedelta(days=1)
        start = end - timedelta(days=window_days - 1)
        actual_by_day = _clean_actual_by_day(db, arr, start, end)

        fc = forecasting.build_forecast(
            nameplate_kw=nameplate, lat=arr.latitude, lng=arr.longitude,
            tilt_deg=tilt, azimuth_deg=az,
            tilt_assumed=tilt_assumed, azimuth_assumed=az_assumed,
            geocode_source=arr.geocode_source, geocoded_address=arr.geocoded_address,
            actual_by_day=actual_by_day, window_days=window_days, today=today,
            expected_kwh_per_kw_day=expected_ratio, pr=pr, pr_assumed=pr_assumed,
        )
        return {"array_id": arr.id, "array_name": arr.name, **fc.to_dict()}


class ArrayGeometryBody(BaseModel):
    tilt_deg: Optional[float] = None
    azimuth_deg: Optional[float] = None
    # Optional PR override (0.5–1.0). Omitted = leave unchanged; explicit null
    # clears back to DEFAULT_PR. Field is optional so old clients still work.
    performance_ratio: Optional[float] = None
    clear_performance_ratio: bool = False


@router.post("/v1/array-owners/arrays/{array_id}/geometry")
def set_array_geometry_ep(array_id: int, body: ArrayGeometryBody,
                          authorization: str | None = Header(default=None)) -> dict:
    """Operator override of an array's tilt/azimuth/PR assumptions (the forecast's
    user-tunable model inputs). Sending null tilt+azimuth clears geometry → back
    to labeled defaults (tilt ≈ latitude, due south). performance_ratio is the
    losses derate; clear_performance_ratio=true restores the fleet default PR."""
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    if body.tilt_deg is not None and not (0 <= body.tilt_deg <= 90):
        raise HTTPException(400, "tilt_deg must be 0–90")
    if body.azimuth_deg is not None and not (-180 <= body.azimuth_deg <= 180):
        raise HTTPException(400, "azimuth_deg must be -180..180 (0=south, -90=east, 90=west)")
    if body.performance_ratio is not None and not (0.5 <= body.performance_ratio <= 1.0):
        raise HTTPException(400, "performance_ratio must be between 0.5 and 1.0")
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(
                Array.id == array_id, Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if arr is None:
            raise HTTPException(404, "Array not found")
        # Geometry: only update when the field is present in the request (model
        # defaults both to None which also means "clear" for the classic API).
        arr.tilt_deg = body.tilt_deg
        arr.azimuth_deg = body.azimuth_deg
        arr.geometry_source = (
            "manual" if (body.tilt_deg is not None or body.azimuth_deg is not None)
            else None)
        if body.clear_performance_ratio:
            arr.performance_ratio = None
        elif body.performance_ratio is not None:
            arr.performance_ratio = round(float(body.performance_ratio), 3)
        db.commit()
        result = {"ok": True, "array_id": arr.id,
                  "tilt_deg": arr.tilt_deg, "azimuth_deg": arr.azimuth_deg,
                  "geometry_source": arr.geometry_source,
                  "performance_ratio": arr.performance_ratio}
    invalidate_fleet_forecast(tenant.id)
    return result


class FleetModelParamsBody(BaseModel):
    """Apply model params across many arrays at once (Analysis tab bulk edit)."""
    tilt_deg: Optional[float] = None
    azimuth_deg: Optional[float] = None
    performance_ratio: Optional[float] = None
    # When true (default), only fill arrays that still use ASSUMED values — never
    # clobber an array the operator already customized.
    only_assumed: bool = True
    array_ids: Optional[list[int]] = None
    clear_geometry: bool = False
    clear_performance_ratio: bool = False


@router.post("/v1/array-owners/forecast-params")
def set_fleet_forecast_params_ep(body: FleetModelParamsBody,
                                 authorization: str | None = Header(default=None)) -> dict:
    """Bulk-set tilt / azimuth / performance ratio for the weather model.

    Intelligent defaults (Ford 2026-07-13): apply only to arrays still on
    assumed geometry/PR unless only_assumed=false. Scope with array_ids or
    all of the tenant's live arrays. Returns how many rows changed so the UI
    can confirm.
    """
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    if body.tilt_deg is not None and not (0 <= body.tilt_deg <= 90):
        raise HTTPException(400, "tilt_deg must be 0–90")
    if body.azimuth_deg is not None and not (-180 <= body.azimuth_deg <= 180):
        raise HTTPException(400, "azimuth_deg must be -180..180 (0=south)")
    if body.performance_ratio is not None and not (0.5 <= body.performance_ratio <= 1.0):
        raise HTTPException(400, "performance_ratio must be between 0.5 and 1.0")
    with SessionLocal() as db:
        q = select(Array).where(
            Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
        if body.array_ids:
            q = q.where(Array.id.in_(body.array_ids))
        arrays = db.execute(q).scalars().all()
        updated = 0
        for arr in arrays:
            changed = False
            if body.clear_geometry:
                if arr.tilt_deg is not None or arr.azimuth_deg is not None:
                    arr.tilt_deg = arr.azimuth_deg = None
                    arr.geometry_source = None
                    changed = True
            else:
                if body.tilt_deg is not None:
                    if (not body.only_assumed) or arr.tilt_deg is None:
                        arr.tilt_deg = float(body.tilt_deg)
                        arr.geometry_source = "manual"
                        changed = True
                if body.azimuth_deg is not None:
                    if (not body.only_assumed) or arr.azimuth_deg is None:
                        arr.azimuth_deg = float(body.azimuth_deg)
                        arr.geometry_source = "manual"
                        changed = True
            if body.clear_performance_ratio:
                if arr.performance_ratio is not None:
                    arr.performance_ratio = None
                    changed = True
            elif body.performance_ratio is not None:
                if (not body.only_assumed) or arr.performance_ratio is None:
                    arr.performance_ratio = round(float(body.performance_ratio), 3)
                    changed = True
            if changed:
                updated += 1
        db.commit()
        result = {"ok": True, "updated": updated, "scoped": len(arrays)}
    invalidate_fleet_forecast(tenant.id)
    return result


class ArrayExpectedRatioBody(BaseModel):
    # UNITS: kWh per kW PER DAY (specific yield). null clears the override →
    # back to the weather back-calculation.
    expected_kwh_per_kw_day: Optional[float] = None


@router.post("/v1/array-owners/arrays/{array_id}/expected-ratio")
def set_array_expected_ratio_ep(array_id: int, body: ArrayExpectedRatioBody,
                                authorization: str | None = Header(default=None)) -> dict:
    """Operator override of an array's EXPECTED specific yield (kWh per kW per
    day). Bruce's ask: the operator knows a site's unique derates (2014-era
    panels, rack orientation, wire-run losses) better than a generic weather
    model — when set, predicted-vs-actual uses expected = ratio × nameplate
    (flat per day over the window) for THIS array. Sending null clears it →
    back to the weather back-calculation. Follows the /geometry override
    pattern (tenant-scoped, demo-blocked, physical-range validated)."""
    tenant = _tenant_from_bearer(authorization)
    from .account import require_not_demo
    require_not_demo(tenant)
    v = body.expected_kwh_per_kw_day
    if v is not None and not (0 < v <= 12):
        # >12 kWh/kW/day would beat the sunniest desert with a tracker — a typo.
        raise HTTPException(400, "expected_kwh_per_kw_day must be between 0 and 12 (kWh per kW per day)")
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(
                Array.id == array_id, Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if arr is None:
            raise HTTPException(404, "Array not found")
        arr.expected_kwh_per_kw_day = (round(float(v), 2) if v is not None else None)
        db.commit()
    invalidate_fleet_forecast(tenant.id)   # target changed → next fetch recomputes fresh
    return {"ok": True, "array_id": array_id,
            "expected_kwh_per_kw_day": (round(float(v), 2) if v is not None else None),
            "units": "kWh per kW per day"}


class ArrayLocationBody(BaseModel):
    place: Optional[str] = None          # a town/address to geocode ("Londonderry, VT")
    latitude: Optional[float] = None     # OR set coordinates directly
    longitude: Optional[float] = None
    use_name: Optional[bool] = None      # OR geocode the array's own name


@router.post("/v1/array-owners/model-autofill")
def model_autofill_ep(authorization: str | None = Header(default=None)) -> dict:
    """Auto-propagate model inputs for every inverter array.

    Fills blank addresses from utility bills, vendor site details, and the
    array's own place-name — so the Analysis model editor isn't a wall of
    empty fields. Never overwrites an operator-entered address or geometry.
    Invalidates the fleet forecast snapshot so the next paint is fresh.
    """
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        result = autofill_fleet_model(db, tenant)
    invalidate_fleet_forecast(tenant.id)
    return result


@router.post("/v1/array-owners/arrays/{array_id}/location")
def set_array_location_ep(array_id: int, body: ArrayLocationBody,
                          authorization: str | None = Header(default=None)) -> dict:
    """Give an array a location so the weather model can run.

    Inverter-onboarded arrays (Chint/Fronius/SMA/SolarEdge) have no utility
    service address to geocode, so they can never be modeled (skipped as
    `no_location`). This lets the operator set one — by coordinates, by geocoding
    a place string, or by geocoding the array's own name — which unblocks the
    predicted-vs-actual view. Unlike the geometry override, this is allowed for
    demo tenants (a demo fleet still wants a working forecast).
    """
    from . import forecasting
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(
                Array.id == array_id, Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if arr is None:
            raise HTTPException(404, "Array not found")

        lat = lng = None
        source = "manual"
        matched = None
        if body.latitude is not None and body.longitude is not None:
            if not (-90 <= body.latitude <= 90) or not (-180 <= body.longitude <= 180):
                raise HTTPException(400, "latitude/longitude out of range")
            lat, lng = body.latitude, body.longitude
            matched = (body.place or f"{round(lat, 4)}, {round(lng, 4)}")
        else:
            query = (body.place or "").strip()
            if not query and body.use_name:
                # geocode the site's own name (strip a trailing size/number suffix
                # like "Londonderry 186" → "Londonderry") — a coarse but useful default
                import re
                query = re.sub(r"\s+\d[\d.]*\s*(kw|kW|mw|MW)?\s*$", "", arr.name or "").strip()
            if not query:
                raise HTTPException(400, "Provide coordinates, a place, or use_name")
            geo = forecasting.geocode_oneline(query)
            if not geo:
                raise HTTPException(422, f"Couldn't find a location for “{query}”")
            lat, lng = geo["lat"], geo["lng"]
            source = (geo.get("source") or "geocoded")[:24]
            matched = geo.get("matched") or query

        arr.latitude = lat
        arr.longitude = lng
        arr.geocode_source = source
        arr.geocoded_address = (matched or "")[:500] if matched else None
        arr.geocoded_at = now()
        db.commit()
        result = {"ok": True, "array_id": arr.id, "latitude": arr.latitude,
                  "longitude": arr.longitude, "geocode_source": arr.geocode_source,
                  "geocoded_address": arr.geocoded_address, "modeled": True}
    invalidate_fleet_forecast(tenant.id)   # new location → next fetch recomputes fresh
    return result


# ── fleet-forecast snapshot cache (makes the Analysis tab load instantly) ─────
# The forecast is expensive (geocode + Open-Meteo POA/sky + per-array roll-up over
# the window). Computing it on the request path left the Analysis tab sitting on
# empty "needs your weather-modeled data" states for seconds on every cold load.
# We precompute it OFF the request path (refresh_fleet_forecasts scheduler job)
# and store the JSON; the endpoint serves the stored snapshot immediately.
_FLEET_SNAPSHOT_MAX_SERVE_S = 6 * 3600   # serve a stored snapshot up to 6h old
_FLEET_SNAPSHOT_WINDOWS = (10, 14)       # windows the scheduler precomputes (UI uses 10)


def _store_fleet_snapshot(db, tenant_id: str, window_days: int, payload: dict) -> None:
    """Upsert the computed forecast for (tenant, window). Best-effort — a failed
    cache write must never break the request or the scheduler tick."""
    snap = db.execute(
        select(FleetForecastSnapshot).where(
            FleetForecastSnapshot.tenant_id == tenant_id,
            FleetForecastSnapshot.window_days == window_days,
        )
    ).scalar_one_or_none()
    if snap is None:
        db.add(FleetForecastSnapshot(tenant_id=tenant_id, window_days=window_days,
                                     payload=payload, computed_at=now()))
    else:
        snap.payload = payload
        snap.computed_at = now()
    try:
        db.commit()
    except Exception:
        db.rollback()
        log.warning("fleet forecast snapshot write failed t=%s w=%s",
                    tenant_id, window_days, exc_info=True)


def invalidate_fleet_forecast(tenant_id: str) -> None:
    """Drop a tenant's precomputed snapshots so the next fetch recomputes fresh.
    Call after any mutation that changes the model (set-location, set target) so
    the instant-cache is never a stale lie."""
    try:
        with SessionLocal() as db:
            db.query(FleetForecastSnapshot).filter(
                FleetForecastSnapshot.tenant_id == tenant_id).delete()
            db.commit()
    except Exception:
        log.warning("fleet forecast snapshot invalidate failed t=%s", tenant_id, exc_info=True)


@router.get("/v1/array-owners/forecast-fleet")
def fleet_forecast_ep(window_days: int = Query(14, ge=3, le=30),
                      authorization: str | None = Header(default=None)) -> dict:
    """Predicted-vs-actual for the WHOLE fleet in ONE call — the data behind the
    Analysis tab's "Production vs expected" + "Fleet health · kWh/kW" cards.

    INSTANT by default: returns a precomputed snapshot (kept warm off the request
    path by the refresh_fleet_forecasts scheduler job) when one is fresh enough, so
    a cold Analysis tab is one indexed SELECT — not a fan-out of geocode + Open-
    Meteo calls. Falls through to an inline compute (then stores it) only when no
    fresh snapshot exists: first-ever load, or right after a set-location / target
    change invalidated it."""
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        snap = db.execute(
            select(FleetForecastSnapshot).where(
                FleetForecastSnapshot.tenant_id == tenant.id,
                FleetForecastSnapshot.window_days == window_days,
            )
        ).scalar_one_or_none()
        if snap is not None and (
            (now() - snap.computed_at).total_seconds() < _FLEET_SNAPSHOT_MAX_SERVE_S
        ):
            return snap.payload
    payload = compute_fleet_forecast(tenant, window_days)
    with SessionLocal() as db:
        _store_fleet_snapshot(db, tenant.id, window_days, payload)
    return payload


def compute_fleet_forecast(tenant, window_days: int) -> dict:
    """Compute the fleet forecast for ONE tenant (the body behind forecast-fleet,
    factored out so the scheduler can precompute it off the request path).

    Loops the tenant's arrays, geocodes each lazily (cached), and rolls the
    per-array expected/actual kWh into a fleet total + a weather-aware ratio. Per
    call it MEMOIZES the Open-Meteo POA per (rounded lat,lng,tilt) so the many
    arrays that share an address/geometry (e.g. several 150 kW units at the same
    site) cost one irradiance fetch, not N. Returns the same transparent `inputs`
    as the per-array endpoint (from a representative array) so the card can show
    the full math, plus a compact per-array breakdown for drill-down.

    Honest by construction: arrays we can't model (no nameplate / ungeocodable /
    no measured days) are reported in `skipped` with a reason and excluded from
    the ratio — never silently counted as 0 or guessed.
    """
    from . import forecasting
    today = now().date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=window_days - 1)

    poa_memo: dict[tuple, dict] = {}
    wx_memo: dict[tuple, int | None] = {}

    def poa_for(lat, lng, tilt, az):
        key = (round(lat, 3), round(lng, 3), round(tilt, 1), round(az, 1))
        if key not in poa_memo:
            poa_memo[key] = forecasting.fetch_poa_daily(lat, lng, tilt, az, start, end)
        return poa_memo[key]

    def wx_for(lat, lng):
        # Current sky at the site (Analysis "Sky" column); memoized per location so
        # co-located arrays cost one call. Fail-soft: None → the UI shows no icon.
        key = (round(lat, 3), round(lng, 3))
        if key not in wx_memo:
            wx_memo[key] = forecasting.fetch_current_weather_code(lat, lng)
        return wx_memo[key]

    with SessionLocal() as db:
        # Analysis is a VENDOR/INVERTER-telemetry view — a utility-billing-only
        # array (a GMP/VEC account with no physical inverter monitoring hooked
        # up, e.g. Bruce's ~800 REC-agent utility rows) has no kWh/kW, no
        # forecast, nothing this endpoint can honestly report on. It used to be
        # included anyway and land in `skipped` as "no nameplate — excluded" —
        # technically correct math, but hundreds of utility-only rows flooding
        # the Analysis tab's array list read as GMP data leaking into a surface
        # that should be vendor-only (Ford 2026-07-08, live on Bruce's account).
        # Filter to arrays with at least one real Inverter row before this
        # endpoint (or the scheduler snapshot job) ever sees them.
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False),
                exists().where(Inverter.array_id == Array.id),
            )
        ).scalars().all()

        rows: list[dict] = []
        skipped: list[dict] = []
        fleet_exp = 0.0          # expected over days WITH a measured actual (for the ratio)
        fleet_act = 0.0
        fleet_exp_all = 0.0      # expected over the full window (informational)
        total_measured_days = 0
        rep_inputs = None        # a representative array's inputs for the card's "how"
        rep_nameplate = 0.0
        rep_is_weather = False   # prefer a weather-model array for the "how" panel
        sunny_rows: list[dict] = []
        ratio_based = 0          # arrays whose expected is an operator-set kWh/kW
        # kWh/kW health headline (Bruce): nameplate-weighted fleet specific yield,
        # measured days only. per-day = Σ actual ÷ Σ (nameplate × measured_days).
        kk_act = 0.0
        kk_np_days = 0.0
        kk_np = 0.0
        kk_arrays = 0

        def _skip_with_measured_kk(arr, nameplate: float, reason: str) -> None:
            """Record a skip — but when the array has a nameplate AND measured
            days, still carry its kWh/kW (Bruce's headline health metric needs
            only actual ÷ kW, no weather model) so the ranking isn't blind to
            un-geocoded arrays. Counted into the fleet kWh/kW aggregate too."""
            nonlocal kk_act, kk_np_days, kk_np, kk_arrays
            entry: dict = {"array_id": arr.id, "array_name": arr.name, "reason": reason}
            if nameplate > 0:
                by_day = _clean_actual_by_day(db, arr, start, end)
                mdays = len(by_day)
                if mdays > 0:
                    act = sum(by_day.values())
                    entry.update({
                        "nameplate_kw": round(nameplate, 1),
                        "measured_days": mdays,
                        "actual_kwh": round(act, 1),
                        "kwh_per_kw_day": round(act / nameplate / mdays, 2),
                        "kwh_per_kw_window": round(act / nameplate, 1),
                    })
                    kk_act += act
                    kk_np_days += nameplate * mdays
                    kk_np += nameplate
                    kk_arrays += 1
            skipped.append(entry)

        for arr in arrays:
            nameplate = _array_nameplate_kw(db, arr)
            if nameplate <= 0:
                skipped.append({"array_id": arr.id, "array_name": arr.name, "reason": "no_nameplate"})
                continue
            # Operator-entered expected kWh/kW/day: expected works WITHOUT a
            # location, so only the weather basis requires geocoding.
            expected_ratio = arr.expected_kwh_per_kw_day
            has_loc = _ensure_array_geocoded(db, arr)
            if expected_ratio is None and not has_loc:
                _skip_with_measured_kk(arr, nameplate, "no_location")
                continue

            tilt_assumed = arr.tilt_deg is None
            az_assumed = arr.azimuth_deg is None
            tilt = arr.tilt_deg if arr.tilt_deg is not None else (
                forecasting.default_tilt_deg(arr.latitude) if has_loc else 0.0)
            az = arr.azimuth_deg if arr.azimuth_deg is not None else forecasting.DEFAULT_AZIMUTH_DEG
            pr_assumed = arr.performance_ratio is None
            pr = (float(arr.performance_ratio)
                  if arr.performance_ratio is not None else forecasting.DEFAULT_PR)

            poa_by_day = None
            if expected_ratio is None:
                poa_by_day = poa_for(arr.latitude, arr.longitude, tilt, az)
                if not poa_by_day:
                    _skip_with_measured_kk(arr, nameplate, "irradiance_unavailable")
                    continue

            actual_by_day = _clean_actual_by_day(db, arr, start, end)
            fc = forecasting.build_forecast(
                nameplate_kw=nameplate, lat=arr.latitude, lng=arr.longitude,
                tilt_deg=tilt, azimuth_deg=az,
                tilt_assumed=tilt_assumed, azimuth_assumed=az_assumed,
                geocode_source=arr.geocode_source, geocoded_address=arr.geocoded_address,
                actual_by_day=actual_by_day, window_days=window_days, today=today,
                _poa_by_day=poa_by_day,
                expected_kwh_per_kw_day=expected_ratio, pr=pr, pr_assumed=pr_assumed,
            )
            # Expose customizability flags on the row for the Analysis UI.
            d_flags = {
                "tilt_deg": round(tilt, 1), "azimuth_deg": round(az, 1),
                "performance_ratio": round(pr, 3),
                "performance_ratio_assumed": pr_assumed,
                "azimuth_assumed": az_assumed,
                # Clean address for the per-array model editor (never re-geocode
                # parenthetical labels like "… (from site name)").
                "address": (arr.geocoded_address or None),
                "latitude": arr.latitude, "longitude": arr.longitude,
            }
            d = fc.to_dict()
            measured_days = d["inputs"].get("measured_days", 0) if d.get("available") else 0
            # Expected restricted to matched days — the apples-to-apples denominator
            # for the ratio (audit #15). Now carried on the Forecast itself, so the
            # fleet headline and the per-array row share ONE matched-day expected.
            exp_matched = d["expected_matched_kwh"]
            basis = d["inputs"].get("expected_basis", "weather_model")
            if basis == "operator_ratio":
                ratio_based += 1
            # Per-array MEASURED specific yield — Bruce's headline health number.
            # UNITS: kwh_per_kw_day = kWh per kW per DAY averaged over the days we
            # actually measured; kwh_per_kw_window = the measured total ÷ nameplate
            # (covers only the measured days within the window — never extrapolated).
            kk_day = (
                round(d["actual_kwh"] / nameplate / measured_days, 2)
                if measured_days > 0 else None
            )
            kk_window = (
                round(d["actual_kwh"] / nameplate, 1)
                if measured_days > 0 else None
            )
            rows.append({
                "array_id": arr.id, "array_name": arr.name,
                "nameplate_kw": round(nameplate, 1),
                "expected_kwh": d["expected_kwh"],
                # Matched-day expected so a frontend sites-grid rollup divides matched
                # actual by matched-day expected too (never by the full window).
                "expected_matched_kwh": exp_matched,
                "actual_kwh": d["actual_kwh"],
                "kwh_per_kw_day": kk_day,
                "kwh_per_kw_window": kk_window,
                "expected_basis": basis,
                "expected_kwh_per_kw_day": (
                    round(float(expected_ratio), 2) if expected_ratio is not None else None
                ),
                "ratio_pct": d["ratio_pct"], "measured_days": measured_days,
                "confidence": d["confidence"],
                "tilt_assumed": tilt_assumed, "geocode_source": arr.geocode_source,
                "weather_code": (wx_for(arr.latitude, arr.longitude) if has_loc else None),
                **d_flags,
            })
            if measured_days > 0:
                kk_act += d["actual_kwh"]
                kk_np_days += nameplate * measured_days
                kk_np += nameplate
                kk_arrays += 1
            fleet_exp += exp_matched
            fleet_act += d["actual_kwh"]
            fleet_exp_all += d["expected_kwh"]
            total_measured_days = max(total_measured_days, measured_days)
            # Representative inputs for the card's "How we calculated this": prefer
            # the biggest WEATHER-modeled array (its inputs show the full model);
            # fall back to a ratio-based array only when nothing is weather-modeled.
            is_weather = basis == "weather_model"
            if (rep_inputs is None or (is_weather and not rep_is_weather)
                    or (is_weather == rep_is_weather and nameplate > rep_nameplate)):
                rep_inputs, rep_nameplate, rep_is_weather = d["inputs"], nameplate, is_weather
            for x in d["days"]:
                if x["sunny"] and x["actual_kwh"] is not None:
                    sunny_rows.append({"array_id": arr.id, "array_name": arr.name, **x})

        ratio_pct = round(fleet_act / fleet_exp * 100) if fleet_exp > 0 else None
        pr_measured = round(fleet_act / fleet_exp, 3) if fleet_exp > 0 else None
        if total_measured_days >= 10 and rows:
            confidence = "high"
        elif total_measured_days >= 4 and rows:
            confidence = "medium"
        elif rows:
            confidence = "low"
        else:
            confidence = "none"

        # Spotlight the clearest sunny day (best POA across the fleet) — Ford's
        # most-legible proof point. Pick the sunny row with the highest POA.
        sunny_rows.sort(key=lambda r: r.get("poa_kwh_m2", 0), reverse=True)
        spotlight = sunny_rows[0] if sunny_rows else None

        return {
            "available": bool(rows),
            "ratio_pct": ratio_pct,
            "expected_kwh": round(fleet_exp, 1),          # over matched days
            "actual_kwh": round(fleet_act, 1),
            "expected_kwh_window": round(fleet_exp_all, 1),
            "performance_ratio_measured": pr_measured,
            "confidence": confidence,
            "arrays_modeled": len(rows),
            "arrays_skipped": len(skipped),
            # How many arrays' "expected" is an operator-entered kWh/kW target
            # (vs the weather model) — so the UI can footnote the mix honestly.
            "arrays_ratio_based": ratio_based,
            # Fleet kWh/kW health headline (Bruce): nameplate-weighted measured
            # specific yield. UNITS: kWh per kW per DAY, over measured days only —
            # never extrapolated across unmeasured days.
            "kwh_per_kw": {
                "fleet_per_day": (round(kk_act / kk_np_days, 2) if kk_np_days > 0 else None),
                "nameplate_kw": round(kk_np, 1),
                "arrays_counted": kk_arrays,
                "units": "kWh per kW per day, averaged over measured days",
            },
            "inputs": rep_inputs or {},
            "rows": sorted(rows, key=lambda r: (r["ratio_pct"] is None, r["ratio_pct"] or 0)),
            "skipped": skipped,
            "sunny_spotlight": spotlight,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": window_days},
        }


# ── SMA owner-consent connect ──────────────────────────────────────────────────
# SMA's API model is the inverse of key-paste: our ONE registered app + a
# per-owner backchannel consent the plant owner approves inside their Sunny
# Portal account. No passwords, no keys for the owner to hunt — one approval
# click. Flow: POST /sma/consent (send the prompt) → GET /sma/consent/status
# (poll; re-POSTs bc-authorize under the hood, state persisted in SmaConsent so
# it resumes across sessions) → POST /sma/connect-account (list the app-readable
# plants, attach the chosen ones — same discover→attach cascade as SolarEdge).
# The app credentials stay in the environment (SMA_APP_CLIENT_ID/SECRET); per-
# connection config carries only {system_id}. /sma/available tells the UI whether
# the flow is live (creds present) so it can show a graceful "coming soon" else.
# Consent + discovery shapes VERIFIED against the sandbox 2026-07-08 (see the
# banner in api/inverters/sma.py); the measurement VALUES await a real plant.


@router.get("/v1/array-owners/sma/available")
def sma_available(authorization: str | None = Header(default=None)) -> dict:
    """Is the one-click SMA consent flow live? (True once SMA approves our app
    registration and the app credentials land in the environment.)"""
    _tenant_from_bearer(authorization)
    return {"ok": True, "configured": inverters.sma.is_app_configured()}


class SmaConsentBody(BaseModel):
    owner_email: str


@router.post("/v1/array-owners/sma/consent")
def sma_consent_start(
    body: SmaConsentBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Send (or re-send) the SMA consent prompt to a plant owner's Sunny Portal
    account. Persists the request per (tenant, owner email) so the UI can poll
    and resume. Re-posting for the same owner re-sends after a rejection."""
    tenant = _tenant_from_bearer(authorization)
    _guard_vendor_discover(tenant.id, "sma")
    if not inverters.sma.is_app_configured():
        raise HTTPException(503, "SMA linking isn't live yet — our SMA app "
                                 "registration is still in progress.")

    email = (body.owner_email or "").strip()
    if "@" not in email:
        raise HTTPException(400, "A valid plant-owner email is required.")

    try:
        res = inverters.sma.request_consent(email)
    except InverterAuthError as exc:
        raise HTTPException(502, f"SMA rejected our app credentials: {exc}")
    except InverterError as exc:
        raise HTTPException(502, f"SMA consent request failed: {exc}")

    # SMA returns the CURRENT state on the bc-authorize call — if this owner had
    # already approved (e.g. re-connecting), it comes back "accepted" straight
    # away and the UI can skip the waiting screen.
    state = res.get("state") or "pending"

    with SessionLocal() as db:
        row = db.execute(
            select(SmaConsent).where(
                SmaConsent.tenant_id == tenant.id,
                SmaConsent.owner_email_lc == email.lower(),
            )
        ).scalar_one_or_none()
        if row is None:
            row = SmaConsent(tenant_id=tenant.id, owner_email=email,
                             owner_email_lc=email.lower())
            db.add(row)
        row.owner_email = email
        row.status = state
        row.auth_req_id = res.get("expiration")   # store the consent expiry for reference
        row.last_error = None
        row.requested_at = now()
        db.commit()

    msg = (f"Already connected — {email} approved data sharing."
           if state == "accepted" else
           f"Approval request sent — {email} will see it in their Sunny Portal "
           "account. This page updates once they approve.")
    return {"ok": True, "status": state, "message": msg}


@router.get("/v1/array-owners/sma/consent/status")
def sma_consent_status(
    owner_email: str,
    authorization: str | None = Header(default=None),
) -> dict:
    """Poll one owner's consent state. Refreshes from SMA (best-effort) and
    persists; a transient SMA error returns the last KNOWN state, flagged."""
    tenant = _tenant_from_bearer(authorization)
    email = (owner_email or "").strip()
    with SessionLocal() as db:
        row = db.execute(
            select(SmaConsent).where(
                SmaConsent.tenant_id == tenant.id,
                SmaConsent.owner_email_lc == email.lower(),
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "No consent request on file for that email.")
        stale = False
        try:
            row.status = inverters.sma.consent_status(email)
            row.last_error = None
        except InverterError as exc:
            stale = True
            row.last_error = str(exc)[:500]
        db.commit()
        return {"ok": True, "status": row.status, "stale": stale,
                "requested_at": row.requested_at.isoformat()}


class SmaConnectAccountBody(BaseModel):
    # Attach a subset of the app-readable plants; omit to attach every plant
    # SELECTED in the UI — the caller should normally pass explicit ids, since
    # the app token sees ALL consented owners' plants (cross-tenant listing).
    system_ids: Optional[list[str]] = None


@router.post("/v1/array-owners/sma/connect-account")
def sma_connect_account(
    body: SmaConnectAccountBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Attach SMA plants to this tenant's arrays (match by existing connection
    system_id, then exact name; create otherwise; idempotent — the same
    cascade as SolarEdge/Fronius/Locus).

    CROSS-TENANT GUARD: our app token lists the plants of EVERY consented
    owner across ALL tenants (SMA's /plants has no per-owner filter), so a
    naive "attach everything discovered" would hand tenant B a plant that
    tenant A's owner already consented to and connected. We close that using
    OUR OWN data instead of SMA's: any system_id already attached to another
    tenant's array is excluded before the create/match cascade runs, whether
    it came from an explicit system_ids list or the "attach everything"
    default. A tenant re-running discover/connect for plants it already owns
    is unaffected (those match this tenant's own InverterConnection rows)."""
    tenant = _tenant_from_bearer(authorization)
    _guard_vendor_discover(tenant.id, "sma")
    if not inverters.sma.is_app_configured():
        raise HTTPException(503, "SMA linking isn't live yet — our SMA app "
                                 "registration is still in progress.")

    try:
        discovered = inverters.sma.discover_systems()
    except InverterAuthError as exc:
        raise HTTPException(502, f"SMA rejected our app credentials: {exc}")
    except InverterError as exc:
        raise HTTPException(502, f"SMA error: {exc}")

    requested: set[str] | None = None
    if body.system_ids is not None:
        requested = {str(s) for s in body.system_ids}
        discovered = [s for s in discovered if str(s["system_id"]) in requested]

    with SessionLocal() as _guard_db:
        other_tenant_ids: set[str] = {
            str((conn_config or {}).get("system_id"))
            for (conn_config, owner_tenant_id) in _guard_db.execute(
                select(InverterConnection.config, Array.tenant_id)
                .join(Array, Array.id == InverterConnection.array_id)
                .where(InverterConnection.vendor == "sma",
                       Array.tenant_id != tenant.id,
                       Array.deleted_at.is_(None))
            ).all()
            if (conn_config or {}).get("system_id")
        }
    if other_tenant_ids:
        claimed_elsewhere = [s for s in discovered if str(s["system_id"]) in other_tenant_ids]
        discovered = [s for s in discovered if str(s["system_id"]) not in other_tenant_ids]
        if claimed_elsewhere and requested is not None:
            # The tenant explicitly asked for a plant another tenant already owns —
            # say so plainly instead of silently dropping it.
            names = ", ".join(s.get("name") or s["system_id"] for s in claimed_elsewhere)
            raise HTTPException(
                409, f"Plant already connected to a different EnergyAgent account: {names}. "
                     "If this is your plant, contact support."
            )

    if not discovered:
        return {"ok": True, "connected": [], "created": [], "matched": [],
                "message": "No SMA plants to connect."}

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

        by_system_id: dict[str, list[int]] = defaultdict(list)
        by_name: dict[str, list[int]] = defaultdict(list)
        all_names_lower: set[str] = {
            n.strip().lower() for (n,) in db.execute(
                select(Array.name).where(Array.tenant_id == tenant.id)
            ).all()
        }
        arr_by_id = {a.id: a for a in arrays}
        for a in arrays:
            by_name[a.name.strip().lower()].append(a.id)
            c = conns.get(a.id)
            if c is not None and c.vendor == "sma":
                sid = (c.config or {}).get("system_id")
                if sid:
                    by_system_id[str(sid)].append(a.id)

        used: set[int] = set()
        for system in discovered:
            sid = str(system["system_id"])
            sys_name = (system.get("name") or "").strip() or f"SMA plant {sid}"
            entry = {"array_id": None, "name": sys_name, "system_id": sid}

            claimants = [aid for aid in by_system_id.get(sid, []) if aid not in used]
            if len(claimants) > 1:
                raise HTTPException(
                    409,
                    f"Two arrays already claim SMA plant {sid} "
                    f"(arrays {sorted(claimants)}). Resolve the duplicate first.",
                )

            target = None
            if claimants:
                target = arr_by_id[claimants[0]]
            else:
                name_hits = [aid for aid in by_name.get(sys_name.lower(), [])
                             if aid not in used]
                if len(name_hits) == 1:
                    target = arr_by_id[name_hits[0]]

            # Per-connection config carries ONLY the system id — the app-level
            # credentials resolve from the environment at pull time, so no
            # secret is duplicated per tenant row.
            if target is not None:
                conn = conns.get(target.id)
                if conn is None:
                    conn = InverterConnection(array_id=target.id, vendor="sma",
                                              config={"system_id": sid}, status="ok")
                    db.add(conn)
                else:
                    conn.vendor = "sma"
                    conn.config = {"system_id": sid}
                    conn.status = "ok"
                    conn.last_error = None
                used.add(target.id)
                entry["array_id"] = target.id
                entry["name"] = target.name
                matched.append(entry)
            else:
                name = sys_name
                if name.lower() in all_names_lower:
                    name = f"{sys_name} ({sid[:8]})"
                new_arr = Array(tenant_id=tenant.id, name=name, client_id=None,
                                fuel_type="solar")
                db.add(new_arr)
                db.flush()
                db.add(InverterConnection(array_id=new_arr.id, vendor="sma",
                                          config={"system_id": sid}, status="ok"))
                used.add(new_arr.id)
                all_names_lower.add(name.lower())
                arr_by_id[new_arr.id] = new_arr
                entry["array_id"] = new_arr.id
                entry["name"] = new_arr.name
                created.append(entry)

            connected.append(entry)

        db.commit()

        for _e in connected:
            if _e.get("array_id"):
                _trigger_history_backfill(db, _e["array_id"])

    return {
        "ok": True,
        "connected": connected,
        "created": created,
        "matched": matched,
        "message": (f"{len(connected)} arrays connected: "
                    f"{len(created)} new, {len(matched)} matched."),
    }
