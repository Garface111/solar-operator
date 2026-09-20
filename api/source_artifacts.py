"""Lossless tenant-scoped source evidence. Callers own transactions.

CSV boundaries depend on line content, not date-window offsets: overlapping
historical downloads share interior chunks without discarding any interval,
header, whitespace, newline or unknown column. Binary documents use fixed
chunks. SHA-256 and byte lengths are verified during every reconstruction.
"""
from __future__ import annotations

import hashlib
import zlib
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import SourceArtifact, SourceArtifactChunk, GmpUsageRawVersion

MIN_CHUNK = 4096
MAX_CHUNK = 65536


def content_chunks(payload: bytes, mime_type: str = "application/octet-stream"):
    """C-speed line hashing; no per-byte Python loop or normalization."""
    if "csv" not in mime_type.lower():
        for offset in range(0, len(payload), MAX_CHUNK):
            yield payload[offset:offset + MAX_CHUNK]
        return
    start = cursor = 0
    while cursor < len(payload):
        line_start = cursor
        newline = payload.find(b"\n", cursor, cursor + MAX_CHUNK)
        end = newline + 1 if newline >= 0 else min(cursor + MAX_CHUNK, len(payload))
        if end - start > MAX_CHUNK:
            if cursor > start:
                yield payload[start:cursor]
                start = cursor
        cursor = end
        if cursor - start >= MAX_CHUNK or (
            cursor - start >= MIN_CHUNK and zlib.crc32(payload[line_start:cursor]) & 127 == 0
        ):
            yield payload[start:cursor]
            start = cursor
    if start < len(payload):
        yield payload[start:]


def _insert_ignore(db, model, values, keys):
    dialect = db.get_bind().dialect.name
    factory = {"postgresql": pg_insert, "sqlite": sqlite_insert}.get(dialect)
    if factory is None:
        raise ValueError("source artifacts require PostgreSQL or SQLite")
    if values:
        db.execute(factory(model).values(values).on_conflict_do_nothing(index_elements=keys))


def put_artifact(db, tenant_id: str, payload: bytes, mime_type: str = "application/octet-stream") -> int:
    """Store exact bytes, returning a durable artifact ID without committing."""
    if not tenant_id or not isinstance(payload, bytes):
        raise ValueError("tenant_id and bytes payload are required")
    digest = hashlib.sha256(payload).hexdigest()
    existing = db.execute(select(SourceArtifact).where(
        SourceArtifact.tenant_id == tenant_id, SourceArtifact.sha256 == digest)).scalar_one_or_none()
    if existing is not None:
        if existing.byte_length != len(payload):
            raise ValueError("artifact hash/length conflict")
        return existing.id
    ordered = []
    chunks = {}
    for data in content_chunks(payload, mime_type):
        sha = hashlib.sha256(data).hexdigest()
        ordered.append((sha, len(data)))
        if sha not in chunks:
            compressed = zlib.compress(data, 6)
            chunks[sha] = {"tenant_id": tenant_id, "sha256": sha, "byte_length": len(data),
                           "codec": "zlib" if len(compressed) < len(data) else "raw",
                           "data": compressed if len(compressed) < len(data) else data}
    found = {}
    hashes = list(chunks)
    for offset in range(0, len(hashes), 100):
        batch = hashes[offset:offset + 100]
        rows = db.execute(select(SourceArtifactChunk.id, SourceArtifactChunk.sha256, SourceArtifactChunk.byte_length).where(
            SourceArtifactChunk.tenant_id == tenant_id, SourceArtifactChunk.sha256.in_(batch))).all()
        known = {r.sha256 for r in rows}
        _insert_ignore(db, SourceArtifactChunk, [chunks[h] for h in batch if h not in known], ["tenant_id", "sha256"])
        for row in db.execute(select(SourceArtifactChunk.id, SourceArtifactChunk.sha256, SourceArtifactChunk.byte_length).where(
            SourceArtifactChunk.tenant_id == tenant_id, SourceArtifactChunk.sha256.in_(batch))).all():
            if row.byte_length != chunks[row.sha256]["byte_length"]:
                raise ValueError("chunk hash/length conflict")
            found[row.sha256] = row.id
    manifest = [{"id": found[sha], "byte_length": size} for sha, size in ordered]
    _insert_ignore(db, SourceArtifact, [{"tenant_id": tenant_id, "sha256": digest,
        "byte_length": len(payload), "mime_type": mime_type[:120], "manifest": manifest}], ["tenant_id", "sha256"])
    return db.execute(select(SourceArtifact.id).where(
        SourceArtifact.tenant_id == tenant_id, SourceArtifact.sha256 == digest)).scalar_one()


