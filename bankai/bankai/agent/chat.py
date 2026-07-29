"""Claude chat agent over the household finance model.

Manual tool-use loop (no beta dependency) against the Messages API. The model only
sees data returned by the read-only tools in agent/tools.py.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from .. import config
from .tools import TOOLS, execute_tool

MAX_TOOL_ROUNDS = 12
MAX_HISTORY_MESSAGES = 40

SYSTEM_PROMPT = """You are the household finance copilot for Ford and their husband. You have
read-only tools over their joint banking model: accounts, transactions (negative = money out),
auto-detected recurring bills, and net-worth history. You can also create and disable
reminder/alert rules (email notifications) when asked — that is the only write you can do;
you cannot move money or reach their banks.

Ground every number in tool results — never estimate a balance or total from memory. If data
looks incomplete (few transactions, stale balances), say so plainly rather than papering over
it. Keep answers short and concrete: lead with the answer, then only the detail that matters.
Amounts in dollars. Today's date is {today}.

When creating a rule, restate exactly what you set up (kind, schedule/threshold, message) so
they can correct you."""


def _client():
    import anthropic

    return anthropic.Anthropic()


def chat(session: Session, history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    """Run one user turn through the tool loop.

    history: prior messages (already in API shape). Returns (reply_text, new_history).
    """
    client = _client()
    messages = history[-MAX_HISTORY_MESSAGES:] + [{"role": "user", "content": user_message}]
    system = SYSTEM_PROMPT.format(today=date.today().isoformat())

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=8000,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            reply = "I can't help with that request."
            messages.append({"role": "assistant", "content": reply})
            return reply, messages
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(session, block.name, block.input)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result}
                    )
            messages.append({"role": "user", "content": tool_results})
            continue
        # end_turn / max_tokens: extract text and finish
        reply = "".join(b.text for b in response.content if b.type == "text").strip()
        messages.append({"role": "assistant", "content": response.content})
        return reply or "(no response)", messages

    reply = "I hit my tool-call limit for one question — try asking something narrower."
    messages.append({"role": "assistant", "content": reply})
    return reply, messages
