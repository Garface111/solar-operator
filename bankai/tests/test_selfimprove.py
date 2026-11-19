"""Self-improvement: the copilot reads its own code and proposes real patches,
and the path guard that keeps that power off the secrets."""
import json

import pytest

from bankai import selfimprove
from bankai.agent.tools import execute_tool
from bankai.models import CodeProposal


# --- the path guard is the security control ---

def test_in_scope_paths_pass():
    assert selfimprove.safe_repo_path("bankai/pending.py") == "bankai/pending.py"
    assert selfimprove.safe_repo_path("tests/test_pending.py") == "tests/test_pending.py"
    assert selfimprove.safe_repo_path("bankai/connectors/csv_import.py")


def test_secrets_and_data_are_never_in_scope():
    for bad in (".env", "bankai/../.env", "/opt/bankai/.env", "bankai/data.db",
                "documents/deed.pdf", "../etc/passwd", "run.py", "bankai/.env"):
        with pytest.raises(selfimprove.PathRefused):
            selfimprove.safe_repo_path(bad)


def test_read_source_refuses_out_of_scope(session):
    out = json.loads(execute_tool(session, "read_source", {"path": ".env"}))
    assert "error" in out
    out2 = json.loads(execute_tool(session, "read_source", {"path": "bankai/pending.py"}))
    assert "content" in out2 and "def reconcile" in out2["content"]


def test_read_source_missing_file_is_a_clean_error(session):
    out = json.loads(execute_tool(session, "read_source", {"path": "bankai/nope.py"}))
    assert "error" in out and "no such" in out["error"]


def test_list_source_only_returns_in_scope_files(session):
    out = json.loads(execute_tool(session, "list_source", {"subdir": "bankai"}))
    assert "bankai/pending.py" in out["files"]
    assert not any(".env" in f for f in out["files"])


# --- proposing a patch ---

def test_a_proposal_is_recorded_and_diffed(session):
    out = json.loads(execute_tool(session, "propose_patch", {
        "title": "Add a docstring line",
        "rationale": "clarity",
        "files": {"bankai/pending.py": "# a totally new body\n"},
        "test_paths": "tests/test_pending.py",
    }))
    assert out["proposed"] is True
    row = session.query(CodeProposal).one()
    assert row.status == "proposed"
    assert "bankai/pending.py" in json.loads(row.files_json)
    # the diff is computed against the real deployed source
    assert "a totally new body" in row.diff
    assert "def reconcile" in row.diff  # the old content shows as removed


def test_a_proposal_touching_secrets_is_refused(session):
    out = json.loads(execute_tool(session, "propose_patch", {
        "title": "sneak", "rationale": "x",
        "files": {".env": "STOLEN=1"},
    }))
    assert "error" in out
    assert session.query(CodeProposal).count() == 0


def test_a_proposal_with_an_out_of_scope_test_is_refused(session):
    out = json.loads(execute_tool(session, "propose_patch", {
        "title": "x", "rationale": "y",
        "files": {"bankai/pending.py": "x=1\n"},
        "test_paths": "../evil.py",
    }))
    assert "error" in out
    assert session.query(CodeProposal).count() == 0


def test_empty_proposal_is_rejected(session):
    out = json.loads(execute_tool(session, "propose_patch", {
        "title": "x", "rationale": "y", "files": {},
    }))
    assert "error" in out


def test_list_code_proposals_round_trips(session):
    execute_tool(session, "propose_patch", {
        "title": "First", "rationale": "r",
        "files": {"bankai/pending.py": "x=1\n"},
    })
    listed = json.loads(execute_tool(session, "list_code_proposals", {}))
    assert len(listed["proposals"]) == 1
    assert listed["proposals"][0]["title"] == "First"
    assert listed["proposals"][0]["status"] == "proposed"


# --- the deploy gate is real: the copilot cannot ship itself ---

def test_there_is_no_tool_that_ships_a_proposal():
    from bankai.agent.tools import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "propose_patch" in names
    # nothing that merges, deploys, ships, or applies to the live tree
    for forbidden in ("ship_proposal", "merge_proposal", "deploy", "apply_patch"):
        assert forbidden not in names


# --- evaluation refuses to fake a sandbox ---

def test_evaluation_without_a_sandbox_does_not_execute(session, monkeypatch):
    from bankai import config, selfimprove_sandbox

    monkeypatch.setattr(config, "SELFIMPROVE_SANDBOX", "")
    row = selfimprove.record_proposal(
        session, title="x", rationale="y",
        files={"bankai/pending.py": "x=1\n"},
    )
    result = selfimprove_sandbox.evaluate(session, row.id)
    assert result["status"] == "awaiting_sandbox"
    assert "NOT run" in session.get(CodeProposal, row.id).test_output


# --- doctrine ---

def test_the_prompt_invites_self_improvement(session):
    from bankai.agent import chat as agent_chat
    system = agent_chat.build_system(session, channel="web")
    assert "improve your own code" in system
    assert "propose_patch" in system
    assert "cannot deploy yourself" in system