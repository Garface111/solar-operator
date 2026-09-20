from datetime import datetime, timedelta
import json
import secrets
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from api.db import SessionLocal
from api.models import Base, CaptureEvent, Job, Tenant
from api.capture_events import _safe_excerpt, PAYLOAD_MAX_BYTES
from api.data_retention import prune_runtime_diagnostics


@pytest.fixture
def retention_db(monkeypatch):
    engine=create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine)
    monkeypatch.setattr("api.data_retention.SessionLocal",sessions)
    monkeypatch.setattr(sys.modules[__name__],"SessionLocal",sessions)
    yield
    engine.dispose()


def test_retention_is_bounded_dry_run_and_preserves_active_unknown_and_recent(retention_db):
    t=datetime(2026,9,19,12)
    tid="retention_"+secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid,tenant_key=tid,name="Retention test",contact_email=tid+"@example.test"));db.flush()
        keep_event=CaptureEvent(tenant_id=tid,capture_id="recent_error",stage="capture_error",created_at=t-timedelta(days=40))
        old_event=CaptureEvent(tenant_id=tid,capture_id="old_debug",stage="ingest_received",created_at=t-timedelta(days=40))
        old_error=CaptureEvent(tenant_id=tid,capture_id="old_error",stage="capture_error",created_at=t-timedelta(days=100))
        jobs=[Job(tenant_id=tid,kind=kind,status=status,finished_at=finished,created_at=t-timedelta(days=120)) for kind,status,finished in [
            ("pull_bills","succeeded",t-timedelta(days=40)),
            ("pull_bills","failed",t-timedelta(days=100)),
            ("pull_bills","failed",t-timedelta(days=40)),
            ("pull_bills","running",t-timedelta(days=100)),
            ("pull_bills","queued",None),
            ("generate_report","succeeded",t-timedelta(days=100)),
            ("pull_bills","succeeded",None),
        ]]
        db.add_all([keep_event,old_event,old_error,*jobs]);db.commit()
        event_ids=[r.id for r in [keep_event,old_event,old_error]];job_ids=[r.id for r in jobs]
    preview=prune_runtime_diagnostics(now=t,batch_size=1,max_batches=1)
    assert not preview["apply"]
    assert all(x["deleted"]==0 for x in preview["tables"].values())
    with SessionLocal() as db:
        assert all(db.get(Job,i) for i in job_ids)
        assert all(db.get(CaptureEvent,i) for i in event_ids)
    # Bounded first pass, then repeat safely to completion.
    first=prune_runtime_diagnostics(apply=True,now=t,batch_size=1,max_batches=1)
    assert all(x["deleted"]<=1 for x in first["tables"].values())
    prune_runtime_diagnostics(apply=True,now=t)
    with SessionLocal() as db:
        assert db.get(CaptureEvent,event_ids[0])
        assert not db.get(CaptureEvent,event_ids[1]) and not db.get(CaptureEvent,event_ids[2])
        assert not db.get(Job,job_ids[0]) and not db.get(Job,job_ids[1])
        assert all(db.get(Job,i) for i in job_ids[2:])
    assert all(x["deleted"]==0 for x in prune_runtime_diagnostics(apply=True,now=t)["tables"].values())


def test_capture_excerpt_cannot_store_unbounded_profiles_nested_secrets_or_excess_accounts():
    raw={"provider":"gmp","auth":{"token":"private-token"},
         "user":{"email":"owner@example.test","name":"\U0001f31e"*10000,"access_token":"private-token","profile":{"secret":"private-token"}},
         "accounts":[{"account_number":str(i),"service_address":"\U0001f31e"*5000,"extra":{"secret":"private-token"}} for i in range(100)]}
    value=_safe_excerpt(raw)
    encoded=json.dumps(value,ensure_ascii=False).encode()
    assert len(encoded)<=PAYLOAD_MAX_BYTES
    assert len(json.dumps(value).encode())<=PAYLOAD_MAX_BYTES
    assert b"private-token" not in encoded
    assert value["provider"]=="gmp" and value["account_count"]==100
    assert len(value["accounts_summary"])<=20 and value["_truncated"] is True
    assert "email" in value["user"]


def test_capture_excerpt_rejects_structured_values_and_keeps_small_identity():
    result=_safe_excerpt({"provider":"gmp","user":{"email":"owner@example.test","username":{"token":"secret"}},"accounts":[{"account_number":"123","extra":{"token":"secret"}},None]})
    assert result["user"]=={"email":"owner@example.test"}
    assert result["accounts_summary"]==[{"account_number":"123"}]
    assert "secret" not in json.dumps(result)
