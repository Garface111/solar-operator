"""Standing projects: the copilot carries multi-step work across turns, and the
tending cadence advances it — without widening what any step is allowed to do."""
import json

from bankai import initiatives
from bankai.agent import chat as agent_chat
from bankai.agent.tools import execute_tool
from bankai.models import Initiative


def test_open_and_list_round_trip(session):
    out = json.loads(execute_tool(session, "open_initiative", {
        "title": "Debt triage desk",
        "goal": "1420 to $0 by Mar 2027, exact due dates pinned",
        "plan": "1) confirm APR 2) fee-waiver letter 3) autopay minimums",
        "next_action": "Confirm the 1420's real post-promo APR",
        "priority": 10,
    }))
    assert out["opened"] is True
    listed = json.loads(execute_tool(session, "list_initiatives", {}))
    assert len(listed["initiatives"]) == 1
    assert listed["initiatives"][0]["title"] == "Debt triage desk"
    assert listed["initiatives"][0]["next_action"].startswith("Confirm")


def test_worklog_is_append_only_and_advances(session):
    row = initiatives.open_initiative(session, title="Surrogacy finance file", goal="paid vs remaining")
    initiatives.update_initiative(session, row.id, worklog_entry="Vaulted the agency contract",
                                  next_action="Extract the milestone schedule")
    initiatives.update_initiative(session, row.id, worklog_entry="Built the paid-vs-remaining ledger")
    fresh = session.get(Initiative, row.id)
    assert "Vaulted the agency contract" in fresh.worklog
    assert "paid-vs-remaining ledger" in fresh.worklog
    assert fresh.worklog.index("Vaulted") < fresh.worklog.index("ledger")  # order preserved
    assert fresh.next_action == "Extract the milestone schedule"


def test_blocking_surfaces_the_need(session):
    row = initiatives.open_initiative(session, title="Surrogacy file", goal="x")
    out = json.loads(execute_tool(session, "update_initiative", {
        "initiative_id": row.id,
        "blocked_on": "the agency contract and payment schedule",
    }))
    assert out["status"] == "blocked"
    needs = initiatives.blocked_needs(session)
    assert needs and needs[0]["blocked_on"].startswith("the agency contract")


def test_priority_orders_what_gets_advanced(session):
    a = initiatives.open_initiative(session, title="low", goal="x", priority=200)
    b = initiatives.open_initiative(session, title="high", goal="x", priority=5)
    initiatives.open_initiative(session, title="mid", goal="x", priority=100)
    assert initiatives.next_to_advance(session).id == b.id
    # a blocked top-priority item is skipped for advancement
    initiatives.update_initiative(session, b.id, blocked_on="waiting")
    nxt = initiatives.next_to_advance(session)
    assert nxt.title == "mid"


def test_closing_removes_it_from_the_open_set(session):
    row = initiatives.open_initiative(session, title="done thing", goal="x")
    initiatives.update_initiative(session, row.id, status="done")
    assert initiatives.as_dicts(session) == []
    assert len(initiatives.as_dicts(session, include_closed=True)) == 1


def test_active_projects_ride_into_every_system_prompt(session):
    initiatives.open_initiative(
        session, title="Planning-sheet sync", goal="publish actuals weekly",
        next_action="Publish this week's actuals to the tab", priority=20,
    )
    system = agent_chat.build_system(session, channel="web")
    assert "standing projects" in system.lower()
    assert "Planning-sheet sync" in system
    assert "Publish this week's actuals" in system


def test_no_projects_means_no_section(session):
    assert "standing projects" not in agent_chat.build_system(session, channel="web").lower()


def test_the_tending_prompt_puts_projects_first(session):
    system = agent_chat.build_system(session, channel="tending")
    assert "advance your projects" in system
    assert "list_initiatives" in system


# --- generate_report ---

def test_generate_report_renders_and_reports_delivery(session, monkeypatch, tmp_path):
    from bankai import reports
    from bankai.messaging import email_thread

    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(email_thread, "configured", lambda: False)  # no email in test
    out = json.loads(execute_tool(session, "generate_report", {
        "title": "Debt paydown plan",
        "sections": [
            {"heading": "Where it stands", "body": "1420 at ~22% APR, $11,281 owed."},
            {"heading": "The plan", "body": "$1,600/mo synced to the redemption cadence."},
        ],
        "print_copy": False,
    }))
    assert out["generated"] is True
    assert (tmp_path / "report-debt-paydown-plan.pdf").exists()
    # honest about delivery when email is off
    assert "not configured" in out["delivery_note"]


def test_generate_report_needs_sections(session):
    out = json.loads(execute_tool(session, "generate_report", {
        "title": "Empty", "sections": [],
    }))
    assert "error" in out