"""The subscription backend must never become a way to drain Ford's own seat.

These cover the two things that matter: the JSON tool contract survives the
shapes a model actually emits, and every guard refuses INTO the chain (raises)
rather than letting the product hammer a personal Claude subscription.
"""
import json

import pytest

from api import claude_cli


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    claude_cli._day = ""
    claude_cli._calls_today = 0
    claude_cli._cold_until = 0.0
    monkeypatch.setenv("EA_CLAUDE_CLI", "1")
    yield
    claude_cli._cold_until = 0.0


# ── the output contract ─────────────────────────────────────────────────────
def test_bare_json_becomes_content():
    m = claude_cli._to_message('{"content": "Nine arrays are reporting.", "tool_calls": []}')
    assert m["content"] == "Nine arrays are reporting."
    assert "tool_calls" not in m


def test_fenced_json_is_unwrapped():
    m = claude_cli._to_message('```json\n{"content": "hi", "tool_calls": []}\n```')
    assert m["content"] == "hi"


def test_json_embedded_in_prose_is_recovered():
    reply = 'Sure, here you go:\n{"content": "ok", "tool_calls": []}\nHope that helps.'
    assert claude_cli._to_message(reply)["content"] == "ok"


def test_tool_calls_map_to_openai_shape():
    m = claude_cli._to_message(json.dumps({
        "content": "",
        "tool_calls": [{"name": "fleet_status", "arguments": {"tenant": "t1"}}],
    }))
    (call,) = m["tool_calls"]
    assert call["type"] == "function"
    assert call["function"]["name"] == "fleet_status"
    # arguments must be a JSON *string*, like the metered providers return
    assert json.loads(call["function"]["arguments"]) == {"tenant": "t1"}
    assert call["id"].startswith("cli_")


def test_non_json_reply_degrades_to_prose_not_an_error():
    """A chatty answer is a worse answer -- never a broken turn."""
    m = claude_cli._to_message("I could not format that as JSON, sorry.")
    assert m["content"].startswith("I could not format")
    assert "tool_calls" not in m


def test_nameless_tool_call_is_dropped():
    m = claude_cli._to_message(json.dumps({"content": "x", "tool_calls": [{"arguments": {}}]}))
    assert "tool_calls" not in m


# ── rendering ───────────────────────────────────────────────────────────────
def test_render_splits_system_from_transcript():
    system, transcript = claude_cli._render([
        {"role": "system", "content": "You are Energy Agent."},
        {"role": "user", "content": "how is the fleet"},
        {"role": "tool", "name": "fleet_status", "content": "9 ok"},
    ])
    assert "You are Energy Agent." in system
    assert "[OWNER]" in transcript and "how is the fleet" in transcript
    assert "[TOOL RESULT fleet_status]" in transcript
    assert "You are Energy Agent." not in transcript


def test_render_keeps_text_of_multimodal_content():
    _system, transcript = claude_cli._render([
        {"role": "user", "content": [{"type": "text", "text": "look at this"},
                                     {"type": "image_url", "image_url": {"url": "x"}}]},
    ])
    assert "look at this" in transcript


def test_tool_catalog_lists_every_tool_by_name():
    cat = claude_cli._tool_catalog([
        {"function": {"name": "a", "description": "does a", "parameters": {"type": "object"}}},
        {"function": {"name": "b", "description": "does b", "parameters": {"type": "object"}}},
    ])
    assert "- a:" in cat and "- b:" in cat


# ── the guards ──────────────────────────────────────────────────────────────
def test_disabled_backend_refuses(monkeypatch):
    monkeypatch.setenv("EA_CLAUDE_CLI", "0")
    assert claude_cli.enabled() is False
    with pytest.raises(RuntimeError, match="not enabled"):
        claude_cli.call([], [])


def test_daily_ceiling_refuses_into_the_chain(monkeypatch):
    monkeypatch.setenv("EA_CLAUDE_CLI_DAILY_MAX", "2")
    claude_cli._reserve_slot()
    claude_cli._reserve_slot()
    with pytest.raises(RuntimeError, match="daily ceiling"):
        claude_cli._reserve_slot()


