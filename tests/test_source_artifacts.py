import hashlib
import secrets
from datetime import date, datetime

import pytest
from sqlalchemy import select, func, update, event
from api.db import SessionLocal, engine
from api.models import (Tenant, Array, UtilityAccount, GmpUsageRaw, GmpUsageRawVersion,
                        SourceArtifact, SourceArtifactChunk)
from api.source_artifacts import put_artifact, get_artifact, content_chunks, read_raw_csv
from scripts.compact_gmp_sources import compact_batch


@pytest.fixture
def source_account():
    tid = "src_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Artifact test", tenant_key=secrets.token_hex(16), contact_email=tid + "@example.test"))
        array = Array(tenant_id=tid, name="Source array")
        db.add(array)
        db.flush()
        account = UtilityAccount(tenant_id=tid, array_id=array.id, provider="gmp", account_number="1234")
        db.add(account)
        db.commit()
        return tid, account.id, array.id


def csv_rows(count=10000):
    return [f"SA,2026-01-{1 + i // 96:02d} {i % 96 // 4:02d}:{i % 4 * 15:02d}:00,end,{i * .25},kWh,unknown {i}\r\n".encode() for i in range(count)]


def add_raw(db, tid, aid, body, start=date(2026, 1, 1), end=date(2026, 2, 1)):
    row = GmpUsageRaw(tenant_id=tid, account_id=aid, account_number="1234", window_start=start,
        window_end=end, raw_csv=body, row_count=2, interval_min=start, interval_max=end,
        http_status=200, fetched_at=datetime(2026, 3, 1))
    db.add(row)
    db.flush()
    return row


def test_exact_roundtrip_and_content_defined_overlap(source_account):
    tid, _, _ = source_account
    lines = csv_rows()
    first = b"header\r\n" + b"".join(lines[:8000])
    second = b"header\r\n" + b"".join(lines[1000:])
    first_chunks = set(content_chunks(first, "text/csv"))
    second_chunks = set(content_chunks(second, "text/csv"))
    assert len(first_chunks & second_chunks) > 10
    assert b"".join(content_chunks(first, "text/csv")) == first
    with SessionLocal() as db:
        first_id = put_artifact(db, tid, first, "text/csv")
        assert put_artifact(db, tid, first, "text/csv") == first_id
        second_id = put_artifact(db, tid, second, "text/csv")
        assert get_artifact(db, tid, first_id) == first
        assert get_artifact(db, tid, second_id) == second
        assert db.scalar(select(func.count()).select_from(SourceArtifact).where(SourceArtifact.tenant_id == tid)) == 2
        assert db.scalar(select(func.count()).select_from(SourceArtifactChunk).where(SourceArtifactChunk.tenant_id == tid)) == len(first_chunks | second_chunks)
        for payload in (b"", b"\xef\xbb\xbf a,  b\r\n1,2\nno newline", bytes(range(256)) * 1000):
            identity = put_artifact(db, tid, payload)
            assert get_artifact(db, tid, identity) == payload
        with pytest.raises(ValueError, match="tenant"):
            get_artifact(db, "foreign", first_id)


def test_corruption_fails_closed(source_account):
    tid, _, _ = source_account
    with SessionLocal() as db:
        identity = put_artifact(db, tid, b"evidence" * 10000)
        chunk = db.scalar(select(SourceArtifactChunk).where(SourceArtifactChunk.tenant_id == tid))
        db.execute(update(SourceArtifactChunk).where(SourceArtifactChunk.id == chunk.id).values(data=b"corrupt"))
        db.expire_all()
        with pytest.raises(ValueError, match="chunk"):
            get_artifact(db, tid, identity)


def test_dry_run_resumable_apply_and_unchanged_metadata(source_account):
    tid, aid, _ = source_account
    with SessionLocal() as db:
        rows = [add_raw(db, tid, aid, "Header\r\n1,ü\r\n", start=date(2026, 1, i), end=date(2026, 2, i)) for i in (1, 2)]
        ids = [r.id for r in rows]
        db.commit()
    dry = compact_batch(tenant_id=tid, limit=1)
    assert dry["mode"] == "dry-run" and dry["processed"] == 1 and dry["more"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SourceArtifact).where(SourceArtifact.tenant_id == tid)) == 0
    migrated = compact_batch(apply=True, tenant_id=tid, limit=1)
    assert migrated["processed"] == 1
    resumed = compact_batch(apply=True, tenant_id=tid, limit=1, after_id=migrated["last_id"])
    assert resumed["processed"] == 1
    assert compact_batch(apply=True, tenant_id=tid)["processed"] == 0
    with SessionLocal() as db:
        for identity in ids:
            row = db.get(GmpUsageRaw, identity)
            assert row.raw_csv is None and read_raw_csv(db, row) == "Header\r\n1,ü\r\n"
            assert row.fetched_at == datetime(2026, 3, 1) and row.row_count == 2
        versions = db.scalars(select(GmpUsageRawVersion).where(GmpUsageRawVersion.tenant_id == tid)).all()
        assert len(versions) == 2
        assert {v.source_metadata["id"] for v in versions} == set(ids)


