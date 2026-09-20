import json
import os
import secrets
import subprocess
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import event
from api.db import SessionLocal, engine
from api.models import Tenant, UtilityAccount, GmpUsageRaw
from api import source_compaction as compaction


@pytest.fixture
def legacy_source():
    tid = "compact_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Compactor", tenant_key=secrets.token_hex(16), contact_email=tid + "@example.test"))
        db.flush()
        account = UtilityAccount(tenant_id=tid, provider="gmp", account_number="1234")
        db.add(account)
        db.flush()
        raw = GmpUsageRaw(tenant_id=tid, account_id=account.id, account_number="1234",
                          window_start=date(2026, 1, 1), window_end=date(2026, 2, 1),
                          raw_csv="header\r\n123,456\r\n", fetched_at=datetime(2026, 3, 1))
        db.add(raw)
        db.commit()
        return tid, raw.id


def test_scheduler_disabled_never_queries_or_migrates(monkeypatch):
    from api.scheduler import compact_gmp_sources_job
    monkeypatch.delenv("GMP_SOURCE_COMPACTION_ENABLED", raising=False)
    monkeypatch.setattr(compaction, "compact_legacy_raw", lambda **kw: pytest.fail("disabled job ran"))
    assert compact_gmp_sources_job() == {"processed": 0, "skipped": "disabled"}


def test_scheduler_enabled_config_clamped_and_no_email(monkeypatch):
    from api import scheduler
    monkeypatch.setenv("GMP_SOURCE_COMPACTION_ENABLED", "1")
    monkeypatch.setenv("GMP_SOURCE_COMPACTION_MAX_ROWS", "100000")
    monkeypatch.setenv("GMP_SOURCE_COMPACTION_MAX_MIB", "100000")
    monkeypatch.setenv("GMP_SOURCE_COMPACTION_MAX_SECONDS", "100000")
    calls = []
    monkeypatch.setattr(compaction, "compact_legacy_raw", lambda **kw: calls.append(kw) or {"processed": 1})
    monkeypatch.setattr(scheduler, "send_internal_alert", lambda *a, **kw: pytest.fail("no email allowed"))
    assert scheduler.compact_gmp_sources_job()["processed"] == 1
    assert calls == [{"apply": True, "max_rows": 1000, "max_bytes": 64 * 1024 * 1024, "max_seconds": 15}]
    monkeypatch.setenv("GMP_SOURCE_COMPACTION_MAX_ROWS", "bad")
    monkeypatch.setenv("GMP_SOURCE_COMPACTION_MAX_MIB", "-1")
    monkeypatch.setenv("GMP_SOURCE_COMPACTION_MAX_SECONDS", "0")
    config = compaction.scheduled_config()
    assert config["max_rows"] == 1000 and config["max_bytes"] == 1024 * 1024 and config["max_seconds"] == 1


def test_scheduler_failure_contained_and_registration(monkeypatch):
    from api import scheduler
    monkeypatch.setenv("GMP_SOURCE_COMPACTION_ENABLED", "true")
    def fail(**kw):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(compaction, "compact_legacy_raw", fail)
    monkeypatch.setattr(scheduler, "send_internal_alert", lambda *a, **kw: pytest.fail("no email allowed"))
    assert scheduler.compact_gmp_sources_job()["error"] == "exception"
    jobs = []
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *args, **kw: jobs.append((args, kw)))
    scheduler._register_source_compaction_job()
    assert jobs[0][1] == {"minutes": 5, "id": "compact_gmp_sources", "replace_existing": True,
                           "max_instances": 1, "coalesce": True}


def test_apply_uses_one_roundtrip_and_no_estimate_pass(legacy_source, monkeypatch):
    from api import source_artifacts
    tid, identity = legacy_source
    original = source_artifacts.get_artifact
    calls = []
    def verified(*args):
        calls.append(args[2])
        return original(*args)
    monkeypatch.setattr(source_artifacts, "get_artifact", verified)
    monkeypatch.setattr(compaction, "content_chunks", lambda *a: pytest.fail("apply repeated estimate"))
    result = compaction.compact_legacy_raw(apply=True, tenant_id=tid)
    assert result["processed"] == 1 and result["new_compressed_bytes_upper_bound"] is None
    assert len(calls) == 1
    with SessionLocal() as db:
        assert db.get(GmpUsageRaw, identity).raw_csv is None
    assert compaction.compact_legacy_raw(apply=True, tenant_id=tid)["processed"] == 0


def test_empty_queue_has_no_full_count_or_payload_select():
    statements = []
    def record(conn, cursor, statement, params, context, many):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert compaction.compact_legacy_raw(tenant_id="empty-test-tenant")["processed"] == 0
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not any("count(" in sql for sql in statements)
    assert not any("select gmp_usage_raw.tenant_id" in sql for sql in statements)
    assert any("raw_csv is not null" in sql and "limit" in sql for sql in statements)


def test_cross_process_lock_prevents_duplicate_work(legacy_source):
    tid, identity = legacy_source
    code = ("import json; from api.source_compaction import compact_legacy_raw; "
            f"print(json.dumps(compact_legacy_raw(apply=True,tenant_id={tid!r})))")
    # Other legacy test modules set DATABASE_URL during collection after the
    # shared engine is initialized. The child must use the engine under test.
    child_env = os.environ.copy()
    child_env["DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
    with compaction.compaction_lock() as acquired:
        assert acquired
        child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               env=child_env, timeout=15, check=True)
        assert json.loads(child.stdout)["skipped"] == "already_running"
        with SessionLocal() as db:
            assert db.get(GmpUsageRaw, identity).raw_csv is not None
    assert compaction.compact_legacy_raw(apply=True, tenant_id=tid)["processed"] == 1


def test_compaction_interval_is_bounded(monkeypatch):
    from api import scheduler
    jobs = []
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *args, **kw: jobs.append(kw))
    for value, expected in [("0", 1), ("99999", 60), ("bad", 5)]:
        monkeypatch.setenv("GMP_SOURCE_COMPACTION_INTERVAL_MINUTES", value)
        scheduler._register_source_compaction_job()
        assert jobs[-1]["minutes"] == expected
