"""Sovereign Mind — full runtime tests (observe, audit, gates, admin API)."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def _clear_sovereign_env(monkeypatch):
    for k in (
        "SOVEREIGN_ENABLED",
        "SOVEREIGN_ACT_ENABLED",
        "SOVEREIGN_SPEAK_ENABLED",
        "SOVEREIGN_SENSE_ENABLED",
        "SOVEREIGN_SPEAK_ALL",
        "SOVEREIGN_CAPABILITIES",
        "SOVEREIGN_ARM_T4_T5",
        "SOVEREIGN_SERVICE_KEY",
        "ADMIN_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def test_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "0")
    from api.energy_agent_sovereign import capability_allowed, sovereign_tick

    assert capability_allowed("sense.queues") is False
    out = sovereign_tick(reason="test")
    assert out["mode"] == "dark"
    assert out["enabled"] is False


def test_money_never_autonomous(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ARM_T4_T5", "1")
    monkeypatch.setenv("SOVEREIGN_CAPABILITIES", "*")
    from api.energy_agent_sovereign import plan_action

    out = plan_action("act.money_identity", {"do": "bad"})
    assert out["denied"] is True


def test_observe_and_tick_with_db(monkeypatch):
    """In-memory tables: observe digests + world save + audit observe row."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")  # no inject in this test

    from api.models import Base
    import api.energy_agent_sovereign as sov
    # Import mind models for EaTask etc if needed
    import api.energy_agent  # noqa: F401
    import api.energy_agent_mind  # noqa: F401
    import api.utility_requests  # noqa: F401
    import api.feature_suggestions  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Patch SessionLocal used by sovereign
    monkeypatch.setattr(sov, "SessionLocal", Session)

    with Session() as db:
        from api.utility_requests import UtilityRequest
        db.add(UtilityRequest(name="Test Co-op", product="array_operator", status="new"))
        db.commit()

    out = sov.sovereign_tick(reason="unit")
    assert out["ok"] is True
    assert out["mode"] == "live"
    assert out.get("digests", {}).get("queues", {}).get("utility_new", 0) >= 1

    with Session() as db:
        state = db.get(sov.EaSovereignState, "product")
        assert state is not None
        assert state.revision >= 1
        n_audit = db.query(sov.EaSovereignAction).count()
        assert n_audit >= 1
        # triage may have moved utility to researching
        from api.utility_requests import UtilityRequest
        u = db.query(UtilityRequest).first()
        assert u.status in ("new", "researching")


def test_stage_feature_and_code_hire(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")

    from api.models import Base
    import api.energy_agent_sovereign as sov
    import api.feature_suggestions  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)

    # Silence mailer
    monkeypatch.setattr(sov, "email_ford", lambda *a, **k: True)

    out = sov.plan_action("act.soft_stage", {"text": "Make Ops modals better"})
    assert out.get("ok") is True
    assert out.get("feature_id")

    job = sov.plan_action("act.code_hire", {
        "title": "Fix ops overlap",
        "brief": "Pin modal scrim to content column when EA open.",
    })
    assert job.get("ok") is True
    assert job.get("job_id")


def test_inject_dogfood_only(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ALL", "0")
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "0")

    from api.models import Base, Tenant
    import api.energy_agent_sovereign as sov
    import api.energy_agent as ea
    import api.energy_agent_mind as mind  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)

    with Session() as db:
        db.add(Tenant(
            id="ten_other",
            tenant_key="sol_live_other",
            name="Other",
            product="array_operator",
            contact_email="stranger@example.com",
            active=True,
        ))
        db.add(Tenant(
            id="ten_ford",
            tenant_key="sol_live_ford",
            name="Ford",
            product="array_operator",
            contact_email="ford.genereaux@gmail.com",
            active=True,
        ))
        db.add(ea.EaSession(
            id="ses_ford",
            tenant_id="ten_ford",
            status="open",
        ))
        db.commit()

    denied = sov.plan_inject(
        tenant_ids=["ten_other"],
        speak="Hello product update",
    )
    assert denied["ok"] is True
    assert denied["results"][0].get("denied") is True

    ok = sov.plan_inject(
        tenant_ids=["ten_ford"],
        speak="Hello Ford — sovereign is live.",
    )
    assert ok["results"][0].get("ok") is True


def test_admin_api(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-sovereign")
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    from api.app import app

    client = TestClient(app)
    r = client.get("/admin/sovereign/state")
    assert r.status_code in (401, 403)

    r2 = client.get(
        "/admin/sovereign/state",
        headers={"Authorization": "Bearer test-admin-key-sovereign"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["enabled"] is True
    assert "capabilities" in body
    assert "act.money_identity" in body["capabilities"]
