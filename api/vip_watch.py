"""VIP watch — every REAL account gets a tight self-heal SLA AND fast Ford
alerting if it doesn't work. No tiers, no "protect the inbox" throttle.

Ford, 2026-07-08 (re: Lester/Brattleboro Solar): "set up an agent that monitors
his account for anomalies. if the data gets stale, more than 15 min, his
extension triggers." Then: "I think all accounts should be babied like this."
Then, same day, after finding this alert sweep had been tiered (fast for
hand-picked `vip_watch` tenants, a 6-HOUR bar for everyone else, specifically
to avoid flooding his inbox): "we are hardcore not babies who try to conserve
electricity... find every instance of us intentionally sabotaging our own
reliability and fix it." The 6-hour tier for "everyone else" was exactly that
kind of sabotage — a self-imposed politeness tradeoff, not a real constraint.
Fixed: ONE fast bar for every tenant, no exceptions, no tiers.

The inbox-flooding problem this was originally trying to solve was misdiagnosed
anyway: a real prod census (2026-07-08) found the noise wasn't "too many real
accounts alerting" — `Tenant.is_demo` was simply never checked here, so most
of what would have alerted were Ford's OWN test/scratch signups (see
fronius-real-stale-census memory). The actual fix for noise is filtering
`is_demo`, not slowing down alerts for real customers.

Two halves:

  1. `vip_stale_vendors()` — called from capture_debt.compute_capture_debt for
     EVERY non-demo tenant. Every extension heartbeat (every 60s, from
     whichever browser is open) already carries a "debt" instruction telling
     the extension what to recapture — this makes that check MUCH tighter
     (minutes, not the normal multi-day bar), gated on DAYLIGHT so a normal
     overnight gap is never mistaken for staleness. No new extension-side code
     needed: the drain mechanism already exists.

  2. `vip_watch_sweep()` — a scheduler job, independent of any heartbeat (a
     genuinely-closed browser never heartbeats at all, so #1 can only help
     while SOME browser is open). Runs for every ACTIVE, non-demo tenant, one
     universal fast bar (ALERT_AFTER_MINUTES). Alerts Ford ONCE per incident,
     clears on recovery, can re-fire later.

DISABLED BY FORD 2026-07-19 — both halves are gated off by default; see
VIP_WATCH_ENABLED below.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import select

log = logging.getLogger("vip_watch")

# Minute-granularity self-heal bar — applies to every non-demo tenant (see #1 above).
# Deliberately far tighter than capture_debt's VENDOR_STALE_DAYS=2; safe only
# because it's daylight-gated, so a normal overnight gap never counts.
VIP_STALE_MINUTES = 15
# Alert-Ford bar. ONE tier, universal -- no "protect the inbox" throttle.
ALERT_AFTER_MINUTES = 90


# Ford, 2026-07-19: "please disable the vip watch system." OFF by default — this
# gates BOTH halves (the silent per-heartbeat self-heal nudge AND the alert
# sweep), so the whole system is inert unless explicitly switched back on.
#
# NOTE this is a deliberate, Ford-directed shutdown, NOT the self-sabotage the
# module docstring above argues against: that rule is about agents quietly
# disarming their own reliability to "protect the inbox". Re-enable with
# VIP_WATCH_ENABLED=1 (no code change needed).
def vip_watch_enabled() -> bool:
    return (os.getenv("VIP_WATCH_ENABLED", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
_EXT_VENDORS = ("fronius", "sma", "chint")


def vip_stale_vendors(db, tenant_id: str, *, now_: datetime | None = None) -> set[str]:
    """Extension vendors with at least one DAYTIME array whose newest live
    capture (Inverter.last_power_at) is older than VIP_STALE_MINUTES. Cheap,
    read-only, called for every tenant on every heartbeat. Merge the result
    into a capture-debt "drain" list so the next heartbeat (within ~60s, if a
    browser is open) nudges a recapture — far faster than the normal
    day-granularity bar.

    Returns an empty set while the system is disabled (VIP_WATCH_ENABLED)."""
    if not vip_watch_enabled():
        return set()

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
    """Scheduler job: for every ACTIVE, non-demo tenant, flag any DAYTIME array
    whose extension-vendor data has been stale past ALERT_AFTER_MINUTES — long
    enough that the universal self-heal nudge (vip_stale_vendors, above) hasn't
    been able to fix it, meaning the owner's browser is probably not open. ONE
    bar for everyone, no tiers. Alerts Ford ONCE per incident (InverterAlertState
    dedup, namespaced 'vip_stale:' — this table is SHARED across alert jobs,
    never touch a key outside your own namespace); the incident clears itself,
    and can re-fire later, once the array reports fresh again.

    No-ops while the system is disabled (VIP_WATCH_ENABLED)."""
    if not vip_watch_enabled():
        log.info("vip_watch_sweep: disabled (VIP_WATCH_ENABLED off) — skipping")
        return {"alerted": [], "recovered_cleared": 0, "skipped_dedup": 0,
                "dry_run": dry_run, "disabled": True}

    from .db import SessionLocal
    from .inverter_fleet import _daylight_for
    from .models import Array, Inverter, InverterAlertState, Tenant
    from .notify import send_internal_alert

    out = {"alerted": [], "recovered_cleared": 0, "skipped_dedup": 0, "dry_run": dry_run}
    now_ = datetime.utcnow()
    alert_cutoff = now_ - timedelta(minutes=ALERT_AFTER_MINUTES)

    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(Tenant.active.is_(True), Tenant.is_demo.is_(False))
        ).scalars().all()
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
            if not invs:
                continue
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
                            f"This array has been stale for over {ALERT_AFTER_MINUTES} minutes during "
                            "daylight, despite the tight self-heal nudge every tenant gets on every "
                            "extension heartbeat. That combination almost always means their browser "
                            "isn't open right now — Fronius/SMA only refresh while a signed-in "
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
