"""Retention for Cloud Capture audit tables (T2 hygiene).

``harvest_run`` is append-only and was growing unbounded (~28k rows / 7 days
with real customers — mostly warm-session OK ticks). Keep enough for the
trust UI + ops diagnosis; hard-delete older rows.

Tiered policy:
  * **warm OK** (status=ok, not logged_in_fresh) → keep WARM_OK_DAYS (14)
  * **everything else** (failures, fresh logins, errors) → keep KEEP_DAYS (45)

Never soft-delete secrets elsewhere — this is history only.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from sqlalchemy import and_, delete, or_, select

from .db import SessionLocal
from .models import HarvestRun, now

log = logging.getLogger("solar.vault_retention")

# Long retention for failures / fresh password logins (dispute + diagnosis).
DEFAULT_KEEP_DAYS = int(os.environ.get("HARVEST_RUN_KEEP_DAYS") or "45")
# Short retention for the high-volume warm-session OK ticks.
DEFAULT_WARM_OK_DAYS = int(os.environ.get("HARVEST_RUN_WARM_OK_DAYS") or "14")
_BATCH = int(os.environ.get("HARVEST_RUN_PRUNE_BATCH") or "5000")


def _floor_days(days: int, floor: int = 7) -> int:
    d = int(days)
    return floor if d < floor else d


def _delete_batches(db, where_clause, *, max_batches: int) -> tuple[int, int]:
    deleted = 0
    batches = 0
    for _ in range(max_batches):
        ids = db.execute(
            select(HarvestRun.id)
            .where(where_clause)
            .order_by(HarvestRun.started_at.asc())
            .limit(_BATCH)
        ).scalars().all()
        if not ids:
            break
        res = db.execute(delete(HarvestRun).where(HarvestRun.id.in_(ids)))
        n = int(res.rowcount or 0)
        db.commit()
        batches += 1
        deleted += n
        if n < _BATCH:
            break
    return deleted, batches


def prune_harvest_runs(
    *,
    keep_days: int | None = None,
    warm_ok_days: int | None = None,
    max_batches: int = 50,
) -> dict:
    """Hard-delete aged harvest_run rows under the tiered policy."""
    long_days = _floor_days(keep_days if keep_days is not None else DEFAULT_KEEP_DAYS)
    warm_days = _floor_days(
        warm_ok_days if warm_ok_days is not None else DEFAULT_WARM_OK_DAYS,
        floor=3,
    )
    # warm window must not exceed long window
    if warm_days > long_days:
        warm_days = long_days

    _now = now()
    long_cutoff = _now - timedelta(days=long_days)
    warm_cutoff = _now - timedelta(days=warm_days)

    with SessionLocal() as db:
        # 1) Absolute age: anything older than long_days
        del_long, bat_long = _delete_batches(
            db, HarvestRun.started_at < long_cutoff, max_batches=max_batches,
        )
        # 2) Warm OK only: status=ok AND not fresh login, older than warm_ok_days
        del_warm, bat_warm = _delete_batches(
            db,
            and_(
                HarvestRun.started_at < warm_cutoff,
                HarvestRun.status == "ok",
                or_(
                    HarvestRun.logged_in_fresh.is_(False),
                    HarvestRun.logged_in_fresh.is_(None),
                ),
            ),
            max_batches=max_batches,
        )

    out = {
        "ok": True,
        "deleted": del_long + del_warm,
        "deleted_long": del_long,
        "deleted_warm_ok": del_warm,
        "batches": bat_long + bat_warm,
        "keep_days": long_days,
        "warm_ok_days": warm_days,
        "long_cutoff": long_cutoff.isoformat() + "Z",
        "warm_cutoff": warm_cutoff.isoformat() + "Z",
    }
    log.info(
        "harvest_run_prune deleted=%s long=%s warm_ok=%s keep=%sd warm=%sd",
        out["deleted"], del_long, del_warm, long_days, warm_days,
    )
    return out