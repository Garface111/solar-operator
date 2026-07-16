"""Succession full authority — money, brand, hard-delete, HAR (gated)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SUCCESSION_FULL", "1")
    monkeypatch.setenv("SOVEREIGN_OPS_AUTHORITY", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ARM_T4_T5", "1")

    from api.models import Base, Tenant
    import api.energy_agent_sovereign as sov  # noqa: F401
    import api.energy_agent_sovereign_succession as succ

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Tenant(
            id="ten_test",
            tenant_key="sol_test",
            name="Test Co",
            product="array_operator",
            contact_email="t@example.com",
            active=True,
            subscription_status="active",
            stripe_customer_id="cus_test",
            stripe_subscription_id="sub_test",
        ))
        db.commit()
        yield db, succ, sov


def test_succession_status_on(db_session):
    db, succ, sov = db_session
    st = succ.succession_status()
    assert st["succession_full"] is True
    assert st["domains"]["money_stripe"] is True
    assert st["domains"]["brand"] is True
    assert st["domains"]["hard_delete"] is True
    assert st["domains"]["har_capture"] is True


def test_capability_money_never_autonomous(db_session, monkeypatch):
    """Ford 2026-07-16 re-invert: money/brand/hard-delete/HAR are NEVER granted to
    an autonomous tick, even with every arm flag on. They are draft-only."""
    db, succ, sov = db_session  # fixture sets SUCCESSION_FULL=1 + ARM_T4_T5=1
    assert sov.capability_allowed("act.money_identity") is False
    assert sov.capability_allowed("act.brand") is False
    assert sov.capability_allowed("act.hard_delete") is False
    assert sov.capability_allowed("act.har_capture") is False
    # The armed flag still lets a Ford-authorized direct call run the function,
    # but capability_allowed (the autonomous grant) stays denied.
    assert sov.succession_full_enabled() is True
    monkeypatch.setenv("SOVEREIGN_SUCCESSION_FULL", "0")
    assert sov.succession_full_enabled() is False


def test_brand_set(db_session):
    db, succ, sov = db_session
    r = succ.brand_set(db, key="voice", value="Honest, direct, solar-operator clear.")
    assert r["ok"] is True
    mem = {m["key"]: m["value"] for m in sov.memory_get_all(db)}
    assert "brand:voice" in mem
    assert mem.get("brand_owner") == "sovereign"


def test_billing_status(db_session):
    db, succ, sov = db_session
    r = succ.stripe_set_status(
        db, tenant_id="ten_test", subscription_status="comped", active=True, note="pilot",
    )
    assert r["ok"] and r["subscription_status"] == "comped"


def test_soft_delete_and_hard_purge_confirm(db_session):
    db, succ, sov = db_session
    soft = succ.tenant_soft_delete(db, tenant_id="ten_test", reason="test")
    assert soft["ok"]
    bad = succ.tenant_hard_purge(db, tenant_id="ten_test", confirm="wrong")
    assert bad.get("ok") is False
    hard = succ.tenant_hard_purge(db, tenant_id="ten_test", confirm="ten_test", reason="test purge")
    assert hard["ok"] is True


def test_har_stage(db_session):
    db, succ, sov = db_session
    r = succ.har_stage(
        db, utility_name="Palmetto Electric", url="https://example.com",
        note="need owner HAR",
    )
    assert r["ok"] is True
    assert r["queue_len"] >= 1
    mem = {m["key"]: m["value"] for m in sov.memory_get_all(db)}
    assert "har_capture_queue" in mem


def test_brain_actions_succession(db_session):
    """Benign succession acts (brand memory, HAR stage) still run; a money act
    (billing_status) is drafted for Ford instead of executing."""
    db, succ, sov = db_session
    out = sov.execute_brain_actions(
        db,
        [
            {"type": "brand_set", "key": "tagline", "value": "REC truth for every array"},
            {"type": "har_stage", "utility_name": "Alaska Coop", "text": "HAR needed"},
            {"type": "billing_status", "tenant_id": "ten_test", "status": "active"},
        ],
        tick_id="tick_succ",
    )
    by_kind = {o["kind"]: (o.get("result") or {}) for o in out}
    assert by_kind.get("brand_set", {}).get("ok") is True
    assert by_kind.get("har_stage", {}).get("ok") is True
    # money act deferred, not executed
    assert by_kind.get("billing_status", {}).get("deferred") is True
    assert by_kind.get("billing_status", {}).get("approval_id")
