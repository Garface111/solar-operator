"""Bounded cleanup of disposable diagnostics; financial/source evidence is excluded.

Invoice/source artifacts, bills, daily generation, uploaded files, dispatch
idempotency records and payments have no expiry in this policy.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from sqlalchemy import and_, delete, or_, select
from .db import SessionLocal
from .models import CaptureEvent, Job

log = logging.getLogger(__name__)


def prune_runtime_diagnostics(*, apply=False, now=None, batch_size=1000, max_batches=10):
    """Preview by default. Only terminal pull-job and capture-debug rows expire."""
    now = now or datetime.utcnow()
    batch_size = max(1, min(2000, int(batch_size)))
    max_batches = max(1, min(20, int(max_batches)))
    normal = now - timedelta(days=30)
    failures = now - timedelta(days=90)
    policies = [
        (CaptureEvent, or_(CaptureEvent.created_at < failures,
            and_(CaptureEvent.created_at < normal, CaptureEvent.stage != "capture_error"))),
        (Job, and_(Job.kind == "pull_bills", or_(
            and_(Job.status == "succeeded", Job.finished_at < normal),
            and_(Job.status == "failed", Job.finished_at < failures)))),
    ]
    result = {"apply": bool(apply), "retention_days": {"normal_diagnostics":30,"failure_diagnostics":90},
              "protected": "All source, invoice, financial, delivery and uploaded-file records", "tables": {}}
    with SessionLocal() as db:
        for model, predicate in policies:
            inspected = removed = 0
            after_id = 0
            for _ in range(max_batches):
                ids = db.execute(select(model.id).where(predicate, model.id > after_id)
                                 .order_by(model.id).limit(batch_size)).scalars().all()
                if not ids: break
                inspected += len(ids)
                after_id = ids[-1]
                if apply:
                    # Recheck eligibility in the write; a changed active job is protected.
                    removed += max(0, db.execute(delete(model).where(model.id.in_(ids), predicate)).rowcount or 0)
                    db.commit()
                if len(ids) < batch_size: break
            result["tables"][model.__tablename__] = {"eligible_in_bounded_scan":inspected,"deleted":removed}
    if apply: log.info("runtime diagnostic retention: %s", result["tables"])
    return result