def test_usage_limit_trips_the_breaker_and_goes_cold(monkeypatch):
    monkeypatch.setenv("EA_CLAUDE_CLI_COOLDOWN_S", "900")
    monkeypatch.setattr(claude_cli, "send_internal_alert", lambda *a, **k: None, raising=False)
    claude_cli._trip_breaker("Claude usage limit reached")
    with pytest.raises(RuntimeError, match="cold for"):
        claude_cli._reserve_slot()
    assert claude_cli.status()["cold_for_s"] > 0


@pytest.mark.parametrize("msg", [
    "Claude usage limit reached",
    "rate limit exceeded",
    "HTTP 429 too many requests",
    "out of credits",
    "session limit",
])
def test_every_limit_phrasing_is_recognised(msg):
    assert claude_cli._USAGE_ERROR.search(msg)


def test_a_normal_failure_does_not_trip_the_breaker():
    assert not claude_cli._USAGE_ERROR.search("connection reset by peer")


def test_cli_never_gets_tools_or_the_source_tree(monkeypatch):
    """The harness runs blind: no tools, one turn, and not in /app."""
    seen = {}

    class P:
        returncode = 0
        stdout = json.dumps({"result": '{"content":"ok","tool_calls":[]}'})
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        seen["stdin"] = kw.get("stdin")
        return P()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = claude_cli.call([{"role": "user", "content": "hi"}], [])
    assert out["provider"] == "claude-cli"
    assert out["message"]["content"] == "ok"
    assert "--disallowedTools" in seen["cmd"]
    # `--tools ""` empties the built-in set. Denylisting alone left the tools
    # visible and the model reached for them -- a real turn died on
    # error_max_turns / stop_reason=tool_use before this was added.
    assert seen["cmd"][seen["cmd"].index("--tools") + 1] == ""
    assert seen["cmd"][seen["cmd"].index("--max-turns") + 1] == "1"
    assert seen["cwd"] == "/tmp"
    assert seen["stdin"] is not None  # argv prompt must not leave stdin open


def test_long_prompt_travels_on_stdin_not_argv(monkeypatch):
    """MAX_ARG_STRLEN is 128 KiB; a big fleet context must not E2BIG."""
    seen = {}

    class P:
        returncode = 0
        stdout = json.dumps({"result": '{"content":"ok","tool_calls":[]}'})
        stderr = ""

    def fake_run(cmd, **kw):
        seen["input"] = kw.get("input")
        seen["cmd"] = cmd
        return P()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.call([{"role": "user", "content": "x" * 200_000}], [])
    assert seen["input"] is not None          # went down the pipe
    assert len(seen["cmd"]) < 20              # and not along the command line


def test_cli_error_raises_so_the_chain_falls_through(monkeypatch):
    class P:
        returncode = 1
        stdout = ""
        stderr = "something broke"

    monkeypatch.setattr(claude_cli.subprocess, "run", lambda *a, **k: P())
    with pytest.raises(RuntimeError, match="claude-cli failed"):
        claude_cli.call([{"role": "user", "content": "hi"}], [])


# ── credential isolation ────────────────────────────────────────────────────
# Production 2026-09-18: the CLI prefers ANTHROPIC_API_KEY over the OAuth token,
# so an inherited env made every "subscription" call bill the metered account --
# which was out of credits. The CLI answered "Credit balance is too low" and the
# breaker tripped on a subscription that was perfectly healthy.
def test_metered_anthropic_credentials_never_reach_the_cli(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-dead")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "nope")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    env = claude_cli._child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"


def test_child_env_puts_local_bin_on_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    assert ".local/bin" in claude_cli._child_env()["PATH"]


def test_the_call_actually_passes_the_scrubbed_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-dead")
    seen = {}

    class P:
        returncode = 0
        stdout = json.dumps({"result": '{"content":"ok","tool_calls":[]}'})
        stderr = ""

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return P()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.call([{"role": "user", "content": "hi"}], [])
    assert seen["env"] is not None
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_bin_falls_back_to_local_bin_when_not_on_path(monkeypatch, tmp_path):
    monkeypatch.delenv("EA_CLAUDE_CLI_BIN", raising=False)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _n: None)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "claude").write_text("stub")
    monkeypatch.setattr(claude_cli.os.path, "expanduser", lambda _p: str(home))
    assert claude_cli._bin().endswith(".local/bin/claude")


def test_explicit_bin_env_wins(monkeypatch):
    monkeypatch.setenv("EA_CLAUDE_CLI_BIN", "/opt/claude")
    assert claude_cli._bin() == "/opt/claude"
