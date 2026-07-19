"""Retention for Cloud Capture audit tables (T2 hygiene).

``harvest_run`` is append-only and was growing unbounded (~28k rows / 7 days
with real customers). Keep enough for trust UI + ops diagnosis; hard-delete
older rows. Never soft-delete secrets elsewhere — this is history only.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from sqlalchemy import delete, select

from .db import SessionLocal
from .models import HarvestRun, now

log = logging.getLogger("solar.vault_retention")

# Default 45 days: covers monthly bill cycles + a little cushion for disputes.
DEFAULT_KEEP_DAYS = int(os.environ.get("HARVEST_RUN_KEEP_DAYS") or "45")
# Batch deletes so a huge backlog doesn't lock the table forever.
_BATCH = int(os.environ.get("HARVEST_RUN_PRUNE_BATCH") or "5000")


def prune_harvest_runs(*, keep_days: int | None = None, max_batches: int = 50) -> dict:
    """Hard-delete harvest_run rows older than keep_days. Returns counts."""
    days = int(keep_days if keep_days is not None else DEFAULT_KEEP_DAYS)
    if days < 7:
        # Safety floor — never prune last week of audit trail by misconfig.
        days = 7
    cutoff = now() - timedelta(days=days)
    deleted = 0
    batches = 0
    with SessionLocal() as db:
        for _ in range(max_batches):
            ids = db.execute(
                select(HarvestRun.id)
                .where(HarvestRun.started_at < cutoff)
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
    out = {
        "ok": True,
        "deleted": deleted,
        "batches": batches,
        "keep_days": days,
        "cutoff": cutoff.isoformat() + "Z",
    }
    log.info("harvest_run_prune deleted=%s keep_days=%s", deleted, days)
    return out