def test_failed_roundtrip_keeps_inline_and_rolls_back(source_account, monkeypatch):
    tid, aid, _ = source_account
    with SessionLocal() as db:
        identity = add_raw(db, tid, aid, "original").id
        db.commit()
    monkeypatch.setattr("scripts.compact_gmp_sources.get_artifact", lambda *a: b"wrong")
    with pytest.raises(ValueError, match="Roundtrip"):
        compact_batch(apply=True, tenant_id=tid)
    with SessionLocal() as db:
        row = db.get(GmpUsageRaw, identity)
        assert row.raw_csv == "original" and row.artifact_id is None
        assert db.scalar(select(func.count()).select_from(SourceArtifact).where(SourceArtifact.tenant_id == tid)) == 0


def test_source_changes_versioned_and_404_cannot_erase(source_account):
    from api.jobs.gmp_daily_backfill import _persist_window, _record_404, _empty_parsed
    tid, aid, _ = source_account
    with SessionLocal() as db:
        row = add_raw(db, tid, aid, "original")
        identity = row.id
        db.commit()
        account = db.get(UtilityAccount, aid)
        _persist_window(db, account, row.window_start, row.window_end, "corrected", _empty_parsed())
        db.commit()
        versions = db.scalars(select(GmpUsageRawVersion).where(GmpUsageRawVersion.raw_id == identity)).all()
        assert {get_artifact(db, tid, v.artifact_id) for v in versions} == {b"original", b"corrected"}
        assert read_raw_csv(db, row) == "corrected"
        count = len(versions)
        _persist_window(db, account, row.window_start, row.window_end, "corrected", _empty_parsed())
        db.commit()
        assert db.scalar(select(func.count()).select_from(GmpUsageRawVersion).where(GmpUsageRawVersion.raw_id == identity)) == count
        stamp = row.fetched_at
        _record_404(db, account, row.window_start, row.window_end)
        db.refresh(row)
        assert read_raw_csv(db, row) == "corrected" and row.http_status == 200
        assert row.fetched_at == stamp


