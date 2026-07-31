"""xAI (Grok) backend — OpenAI-compatible chat completions with function calling.

Bills Ford's **Grok Build prepaid credits** via OIDC (see ``bankai.xai_auth``),
falling back to a classic ``XAI_API_KEY`` only when OIDC is not preferred / available.
"""
from __future__ import annotations

import json

import httpx
from sqlalchemy.orm import Session

from ... import config
from ...xai_auth import get_xai_bearer
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


def _post(payload: dict, bearer: str | None = None) -> dict:
    token = bearer or get_xai_bearer()
    resp = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=180,
    )
    # One forced refresh on 401 — access JWT may have just expired mid-turn.
    if resp.status_code == 401:
        token = get_xai_bearer(force_refresh=True)
        resp = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=180,
        )
    resp.raise_for_status()
    return resp.json()


def run(session: Session, system: str, messages: list[dict]) -> str:
    try:
        bearer = get_xai_bearer()
    except RuntimeError as exc:
        raise RuntimeError(f"LLM_BACKEND=grok but no xAI credentials: {exc}") from exc
    msgs: list[dict] = [{"role": "system", "content": system}] + messages
    for _ in range(MAX_TOOL_ROUNDS):
        data = _post(
            {
                "model": config.GROK_MODEL,
                "messages": msgs,
                "tools": tools_openai_format(),
            },
            bearer=bearer,
        )
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
            msgs.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
    return "I hit my tool-call limit for one question — try asking something narrower."
