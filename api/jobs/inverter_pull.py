"""Generalized daily-generation pull across ALL inverter vendors.

Supersedes the SolarEdge-only pull: iterates every InverterConnection row plus
virtual connections synthesized from legacy Array.solaredge_* columns (arrays
connected before the InverterConnection table existed), dispatches per vendor,
and upserts DailyGeneration by (array_id, day).

Vendors that don't support a daily pull (chint) are skipped gracefully. Per-
connection errors are recorded on the row (status="error", last_error) and
logged, but never crash the scheduler.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from ..adapters import solaredge as _se
from ..db import SessionLocal
from ..inverters import VENDORS, InverterError
from ..inverters import fronius as _fronius
from ..models import Array, DailyGeneration, InverterConnection, now

log = logging.getLogger(__name__)


def _resolve_connections(db) -> list:
    """All pullable connections: real InverterConnection rows first, then
    virtual solaredge connections for legacy arrays without a row.

    Returns a list of lightweight records with .array, .vendor, .config and an
    optional .row (the InverterConnection, None for virtual)."""
    out: list = []
    seen: set[int] = set()

    rows = db.execute(select(InverterConnection)).scalars().all()
    for row in rows:
        arr = db.get(Array, row.array_id)
        if arr is None or arr.deleted_at is not None:
            continue
        out.append(SimpleNamespace(
            array=arr, vendor=row.vendor, config=dict(row.config or {}), row=row,
        ))
        seen.add(row.array_id)

    legacy = db.execute(
        select(Array).where(
            Array.solaredge_api_key.isnot(None),
            Array.deleted_at.is_(None),
        )
    ).scalars().all()
    for arr in legacy:
        if arr.id in seen or not arr.solaredge_site_id:
            continue
        out.append(SimpleNamespace(
            array=arr,
            vendor="solaredge",
            config={"api_key": arr.solaredge_api_key, "site_id": arr.solaredge_site_id},
            row=None,
        ))

    return out


def _backfill_solaredge_location(db, arr: Array, config: dict, row=None) -> None:
    """Self-heal SolarEdge model inputs on the daily pull.

    Location: SE site address was only ever captured ONCE at initial connect —
    older arrays (or a first capture that missed address) stayed stuck on
    "set location" (Ford, 2026-07-02). Geocode when lat is still missing.

    Nameplate: SE's site peakPower is what the weather model needs when we have
    no Inverter rows yet (or residential models never stamped nameplate_kw).
    Stamp config.peak_power_kw from site_details when absent so Analysis leaves
    "not modeled yet" without waiting for a fleet inventory sync
    (Ford, 2026-07-20 — Cover/Starlake).

    Cheap short-circuit when both location and peak are already present; never
    blocks or fails the actual daily energy pull.
    """
    need_loc = arr.latitude is None
    cfg = dict(config or {})
    need_peak = not (cfg.get("peak_power_kw") or cfg.get("peak_kw"))
    if not need_loc and not need_peak:
        return
    try:
        api_key = cfg.get("api_key")
        site_id = cfg.get("site_id")
        if not api_key or not site_id:
            return
        details = _se.site_details(api_key, int(site_id))
        if need_peak and details.get("peak_kw"):
            try:
                pk = float(details["peak_kw"])
            except (TypeError, ValueError):
                pk = None
            if pk is not None and pk > 0:
                cfg["peak_power_kw"] = pk
                # Persist onto the real connection row when we have one; virtual
                # legacy connections have no row to stamp (Array.solaredge_* only).
                if row is not None:
                    row.config = cfg
                # Keep the in-memory config the pull is using in sync too.
                if isinstance(config, dict):
                    config["peak_power_kw"] = pk
        if need_loc and details.get("address"):
            from .. import array_owners as _ao
            _ao._set_array_location(db, arr, address=details["address"],
                                    source_label="vendor:solaredge")
    except Exception:  # noqa: BLE001 — best-effort only, never break the daily pull
        log.info("solaredge location/nameplate backfill failed for array=%s",
                 arr.id, exc_info=True)


def _backfill_fronius_location(db, arr: Array, config: dict) -> None:
    """Same self-heal as SolarEdge, for Fronius. A Fronius array is inverter-
    onboarded (no linked utility service address), so its ONLY location source is
    the address Solar.web reports for the PV system — captured once at connect
    (_attach_fronius). An array connected before that harvest existed, or whose
    Solar.web record had no address then, is otherwise stuck on "set location"
    forever with no second chance (the exact gap Ford saw on Chester/Waterford:
    Fronius sites with no Sky/Expected). Piggyback the daily pull — it runs for
    every connected array regardless of connect date — so it self-heals. Cheap:
    no-ops the instant the array already has a location; never blocks the energy
    pull. If Solar.web truly has no address on file, this stays a no-op and the
    one-click "set location" remains the fallback."""
    if arr.latitude is not None:
        return
    try:
        details = _fronius.fetch_details(config)
        if details.get("address"):
            from .. import array_owners as _ao
            _ao._set_array_location(db, arr, address=details["address"],
                                    source_label="vendor:fronius")
    except Exception:  # noqa: BLE001 — best-effort only, never break the daily pull
        log.info("fronius location backfill failed for array=%s", arr.id, exc_info=True)


def _upsert_daily(db, tenant_id: str, array_id: int, vendor: str, entries: list[dict]) -> int:
    """Upsert [{day, kwh}] into DailyGeneration with source=vendor. Returns count."""
    if not entries:
        return 0
    days = [e["day"] for e in entries]
    existing = {
        r.day: r for r in db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == array_id,
                DailyGeneration.day.in_(days),
            )
        ).scalars().all()
    }
    for entry in entries:
        day, kwh = entry["day"], entry["kwh"]
        if day in existing:
            existing[day].kwh = kwh
            existing[day].source = vendor
            existing[day].uploaded_at = now()
        else:
            db.add(DailyGeneration(
                tenant_id=tenant_id, array_id=array_id, day=day, kwh=kwh, source=vendor,
            ))
    return len(entries)


def pull_all_inverters(days_back: int = 90) -> dict:
    """Pull daily generation for every inverter connection (all vendors).

    Called by the scheduler at 03:00 UTC daily. Errors per connection are
    logged + recorded on the row but do not crash the scheduler.
    """
    today = date.today()
    start = today - timedelta(days=days_back)
    end = today

    results: list[dict] = []

    with SessionLocal() as db:
        for c in _resolve_connections(db):
            arr = c.array
            module = VENDORS.get(c.vendor)
            if module is None:
                results.append({
                    "array_id": arr.id, "vendor": c.vendor,
                    "skipped": "unknown vendor",
                })
                continue
            if not getattr(module, "SUPPORTS_DAILY", False):
                # e.g. chint — manual CSV only, nothing to pull.
                results.append({
                    "array_id": arr.id, "vendor": c.vendor,
                    "skipped": "vendor has no daily pull",
                })
                continue

            try:
                entries = module.fetch_daily(c.config, start, end)
                n = _upsert_daily(db, arr.tenant_id, arr.id, c.vendor, entries)
                if c.row is not None:
                    c.row.status = "ok"
                    c.row.last_error = None
                    c.row.last_sync_at = now()
                if c.vendor == "solaredge":
                    _backfill_solaredge_location(db, arr, c.config, row=c.row)
                elif c.vendor == "fronius":
                    _backfill_fronius_location(db, arr, c.config)
                db.commit()
                results.append({"array_id": arr.id, "vendor": c.vendor, "days_pulled": n})
            except InverterError as exc:
                db.rollback()
                if c.row is not None:
                    c.row.status = "error"
                    c.row.last_error = str(exc)
                    db.commit()
                log.warning("inverter_pull array=%d vendor=%s error=%s", arr.id, c.vendor, exc)
                results.append({"array_id": arr.id, "vendor": c.vendor, "errors": [str(exc)]})
            except Exception as exc:  # noqa: BLE001 — one bad connection mustn't stop the rest
                db.rollback()
                log.error("inverter_pull unhandled array=%d vendor=%s: %s", arr.id, c.vendor, exc)
                results.append({"array_id": arr.id, "vendor": c.vendor, "errors": [str(exc)]})

    return {"connections_processed": len(results), "results": results}
