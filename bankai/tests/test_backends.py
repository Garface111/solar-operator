import json

import pytest

from bankai import config
from bankai.agent import chat as agent_chat
from bankai.agent import mcp_server
from bankai.agent import verify
from bankai.agent.backends import claude_cli, grok_backend
from bankai.agent.tools import TOOLS
from bankai.ingest import upsert_account

CONSEQUENTIAL = "You should move $5,000 into savings."


def test_dispatch_uses_configured_backend(session, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "LLM_BACKEND", "grok")
    monkeypatch.setattr(
        grok_backend, "run", lambda s, system, messages: calls.append(messages) or "hi from grok"
    )
    reply = agent_chat.run_turn(session, [{"role": "user", "content": "hello"}])
    assert reply == "hi from grok"
    assert calls[0][-1] == {"role": "user", "content": "hello"}


def test_backend_chain_falls_back_in_order(session, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli,grok")

    def cli_down(s, system, messages):
        raise RuntimeError("the Claude CLI on this machine is not logged in")

    monkeypatch.setattr(claude_cli, "run", cli_down)
    monkeypatch.setattr(grok_backend, "run", lambda s, system, messages: "grok took over")
    reply = agent_chat.run_turn(session, [{"role": "user", "content": "hi"}])
    assert reply == "grok took over"


def test_backend_chain_first_success_stops_the_chain(session, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli,grok")
    monkeypatch.setattr(claude_cli, "run", lambda s, system, messages: "cli answered")
    monkeypatch.setattr(
        grok_backend, "run",
        lambda s, system, messages: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert agent_chat.run_turn(session, [{"role": "user", "content": "hi"}]) == "cli answered"


def test_backend_chain_reports_every_failure(session, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli,grok")

    def cli_down(s, system, messages):
        raise RuntimeError("not logged in — run /login")

    def grok_down(s, system, messages):
        raise RuntimeError("XAI_API_KEY is not set")

    monkeypatch.setattr(claude_cli, "run", cli_down)
    monkeypatch.setattr(grok_backend, "run", grok_down)
    with pytest.raises(RuntimeError) as exc:
        agent_chat.run_turn(session, [{"role": "user", "content": "hi"}])
    msg = str(exc.value)
    assert "claude-cli:" in msg and "grok:" in msg and "XAI_API_KEY" in msg


def test_run_turn_passes_reply_through_when_verifier_disabled(session, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "grok")
    monkeypatch.setattr(verify, "VERIFY_REPLIES", False)
    calls = []
    monkeypatch.setattr(
        grok_backend, "run", lambda s, system, messages: calls.append(system) or CONSEQUENTIAL
    )
    assert agent_chat.run_turn(session, [{"role": "user", "content": "hi"}]) == CONSEQUENTIAL
    assert len(calls) == 1  # no critic pass


def test_run_turn_verifies_with_the_backend_that_answered(session, monkeypatch):
    """On a fallback chain the critic must be the winning backend, not a re-resolve."""
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli,grok")
    monkeypatch.setattr(verify, "VERIFY_REPLIES", True)

    def cli_down(s, system, messages):
        raise RuntimeError("not logged in")

    systems = []

    def grok(s, system, messages):
        systems.append(system)
        if len(systems) == 1:
            return CONSEQUENTIAL
        if len(systems) == 2:
            return '{"verdict": "revise", "problems": ["the $5,000 is unsupported"], "severity": "high"}'
        return "Corrected reply."

    monkeypatch.setattr(claude_cli, "run", cli_down)
    monkeypatch.setattr(grok_backend, "run", grok)
    assert agent_chat.run_turn(session, [{"role": "user", "content": "hi"}]) == "Corrected reply."
    # The dead backend was never asked to critique; grok answered all three calls.
    assert len(systems) == 3
    assert systems[1] == verify.CRITIC_SYSTEM and systems[2] == verify.REVISION_SYSTEM


def test_run_turn_skips_verification_on_sms(session, monkeypatch):
    """A revision is generated without SMS_ADDENDUM, so SMS replies are never revised."""
    monkeypatch.setattr(config, "LLM_BACKEND", "grok")
    monkeypatch.setattr(verify, "VERIFY_REPLIES", True)
    calls = []
    monkeypatch.setattr(
        grok_backend, "run", lambda s, system, messages: calls.append(system) or CONSEQUENTIAL
    )
    reply = agent_chat.run_turn(session, [{"role": "user", "content": "hi"}], channel="sms")
    assert reply == CONSEQUENTIAL
    assert len(calls) == 1


def test_run_turn_keeps_original_reply_when_critic_fails(session, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "grok")
    monkeypatch.setattr(verify, "VERIFY_REPLIES", True)
    state = {"n": 0}

    def grok(s, system, messages):
        state["n"] += 1
        if state["n"] == 1:
            return CONSEQUENTIAL
        raise RuntimeError("critic backend exploded")

    monkeypatch.setattr(grok_backend, "run", grok)
    # The critic failing must degrade to the original reply, not to the next backend.
    assert agent_chat.run_turn(session, [{"role": "user", "content": "hi"}]) == CONSEQUENTIAL


def test_claude_cli_not_logged_in_is_actionable(session, monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = "Not logged in · Please run /login"
        stderr = ""

    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: FakeProc())
    with pytest.raises(RuntimeError, match="not logged in"):
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}])


def test_grok_tools_are_openai_shaped():
    tools = grok_backend.tools_openai_format()
    assert len(tools) == len(TOOLS)
    fn = tools[0]["function"]
    assert tools[0]["type"] == "function"
    assert set(fn) == {"name", "description", "parameters"}
    assert fn["parameters"]["type"] == "object"


def test_grok_tool_loop(session, monkeypatch):
    upsert_account(session, source="csv", name="Checking", balance=250.0)
    monkeypatch.setattr(config, "XAI_API_KEY", "xai-test")
    responses = [
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_accounts", "arguments": "{}"},
                    }],
                }
            }]
        },
        {"choices": [{"message": {"role": "assistant", "content": "You have $250 in Checking."}}]},
    ]
    payloads = []
    monkeypatch.setattr(
        grok_backend, "_post", lambda payload: payloads.append(payload) or responses.pop(0)
    )
    reply = grok_backend.run(session, "system", [{"role": "user", "content": "balance?"}])
    assert reply == "You have $250 in Checking."
    # Second request carried the tool result back.
    tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "call_1"
    assert "250" in tool_msgs[0]["content"]


