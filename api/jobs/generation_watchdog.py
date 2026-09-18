"""Billing-safety watchdog: scan DailyGeneration + InverterDaily for physically
impossible kWh values and alert if any are found.

Belt-and-suspenders behind the ingest plausibility guard in
api/array_owners.py (inverter_capture). The guard should stop bad values at the
door, but a value can also arrive via a future code path, a manual import, or a
backfill — and Array Operator bills per-kWh, so a single cumulative/lifetime
value in a DAILY slot can invoice thousands (it did once: a 677,533 kWh row on a
144 kW array → ~$4k phantom bill). This watchdog runs daily BEFORE the usage
report so a bad row is caught and alerted before it can bill, never after.

A daily kWh value is "impossible" when it exceeds the array's (or inverter's)
rated power running flat-out for 24h — a deliberately generous ceiling (~4-5× a
real sunny day) that only ever catches unit-error / cumulative junk, never a
legitimately strong production day.

Read-only: it ALERTS, it does not mutate. Auto-correction stays a deliberate
human-run sweep so we never silently rewrite an owner's generation data.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Array, DailyGeneration, Inverter, InverterDaily, Tenant
from ..notify import send_internal_alert

log = logging.getLogger(__name__)

# Hours/day a system can physically run at full nameplate. 24h is the absolute
# ceiling; real sunny days are ~4-6 "peak sun hours", so this is ~4-5× generous.
_MAX_HOURS_PER_DAY = 24.0


def scan_implausible_generation() -> dict:
    """Return {'daily': [...], 'inverter': [...], 'ok': bool}. Each list holds
    dicts describing an implausible row. Pure read — never mutates."""
    daily_bad: list[dict] = []
    inv_bad: list[dict] = []

    with SessionLocal() as db:
        # Tenants that can still be invoiced. A deleted/deactivated tenant's
        # arrays are NOT soft-deleted with it, so its bad rows kept surfacing
        # here forever under the scrubbed contact "deleted+ten_xxx@invalid.local"
        # — 37 alerts in the ops inbox naming accounts that can never bill
        # (Ford, 2026-09-18). This is a BILLING-safety watchdog: no billing, no
        # finding.
        live_tenants = {
            t.id: t for t in db.execute(
                select(Tenant).where(Tenant.active.is_(True))
            ).scalars().all()
            if not (t.contact_email or "").lower().endswith("@invalid.local")
        }

        # ── Array-level DailyGeneration: ceiling = Σ inverter nameplates × 24h ──
        arrays = [
            a for a in db.execute(
                select(Array).where(Array.deleted_at.is_(None))
            ).scalars().all()
            if a.tenant_id in live_tenants
        ]
        for a in arrays:
            np = db.execute(
                select(func.coalesce(func.sum(Inverter.nameplate_kw), 0.0)).where(
                    Inverter.array_id == a.id, Inverter.deleted_at.is_(None)
                )
            ).scalar() or 0.0
            if np <= 0:
                continue  # no nameplate basis → can't judge; skip (don't guess)
            cap = np * _MAX_HOURS_PER_DAY
            for r in db.execute(
                select(DailyGeneration).where(
                    DailyGeneration.array_id == a.id, DailyGeneration.kwh > cap
                )
            ).scalars().all():
                t = live_tenants.get(a.tenant_id)
                daily_bad.append({
                    "tenant": getattr(t, "contact_email", None) or a.tenant_id,
                    "array": a.name, "array_id": a.id,
                    "day": r.day.isoformat(), "kwh": r.kwh,
                    "ceiling": round(cap, 1), "nameplate_kw": round(np, 1),
                })

        # ── Per-inverter InverterDaily: ceiling = nameplate × 24h ──
        invs = [
            iv for iv in db.execute(
                select(Inverter).where(
                    Inverter.deleted_at.is_(None), Inverter.nameplate_kw > 0
                )
            ).scalars().all()
            if iv.tenant_id in live_tenants
        ]
        for iv in invs:
            iv_np = float(iv.nameplate_kw or 0.0)
            if iv_np <= 0:
                continue
            cap = iv_np * _MAX_HOURS_PER_DAY
            for r in db.execute(
                select(InverterDaily).where(
                    InverterDaily.inverter_id == iv.id, InverterDaily.kwh > cap
                )
            ).scalars().all():
                t = live_tenants.get(iv.tenant_id)
                inv_bad.append({
                    "tenant": getattr(t, "contact_email", None) or iv.tenant_id,
                    "inverter": iv.name, "inverter_id": iv.id,
                    "day": r.day.isoformat(), "kwh": r.kwh,
                    "ceiling": round(cap, 1), "nameplate_kw": round(iv_np, 1),
                })

    return {"daily": daily_bad, "inverter": inv_bad,
            "ok": not daily_bad and not inv_bad}


def run_generation_watchdog() -> dict:
    """Daily watchdog: alert (once per run) if any implausible kWh row exists in
    the billing meter (DailyGeneration) or per-inverter history (InverterDaily).
    Stays SILENT when everything is clean. Returns the scan result."""
    result = scan_implausible_generation()
    if result["ok"]:
        log.info("generation_watchdog: clean — no implausible kWh rows")
        return result

    n_d = len(result["daily"])
    n_i = len(result["inverter"])
    lines = [
        f"⚠️ Implausible generation rows detected: {n_d} DailyGeneration "
        f"(billing meter) + {n_i} InverterDaily.",
        "A daily kWh value above the array/inverter nameplate × 24h is "
        "physically impossible — almost always a cumulative/lifetime value that "
        "leaked into a daily slot. Array Operator bills per-kWh, so a "
        "DailyGeneration hit can over-invoice. Investigate + run the correction "
        "sweep (replace with the median sane day).",
        "",
    ]
    for b in result["daily"][:25]:
        lines.append(
            f"  [DAILY/BILLING] {b['tenant']} · {b['array']} {b['day']}: "
            f"{b['kwh']:,.0f} kWh (ceiling {b['ceiling']:,.0f}, {b['nameplate_kw']} kW)"
        )
    for b in result["inverter"][:25]:
        lines.append(
            f"  [INVERTER] {b['tenant']} · {b['inverter']} {b['day']}: "
            f"{b['kwh']:,.0f} kWh (ceiling {b['ceiling']:,.0f}, {b['nameplate_kw']} kW)"
        )
    if n_d + n_i > 50:
        lines.append(f"  … and {n_d + n_i - 50} more.")

    # Re-alert on CHANGE, not on the calendar. The finding set is stable until
    # someone runs the correction sweep, so a daily re-send of the identical
    # list was 37 copies of one unread email (Ford, 2026-09-18). The dedupe key
    # in notify is subject+body, so a NEW bad row changes the body and pages
    # immediately; an unchanged set repeats at most weekly as a reminder.
    send_internal_alert(
        f"Generation watchdog: {n_d + n_i} implausible kWh row(s)",
        "\n".join(lines),
        throttle_min=float(os.getenv("GEN_WATCHDOG_REPEAT_MIN") or 60 * 24 * 7),
    )
    log.warning("generation_watchdog: %d daily + %d inverter implausible rows", n_d, n_i)
    return result
