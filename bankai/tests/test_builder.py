"""The automatic builder: approving a code change dispatches an agent.

These tests exist because this is the one subsystem that can change the code
running the household's money. Every gate is tested from the outside — what the
dispatcher does when the agent lies, wanders, or breaks the suite.
"""
import json

import pytest

from bankai import builder, config
from bankai.db import session_scope
from bankai.models import AgentAction, ChatMessage


@pytest.fixture()
def action(session):
    """run_build reaches the module-level engine, so the row must live in the
    real (throwaway) DB — and be rebuilt per test, since that DB persists
    across the module."""
    with session_scope() as s:
        existing = s.get(AgentAction, "act_test_build")
        if existing is not None:
            s.delete(existing)
    row = AgentAction(
        id="act_test_build",
        kind="code_change",
        title="Add a transfer flag",
        rationale="Own-card payments inflate spend.",
        body="FILE: bankai/bankai/models.py\nAdd a transfer flag.",
        status="proposed",
    )
    with session_scope() as s:
        s.add(row)
    return row


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _agent_says(text):
    return FakeProc(stdout=json.dumps({"result": text}))


def test_scope_guard_names_every_stray_path():
    ok = ["bankai/bankai/models.py", "bankai/tests/test_models.py"]
    assert builder.out_of_scope(ok) == []
    stray = builder.out_of_scope(ok + [
        "bankai/.env",
        "api/app.py",                       # a different project in the repo
        "bankai/bankai.db",
        ".github/workflows/deploy.yml",
    ])
    assert stray == ["bankai/.env", "api/app.py", "bankai/bankai.db",
                     ".github/workflows/deploy.yml"]


def test_every_build_starts_from_the_remote_not_the_last_build(monkeypatch, tmp_path):
    """The build worktree is reset to origin before each run, so leftovers from
    a previous build can never ride along in an approved diff."""
    calls = []
    monkeypatch.setattr(config, "BUILDER_WORKTREE", str(tmp_path))
    monkeypatch.setattr(config, "BUILDER_BRANCH", "the-branch")
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(builder, "_git",
                        lambda w, *a, **k: calls.append(a) or "")
    builder.ensure_worktree()
    assert ("fetch", "origin", "the-branch") in calls
    assert ("reset", "--hard", "origin/the-branch") in calls
    assert ("clean", "-fd") in calls