def test_grok_requires_key(session, monkeypatch):
    monkeypatch.setattr(config, "XAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        grok_backend.run(session, "s", [{"role": "user", "content": "x"}])


def test_claude_cli_command_and_parse(session, monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"type": "result", "result": "Net worth is $10."})
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    reply = claude_cli.run(
        session, "SYSTEM", [{"role": "user", "content": "[Ford] net worth?"}]
    )
    assert reply == "Net worth is $10."
    cmd = captured["cmd"]
    assert cmd[0] == config.CLAUDE_CLI_BIN and cmd[1] == "-p"
    assert "--append-system-prompt" in cmd and "SYSTEM" in cmd
    # Read is allowed so the copilot can open vault images (pasted screenshots)
    assert "--allowedTools" in cmd and "mcp__bankai__*,WebSearch,WebFetch,Read" in cmd
    mcp_cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert "bankai" in mcp_cfg["mcpServers"]
    assert "[Ford] net worth?" in cmd[2]


def test_claude_cli_missing_binary(session, monkeypatch):
    def raise_missing(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(claude_cli.subprocess, "run", raise_missing)
    with pytest.raises(RuntimeError, match="not found"):
        claude_cli.run(session, "s", [{"role": "user", "content": "x"}])


def test_mcp_server_initialize_list_call(session):
    init = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}}
    )
    assert init["result"]["serverInfo"]["name"] == "bankai"
    assert init["result"]["protocolVersion"] == "2025-06-18"

    assert mcp_server.handle_request({"method": "notifications/initialized"}) is None

    listed = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "get_accounts" in names and "create_rule" in names

    upsert_account(session, source="csv", name="Checking", balance=99.0)
    called = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_accounts", "arguments": {}}},
        session=session,
    )
    text = called["result"]["content"][0]["text"]
    assert "99" in text

    unknown = mcp_server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert unknown["error"]["code"] == -32601


# --- each spouse can hold their own bank connection ---

def test_multiple_simplefin_bridges_are_all_pulled(monkeypatch):
    from bankai.connectors import simplefin

    monkeypatch.setattr(config, "SIMPLEFIN_ACCESS_URLS", ["https://a/x", "https://b/y"])
    seen = []
    monkeypatch.setattr(
        simplefin, "_sync_one",
        lambda url, days=90: seen.append(url) or
        {"status": "ok", "accounts": 3, "added": 5, "skipped": 1},
    )
    out = simplefin.sync()
    assert seen == ["https://a/x", "https://b/y"]
    assert out["bridges"] == 2 and out["accounts"] == 6 and out["added"] == 10


def test_one_broken_bridge_does_not_stop_the_other(monkeypatch):
    """A spouse's expired connection must not silently halt the household's sync."""
    from bankai.connectors import simplefin

    monkeypatch.setattr(config, "SIMPLEFIN_ACCESS_URLS", ["https://good/x", "https://dead/y"])
    monkeypatch.setattr(
        simplefin, "_sync_one",
        lambda url, days=90: (
            {"status": "ok", "accounts": 2, "added": 4, "skipped": 0}
            if "good" in url
            else {"status": "error", "detail": "connection expired"}
        ),
    )
    out = simplefin.sync()
    assert out["status"] == "partial"
    assert out["accounts"] == 2 and out["added"] == 4
    assert out["failures"] == ["connection expired"]


def test_a_single_url_still_works_unchanged(monkeypatch):
    from bankai.connectors import simplefin

    monkeypatch.setattr(config, "SIMPLEFIN_ACCESS_URLS", ["https://only/one"])
    monkeypatch.setattr(
        simplefin, "_sync_one", lambda url, days=90: {"status": "ok", "accounts": 8}
    )
    out = simplefin.sync()
    assert out == {"status": "ok", "accounts": 8}  # not wrapped in the multi shape
