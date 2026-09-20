"""The reality generator's memory: life facts with evidence, honest confidence,
and a place in every turn's system prompt."""
import json

from bankai import lifemodel
from bankai.agent import chat as agent_chat
from bankai.agent.tools import execute_tool


def test_a_fact_carries_its_evidence_into_the_system_prompt(session):
    lifemodel.record(
        session,
        statement="They built a home gym in late July",
        kind="event",
        evidence="$3.7k Amazon cluster 7/19-7/27 incl. identical $463.04 pair; Home Depot 7/25",
        confidence="medium",
    )
    system = agent_chat.build_system(session, channel="web")
    assert "Your life model" in system
    assert "home gym" in system
    assert "$463.04" in system


def test_no_facts_means_no_section(session):
    assert "Your life model" not in agent_chat.build_system(session, channel="web")


def test_refuted_and_retired_facts_leave_the_prompt(session):
    row = lifemodel.record(session, statement="They own a boat", kind="event",
                           evidence="one marina charge", confidence="low")
    lifemodel.update(session, row.id, status="refuted")
    assert lifemodel.render_for_system(session) == ""
    # but the track record is still consultable
    all_facts = lifemodel.as_dicts(session, include_closed=True)
    assert len(all_facts) == 1 and all_facts[0]["status"] == "refuted"


def test_the_render_budget_is_respected(session):
    for i in range(60):
        lifemodel.record(session, statement=f"Fact number {i} " + "x" * 90,
                         kind="rhythm", evidence="e" * 100)
    rendered = lifemodel.render_for_system(session)
    assert len(rendered) < lifemodel.RENDER_BUDGET_CHARS + 200
    assert "more — list_life_facts" in rendered


def test_the_full_tool_loop(session):
    out = json.loads(execute_tool(session, "record_life_fact", {
        "statement": "Cash withdrawal ~$200 near the 24th, monthly",
        "kind": "rhythm",
        "evidence": "HARBORSIDE SA WITHDRWL $203.50 on 6/24, 7/13, 7/24",
        "confidence": "high",
    }))
    assert out["recorded"] is True
    updated = json.loads(execute_tool(session, "update_life_fact", {
        "fact_id": out["fact_id"], "status": "confirmed",
    }))
    assert updated["status"] == "confirmed"
    listed = json.loads(execute_tool(session, "list_life_facts", {}))
    assert len(listed["facts"]) == 1
    assert listed["facts"][0]["confidence"] == "high"
    missing = json.loads(execute_tool(session, "update_life_fact", {
        "fact_id": "life_nope",
    }))
    assert "error" in missing


def test_life_review_is_marker_gated(session, monkeypatch):
    from bankai import config, scheduler

    calls = {"n": 0}

    def fake_turn(s, history, channel="web"):
        calls["n"] += 1
        return agent_chat.SILENCE

    monkeypatch.setattr(agent_chat, "run_turn", fake_turn)
    monkeypatch.setattr(config, "LIFE_REVIEW_DAYS", 7)
    first = scheduler.run_life_review_once()
    assert first["status"] == "quiet" and calls["n"] == 1
    second = scheduler.run_life_review_once()
    assert second["status"] == "not_due" and calls["n"] == 1  # marker holds


def test_the_doctrine_is_in_every_channel(session):
    system = agent_chat.build_system(session, channel="web")
    assert "reality generator" in system
    assert "record_life_fact" in system