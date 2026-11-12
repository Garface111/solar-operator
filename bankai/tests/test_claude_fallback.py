"""Out of Fable credits -> try Opus, then Sonnet, before leaving Claude."""
import json

import pytest

from bankai import config
from bankai.agent.backends import claude_cli


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture(autouse=True)
def fallbacks(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_MODEL_FALLBACKS", "claude-opus-5,claude-sonnet-5")
    monkeypatch.setattr(config, "CLAUDE_CLI_MODEL", "")
    monkeypatch.setattr(config, "WEB_ACCESS", False)


def _model_of(cmd):
    return cmd[cmd.index("--model") + 1] if "--model" in cmd else "(default)"


def test_model_chain_is_routed_first_then_fallbacks(monkeypatch):
    assert claude_cli._model_chain("claude-fable-5") == [
        "claude-fable-5", "claude-opus-5", "claude-sonnet-5"]
    # the routed model is never duplicated if it's already in the fallback list
    assert claude_cli._model_chain("claude-opus-5") == ["claude-opus-5", "claude-sonnet-5"]


def test_out_of_fable_credits_falls_to_opus(session, monkeypatch):
    tried = []

    def fake_run(cmd, **kw):
        m = _model_of(cmd)
        tried.append(m)
        if m == "claude-fable-5":
            return FakeProc(stdout="Error: usage limit reached for claude-fable-5", returncode=1)
        return FakeProc(stdout=json.dumps({"result": f"answered on {m}"}))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = claude_cli.run(session, "sys", [{"role": "user", "content": "hi"}],
                         model="claude-fable-5", effort="max")
    assert out == "answered on claude-opus-5"
    assert tried == ["claude-fable-5", "claude-opus-5"]  # Fable tried, then Opus


def test_a_non_credit_error_does_not_switch_models(session, monkeypatch):
    tried = []

    def fake_run(cmd, **kw):
        tried.append(_model_of(cmd))
        return FakeProc(stdout="Error: the MCP server crashed", returncode=1)

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="claude CLI failed"):
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")
    assert tried == ["claude-fable-5"]  # a crash on Opus would be identical — don't waste it


def test_not_logged_in_never_tries_other_models(session, monkeypatch):
    tried = []

    def fake_run(cmd, **kw):
        tried.append(_model_of(cmd))
        return FakeProc(stdout="Invalid API key · Please run /login", returncode=1)

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="not logged in"):
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")
    assert tried == ["claude-fable-5"]  # login is dead for every model


def _expired_login_json() -> str:
    """The shape a logged-out CLI really prints: exit 1, a "success" subtype, and
    the reason buried ~900 characters in, after the counters."""
    return json.dumps({
        "duration_api_ms": 0, "stop_reason": "stop_sequence",
        "session_id": "00000000-0000-0000-0000-000000000000", "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                  "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                  "service_tier": "standard", "iterations": [], "speed": "standard"},
        "modelUsage": {}, "permission_denials": [], "terminal_reason": "api_error",
        "padding": "x" * 700,
        "is_error": True, "num_turns": 1, "subtype": "success", "api_error_status": None,
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
        "type": "result",
    })


def test_an_expired_login_is_named_not_buried_in_json(session, monkeypatch):
    """The reason sits past the old 500-char cut. It must still be recognised as
    a dead login — and say which command brings the brain back."""
    tried = []

    def fake_run(cmd, **kw):
        tried.append(_model_of(cmd))
        return FakeProc(stdout=_expired_login_json(), returncode=1)

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as err:
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")
    message = str(err.value)
    assert "not logged in" in message
    assert "OAuth session expired" in message  # what the CLI actually said
    assert "auth login" in message  # what to do about it
    assert tried == ["claude-fable-5"]  # no other model can fix a dead login


def test_a_401_status_alone_reads_as_a_dead_login(session, monkeypatch):
    def fake_run(cmd, **kw):
        return FakeProc(returncode=1, stdout=json.dumps(
            {"is_error": True, "api_error_status": 401, "result": "API Error"}))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="not logged in"):
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")


