"""Sovereign self-modification: propose → Ford approve → apply."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    from api.models import Base
    import api.energy_agent_sovereign as sov
    import api.energy_agent_sovereign_desk  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)
    with Session() as db:
        yield db, sov


def test_detect_approval_and_reject():
    from api.energy_agent_sovereign import detect_ford_approval, detect_ford_rejection

    assert detect_ford_approval("yes")
    assert detect_ford_approval("Approved — do it")
    assert detect_ford_approval("go ahead")
    assert not detect_ford_approval("what do you recommend?")
    assert detect_ford_rejection("reject that")
    assert detect_ford_rejection("veto")


def test_propose_then_apply(db_session):
    db, sov = db_session
    prop = sov.propose_mind_patch(
        db,
        {
            "summary": "Prefer high-level digests only",
            "why": "Ford asked for quieter email",
            "persona_addendum": "Always write digests like a partner letter.",
            "directives": "Never invent customer urgency from demo tenants.",
            "memory_writes": [
                {"key": "policy_email_tone", "value": "high_level_only"},
            ],
        },
        source="test",
    )
    assert prop["ok"] is True
    assert prop["awaiting_ford_approval"] is True
    pending = sov.get_pending_mind_patch(db)
    assert pending is not None

    applied = sov.apply_pending_mind_patch(db, approved_by="ford_chat")
    assert applied["ok"] is True
    assert applied["applied"] is True
    assert "persona_addendum" in applied["applied_parts"]
    assert "mind_directives" in applied["applied_parts"]
    assert "memory:policy_email_tone" in applied["applied_parts"]

    # Pending cleared
    assert sov.get_pending_mind_patch(db) is None
    # Values landed
    row = db.get(sov.EaSovereignMemory, "policy_email_tone")
    assert row and row.value == "high_level_only"
    persona = db.get(sov.EaSovereignMemory, "persona_addendum")
    assert persona and "partner letter" in persona.value
    db.commit()


def test_cannot_apply_without_pending(db_session):
    db, sov = db_session
    out = sov.apply_pending_mind_patch(db)
    assert out.get("applied") is False
