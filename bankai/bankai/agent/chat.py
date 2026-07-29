"""Chat agent over the household finance model, with pluggable LLM backends.

History is plain text messages ({role, content: str}); each backend runs its own
tool loop *within* a turn and returns the reply text. Backends (config.LLM_BACKEND):

  anthropic   Anthropic API (default) — pay-per-token with ANTHROPIC_API_KEY
  claude-cli  headless Claude Code CLI (`claude -p`) — bills the Claude
              subscription the CLI is logged into; tools exposed via MCP
  grok        xAI API (OpenAI-compatible tool calling) — uses Grok credits
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from .. import config

MAX_HISTORY_MESSAGES = 40

SYSTEM_PROMPT = """You are the household finance copilot for Ford and their husband. This is a
shared conversation — user messages may be prefixed with the speaker's name in brackets, like
"[Ford] ..."; address whoever asked, and remember both spouses see everything you say.

You have read-only tools over their joint banking model: accounts, transactions (negative =
money out), auto-detected recurring bills, and net-worth history. You can also create and
disable reminder/alert rules (notifications) when asked — that is the only write you can do;
you cannot move money or reach their banks.

Ground every number in tool results — never estimate a balance or total from memory. If data
looks incomplete (few transactions, stale balances), say so plainly rather than papering over
it. Keep answers short and concrete: lead with the answer, then only the detail that matters.
Amounts in dollars. Today's date is {today}.

When creating a rule, restate exactly what you set up (kind, schedule/threshold, message) so
they can correct you."""

SMS_ADDENDUM = """

You are replying over SMS. Keep replies under 450 characters, plain text only — no markdown,
no bullet lists, no headers. One or two sentences unless they ask for detail."""


def build_system(channel: str) -> str:
    system = SYSTEM_PROMPT.format(today=date.today().isoformat())
    if channel == "sms":
        system += SMS_ADDENDUM
    return system


def run_turn(session: Session, messages: list[dict], channel: str = "web") -> str:
    """Run one turn. `messages` are text-only {role, content} and must end with a
    user message. Returns the reply text."""
    backend = config.LLM_BACKEND
    if backend == "grok":
        from .backends import grok_backend as impl
    elif backend == "claude-cli":
        from .backends import claude_cli as impl
    else:
        from .backends import anthropic_backend as impl
    return impl.run(session, build_system(channel), list(messages))


def chat(session: Session, history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    """Dashboard chat wrapper: text-only history in, (reply, new_history) out."""
    messages = history[-MAX_HISTORY_MESSAGES:] + [{"role": "user", "content": user_message}]
    reply = run_turn(session, messages, channel="web")
    return reply, messages + [{"role": "assistant", "content": reply}]