def test_hourly_metadata_exports_and_rederive_preserved(source_account, monkeypatch):
    from api.reports.gmp_daily_read import get_hourly_series, get_raw_windows
    from api.jobs.gmp_daily_backfill import rederive_account
    tid, aid, array_id = source_account
    body = "ServiceAgreement,IntervalStart,IntervalEnd,Quantity,UnitOfMeasure\nSA,2026-01-01 00:00:00,2026-01-01 00:15:00,1.5,kWh\n"
    with SessionLocal() as db:
        row = add_raw(db, tid, aid, body)
        row.row_count = 1
        row.interval_max = date(2026, 1, 1)
        db.commit()
        before = get_hourly_series(array_id, db=db)
        exports = get_raw_windows(aid, include_payload=True, db=db)
    assert before and before[0]["kwh"] == 1.5
    compact_batch(apply=True, tenant_id=tid)
    with SessionLocal() as db:
        assert get_hourly_series(array_id, db=db) == before
        assert get_raw_windows(aid, include_payload=True, db=db) == exports
        rederive_account(db, aid)
        assert get_raw_windows(aid, include_payload=True, db=db) == exports
    statements = []
    def record(conn, cursor, statement, params, context, many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        with SessionLocal() as db:
            monkeypatch.setattr("api.reports.gmp_daily_read.read_raw_csv", lambda *a: pytest.fail("metadata must not read payload"))
            assert get_raw_windows(aid, include_payload=False, db=db)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert all("gmp_usage_raw.raw_csv" not in statement for statement in statements)


def test_concurrent_same_content_has_one_artifact(source_account):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    tid, _, _ = source_account
    barrier = Barrier(2)
    payload = b"immutable invoice bytes" * 10000
    def store():
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            identity = put_artifact(db, tid, payload)
            db.commit()
            return identity
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = list(workers.map(lambda _: store(), range(2)))
    assert first == second
    with SessionLocal() as db:
        assert get_artifact(db, tid, first) == payload
        assert db.scalar(select(func.count()).select_from(SourceArtifact).where(SourceArtifact.tenant_id == tid)) == 1


def test_cross_tenant_chunks_are_never_shared_or_read(source_account):
    tid, _, _ = source_account
    foreign = "foreign_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(id=foreign, name="Foreign", tenant_key=secrets.token_hex(16), contact_email=foreign + "@example.test"))
        db.flush()
        payload = b"same bytes"
        local_id = put_artifact(db, tid, payload)
        other_id = put_artifact(db, foreign, payload)
        local = db.get(SourceArtifact, local_id)
        other = db.get(SourceArtifact, other_id)
        assert local_id != other_id and local.manifest[0]["id"] != other.manifest[0]["id"]
        local.manifest = other.manifest
        db.flush()
        with pytest.raises(ValueError, match="chunk"):
            get_artifact(db, tid, local_id)


def test_interrupted_migration_resumes_without_losing_inline(source_account, monkeypatch):
    import scripts.compact_gmp_sources as migration
    tid, aid, _ = source_account
    with SessionLocal() as db:
        first = add_raw(db, tid, aid, "first", start=date(2026, 1, 1)).id
        second = add_raw(db, tid, aid, "second", start=date(2026, 1, 2)).id
        db.commit()
    original = migration.get_artifact
    def interrupt(db, tenant, identity):
        payload = original(db, tenant, identity)
        if payload == b"second":
            raise RuntimeError("simulated worker exit")
        return payload
    monkeypatch.setattr(migration, "get_artifact", interrupt)
    with pytest.raises(RuntimeError, match="worker exit"):
        compact_batch(apply=True, tenant_id=tid)
    with SessionLocal() as db:
        assert db.get(GmpUsageRaw, first).raw_csv is None
        assert db.get(GmpUsageRaw, second).raw_csv == "second"
        assert db.get(GmpUsageRaw, second).artifact_id is None
    monkeypatch.setattr(migration, "get_artifact", original)
    assert compact_batch(apply=True, tenant_id=tid)["processed"] == 1


def test_overlap_precedence_unchanged_after_compaction(source_account):
    from api.reports.gmp_daily_read import get_hourly_series
    tid, aid, array_id = source_account
    header = "ServiceAgreement,IntervalStart,IntervalEnd,Quantity,UnitOfMeasure\n"
    with SessionLocal() as db:
        earlier = add_raw(db, tid, aid, header + "SA,2026-01-15 00:00,2026-01-15 00:15,1,kWh\n")
        earlier.fetched_at = datetime(2026, 8, 1)
        later = add_raw(db, tid, aid, header + "SA,2026-01-15 00:00,2026-01-15 00:15,2,kWh\n", start=date(2026, 1, 2))
        later.fetched_at = datetime(2026, 3, 1)
        db.commit()
        before = get_hourly_series(array_id, db=db)
        assert before[0]["kwh"] == 2  # Existing window-start-first precedence.
    compact_batch(apply=True, tenant_id=tid)
    with SessionLocal() as db:
        assert get_hourly_series(array_id, db=db) == before


def test_oversize_batch_guard_does_not_skip_source(source_account):
    tid, aid, _ = source_account
    with SessionLocal() as db:
        identity = add_raw(db, tid, aid, "larger than cap").id
        db.commit()
    result = compact_batch(apply=True, tenant_id=tid, max_source_bytes=2)
    assert result["blocked_id"] == identity and result["last_id"] == 0
    assert result["processed"] == 0
    with SessionLocal() as db:
        assert db.get(GmpUsageRaw, identity).raw_csv == "larger than cap"


def test_worker_entrypoint_bounds_bytes_and_resumes_without_cursor(source_account):
    from scripts.compact_gmp_sources import compact_legacy_raw
    from sqlalchemy import inspect
    tid, aid, _ = source_account
    with SessionLocal() as db:
        for day in (1, 2):
            add_raw(db, tid, aid, "12345", start=date(2026, 1, day))
        db.commit()
    first = compact_legacy_raw(apply=True, tenant_id=tid, max_rows=10, max_bytes=5)
    assert first["processed"] == 1 and first["inline_bytes"] == 5
    second = compact_legacy_raw(apply=True, tenant_id=tid, max_rows=10, max_bytes=5)
    assert second["processed"] == 1
    assert compact_legacy_raw(apply=True, tenant_id=tid)["processed"] == 0
    assert any(index["name"] == "ix_gmp_raw_inline_id" for index in inspect(engine).get_indexes("gmp_usage_raw"))


@pytest.mark.parametrize("tamper", ["wrong_id", "reordered", "length"])
def test_compact_manifest_detects_tampering(source_account, tamper):
    tid, _, _ = source_account
    payload = b"a" * 65536 + b"b" * 65536
    with SessionLocal() as db:
        identity = put_artifact(db, tid, payload)
        other_id = put_artifact(db, tid, b"c" * 65536)
        artifact = db.get(SourceArtifact, identity)
        assert all(set(entry) == {"id", "byte_length"} for entry in artifact.manifest)
        manifest = [dict(entry) for entry in artifact.manifest]
        if tamper == "wrong_id":
            manifest[0]["id"] = db.get(SourceArtifact, other_id).manifest[0]["id"]
        elif tamper == "reordered":
            manifest.reverse()
        else:
            manifest[0]["byte_length"] -= 1
        artifact.manifest = manifest
        db.flush()
        with pytest.raises(ValueError):
            get_artifact(db, tid, identity)


def test_original_hash_bearing_manifest_remains_readable(source_account):
    tid, _, _ = source_account
    payload = b"original format compatibility"
    with SessionLocal() as db:
        identity = put_artifact(db, tid, payload)
        artifact = db.get(SourceArtifact, identity)
        artifact.manifest = [dict(entry, sha256=db.get(SourceArtifactChunk, entry["id"]).sha256)
                             for entry in artifact.manifest]
        db.flush()
        assert get_artifact(db, tid, identity) == payload
        artifact.manifest = [dict(entry, sha256="0" * 64) for entry in artifact.manifest]
        db.flush()
        with pytest.raises(ValueError, match="chunk"):
            get_artifact(db, tid, identity)
