import json

from bankai.agent.chat import build_system
from bankai.agent.tools import execute_tool
from bankai.models import MemoryNote


def test_save_memory_upserts_by_title(session):
    r1 = json.loads(execute_tool(session, "save_memory",
                                 {"title": "Account nicknames", "content": "BofA = 'the joint'"}))
    assert r1["saved"]
    execute_tool(session, "save_memory",
                 {"title": "Account nicknames", "content": "BofA = 'the joint'; Amex = 'travel card'"})
    notes = session.query(MemoryNote).all()
    assert len(notes) == 1
    assert "travel card" in notes[0].content


def test_delete_memory(session):
    execute_tool(session, "save_memory", {"title": "Stale", "content": "x"})
    r = json.loads(execute_tool(session, "delete_memory", {"title": "Stale"}))
    assert r["deleted"]
    assert session.query(MemoryNote).count() == 0
    missing = json.loads(execute_tool(session, "delete_memory", {"title": "Nope"}))
    assert "error" in missing


def test_persona_keeps_the_guardrails(session):
    """The collector persona must always carry its limits and vault duties."""
    system = build_system(session, "web")
    assert "not a licensed attorney" in system
    assert "calm but intense collector" in system
    assert "Document intake checklist" in system
    assert "annotate_document" in system
    assert "cannot move money" in system


def test_memory_injected_into_system_prompt(session):
    assert "persistent memory notes" not in build_system(session, "web")
    execute_tool(session, "save_memory",
                 {"title": "Goals", "content": "Saving $20k for a house down payment"})
    system = build_system(session, "web")
    assert "## Your persistent memory notes" in system
    assert "house down payment" in system


def test_persona_forbids_stale_figures_and_invented_actions(session):
    """Two live failures, both in one sent email: net worth quoted $10,012 low
    from pre-sync numbers, and 'I've reclassified it as income going forward'
    for a capability that does not exist."""
    system = build_system(session, "web")
    assert "re-pull every figure" in system
    assert "Never describe an action you did not take" in system
    assert "propose_code_change" in system
