"""VIP watch — a tight-SLA staleness override for a hand-picked "babied account".

Ford, 2026-07-08: "set up an agent that monitors [an operator]'s account for
anomalies. if the data gets stale, more than 15 min, his extension triggers."

Two halves, each reusing an EXISTING mechanism rather than inventing a parallel
one (capture_debt.py already solves "nudge a browser to recapture"; the feature-
suggestion pipeline already emails Ford on every submission via notify.py):

  1. `vip_stale_vendors()` — called from capture_debt.compute_capture_debt for
     any vip_watch tenant. Every extension heartbeat (every 60s, from whichever
     browser is open) already carries a "debt" instruction telling the
     extension what to recapture — this just makes that check MUCH tighter
     (minutes, not the normal multi-day bar) for a watched tenant, gated on
     DAYLIGHT so a normal overnight gap is never mistaken for staleness. No new
     extension-side code needed: the drain mechanism already exists.

  2. `vip_watch_sweep()` — a scheduler job, independent of any heartbeat, since
     a genuinely-closed browser never heartbeats at all and #1 can only help
     while SOME browser is open. This catches "stayed stale anyway" — evidence
     the owner's browser probably isn't open (exactly what the in-app toast
     explains to them) — and emails Ford ONCE per incident via the same
     send_internal_alert used for signups/feature-suggestions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

log = logging.getLogger("vip_watch")

# Minute-granularity bar for a watched tenant — deliberately far tighter than
# capture_debt's VENDOR_STALE_DAYS=2. Only ever applied to vip_watch tenants;
# applying this to everyone would false-positive on any normal overnight gap
# capture_debt's daylight-agnostic day bar is built to tolerate.
VIP_STALE_MINUTES = 15
# How long past that self-heal window before it's worth waking Ford up: the
# heartbeat-driven nudge above only works if a browser IS open and pinging;
# staying stale past this means it probably isn't.
VIP_ALERT_AFTER_MINUTES = 90
_EXT_VENDORS = ("fronius", "sma", "chint")


def vip_stale_vendors(db, tenant_id: str, *, now_: datetime | None = None) -> set[str]:
    """Extension vendors with at least one DAYTIME array whose newest live
    capture (Inverter.last_power_at) is older than VIP_STALE_MINUTES. Cheap,
    read-only. Merge the result into a capture-debt "drain" list so the next
    heartbeat (within ~60s, if a browser is open) nudges a recapture — far
    faster than the normal day-granularity bar everyone else rides."""
    from .inverter_fleet import _daylight_for
    from .models import Array, Inverter

    now_ = now_ or datetime.utcnow()
    cutoff = now_ - timedelta(minutes=VIP_STALE_MINUTES)
    stale: set[str] = set()

    arrays = {a.id: a for a in db.execute(
        select(Array).where(Array.tenant_id == tenant_id, Array.deleted_at.is_(None))
    ).scalars().all()}
    if not arrays:
        return stale

    invs = db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant_id,
            Inverter.vendor.in_(_EXT_VENDORS),
            Inverter.array_id.in_(list(arrays)),
            Inverter.deleted_at.is_(None),
        )
    ).scalars().all()

    by_array: dict[int, list] = {}
    for iv in invs:
        by_array.setdefault(iv.array_id, []).append(iv)

    for array_id, ivs in by_array.items():
        arr = arrays.get(array_id)
        if arr is None or not _daylight_for(arr, default=True):
            continue          # nighttime for this array — a capture gap is normal, not stale
        newest = max((iv.last_power_at for iv in ivs if iv.last_power_at), default=None)
        if newest is None or newest < cutoff:
            stale.update(iv.vendor for iv in ivs)
    return stale


def vip_watch_sweep(dry_run: bool = False) -> dict:
    """Scheduler job: for every vip_watch tenant, flag any DAYTIME array whose
    extension-vendor data has been stale past VIP_ALERT_AFTER_MINUTES — long
    enough that the heartbeat-driven nudge (vip_stale_vendors, above) hasn't
    been able to fix it, meaning the owner's browser is probably not open.
    Alerts Ford ONCE per incident (InverterAlertState dedup, namespaced
    'vip_stale:' — this table is SHARED across alert jobs, never touch a key
    outside your own namespace); the incident clears itself, and can re-fire
    later, once the array reports fresh again."""
    from .db import SessionLocal
    from .inverter_fleet import _daylight_for
    from .models import Array, Inverter, InverterAlertState, Tenant
    from .notify import send_internal_alert

    out = {"alerted": [], "recovered_cleared": 0, "skipped_dedup": 0, "dry_run": dry_run}
    now_ = datetime.utcnow()
    alert_cutoff = now_ - timedelta(minutes=VIP_ALERT_AFTER_MINUTES)

    with SessionLocal() as db:
        tenants = db.execute(select(Tenant).where(Tenant.vip_watch.is_(True))).scalars().all()
        for t in tenants:
            arrays = db.execute(
                select(Array).where(Array.tenant_id == t.id, Array.deleted_at.is_(None))
            ).scalars().all()
            if not arrays:
                continue
            array_ids = [a.id for a in arrays]
            invs = db.execute(
                select(Inverter).where(
                    Inverter.tenant_id == t.id,
                    Inverter.vendor.in_(_EXT_VENDORS),
                    Inverter.array_id.in_(array_ids),
                    Inverter.deleted_at.is_(None),
                )
            ).scalars().all()
            by_array: dict[int, list] = {}
            for iv in invs:
                by_array.setdefault(iv.array_id, []).append(iv)
            arr_by_id = {a.id: a for a in arrays}

            for array_id, ivs in by_array.items():
                arr = arr_by_id.get(array_id)
                key = f"vip_stale:{t.id}:{array_id}"
                state = db.execute(select(InverterAlertState).where(
                    InverterAlertState.tenant_id == t.id,
                    InverterAlertState.incident_key == key)).scalar_one_or_none()

                if arr is None or not _daylight_for(arr, default=True):
                    continue   # nighttime — never alert, and never clear a real incident on a night reading
                newest = max((iv.last_power_at for iv in ivs if iv.last_power_at), default=None)
                fresh = newest is not None and newest >= alert_cutoff
                if fresh:
                    if state is not None:
                        if not dry_run:
                            db.delete(state)
                            db.commit()
                        out["recovered_cleared"] += 1
                    continue

                # Stale past the alert bar. Alert ONCE per incident.
                if state is not None and state.last_alerted_at is not None:
                    out["skipped_dedup"] += 1
                    continue

                vendors = sorted({iv.vendor for iv in ivs})
                age_min = int((now_ - newest).total_seconds() / 60) if newest else None
                if not dry_run:
                    send_internal_alert(
                        subject=f"[VIP watch] {t.name} — {arr.name} data is stale",
                        body=(
                            f"Tenant: {t.name} ({t.id})\n"
                            f"Array: {arr.name} (id {array_id})\n"
                            f"Vendor(s): {', '.join(vendors)}\n"
                            f"Last live capture: {'never' if age_min is None else f'{age_min} min ago'}\n\n"
                            f"This array has been stale for over {VIP_ALERT_AFTER_MINUTES} minutes "
                            "during daylight, despite the tight self-heal nudge this tenant gets on "
                            "every extension heartbeat. That combination almost always means their "
                            "browser isn't open right now — Fronius/SMA only refresh while a signed-in "
                            "browser is running. Worth a nudge to the customer."
                        ),
                    )
                    if state is None:
                        state = InverterAlertState(tenant_id=t.id, incident_key=key)
                        db.add(state)
                    state.last_alerted_at = now_
                    db.commit()
                out["alerted"].append({"tenant_id": t.id, "array_id": array_id, "vendors": vendors})
    return out
