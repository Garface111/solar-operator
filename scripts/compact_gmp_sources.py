"""Lossless resumable GMP source migration; dry-run is the default.

Run `python -m scripts.compact_gmp_sources --limit 25`. Add --apply only after
reviewing estimates and provisioning the additive schema with api.migrate.
Every source is locked, archived and roundtrip-verified before its inline field
is cleared in the SAME transaction. No source rows, metadata or IDs are deleted.
Repeated runs naturally skip completed rows; --after-id is an optional checkpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import zlib

from sqlalchemy import select, func, cast, LargeBinary

from api.db import SessionLocal
from api.models import GmpUsageRaw
from api.source_artifacts import content_chunks, preserve_raw_version, get_artifact


def compact_batch(session_factory=SessionLocal, *, apply=False, limit=25, after_id=0,
                  tenant_id=None, max_bytes=32 * 1024 * 1024, max_source_bytes=64 * 1024 * 1024,
                  max_seconds=30):
    if not 1 <= limit <= 1000 or max_bytes <= 0 or max_source_bytes <= 0 or after_id < 0 or max_seconds <= 0:
        raise ValueError("Invalid migration batch bounds")
    started = time.monotonic()
    result = {"mode": "apply" if apply else "dry-run", "processed": 0, "inline_bytes": 0,
              "new_compressed_bytes_upper_bound": 0, "last_id": after_id, "more": False}
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
            for chunk in content_chunks(payload, "text/csv"):
                key = (row.tenant_id, hashlib.sha256(chunk).hexdigest())
                if key not in seen_chunks:
                    seen_chunks.add(key)
                    result["new_compressed_bytes_upper_bound"] += min(len(chunk), len(zlib.compress(chunk, 6)))
            if apply:
                preserve_raw_version(db, row)
                restored = get_artifact(db, row.tenant_id, row.artifact_id)
                if restored != payload or hashlib.sha256(restored).digest() != hashlib.sha256(payload).digest():
                    raise ValueError(f"Roundtrip mismatch for source {row.id}")
                row.raw_csv = None
                db.commit()
            result["processed"] += 1
            result["inline_bytes"] += len(payload)
            result["last_id"] = candidate.id
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit verified migrations; default is read-only")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--tenant-id")
    parser.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--max-seconds", type=float, default=30)
    parser.add_argument("--max-source-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    print(json.dumps(compact_batch(**vars(args)), sort_keys=True))


if __name__ == "__main__":
    main()
