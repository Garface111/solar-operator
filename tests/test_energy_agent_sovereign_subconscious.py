"""Sovereign three-layer mind: subconscious + wake + heat handoff."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "SOVEREIGN_ENABLED",
        "SOVEREIGN_SUBCONSCIOUS",
        "SOVEREIGN_SUBCONSCIOUS_LLM",
        "SOVEREIGN_SENSE_ENABLED",
        "SOVEREIGN_ACT_ENABLED",
        "SOVEREIGN_SPEAK_ENABLED",
        "SOVEREIGN_BRAIN_ENABLED",
        "SOVEREIGN_CORTEX_HEAT_THRESHOLD",
        "SOVEREIGN_CORTEX_MIN_INTERVAL_SEC",
        "SOVEREIGN_SUB_MIN_INTERVAL_SEC",
    ):
        monkeypatch.delenv(k, raising=False)
    # Reset process-local coalesce clocks
    import api.energy_agent_sovereign_subconscious as sub
    sub._last_sub_at = None
    sub._last_cortex_at = None
    sub._pending_cortex_reason = None
    yield


def _mem_session(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SUBCONSCIOUS", "1")
    monkeypatch.setenv("SOVEREIGN_SUBCONSCIOUS_LLM", "0")  # rule monologue only
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "0")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")
    monkeypatch.setenv("SOVEREIGN_BRAIN_ENABLED", "0")  # cortex without LLM
    monkeypatch.setenv("SOVEREIGN_CORTEX_MIN_INTERVAL_SEC", "30")
    monkeypatch.setenv("SOVEREIGN_SUB_MIN_INTERVAL_SEC", "1")

    from api.models import Base
    import api.energy_agent_sovereign as sov
    import api.energy_agent_sovereign_subconscious as sub
    import api.utility_requests  # noqa: F401
    import api.feature_suggestions  # noqa: F401
    import api.energy_agent  # noqa: F401
    import api.energy_agent_mind  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # Explicit event table
    sub.EaSovereignEvent.__table__.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)
    monkeypatch.setattr(sub, "SessionLocal", Session)
    return Session, sov, sub


def test_score_heat_and_rule_monologue():
    from api.energy_agent_sovereign_subconscious import (
        score_event_heat, score_digest_heat, rule_monologue, decide_needs_cortex,
    )
    assert score_event_heat("desk_message") >= 90
    assert score_event_heat("utility_request") >= 70
    assert score_event_heat("job_failed") >= 75
    assert score_event_heat("scheduler") == 0

    digests = {
        "queues": {
            "utility_new": 2,
            "feature_reviewed": 5,
            "escalation_needs_ford": 1,
            "sovereign_jobs_queued": 0,
        }
    }
    h = score_digest_heat(digests)
    assert h >= 60  # new utilities + reviewed features + needs_ford

    mono = rule_monologue(digests, [{"reason": "utility_request", "heat": 72}], heat=h)
    assert "utility new=2" in mono
    assert "heat=" in mono

    # Ambient scheduler + emergency queues + heat over threshold → escalate
    needs, why = decide_needs_cortex(heat=max(h, 75), reason="scheduler", digests=digests)
    assert needs is True
    # Steady backlog alone does NOT escalate on pure scheduler
    quiet = {"queues": {"feature_reviewed": 20, "utility_new": 0, "escalation_needs_ford": 0}}
    needs_q, why_q = decide_needs_cortex(heat=80, reason="scheduler", digests=quiet)
    assert needs_q is False
    assert "ambient" in why_q or "backstop" in why_q
    # Event wake still escalates
    needs_e, why_e = decide_needs_cortex(heat=50, reason="utility_request", digests=quiet)
    assert needs_e is True
    assert "event" in why_e


def test_subconscious_writes_note_and_memory(monkeypatch):
    Session, sov, sub = _mem_session(monkeypatch)

    with Session() as db:
        from api.utility_requests import UtilityRequest
        db.add(UtilityRequest(name="Wake Co-op", product="array_operator", status="new"))
        db.commit()

    out = sub.subconscious_tick(reason="unit", force=True)
    assert out["ok"] is True
    assert out["mode"] == "live"
    assert out.get("heat", 0) >= 1
    assert "monologue_excerpt" in out

    with Session() as db:
        notes = db.query(sov.EaSovereignNote).filter_by(kind="subconscious").all()
        assert len(notes) >= 1
        mem = db.get(sov.EaSovereignMemory, "heat_score")
        assert mem is not None
        assert mem.value.isdigit()
        nc = db.get(sov.EaSovereignMemory, "needs_cortex")
        assert nc is not None
        data = json.loads(nc.value)
        assert "value" in data


def test_wake_sovereign_appends_event(monkeypatch):
    Session, sov, sub = _mem_session(monkeypatch)

    # Don't actually run expensive cortex path long — brain disabled falls to rules
    out = sub.wake_sovereign(
        "feature_suggestion",
        {"id": 99, "text_excerpt": "glass cards"},
        source="test",
        force_cortex=False,
    )
    assert out["ok"] is True
    assert out.get("event_id")
    assert out["subconscious"]["heat"] is not None

    with Session() as db:
        n = db.query(sub.EaSovereignEvent).count()
        assert n >= 1
        ev = db.query(sub.EaSovereignEvent).first()
        assert ev.reason == "feature_suggestion"


def test_cortex_receives_subconscious_tape(monkeypatch):
    Session, sov, sub = _mem_session(monkeypatch)

    # Seed tape
    with Session() as db:
        sov.write_note(
            db, kind="subconscious", title="sub · unit",
            body="utility queue still stuck; feature_reviewed=20",
            provider="rules",
        )
        sov.memory_set(db, "heat_score", "82", source="subconscious")
        db.commit()

    out = sov.sovereign_tick(reason="unit_cortex")
    assert out["ok"] is True
    assert out.get("layer") == "cortex"
    assert out.get("subconscious_tape_n", 0) >= 1


def test_coalesce_blocks_repeat_cortex(monkeypatch):
    from api.energy_agent_sovereign_subconscious import decide_needs_cortex
    needs, why = decide_needs_cortex(
        heat=75, reason="utility_request", last_cortex_age_sec=10,
    )
    assert needs is False
    assert "coalesce" in why

    needs2, _ = decide_needs_cortex(
        heat=95, reason="utility_request", last_cortex_age_sec=10,
    )
    assert needs2 is True  # super-hot breaks coalesce
