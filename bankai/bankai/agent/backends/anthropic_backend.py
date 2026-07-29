"""Anthropic API backend — manual tool-use loop against the Messages API."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ... import config
from ..tools import TOOLS, execute_tool
from . import MAX_TOOL_ROUNDS


def run(session: Session, system: str, messages: list[dict]) -> str:
    import anthropic

    client = anthropic.Anthropic()
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=8000,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            return "I can't help with that request."
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
        reply = "".join(b.text for b in response.content if b.type == "text").strip()
        return reply or "(no response)"
    return "I hit my tool-call limit for one question — try asking something narrower."
