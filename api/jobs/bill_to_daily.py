"""Bill → daily-production transformer.

THE MISSING LINK in the data pipeline. We capture + richly parse 47k GMP bills
(monthly statements with kwh_generated, cost, consumption, rate), but the
frontend reads the DAILY streams (DailyGeneration + the GMP 15-min sponge) — so
a parsed bill never surfaced in Trends and never merged with inverter output.

This module converts each bill's metered generation into the daily stream the
frontend ALREADY knows how to display + merge: it writes DailyGeneration rows
with source="bill_prorate" (a source family the UI already renders as
"Bill (prorated)"). Generation is spread evenly across the bill's service days.

PRIORITY (critical): bill-prorate is the COARSEST source. It ONLY fills days no
real metered reading covers. The (array_id, day) unique constraint + an explicit
source check mean we NEVER overwrite an inverter / CSV / GMP-API daily reading —
real data always wins; bill-prorate just stops a gap from showing as zero. An
older bill_prorate row CAN be refreshed by a newer bill (re-pull / correction).

Idempotent: re-running only writes/updates bill_prorate days; counts what changed.
READ of bills, WRITE only of DailyGeneration rows it owns (source=bill_prorate).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Bill, UtilityAccount, DailyGeneration, Array, now

logger = logging.getLogger(__name__)

BILL_SOURCE = "bill_prorate"
# Sources we must never overwrite with a coarse bill_prorate estimate. DERIVED
# from the canonical registry (api.generation_sources) so it can't drift from
# forecasting / inverter_fleet again (audit #12) — this formerly hard-coded only
# solaredge/fronius/sma/chint and silently omitted enphase/solis/tigo/alsoenergy/
# locus, letting a bill smear clobber those real vendors. `utility_meter` is added
# on top: it is a finer per-day meter reading than a whole-bill smear, so the
# coarser bill_prorate must never overwrite it either. Only a None row or our own
# earlier bill_prorate estimate may be written/refreshed below.
from ..generation_sources import MEASURED_SOURCES as _MEASURED_SOURCES
_REAL_SOURCES = _MEASURED_SOURCES | {"utility_meter"}

# bill-prorate is an ESTIMATE (a monthly bill spread flat across its days). It must
# NEVER stand in for real metered production on days the inverter / GMP-API pull
# still owns — that is exactly how a net-metering bill (~83 kWh/day) ended up shown
# as "daily production" next to gross SMA output (~803 kWh/day). So we never prorate
# today, the future, or the trailing guard window; those days wait for the
# authoritative real reading (or honestly show as a gap). A bill window that runs
# past the cutoff is clamped; one that STARTS past it (a future-dated / mis-parsed
# bill) is skipped entirely.
PRORATE_RECENCY_GUARD_DAYS = 2


def _bill_days(b: Bill) -> Optional[tuple[date, date]]:
    """The bill's inclusive service window as dates, or None if unusable."""
    if b.period_start is None or b.period_end is None:
        return None
    ps = b.period_start.date() if hasattr(b.period_start, "date") else b.period_start
    pe = b.period_end.date() if hasattr(b.period_end, "date") else b.period_end
    if pe < ps:
        return None
    return ps, pe