def test_a_broken_build_worktree_fails_loudly(action, monkeypatch):
    def boom():
        raise RuntimeError("worktree add failed: no such remote")

    monkeypatch.setattr(builder, "ensure_worktree", boom)
    monkeypatch.setattr(builder.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not start the agent"))
    out = builder.run_build("act_test_build")
    assert out["status"] == "failed"
    assert "build worktree unusable" in out["note"]


def test_out_of_scope_edit_is_refused_and_never_committed(action, monkeypatch):
    calls = []
    monkeypatch.setattr(builder, "_git",
                        lambda w, *a, **k: calls.append(a) or "abc123\n")
    # clean before, strays after
    monkeypatch.setattr(builder, "ensure_worktree", lambda: "/tmp/build")
    seq = iter([["bankai/bankai/app.py", "bankai/.env"]])
    monkeypatch.setattr(builder, "changed_files", lambda w: next(seq))
    monkeypatch.setattr(builder.subprocess, "run",
                        lambda *a, **k: _agent_says("Done! Also updated .env."))
    monkeypatch.setattr(builder, "run_tests",
                        lambda w: pytest.fail("must not reach the test gate"))
    monkeypatch.setattr(builder, "deploy", lambda a: pytest.fail("must not deploy"))

    out = builder.run_build("act_test_build")
    assert out["status"] == "failed"
    assert "outside the copilot's own source" in out["note"]
    assert "bankai/.env" in out["note"]
    assert not any(a and a[0] == "commit" for a in calls)


def test_red_suite_blocks_commit_and_deploy_even_if_the_agent_claims_green(
    action, monkeypatch
):
    calls = []
    monkeypatch.setattr(builder, "_git",
                        lambda w, *a, **k: calls.append(a) or "abc123\n")
    monkeypatch.setattr(builder, "ensure_worktree", lambda: "/tmp/build")
    seq = iter([["bankai/bankai/models.py"]])
    monkeypatch.setattr(builder, "changed_files", lambda w: next(seq))
    monkeypatch.setattr(builder.subprocess, "run",
                        lambda *a, **k: _agent_says("All 384 tests pass."))
    monkeypatch.setattr(builder, "run_tests", lambda w: (False, "2 failed, 382 passed"))
    monkeypatch.setattr(builder, "deploy", lambda a: pytest.fail("must not deploy on red"))

    out = builder.run_build("act_test_build")
    assert out["status"] == "failed"
    assert "suite is red" in out["note"]
    assert not any(a and a[0] == "commit" for a in calls)
    with session_scope() as s:
        assert s.get(AgentAction, "act_test_build").status == "failed"


def test_green_build_commits_deploys_and_tells_the_household(action, monkeypatch):
    calls = []

    def fake_git(w, *a, **k):
        calls.append(a)
        if a[0] == "rev-parse" and "--short" in a:
            return "deadbee\n"
        if a[0] == "rev-parse" and "--abbrev-ref" in a:
            return "claude/joint-banking-ai-dashboard-vp8gyq\n"
        return "abc123\n"

    monkeypatch.setattr(builder, "_git", fake_git)
    monkeypatch.setattr(builder, "ensure_worktree", lambda: "/tmp/build")
    seq = iter([["bankai/bankai/models.py", "bankai/tests/test_models.py"]])
    monkeypatch.setattr(builder, "changed_files", lambda w: next(seq))
    monkeypatch.setattr(builder.subprocess, "run",
                        lambda *a, **k: _agent_says("Added the transfer flag and two tests."))
    monkeypatch.setattr(builder, "run_tests", lambda w: (True, "386 passed"))
    monkeypatch.setattr(builder, "deploy", lambda action_id: "deploy launched")
    monkeypatch.setattr(config, "BUILDER_AUTO_DEPLOY", True)

    out = builder.run_build("act_test_build")
    assert out["status"] == "executed"
    assert "deadbee" in out["note"] and "386 passed" in out["note"]

    committed = [a for a in calls if a and a[0] == "commit"]
    assert committed, "a green in-scope build must commit"
    # only the files that passed the gates — never `git add -A` in a shared tree
    added = [a for a in calls if a and a[0] == "add"][0]
    assert set(added[1:]) == {"bankai/bankai/models.py", "bankai/tests/test_models.py"}
    assert any(a and a[0] == "push" for a in calls)

    with session_scope() as s:
        assert s.get(AgentAction, "act_test_build").status == "executed"
        said = [m.content for m in s.query(ChatMessage).all()]
    assert any("transfer flag" in c for c in said)


def test_a_failed_deploy_is_reported_not_swallowed(action, monkeypatch):
    monkeypatch.setattr(builder, "_git", lambda w, *a, **k: "deadbee\n")
    monkeypatch.setattr(builder, "ensure_worktree", lambda: "/tmp/build")
    seq = iter([["bankai/bankai/models.py"]])
    monkeypatch.setattr(builder, "changed_files", lambda w: next(seq))
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: _agent_says("done"))
    monkeypatch.setattr(builder, "run_tests", lambda w: (True, "386 passed"))

    def boom(action_id):
        raise RuntimeError("systemd-run refused to launch")

    monkeypatch.setattr(builder, "deploy", boom)
    monkeypatch.setattr(config, "BUILDER_AUTO_DEPLOY", True)

    out = builder.run_build("act_test_build")
    assert out["status"] == "failed"
    assert "could not be launched" in out["note"] and "untouched" in out["note"]


def test_only_one_build_runs_at_a_time(action, monkeypatch):
    builder._LOCK.acquire()
    try:
        out = builder.run_build("act_test_build")
        assert "another build is already running" in out["note"]
        assert out["status"] == "proposed"   # still approvable once the queue clears
    finally:
        builder._LOCK.release()


def test_prompt_carries_the_proposal_and_the_hard_boundaries(action):
    prompt = builder.build_prompt(action)
    assert "Add a transfer flag" in prompt
    assert "Own-card payments inflate spend." in prompt
    assert "Do NOT commit, push, or deploy" in prompt
    assert ".env" in prompt
    assert "read-only against real money" in prompt


def test_the_build_runs_outside_this_service(action, monkeypatch):
    """A build must not be a child of bankai.service: the service is restarted
    often (deploys, the watchdog), and a restart SIGKILLs its whole cgroup —
    which is exactly how the first real build died, at exit 143."""
    seen = {}

    class P:
        returncode = 0
        stdout = "Running as unit: bankai-build-x.service"
        stderr = ""

    monkeypatch.setattr(builder.subprocess, "run",
                        lambda cmd, **k: seen.update(cmd=cmd) or P())
    unit = builder.spawn("act_test_build")
    assert unit, "spawn should return the transient unit name"
    assert seen["cmd"][0] == "systemd-run"
    assert "-m" in seen["cmd"] and "bankai.builder" in seen["cmd"]
    assert seen["cmd"][-1] == "act_test_build"


