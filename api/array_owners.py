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

import logging
import math
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from .adapters.solaredge import (
    SOLAREDGE_API_BASE,
    SolarEdgeAuthError,
    SolarEdgeError,
)
from .db import SessionLocal
from .models import Array, DailyGeneration, Tenant, UtilityAccount, now
from .rates import REC_PRICE_USD_PER_MWH, get_energy_rate

log = logging.getLogger(__name__)

router = APIRouter()

_TIMEOUT = 20.0  # seconds

# An array is "stale" once its newest DailyGeneration row is older than this.
STALE_DAYS = 3

# ── live SolarEdge overview cache ─────────────────────────────────────────────
# SolarEdge allows ~300 req/day per token, so we cache the /overview response
# per site for 5 minutes. Keyed by site_id -> (fetched_at, overview_dict).
_CACHE_TTL = timedelta(minutes=5)
_overview_cache: dict[int, tuple[datetime, dict]] = {}


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


# ── SolarEdge HTTP helpers (all network goes through this module's httpx) ──────

def _fetch_overview(api_key: str, site_id: int, *, use_cache: bool = True) -> dict:
    """Return the SolarEdge site `overview` block.

    Raises SolarEdgeAuthError on 401/403, SolarEdgeError on any other failure.
    Successful responses are cached per site for _CACHE_TTL.
    """
    if use_cache:
        cached = _overview_cache.get(site_id)
        if cached is not None and (now() - cached[0]) < _CACHE_TTL:
            return cached[1]

    url = f"{SOLAREDGE_API_BASE}/site/{site_id}/overview"
    try:
        resp = httpx.get(url, params={"api_key": api_key}, timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise SolarEdgeError(f"Network error contacting SolarEdge: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SolarEdgeAuthError(
            f"SolarEdge API key rejected for site {site_id} (401/403). "
            "Verify the key and site ID are correct."
        )
    if not resp.is_success:
        raise SolarEdgeError(
            f"SolarEdge /site/{site_id}/overview returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is a SolarEdge error
        raise SolarEdgeError(f"SolarEdge returned non-JSON response: {exc}") from exc

    overview = body.get("overview", {}) or {}
    _overview_cache[site_id] = (now(), overview)
    return overview


def _fetch_site_meta(api_key: str, site_id: int) -> dict:
    """Best-effort site name + peak kW for the connect confirmation screen.

    Returns {"name": str | None, "peak_kw": float | None}. Never raises — the
    key is already validated by the time we call this, so display metadata is
    non-critical.
    """
    url = f"{SOLAREDGE_API_BASE}/site/{site_id}/details"
    try:
        resp = httpx.get(url, params={"api_key": api_key}, timeout=_TIMEOUT)
        if not resp.is_success:
            return {"name": None, "peak_kw": None}
        details = resp.json().get("details", {}) or {}
    except (httpx.RequestError, ValueError, KeyError):
        return {"name": None, "peak_kw": None}

    peak = details.get("peakPower")
    return {
        "name": details.get("name") or None,
        "peak_kw": float(peak) if peak is not None else None,
    }


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

            has_live_source = bool(arr.solaredge_api_key and arr.solaredge_site_id)

            live: dict | None = None
            overview_ok: bool | None = None
            if has_live_source:
                try:
                    ov = _fetch_overview(arr.solaredge_api_key, arr.solaredge_site_id)
                    raw_power = (ov.get("currentPower") or {}).get("power")
                    power_w = float(raw_power) if raw_power is not None else None
                    live = {
                        "source": "solaredge",
                        "current_power_w": power_w,
                        "as_of": ov.get("lastUpdateTime"),
                    }
                    overview_ok = True
                    if power_w is not None:
                        tot_power += power_w
                except SolarEdgeError:
                    overview_ok = False
                    live = {
                        "source": "solaredge",
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


class SolarEdgeConnectBody(BaseModel):
    api_key: str
    site_id: int


@router.post("/v1/array-owners/arrays/{array_id}/solaredge")
def connect_solaredge(
    array_id: int,
    body: SolarEdgeConnectBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Validate a SolarEdge key against a live overview call, then save it.

    A rejected key (401/403) or unreachable site returns 400 and persists
    nothing.
    """
    tenant = _tenant_from_bearer(authorization)

    api_key = (body.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant.id:
            raise HTTPException(404, "Array not found")

        # Validate before saving — fresh call, never the cache.
        try:
            _fetch_overview(api_key, body.site_id, use_cache=False)
        except SolarEdgeAuthError as exc:
            raise HTTPException(400, str(exc))
        except SolarEdgeError as exc:
            raise HTTPException(400, f"SolarEdge error: {exc}")

        meta = _fetch_site_meta(api_key, body.site_id)

        arr.solaredge_api_key = api_key
        arr.solaredge_site_id = body.site_id
        db.commit()

    return {
        "ok": True,
        "site_name": meta["name"],
        "peak_power_kw": meta["peak_kw"],
        "site_id": body.site_id,
    }
