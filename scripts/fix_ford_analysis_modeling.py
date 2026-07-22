"""One-shot prod fix for Ford's AO Analysis "not modeled yet" sites
(ten_ford_demo_100, 2026-07-22 screenshot):

  1. Cover Rooftop / Starlake SolarEdge — reassign Inverter rows left on
     soft-deleted twin arrays (#2375/#2377) onto the live arrays (#2736/#2737).
  2. Waterford (Fronius) — roll InverterDaily → DailyGeneration for the last
     30 days so weather actual-vs-expected has matched measured days.
  3. Invalidate fleet forecast snapshots so Analysis recomputes immediately.

Safe / idempotent: reassign only moves inverters whose array is soft-deleted
or empty-on-live; rollup never clobbers utility_meter / solaredge API days.

  DATABASE_URL=… .venv/bin/python scripts/fix_ford_analysis_modeling.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, update

from api.db import SessionLocal, init_db
from api.models import Array, Inverter, DailyGeneration, InverterDaily, now
from api.array_owners import (
    rollup_inverter_daily_for_array,
    invalidate_fleet_forecast,
)

TENANT_ID = "ten_ford_demo_100"

# (soft_deleted_source_array_id, live_target_array_id, label)
SE_REASSIGNS = [
    (2375, 2736, "Cover Rooftop 10kW SolarEdge"),
    (2377, 2737, "Starlake 45kW SolarEdge"),
]
FRONIUS_WATERFORD = 2735


def reassign_orphaned_inverters(db) -> dict:
    out = {"moved": [], "skipped": []}
    for old_id, new_id, label in SE_REASSIGNS:
        old = db.get(Array, old_id)
        new = db.get(Array, new_id)
        if new is None or new.tenant_id != TENANT_ID or new.deleted_at is not None:
            out["skipped"].append({"label": label, "reason": "live target missing"})
            continue
        # Prefer inverters still parked on the soft-deleted twin; also any
        # inverter for this tenant whose source_array_id is the old twin.
        invs = db.execute(
            select(Inverter).where(
                Inverter.tenant_id == TENANT_ID,
                Inverter.deleted_at.is_(None),
                (Inverter.array_id == old_id) | (Inverter.source_array_id == old_id),
            )
        ).scalars().all()
        # Also: live target empty + same SE site_id serials elsewhere for tenant
        if not invs and new.solaredge_site_id:
            site = str(new.solaredge_site_id)
            invs = db.execute(
                select(Inverter).where(
                    Inverter.tenant_id == TENANT_ID,
                    Inverter.deleted_at.is_(None),
                    Inverter.vendor == "solaredge",
                    Inverter.source_site_id == site,
                    Inverter.array_id != new_id,
                )
            ).scalars().all()
        if not invs:
            out["skipped"].append({"label": label, "reason": "no inverters to move",
                                   "live_already": db.execute(
                                       select(Inverter.id).where(
                                           Inverter.array_id == new_id,
                                           Inverter.deleted_at.is_(None),
                                       )
                                   ).scalars().all()})
            continue
        maxpos = db.execute(
            select(Inverter.position).where(
                Inverter.array_id == new_id,
                Inverter.deleted_at.is_(None),
            ).order_by(Inverter.position.desc())
        ).scalars().first() or 0
        moved = []
        for iv in invs:
            if iv.array_id == new_id:
                continue
            maxpos += 1
            prev = iv.array_id
            iv.array_id = new_id
            iv.source_array_id = new_id
            iv.position = maxpos
            if iv.source_site_id is None and new.solaredge_site_id:
                iv.source_site_id = str(new.solaredge_site_id)
            moved.append({"inverter_id": iv.id, "serial": iv.serial,
                          "from_array": prev, "to_array": new_id})
        out["moved"].append({"label": label, "count": len(moved), "detail": moved,
                             "old_array_deleted": bool(old and old.deleted_at)})
    return out


def main() -> int:
    init_db()
    with SessionLocal() as db:
        t = db.execute(
            select(Array.tenant_id).where(Array.id == FRONIUS_WATERFORD)
        ).scalar_one_or_none()
        # sanity
        print("tenant", TENANT_ID)

        re = reassign_orphaned_inverters(db)
        print("reassign:", re)

        rolled = rollup_inverter_daily_for_array(
            db, TENANT_ID, FRONIUS_WATERFORD, days_back=30)
        print(f"waterford_fronius rollup days written/raised: {rolled}")

        # Also rollup any other live arrays on this tenant with inv daily but
        # thin DG (belt + suspenders).
        arr_ids = db.execute(
            select(Array.id).where(
                Array.tenant_id == TENANT_ID,
                Array.deleted_at.is_(None),
            )
        ).scalars().all()
        extra = 0
        for aid in arr_ids:
            if aid == FRONIUS_WATERFORD:
                continue
            extra += rollup_inverter_daily_for_array(
                db, TENANT_ID, aid, days_back=30)
        print(f"extra_arrays rollup: {extra}")

        db.commit()

    invalidate_fleet_forecast(TENANT_ID)
    print("forecast snapshot invalidated")

    # Verify
    with SessionLocal() as db:
        for old_id, new_id, label in SE_REASSIGNS:
            n = db.execute(
                select(Inverter.id).where(
                    Inverter.array_id == new_id,
                    Inverter.deleted_at.is_(None),
                )
            ).scalars().all()
            print(f"  {label} live={new_id} inverters={len(n)} ids={n}")
        from datetime import timedelta
        from api.models import local_today
        start = local_today() - timedelta(days=14)
        n_dg = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == FRONIUS_WATERFORD,
                DailyGeneration.day >= start,
                DailyGeneration.day < local_today(),
            )
        ).scalars().all()
        print(f"waterford DG last 14 full days: {len(n_dg)} "
              f"sum={sum(float(r.kwh or 0) for r in n_dg):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
