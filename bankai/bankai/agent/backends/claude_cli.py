"""Claude Code CLI backend — runs the copilot through headless `claude -p`, so
inference bills the Claude subscription the CLI is logged into (claude.ai Pro/Max)
instead of API credits.

The finance tools are exposed to the CLI as an MCP stdio server
(bankai.agent.mcp_server), which the spawned CLI launches itself and which opens its
own connection to the same database. Requires Claude Code installed and logged in on
the machine running BankAI (`claude` on PATH, or CLAUDE_CLI_BIN).
"""
from __future__ import annotations

import json
import subprocess
import sys

from sqlalchemy.orm import Session

from ... import config

# 120 was tuned for fallback duty behind grok — short enough that a hung CLI
# doesn't brick the chain. As the PRIMARY brain a real tool-using turn (MCP
# server startup + several tool calls + a verify pass) legitimately runs past
# it, and timing out just burns the whole turn. Configurable for both roles.
TIMEOUT_SECONDS = config.CLAUDE_CLI_TIMEOUT_SECONDS

#: Local tools every turn gets. Web tools are appended only when granted.
_BASE_TOOLS = "mcp__bankai__*,Read"


def _allowed_tools() -> str:
    if not config.WEB_ACCESS:
        return _BASE_TOOLS
    return _BASE_TOOLS + ",WebSearch,WebFetch"


def _log_web_access() -> None:
    """Record that a turn ran with live web egress.

    Granting the copilot the open web while its context holds the household's
    entire financial picture is a real risk knowingly accepted, not a
    non-event. Writing it down is what keeps the channel auditable rather than
    silent — if data ever does leave, there is a trail showing which turns
    could have sent it.
    """
    if not config.WEB_ACCESS:
        return
    try:
        from ...db import session_scope
        from ...security import sentinel

        with session_scope() as audit:
            sentinel.record_event(
                audit,
                kind="web_access_turn",
                severity="info",
                actor="copilot",
                summary="turn ran with WebSearch/WebFetch granted",
                detail={"allowed_domains": config.WEB_ALLOWED_DOMAINS or "unrestricted"},
            )
    except Exception:
        # A broken alarm must not break the thing it guards.
        import logging

        logging.getLogger("bankai.security").info(
            "web_access_turn (sentinel unavailable): WebSearch/WebFetch granted"
        )


def _mcp_config() -> str:
    return json.dumps(
        {
            "mcpServers": {
                "bankai": {
                    "command": sys.executable,
                    "args": ["-m", "bankai.agent.mcp_server"],
                    "env": {
                        "DATABASE_URL": config.DATABASE_URL,
                        "PYTHONPATH": str(config.BASE_DIR),
                    },
                }
            }
        }
    )


def _transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        who = "User" if m["role"] == "user" else "Copilot"
        lines.append(f"{who}: {m['content']}")
    return "\n\n".join(lines)


def run(
    session: Session, system: str, messages: list[dict],
    *, model: str | None = None, effort: str | None = None,
) -> str:
    prompt = (
        "Conversation so far:\n\n"
        + _transcript(messages)
        + "\n\nReply to the last user message. Output only the reply text."
    )
    cmd = [
        config.CLAUDE_CLI_BIN,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--append-system-prompt",
        system,
        "--mcp-config",
        _mcp_config(),
        "--allowedTools",
        # Read lets it open vault images (pasted screenshots) at the path
        # read_document hands back — an MCP tool can only return text.
        #
        # WebSearch/WebFetch: granted when config.WEB_ACCESS is on (Ford's call,
        # 2026-08-12 — he wants the copilot able to value assets against the
        # real market instead of guessing a number into the balance sheet).
        # The earlier reasoning for withholding them was sound and still is: the
        # prompt carries untrusted third-party text and the headless CLI runs
        # allowed tools with no human gate, so this is a genuine egress channel.
        # It is now an AUDITED one — _log_web_access records every turn that
        # carries it — and WEB_ALLOWED_DOMAINS can narrow it without code.
        _allowed_tools(),
        # 15 was too few for real work: reading two statement PDFs paged at 30k
        # chars each, annotating both, and creating an account exhausted the
        # budget before it could say what it had done — the work landed and the
        # household saw "(no response)", which reads as total failure.
        "--max-turns",
        "40",
    ]
    # Per-turn override from the router wins; the fixed config is the fallback.
    use_model = model or config.CLAUDE_CLI_MODEL
    use_effort = effort or config.CLAUDE_CLI_EFFORT
    if use_model:
        cmd += ["--model", use_model]
    if use_effort:
        cmd += ["--effort", use_effort]
    _log_web_access()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=config.BASE_DIR
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"LLM_BACKEND=claude-cli but '{config.CLAUDE_CLI_BIN}' was not found. "
            "Install Claude Code and log in, or set CLAUDE_CLI_BIN."
        )
    if proc.returncode != 0:
        detail = (proc.stderr or "") + (proc.stdout or "")
        if "not logged in" in detail.lower() or "/login" in detail:
            raise RuntimeError(
                "the Claude CLI on this machine is not logged in — run "
                f"`{config.CLAUDE_CLI_BIN}` once and type /login to connect the "
                "Claude subscription"
            )
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail[:500]}")
    data = json.loads(proc.stdout)
    reply = (data.get("result") or "").strip()
    if reply:
        return reply
    # An empty result does NOT mean nothing happened — tool calls in this turn
    # may well have changed the data. Saying so is the difference between the
    # household re-doing work and simply asking what was done.
    return (
        "I ran out of steps before I could write my answer. Anything I changed "
        "along the way is saved — ask me what I just did and I'll report it."
    )
