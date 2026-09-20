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
import logging
import re
import subprocess
import sys

from sqlalchemy.orm import Session

from ... import config

log = logging.getLogger("bankai.claude_cli")

# 120 was tuned for fallback duty behind grok — short enough that a hung CLI
# doesn't brick the chain. As the PRIMARY brain a real tool-using turn (MCP
# server startup + several tool calls + a verify pass) legitimately runs past
# it, and timing out just burns the whole turn. Configurable for both roles.
TIMEOUT_SECONDS = config.CLAUDE_CLI_TIMEOUT_SECONDS

# A credit/usage/capacity failure for ONE model — the cue to try another Claude
# model rather than abandon Claude. Distinct from "not logged in" (whole CLI
# dead) or a bad model id, which no other model would fix.
_USAGE_ERROR = re.compile(
    r"usage limit|rate.?limit|out of credit|credits?|quota|exceeded|"
    r"too many requests|\b429\b|limit reached|upgrade your plan|overloaded|"
    r"at capacity|insufficient",
    re.IGNORECASE,
)


def _is_usage_error(detail: str) -> bool:
    return bool(_USAGE_ERROR.search(detail or ""))


def _model_chain(routed: str | None) -> list[str | None]:
    """The routed model first, then the configured Claude fallbacks — so a
    Fable-out-of-credits turn drops to Opus, then Sonnet, before leaving Claude."""
    primary = routed or config.CLAUDE_CLI_MODEL or None
    chain: list[str | None] = [primary]
    for m in (x.strip() for x in config.CLAUDE_MODEL_FALLBACKS.split(",")):
        if m and m != primary:
            chain.append(m)
    return chain

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
    base_cmd = [
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
    use_effort = effort or config.CLAUDE_CLI_EFFORT
    _log_web_access()

    # Try the routed model, then the Claude fallbacks — but ONLY step to the next
    # model on a credit/usage error. Any other failure means trying Opus would
    # fail the same way, so it propagates and the outer chain (grok/kimi) takes it.
    chain = _model_chain(model)
    usage_errors: list[str] = []
    for i, use_model in enumerate(chain):
        cmd = list(base_cmd)
        if use_model:
            cmd += ["--model", use_model]
        if use_effort:
            cmd += ["--effort", use_effort]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=TIMEOUT_SECONDS, cwd=config.BASE_DIR,
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
            # Out of credits on THIS model? Fall to the next Claude model; when
            # even the last one is credit-limited, break to the outer chain.
            if _is_usage_error(detail):
                usage_errors.append(f"{use_model or 'default'}: {detail[:160]}")
                if i < len(chain) - 1:
                    log.warning(
                        "claude model %r is out of credits/usage — falling to %r",
                        use_model or "(default)", chain[i + 1] or "(default)",
                    )
                    continue
                break  # every Claude model exhausted
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail[:500]}")
        data = json.loads(proc.stdout)
        reply = (data.get("result") or "").strip()
        if reply:
            if usage_errors:
                log.info("claude answered on fallback model %r after %d credit skip(s)",
                         use_model or "(default)", len(usage_errors))
            return reply
        # An empty result does NOT mean nothing happened — tool calls in this turn
        # may well have changed the data. Saying so is the difference between the
        # household re-doing work and simply asking what was done.
        return (
            "I ran out of steps before I could write my answer. Anything I changed "
            "along the way is saved — ask me what I just did and I'll report it."
        )
    # Every Claude model is credit-limited — let the outer chain reach for grok/kimi.
    raise RuntimeError(
        "all Claude models are out of credits/usage: " + " | ".join(usage_errors)
    )