def get_artifact(db, tenant_id: str, artifact_id: int) -> bytes:
    """Fail closed on foreign tenant, missing chunk, corruption or wrong length."""
    artifact = db.execute(select(SourceArtifact).where(
        SourceArtifact.tenant_id == tenant_id, SourceArtifact.id == artifact_id)).scalar_one_or_none()
    if artifact is None:
        raise ValueError("Source artifact unavailable for tenant")
    manifest = artifact.manifest
    if not isinstance(manifest, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("id"), int)
        or ("sha256" in entry and (not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64))
        or not isinstance(entry.get("byte_length"), int) or not 0 < entry["byte_length"] <= MAX_CHUNK
        for entry in manifest
    ) or sum(entry["byte_length"] for entry in manifest) != artifact.byte_length:
        raise ValueError("Invalid source artifact manifest")
    # Batch reads; bound compressed chunk memory rather than loading every blob twice.
    output = bytearray()
    for offset in range(0, len(manifest), 100):
        entries = manifest[offset:offset + 100]
        rows = db.execute(select(SourceArtifactChunk).where(
            SourceArtifactChunk.tenant_id == tenant_id,
            SourceArtifactChunk.id.in_({e["id"] for e in entries}))).scalars().all()
        by_id = {r.id: r for r in rows}
        for entry in entries:
            row = by_id.get(entry["id"])
            if row is None or row.byte_length != entry["byte_length"] or (
                "sha256" in entry and row.sha256 != entry["sha256"]
            ):
                raise ValueError("Missing or invalid source chunk")
            try:
                if row.codec == "zlib":
                    decoder = zlib.decompressobj()
                    data = decoder.decompress(row.data, row.byte_length + 1)
                    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                        raise ValueError("Invalid compressed chunk")
                elif row.codec == "raw":
                    data = row.data
                else:
                    raise ValueError("Unsupported source codec")
            except zlib.error as exc:
                raise ValueError("Corrupt source chunk") from exc
            if len(data) != entry["byte_length"] or hashlib.sha256(data).hexdigest() != row.sha256:
                raise ValueError("Source chunk integrity check failed")
            output.extend(data)
    payload = bytes(output)
    if len(payload) != artifact.byte_length or hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise ValueError("Source artifact integrity check failed")
    return payload


def raw_payload_predicate(model):
    return or_(model.raw_csv.isnot(None), model.artifact_id.isnot(None))


def read_raw_csv(db, row):
    """Legacy inline sources remain readable until explicitly migrated."""
    if row.raw_csv is not None:
        return row.raw_csv
    if row.artifact_id is not None:
        return get_artifact(db, row.tenant_id, row.artifact_id).decode("utf-8")
    return None


def preserve_raw_version(db, row):
    """Pin current bytes and metadata before replacement; does not change timestamps."""
    if row.raw_csv is not None:
        artifact_id = put_artifact(db, row.tenant_id, row.raw_csv.encode("utf-8"), "text/csv")
        if get_artifact(db, row.tenant_id, artifact_id) != row.raw_csv.encode("utf-8"):
            raise ValueError("Source roundtrip failed")
        row.artifact_id = artifact_id
    if row.artifact_id is None:
        return
    db.flush()
    metadata = {key: getattr(row, key) for key in (
        "id", "account_id", "account_number", "window_start", "window_end", "fmt", "http_status",
        "row_count", "interval_min", "interval_max", "fetched_at")}
    metadata = {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in metadata.items()}
    _insert_ignore(db, GmpUsageRawVersion, [{"tenant_id": row.tenant_id, "raw_id": row.id,
        "artifact_id": row.artifact_id, "captured_at": row.fetched_at or datetime.min,
        "source_metadata": metadata}], ["raw_id", "artifact_id", "captured_at"])
