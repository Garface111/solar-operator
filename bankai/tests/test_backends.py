import json

import pytest

from bankai import config
from bankai.agent import chat as agent_chat
from bankai.agent import mcp_server
from bankai.agent.backends import claude_cli, grok_backend
from bankai.agent.tools import TOOLS
from bankai.ingest import upsert_account


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
    assert "--allowedTools" in cmd and "mcp__bankai__*,WebSearch,WebFetch" in cmd
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
