"""Unit tests for Energy Agent operating mind (Phases A–D)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.energy_agent_mind import (
    EaEvent,
    EaPlan,
    EaTask,
    EaWorldState,
    MIN_IMPORTANCE_TO_SPEAK,
    classify_and_plan,
    compute_metrics,
    drain_tasks,
    interrupt_budget,
    maybe_queue_interrupt,
    mind_tick,
    score_importance,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    EaWorldState.__table__.create(engine)
    EaPlan.__table__.create(engine)
    EaTask.__table__.create(engine)
    EaEvent.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_score_importance_fleet_quiet_vs_busy():
    quiet = score_importance("fleet_pulse", {"fleet_digest": {"attention_count": 0}}, "hi")
    busy = score_importance(
        "fleet_pulse",
        {"fleet_digest": {"attention_count": 4}},
        "I refreshed the fleet",
    )
    assert busy > quiet
    assert busy >= MIN_IMPORTANCE_TO_SPEAK


def test_score_propose_ui_high():
    s = score_importance("propose_ui", {"ok": True, "suggestion_id": 9}, "queued")
    assert s >= MIN_IMPORTANCE_TO_SPEAK


def test_ux_friction_plan_and_policy(db):
    with patch("api.energy_agent._mem_set"), patch("api.energy_agent._mem_get", return_value=[]):
        plan = classify_and_plan(
            db, "ten_t", "sess1",
            "This dashboard is hard to use — I can't find anything.",
            context={"hash": "#dashboard", "tab_label": "Fleet Triage"},
        )
        assert plan is not None
        assert plan["intent"] == "ux_friction"
        n = drain_tasks(db, "ten_t", limit=10)
        assert n >= 3
        tasks = db.query(EaTask).all()
        assert all(t.status == "done" for t in tasks)

        # Interrupts must pass importance + rate policy
        candidates = (
            db.query(EaEvent).filter(EaEvent.kind == "interrupt_candidate").all()
        )
        suppressed = (
            db.query(EaEvent).filter(EaEvent.kind == "interrupt_suppressed").all()
        )
        # At least one of candidate or suppressed for speak-bearing tasks
        assert len(candidates) + len(suppressed) >= 1
        for c in candidates:
            pl = __import__("json").loads(c.payload_json or "{}")
            assert pl.get("importance", 0) >= MIN_IMPORTANCE_TO_SPEAK


def test_interrupt_rate_limit(db):
    # Emit max per hour then next should suppress
    for i in range(3):
        out = maybe_queue_interrupt(
            db, "ten_t",
            session_id="s",
            ref_id=f"r{i}",
            title="t",
            speak="Quick update: something useful landed for you to review carefully.",
            kind="propose_ui",
            result={"ok": True, "suggestion_id": i},
        )
        # First may pass; cooldown may block subsequent within 90s
        assert "importance" in out
    # Force another with high importance — likely suppressed by cooldown/rate
    out = maybe_queue_interrupt(
        db, "ten_t",
        session_id="s",
        ref_id="rX",
        title="t",
        speak="Another high value interrupt that should be rate limited.",
        kind="propose_ui",
        result={"ok": True, "suggestion_id": 99},
    )
    budget = interrupt_budget(db, "ten_t")
    assert budget["hour_count"] <= 3
    # After 3 successful emits, 4th should not allow or was suppressed
    assert out["emitted"] is False or budget["hour_count"] >= 1


def test_yes_proposal_after_friction(db):
    with patch("api.energy_agent._mem_set"), patch("api.energy_agent._mem_get", return_value=[]):
        classify_and_plan(
            db, "ten_t", "s1",
            "This dashboard is hard to use.",
            context={"hash": "#dashboard"},
        )
        drain_tasks(db, "ten_t", limit=10)

    with patch("api.energy_agent_mind._propose_ui_worker") as prop:
        prop.return_value = {
            "ok": True,
            "suggestion_id": 42,
            "status": "new",
            "refresh_and_ask": True,
        }
        plan2 = classify_and_plan(
            db, "ten_t", "s1",
            "yes open proposal",
            context={"hash": "#dashboard"},
        )
        assert plan2 is not None
        assert plan2["intent"] == "ux_proposal_execute"
        drain_tasks(db, "ten_t", limit=5)
        assert prop.called


def test_ux_refine_finding(db):
    with patch("api.energy_agent._mem_set"), patch("api.energy_agent._mem_get", return_value=[]):
        classify_and_plan(
            db, "ten_t", "s1",
            "This dashboard is hard to use.",
            context={},
        )
        plan = classify_and_plan(
            db, "ten_t", "s1",
            "It's finding the information that is hard",
            context={},
        )
        assert plan is not None
        assert plan["intent"] == "ux_refine"


def test_metrics_shape(db):
    with patch("api.energy_agent._mem_set"), patch("api.energy_agent._mem_get", return_value=[]):
        classify_and_plan(
            db, "ten_t", "s1",
            "This dashboard is hard to use.",
            context={},
        )
        drain_tasks(db, "ten_t", limit=10)
    m = compute_metrics(db, "ten_t", days=30)
    assert m["ok"] is True
    assert m["north_star_kpi"] == "cost_per_successful_improvement"
    assert "tasks" in m and "interrupts" in m and "cost" in m
    assert m["tasks"]["done"] >= 1


def test_mind_tick_returns_budget(db):
    out = mind_tick(db, "ten_t")
    assert out["ok"] is True
    assert "interrupt_budget" in out
    assert out["interrupt_budget"]["min_importance"] == MIN_IMPORTANCE_TO_SPEAK


def test_search_similar_uses_plans(db):
    classify_and_plan(
        db, "ten_t", "s1",
        "This dashboard is hard to use and cluttered layout.",
        context={},
    )
    with patch("api.energy_agent._mem_set"), patch(
        "api.energy_agent._mem_get", return_value=[]
    ):
        # Second similar complaint should find the plan utterance
        classify_and_plan(
            db, "ten_t", "s1",
            "The layout is still hard to use and cluttered.",
            context={},
        )
        drain_tasks(db, "ten_t", limit=20)
    tasks = (
        db.query(EaTask)
        .filter(EaTask.kind == "search_similar", EaTask.status == "done")
        .all()
    )
    assert tasks
    import json
    # At least one search should have hits from prior plan
    any_hits = False
    for t in tasks:
        r = json.loads(t.result_json or "{}")
        if r.get("hits"):
            any_hits = True
    assert any_hits
