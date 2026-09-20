"""Lossless resumable GMP source migration; dry-run is the default.

Run `python -m scripts.compact_gmp_sources --limit 25`. Add --apply only after
reviewing estimates and provisioning the additive schema with api.migrate.
Every source is locked, archived and roundtrip-verified before its inline field
is cleared in the SAME transaction. No source rows, metadata or IDs are deleted.
Repeated runs naturally skip completed rows; --after-id is an optional checkpoint.
"""
from __future__ import annotations

import hashlib
import time
import os
import tempfile
from contextlib import contextmanager
import zlib

from sqlalchemy import select, func, cast, LargeBinary, text

from .db import SessionLocal
from .models import GmpUsageRaw
from .source_artifacts import content_chunks, preserve_raw_version


def _compact_batch_unlocked(session_factory=SessionLocal, *, apply=False, limit=25, after_id=0,
                  tenant_id=None, max_bytes=32 * 1024 * 1024, max_source_bytes=64 * 1024 * 1024,
                  max_seconds=30):
    if not 1 <= limit <= 1000 or max_bytes <= 0 or max_source_bytes <= 0 or after_id < 0 or max_seconds <= 0:
        raise ValueError("Invalid migration batch bounds")
    started = time.monotonic()
    result = {"mode": "apply" if apply else "dry-run", "processed": 0, "inline_bytes": 0,
              "new_compressed_bytes_upper_bound": None if apply else 0, "last_id": after_id, "more": False}
    seen_chunks = set()
    with session_factory() as db:
        # Fetch metadata only; never materialize a whole batch of raw CSVs.
        size_expr = (func.octet_length(GmpUsageRaw.raw_csv) if db.get_bind().dialect.name == "postgresql"
                     else func.length(cast(GmpUsageRaw.raw_csv, LargeBinary)))
        query = select(GmpUsageRaw.id, size_expr.label("size")).where(
            GmpUsageRaw.id > after_id, GmpUsageRaw.raw_csv.isnot(None))
        if tenant_id:
            query = query.where(GmpUsageRaw.tenant_id == tenant_id)
        candidates = db.execute(query.order_by(GmpUsageRaw.id).limit(limit + 1)).all()
    result["more"] = len(candidates) > limit
    for candidate in candidates[:limit]:
        if time.monotonic() - started >= max_seconds:
            result.update(more=True, reason="time budget reached")
            break
        if candidate.size > max_source_bytes:
            result.update(more=True, blocked_id=candidate.id, reason="source exceeds max_source_bytes")
            break
        if result["inline_bytes"] + candidate.size > max_bytes:
            result.update(more=True, reason="byte budget reached")
            if not result["processed"]:
                result["blocked_id"] = candidate.id
            break
        with session_factory() as db:
            query = select(GmpUsageRaw).where(GmpUsageRaw.id == candidate.id)
            if tenant_id:
                query = query.where(GmpUsageRaw.tenant_id == tenant_id)
            if apply:
                query = query.with_for_update()
            row = db.execute(query).scalar_one_or_none()
            if row is None or row.raw_csv is None:
                result["last_id"] = candidate.id
                continue
            payload = row.raw_csv.encode("utf-8")
            # Enforce again under lock in case capture enlarged it since selection.
            if len(payload) > max_source_bytes:
                result.update(more=True, blocked_id=row.id, reason="source exceeds max_source_bytes")
                break
            if result["inline_bytes"] + len(payload) > max_bytes:
                result.update(more=True, reason="byte budget reached")
                if not result["processed"]:
                    result["blocked_id"] = row.id
                break
            if not apply:
                for chunk in content_chunks(payload, "text/csv"):
                    key = (row.tenant_id, hashlib.sha256(chunk).hexdigest())
                    if key not in seen_chunks:
                        seen_chunks.add(key)
                        result["new_compressed_bytes_upper_bound"] += min(len(chunk), len(zlib.compress(chunk, 6)))
            if apply:
                # preserve_raw_version already reconstructs, verifies SHA/length
                # and compares exact bytes before returning. Do not repeat that
                # work or calculate a speculative compression estimate here.
                preserve_raw_version(db, row)
                row.raw_csv = None
                db.commit()
            result["processed"] += 1
            result["inline_bytes"] += len(payload)
            result["last_id"] = candidate.id
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result



@contextmanager
def compaction_lock(session_factory=SessionLocal):
    """One compactor globally, including manual commands and separate workers."""
    with session_factory() as guard:
        bind = guard.get_bind()
        if bind.dialect.name == "postgresql":
            # Transaction-scoped advisory lock: rollback/connection death releases
            # it automatically. Work rows commit on separate sessions.
            acquired = bool(guard.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                        {"key": 714982660491337}))
            try:
                yield acquired
            finally:
                guard.rollback()
            return
        if bind.dialect.name != "sqlite":
            raise ValueError("Unsupported compaction lock dialect")
        # SQLite is for local tools/tests. flock coordinates processes on Linux;
        # Windows uses the matching nonblocking byte-range file lock.
        database = os.path.realpath(bind.url.database or ":memory:")
        identity = hashlib.sha256(database.encode()).hexdigest()[:24]
        lock_path = os.path.join(tempfile.gettempdir(), f"ao-source-compaction-{identity}.lock")
        with open(lock_path, "a+b") as handle:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.path.getsize(lock_path) == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                yield False
                return
            try:
                yield True
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def compact_batch(session_factory=SessionLocal, **kwargs):
    with compaction_lock(session_factory) as acquired:
        if not acquired:
            return {"mode": "apply" if kwargs.get("apply") else "dry-run",
                    "processed": 0, "inline_bytes": 0, "skipped": "already_running"}
        return _compact_batch_unlocked(session_factory, **kwargs)


def scheduled_config():
    def bounded(name, default, minimum, maximum):
        try:
            value = int(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))
    return {
        "enabled": os.environ.get("GMP_SOURCE_COMPACTION_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"},
        "max_rows": bounded("GMP_SOURCE_COMPACTION_MAX_ROWS", 1000, 1, 1000),
        "max_bytes": bounded("GMP_SOURCE_COMPACTION_MAX_MIB", 64, 1, 64) * 1024 * 1024,
        "max_seconds": bounded("GMP_SOURCE_COMPACTION_MAX_SECONDS", 15, 1, 15),
    }


def compact_legacy_raw(*, max_rows=25, max_bytes=32 * 1024 * 1024,
                       max_seconds=15, apply=False, tenant_id=None,
                       session_factory=SessionLocal):
    """Bounded worker entry point; completed rows leave the partial-index queue.

    Time is checked between atomic source transactions; a single source may
    exceed the wall-time budget but is capped by max_bytes. No global cursor is
    required, and failures roll back only the current source.
    """
    return compact_batch(session_factory, apply=apply, limit=max_rows,
                         max_bytes=max_bytes, max_source_bytes=max_bytes,
                         max_seconds=max_seconds, tenant_id=tenant_id)

