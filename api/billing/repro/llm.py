"""
Thin Anthropic client for the repro wrapper.

Matches the codebase convention (raw httpx to /v1/messages with x-api-key from
ANTHROPIC_API_KEY — see api/ingest.py._call_anthropic) rather than adding the
anthropic SDK dependency. Defaults to claude-opus-4-8 (override REPRO_LLM_MODEL).
Vision-capable: pass images as (media_type, bytes) and they're base64-inlined.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

REPRO_LLM_MODEL = os.getenv("REPRO_LLM_MODEL", "claude-opus-4-8")
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class LLMUnavailable(RuntimeError):
    """No ANTHROPIC_API_KEY configured — AI steps are skipped, not fatal."""


def llm_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _content_block(text: Optional[str], images: Optional[list[tuple[str, bytes]]]):
    blocks: list[dict] = []
    for media_type, data in (images or []):
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type,
                       "data": base64.standard_b64encode(data).decode("ascii")},
        })
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks


def call_json(*, system: str, user_text: str,
              images: Optional[list[tuple[str, bytes]]] = None,
              schema: Optional[dict] = None, max_tokens: int = 4096,
              model: Optional[str] = None) -> dict:
    """One structured-JSON turn. Uses output_config.format when a schema is given
    so the reply is guaranteed-parseable. Raises LLMUnavailable with no key."""
    import httpx

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY not set")

    body: dict = {
        "model": model or REPRO_LLM_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": _content_block(user_text, images)}],
    }
    if schema is not None:
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    r = httpx.post(_ANTHROPIC_URL, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, json=body, timeout=120)
    r.raise_for_status()
    data = r.json()
    text = next((b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Salvage a JSON object from a chatty reply (no schema case).
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            return json.loads(text[a:b + 1])
        raise