def test_a_json_failure_reports_its_reason_not_its_counters(session, monkeypatch):
    blob = json.loads(_expired_login_json())
    blob["result"] = "the MCP server crashed on startup"

    def fake_run(cmd, **kw):
        return FakeProc(stdout=json.dumps(blob), returncode=1)

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="claude CLI failed") as err:
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")
    assert "the MCP server crashed on startup" in str(err.value)
    assert "input_tokens" not in str(err.value)


def test_a_json_usage_limit_still_falls_to_the_next_model(session, monkeypatch):
    tried = []
    blob = json.loads(_expired_login_json())
    blob["result"] = "Claude usage limit reached. Your limit will reset at 3am."

    def fake_run(cmd, **kw):
        m = _model_of(cmd)
        tried.append(m)
        if m == "claude-fable-5":
            return FakeProc(stdout=json.dumps(blob), returncode=1)
        return FakeProc(stdout=json.dumps({"result": f"answered on {m}"}))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")
    assert out == "answered on claude-opus-5"
    assert tried == ["claude-fable-5", "claude-opus-5"]


def test_a_429_status_falls_to_the_next_model_whatever_the_wording(session, monkeypatch):
    """The status used to be found by searching the whole blob for 429. Now that
    only the reason is read, the status has to be honoured on its own."""
    tried = []
    blob = json.loads(_expired_login_json())
    blob["result"] = "You've hit your limit · resets 3am"
    blob["api_error_status"] = 429

    def fake_run(cmd, **kw):
        m = _model_of(cmd)
        tried.append(m)
        if m == "claude-fable-5":
            return FakeProc(stdout=json.dumps(blob), returncode=1)
        return FakeProc(stdout=json.dumps({"result": f"answered on {m}"}))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")
    assert out == "answered on claude-opus-5"
    assert tried == ["claude-fable-5", "claude-opus-5"]
    assert claude_cli._is_usage_error("You've hit your limit · resets 3am")
    assert claude_cli._is_usage_error("", 529)


def test_the_reason_survives_a_warning_line_and_keeps_stderr(session, monkeypatch):
    blob = json.loads(_expired_login_json())
    blob["result"] = "the MCP server crashed on startup"

    def fake_run(cmd, **kw):
        return FakeProc(stdout="Warning: something unrelated\n" + json.dumps(blob) + "\n",
                        stderr="node: out of memory", returncode=1)

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="claude CLI failed") as err:
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")
    assert "the MCP server crashed on startup" in str(err.value)
    assert "node: out of memory" in str(err.value)  # stderr is added, never dropped
    assert "input_tokens" not in str(err.value)


def test_failure_reason_falls_back_to_the_raw_output():
    for stdout in ("", "plain text error", "[1, 2]", '{"result": "   "}', '{"result": {"a": 1}}'):
        reason, status = claude_cli._failure_reason(FakeProc(stdout=stdout, stderr="E!"))
        assert reason == "E!" + stdout
        assert status is None


def test_all_claude_models_out_raises_for_the_outer_chain(session, monkeypatch):
    tried = []

    def fake_run(cmd, **kw):
        tried.append(_model_of(cmd))
        return FakeProc(stdout="rate limit exceeded", returncode=1)

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="all Claude models"):
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}], model="claude-fable-5")
    # it exhausted every Claude model before giving up (grok/kimi take it from here)
    assert tried == ["claude-fable-5", "claude-opus-5", "claude-sonnet-5"]


def test_usage_error_detection():
    for msg in ("usage limit reached", "rate limit exceeded", "429 Too Many Requests",
                "you are out of credits", "quota exceeded", "model is overloaded"):
        assert claude_cli._is_usage_error(msg)
    assert not claude_cli._is_usage_error("the MCP server crashed")
    assert not claude_cli._is_usage_error("file not found")
