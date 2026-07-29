"""xAI (Grok) backend — OpenAI-compatible chat completions with function calling,
so chat usage draws from Grok/xAI credits instead of Anthropic API credits."""
from __future__ import annotations

import json

import httpx
from sqlalchemy.orm import Session

from ... import config
from ..tools import TOOLS, execute_tool
from . import MAX_TOOL_ROUNDS

API_URL = "https://api.x.ai/v1/chat/completions"


def tools_openai_format() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


def _post(payload: dict) -> dict:
    resp = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {config.XAI_API_KEY}"},
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()


def run(session: Session, system: str, messages: list[dict]) -> str:
    if not config.XAI_API_KEY:
        raise RuntimeError("LLM_BACKEND=grok but XAI_API_KEY is not set")
    msgs: list[dict] = [{"role": "system", "content": system}] + messages
    for _ in range(MAX_TOOL_ROUNDS):
        data = _post({"model": config.GROK_MODEL, "messages": msgs, "tools": tools_openai_format()})
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return (message.get("content") or "").strip() or "(no response)"
        msgs.append(message)
        for call in tool_calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(session, call["function"]["name"], args)
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return "I hit my tool-call limit for one question — try asking something narrower."
