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

TIMEOUT_SECONDS = 120


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


def run(session: Session, system: str, messages: list[dict]) -> str:
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
        "mcp__bankai__*,WebSearch,WebFetch,Read",
        # 15 was too few for real work: reading two statement PDFs paged at 30k
        # chars each, annotating both, and creating an account exhausted the
        # budget before it could say what it had done — the work landed and the
        # household saw "(no response)", which reads as total failure.
        "--max-turns",
        "40",
    ]
    if config.CLAUDE_CLI_MODEL:
        cmd += ["--model", config.CLAUDE_CLI_MODEL]
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
