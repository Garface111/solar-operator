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


def test_memory_injected_into_system_prompt(session):
    assert "persistent memory notes" not in build_system(session, "web")
    execute_tool(session, "save_memory",
                 {"title": "Goals", "content": "Saving $20k for a house down payment"})
    system = build_system(session, "web")
    assert "## Your persistent memory notes" in system
    assert "house down payment" in system
