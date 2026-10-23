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