def test_a_launch_failure_is_recorded_not_silent(action, monkeypatch):
    class P:
        returncode = 1
        stdout = ""
        stderr = "Failed to start transient service unit"

    monkeypatch.setattr(builder.subprocess, "run", lambda cmd, **k: P())
    assert builder.spawn("act_test_build") == ""
    with session_scope() as s:
        row = s.get(AgentAction, "act_test_build")
    assert row.status == "failed" and "could not launch" in row.result


def test_the_deploy_reports_its_own_outcome(action, monkeypatch, tmp_path):
    """The deploy stops the service the builder may be watching, so it writes
    its own result rather than relying on a caller that may be gone."""
    script_path = tmp_path / "deploy.sh"
    monkeypatch.setattr(builder, "_DEPLOY_SCRIPT", script_path)

    class P:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(builder.subprocess, "run", lambda cmd, **k: P())
    builder.deploy("act_test_build")
    written = script_path.read_text()
    assert "act_test_build" in written
    assert "systemctl stop bankai" in written and "systemctl start bankai" in written
    assert "record " in written                      # writes its own outcome
    assert "api/health" in written                   # and verifies before claiming success
    assert "outcome='claimed'" in written            # claim-safe restart discipline


def _fake_pytest_run(rc, out):
    class P:
        returncode = rc
        stdout = out
        stderr = ""
    return P()


def test_a_red_suite_names_the_failing_tests(monkeypatch, tmp_path):
    """'2 failed' is not a report. The first blocked build said exactly that and
    the failures never reproduced — nobody could act on it."""
    monkeypatch.setattr(builder, "TEST_LOG", tmp_path / "t.log")
    out = ("FAILED tests/test_rules.py::test_digest - assert\n"
           "FAILED tests/test_sms.py::test_reply - assert\n"
           "2 failed, 530 passed in 5.98s")
    monkeypatch.setattr(builder.subprocess, "run",
                        lambda *a, **k: _fake_pytest_run(1, out))
    green, note = builder.run_tests("/tmp/wt")
    assert green is False
    assert "tests/test_rules.py::test_digest" in note
    assert "tests/test_sms.py::test_reply" in note
    assert (tmp_path / "t.log").exists()


def test_a_flake_is_called_a_flake_and_still_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "TEST_LOG", tmp_path / "t.log")
    runs = iter([
        _fake_pytest_run(1, "FAILED tests/test_rules.py::test_digest - assert\n"
                            "2 failed, 530 passed in 5.98s"),
        _fake_pytest_run(0, "532 passed in 5.21s"),
    ])
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: next(runs))
    green, note = builder.run_tests("/tmp/wt")
    assert green is False, "a flaky suite must not clear a change to money software"
    assert "FLAKY" in note
    assert "tests/test_rules.py::test_digest" in note


def test_a_real_break_reports_what_failed_both_times(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "TEST_LOG", tmp_path / "t.log")
    red = _fake_pytest_run(1, "FAILED tests/test_models.py::test_default - assert\n"
                              "1 failed, 531 passed in 5.5s")
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: red)
    green, note = builder.run_tests("/tmp/wt")
    assert green is False
    assert "failing consistently" in note
    assert "tests/test_models.py::test_default" in note


def test_build_subprocesses_never_inherit_household_secrets(monkeypatch):
    """config.load_dotenv puts every live secret in this process's environment.
    Two builds were blocked because the verification suite inherited them and
    ran against the household's real mailbox — and an agent writing code should
    not hold their API keys either."""
    monkeypatch.setenv("RESEND_API_KEY", "re_live_secret")
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL", "https://user:pass@bridge")
    monkeypatch.setenv("APP_PASSWORD", "maple-ledger")
    monkeypatch.setenv("HOME", "/root")
    env = builder._clean_env()
    assert "RESEND_API_KEY" not in env
    assert "SIMPLEFIN_ACCESS_URL" not in env
    assert "APP_PASSWORD" not in env
    assert env["HOME"] == "/root", "the Claude CLI finds its own auth via HOME"
    assert "PATH" in env


def test_the_test_runner_uses_the_scrubbed_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "TEST_LOG", tmp_path / "t.log")
    monkeypatch.setenv("RESEND_API_KEY", "re_live_secret")
    seen = {}

    class P:
        returncode = 0
        stdout = "543 passed in 5.5s"
        stderr = ""

    monkeypatch.setattr(builder.subprocess, "run",
                        lambda *a, **k: seen.update(k) or P())
    builder.run_tests("/tmp/wt")
    assert "env" in seen, "pytest must run with an explicit environment"
    assert "RESEND_API_KEY" not in seen["env"]
