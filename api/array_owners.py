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
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Optional

import httpx  # noqa: F401 — kept so tests can monkeypatch array_owners.httpx.get
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from . import inverters
from .db import SessionLocal
from .inverters import VENDORS, InverterAuthError, InverterError
from .models import Array, DailyGeneration, InverterConnection, Tenant, UtilityAccount, now
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

    arrays_out: list[dict] = []
    tot_power = 0.0
    tot_today = tot_month = tot_life = 0.0
    tot_today_usd = tot_month_usd = tot_life_usd = 0.0

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

            arrays_out.append({
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
            })

            tot_today += today_kwh
            tot_month += month_kwh
            tot_life += lifetime_kwh
            tot_today_usd += value["today_usd"]
            tot_month_usd += value["month_usd"]
            tot_life_usd += value["lifetime_usd"]

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
    }


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
