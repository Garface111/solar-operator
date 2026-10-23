"""Kimi (Moonshot) backend — OpenAI-compatible chat completions with function
calling. Serves **Kimi K3**, the strongest open-weights model, as the fallback
brain behind claude-cli: when the Claude subscription runs out (usage limit,
rate limit, logged out) the CLI backend raises and the chain degrades here
instead of to a dead chat (Ford's ask 2026-08-11).

Bills Ford's Moonshot platform account (metered, prepaid balance) via
KIMI_API_KEY / MOONSHOT_API_KEY. Endpoint + model are configurable so the same
backend can point at the Kimi Coding subscription endpoint or OpenRouter later
without a code change.
"""
from __future__ import annotations

import json

import httpx
from sqlalchemy.orm import Session

from ... import config
from ..tools import TOOLS, execute_tool
from . import MAX_TOOL_ROUNDS


def _api_url() -> str:
    return config.KIMI_BASE_URL.rstrip("/") + "/chat/completions"


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


def _resolve_model(model: str | None) -> str:
    # On a fallback turn the router hands us a Claude model name (e.g.
    # "claude-fable-5"), which Moonshot would reject. Only honor an explicit
    # Kimi/Moonshot model; otherwise use the configured default (kimi-k3).
    if model and (model.startswith("kimi") or model.startswith("moonshot")):
        return model
    return config.KIMI_MODEL


def _post(payload: dict) -> dict:
    if not config.KIMI_API_KEY:
        raise RuntimeError(
            "LLM_BACKEND=kimi but no Moonshot credentials: set KIMI_API_KEY "
            "(or MOONSHOT_API_KEY) to a platform.kimi.com key"
        )
    resp = httpx.post(
        _api_url(),
        headers={"Authorization": f"Bearer {config.KIMI_API_KEY}"},
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()


def run(
    session: Session, system: str, messages: list[dict],
    *, model: str | None = None, effort: str | None = None,
) -> str:
    if not config.KIMI_API_KEY:
        raise RuntimeError(
            "LLM_BACKEND=kimi but no Moonshot credentials: set KIMI_API_KEY "
            "(or MOONSHOT_API_KEY) to a platform.kimi.com key"
        )
    msgs: list[dict] = [{"role": "system", "content": system}] + messages
    use_model = _resolve_model(model)
    for _ in range(MAX_TOOL_ROUNDS):
        data = _post(
            {
                "model": use_model,
                "messages": msgs,
                "tools": tools_openai_format(),
            }
        )
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return (message.get("content") or "").strip() or "(no response)"
        # Carry the assistant's tool-call message back verbatim (content may be
        # null; Moonshot may also attach reasoning_content — pass it through).
        msgs.append(message)
        for call in tool_calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(session, call["function"]["name"], args)
            msgs.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
    return "I hit my tool-call limit for one question — try asking something narrower."