def transform_array_bills(db: Session, array_id: int) -> dict:
    """Prorate every GMP bill for one array into bill_prorate DailyGeneration rows.

    Returns {array_id, bills_seen, days_written, days_updated, days_skipped_real}.
    days_skipped_real = days left untouched because a real reading already covers
    them (the whole point — real data wins).
    """
    acct_ids = list(db.execute(
        select(UtilityAccount.id).where(
            UtilityAccount.array_id == array_id,
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalars().all())
    if not acct_ids:
        return {"array_id": array_id, "bills_seen": 0, "days_written": 0,
                "days_updated": 0, "days_skipped_real": 0}

    tenant_id = db.execute(
        select(Array.tenant_id).where(Array.id == array_id)
    ).scalar_one_or_none()

    # Include bills with generation OR group excess (sent-to-grid). Some group-
    # host months only resolve a clean pool on kwh_sent_to_grid.
    bills = list(db.execute(
        select(Bill).where(
            Bill.account_id.in_(acct_ids),
            Bill.period_end.isnot(None),
        ).order_by(Bill.period_end)
    ).scalars().all())

    # Pre-load existing DailyGeneration rows for this array, keyed by day, so we
    # decide write/update/skip without a query per day.
    existing = {
        d: src for d, src in db.execute(
            select(DailyGeneration.day, DailyGeneration.source)
            .where(DailyGeneration.array_id == array_id)
        ).all()
    }

    written = updated = skipped_real = 0
    # When several bills (multiple meters) cover the same day, sum their per-day
    # prorate so a multi-meter array's day reflects all meters.
    prorate_by_day: dict[date, float] = {}
    # Never estimate today / the future / the trailing guard window — real metered
    # pulls own those days (see PRORATE_RECENCY_GUARD_DAYS).
    cutoff = now().date() - timedelta(days=PRORATE_RECENCY_GUARD_DAYS)
    from ..billing.group_host_bill import group_excess_pool
    for b in bills:
        win = _bill_days(b)
        if win is None:
            continue
        ps, pe = win
        if ps > cutoff:
            # Future-dated / too-recent bill window — refuse to project it forward.
            continue
        # HARD RULE (Colleen / group host): smear Group Excess Shared, never
        # page-1 snapshot gross and never gross when shared < generation.
        pool, _src, _warn = group_excess_pool(b)
        if pool is None or pool <= 0:
            continue
        n_days = (pe - ps).days + 1          # share over the bill's TRUE window
        per_day = float(pool) / n_days
        write_end = min(pe, cutoff)          # only fill days up to the cutoff
        d = ps
        while d <= write_end:
            prorate_by_day[d] = prorate_by_day.get(d, 0.0) + per_day
            d += timedelta(days=1)

    for d, kwh in prorate_by_day.items():
        kwh = round(kwh, 4)
        src = existing.get(d)
        if src is None:
            db.add(DailyGeneration(
                tenant_id=tenant_id, array_id=array_id, day=d,
                kwh=kwh, source=BILL_SOURCE, uploaded_at=now(),
            ))
            written += 1
        elif src == BILL_SOURCE:
            # Refresh our own earlier estimate (re-pull / corrected bill).
            row = db.execute(
                select(DailyGeneration).where(
                    DailyGeneration.array_id == array_id,
                    DailyGeneration.day == d,
                )
            ).scalar_one_or_none()
            if row is not None and abs((row.kwh or 0) - kwh) > 1e-6:
                row.kwh = kwh
                row.uploaded_at = now()
                updated += 1
        else:
            # A real metered reading already covers this day — never clobber it.
            skipped_real += 1

    return {"array_id": array_id, "bills_seen": len(bills),
            "days_written": written, "days_updated": updated,
            "days_skipped_real": skipped_real}


def transform_tenant_bills(tenant_id: str, *, db: Optional[Session] = None) -> dict:
    """Run the bill→daily transform for every array under a tenant. Commits."""
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        array_ids = list(db.execute(
            select(Array.id).where(
                Array.tenant_id == tenant_id,
                Array.deleted_at.is_(None),
            )
        ).scalars().all())
        totals = {"arrays": 0, "bills_seen": 0, "days_written": 0,
                  "days_updated": 0, "days_skipped_real": 0}
        for aid in array_ids:
            r = transform_array_bills(db, aid)
            totals["arrays"] += 1
            for k in ("bills_seen", "days_written", "days_updated", "days_skipped_real"):
                totals[k] += r[k]
        if _own:
            db.commit()
        return totals
    finally:
        if _own:
            db.close()


def transform_all_tenants() -> dict:
    """Scheduled entry point: prorate bills→daily for every active tenant that
    has GMP bills. Idempotent + incremental (only fills/refreshes bill_prorate
    days). Safe to run alongside the inverter + GMP-daily pulls — real readings
    always win the (array, day) slot."""
    from ..models import Tenant
    with SessionLocal() as db:
        tenant_ids = list(db.execute(
            select(Tenant.id).where(Tenant.active == True)  # noqa: E712
        ).scalars().all())
    grand = {"tenants": 0, "arrays": 0, "bills_seen": 0,
             "days_written": 0, "days_updated": 0, "days_skipped_real": 0}
    for tid in tenant_ids:
        t = transform_tenant_bills(tid)
        if t["bills_seen"] or t["days_written"]:
            grand["tenants"] += 1
            for k in ("arrays", "bills_seen", "days_written", "days_updated", "days_skipped_real"):
                grand[k] += t[k]
    logger.info("bill→daily transform: %s", grand)
    return grand
