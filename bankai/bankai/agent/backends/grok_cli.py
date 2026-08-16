"""Grok CLI backend — runs the copilot through headless `grok -p`, xAI's
CLI (Grok Build), as a fallback brain behind claude-cli.

Grok's CLI is a near drop-in for Claude's: `-p` single-turn, `--output-format
json`, `--system-prompt-override`, `--model`, `--max-turns`, `--allowedTools`.
The one structural difference is MCP — Grok reads MCP servers from its own
persistent config (`grok mcp add bankai …`) rather than an inline flag, so the
bankai finance tools must be registered once on the box (scripts/register-grok-
mcp.sh); this backend just references them by name in --allowedTools.

Auth: the Grok CLI must be signed in (`grok login --device-code`) or carry an
XAI_API_KEY. When it is not, this backend raises with that exact instruction, so
the fallback chain degrades cleanly to the next brain instead of a dead turn —
the reason a fallback exists at all.
"""
from __future__ import annotations

import json
import subprocess

from sqlalchemy.orm import Session

from ... import config

TIMEOUT_SECONDS = config.GROK_CLI_TIMEOUT_SECONDS

_BASE_TOOLS = "mcp__bankai__*,Read"


def _resolve_model(model: str | None) -> str:
    """On a fallback turn the router hands a Claude model name (e.g.
    'claude-fable-5'), which Grok would reject. Honor only an explicit grok
    model; otherwise use the configured Grok default."""
    if model and model.lower().startswith("grok"):
        return model
    return config.GROK_CLI_MODEL or config.GROK_MODEL or "grok-4"


def _transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        who = "User" if m["role"] == "user" else "Copilot"
        lines.append(f"{who}: {m['content']}")
    return "\n\n".join(lines)


def _parse(stdout: str) -> str:
    """Best-effort reply text from Grok's --output-format json. The exact key is
    version-dependent, so try the known shapes, then fall back to raw text."""
    stdout = (stdout or "").strip()
    if not stdout:
        return ""
    try:
        data = json.loads(stdout)
    except ValueError:
        # NDJSON or trailing noise — try the last JSON-looking line.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
        else:
            return stdout  # not JSON at all; hand back what we got
    if isinstance(data, dict):
        for key in ("result", "response", "text", "output_text", "message"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):  # Anthropic-style content blocks
            parts = [b.get("text", "") for b in content if isinstance(b, dict)]
            joined = "".join(parts).strip()
            if joined:
                return joined
    return stdout


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
        config.GROK_CLI_BIN,
        "-p", prompt,
        "--output-format", "json",
        # Grok replaces its system prompt with ours (we build a complete one).
        "--system-prompt-override", system,
        "--allowedTools", _BASE_TOOLS,
        # Headless can't prompt for tool approval; auto-run the sandboxed bankai
        # tools. Web search stays OFF unless Ford has enabled web access, for the
        # same exfiltration reason the claude backend gates WebSearch/WebFetch.
        "--always-approve",
        "--max-turns", "40",
        "--model", _resolve_model(model),
    ]
    if not config.WEB_ACCESS:
        cmd.append("--disable-web-search")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=config.BASE_DIR
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"LLM_BACKEND includes grok-cli but '{config.GROK_CLI_BIN}' was not "
            "found. Install the Grok CLI or set GROK_CLI_BIN."
        )
    detail = (proc.stderr or "") + (proc.stdout or "")
    low = detail.lower()
    if proc.returncode != 0 or '"type":"error"' in low or "not signed in" in low:
        if "not signed in" in low or "login" in low or "authenticate" in low:
            raise RuntimeError(
                "the Grok CLI on this machine is not signed in — run "
                "`grok login --device-code` on the box (or set XAI_API_KEY) to "
                "activate it as a fallback brain"
            )
        raise RuntimeError(f"grok CLI failed (exit {proc.returncode}): {detail[:500]}")
    reply = _parse(proc.stdout)
    if reply:
        return reply
    return (
        "I ran out of steps before I could write my answer. Anything I changed "
        "along the way is saved — ask me what I just did and I'll report it."
    )
