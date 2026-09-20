"""The Grok CLI fallback backend: right command, clean auth failure, robust parse."""
import json

import pytest

from bankai import config
from bankai.agent.backends import grok_cli


@pytest.fixture(autouse=True)
def grok_env(monkeypatch):
    monkeypatch.setattr(config, "GROK_CLI_BIN", "grok")
    monkeypatch.setattr(config, "GROK_CLI_MODEL", "")
    monkeypatch.setattr(config, "GROK_MODEL", "grok-4")
    monkeypatch.setattr(config, "WEB_ACCESS", False)


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_command_uses_grok_flags_and_resolves_the_model(session, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        grok_cli.subprocess, "run",
        lambda cmd, **kw: seen.update(cmd=cmd) or FakeProc(json.dumps({"result": "hi"})),
    )
    # the router hands a Claude model name on a fallback turn — grok must ignore it
    out = grok_cli.run(session, "SYSTEM", [{"role": "user", "content": "hi"}],
                       model="claude-fable-5", effort="max")
    assert out == "hi"
    cmd = seen["cmd"]
    assert cmd[0] == "grok" and "-p" in cmd
    assert "--system-prompt-override" in cmd and "SYSTEM" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "grok-4"      # not claude-fable-5
    assert "--always-approve" in cmd
    assert "--disable-web-search" in cmd                   # WEB_ACCESS off
    assert "mcp__bankai__*,Read" in cmd


def test_an_explicit_grok_model_is_honored(session, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        grok_cli.subprocess, "run",
        lambda cmd, **kw: seen.update(cmd=cmd) or FakeProc(json.dumps({"result": "x"})),
    )
    grok_cli.run(session, "s", [{"role": "user", "content": "x"}], model="grok-4-heavy")
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "grok-4-heavy"


def test_not_signed_in_raises_an_actionable_error(session, monkeypatch):
    monkeypatch.setattr(
        grok_cli.subprocess, "run",
        lambda cmd, **kw: FakeProc(
            stdout='{"type":"error","message":"Not signed in. run grok login"}',
            returncode=1),
    )
    with pytest.raises(RuntimeError, match="not signed in"):
        grok_cli.run(session, "s", [{"role": "user", "content": "x"}])


def test_missing_binary_is_actionable(session, monkeypatch):
    def missing(cmd, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(grok_cli.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="not.*found"):
        grok_cli.run(session, "s", [{"role": "user", "content": "x"}])


def test_web_search_stays_on_when_web_access_is_granted(session, monkeypatch):
    monkeypatch.setattr(config, "WEB_ACCESS", True)
    seen = {}
    monkeypatch.setattr(
        grok_cli.subprocess, "run",
        lambda cmd, **kw: seen.update(cmd=cmd) or FakeProc(json.dumps({"result": "x"})),
    )
    grok_cli.run(session, "s", [{"role": "user", "content": "x"}])
    assert "--disable-web-search" not in seen["cmd"]


def test_parse_handles_the_known_shapes():
    assert grok_cli._parse(json.dumps({"result": "A"})) == "A"
    assert grok_cli._parse(json.dumps({"response": "B"})) == "B"
    assert grok_cli._parse(json.dumps({"content": [{"text": "C"}, {"text": "D"}]})) == "CD"
    # trailing noise around a JSON line
    assert grok_cli._parse('log line\n{"result": "E"}') == "E"
    # not JSON at all — hand back the raw text rather than nothing
    assert grok_cli._parse("plain reply") == "plain reply"


# --- it plugs into the fallback chain ---

def test_chain_falls_through_grok_to_kimi_when_grok_is_unauthed(session, monkeypatch):
    from bankai.agent import chat as agent_chat

    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli,grok-cli,kimi")
    monkeypatch.setattr(config, "ROUTER_ENABLED", False)
    monkeypatch.setattr(config, "CLAUDE_CLI_MODEL", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_EFFORT", "")

    from bankai.agent.backends import claude_cli, grok_cli as gc, kimi_backend
    monkeypatch.setattr(claude_cli, "run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("claude down")))
    monkeypatch.setattr(gc, "run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("grok not signed in")))
    monkeypatch.setattr(kimi_backend, "run", lambda *a, **k: "kimi answered")
    monkeypatch.setattr(agent_chat.verify, "verified_turn", lambda *a, **k: ("kimi answered", {}))

    out = agent_chat.run_turn(session, [{"role": "user", "content": "hi"}])
    assert out == "kimi answered"  # grok-cli tried, failed cleanly, kimi caught it
