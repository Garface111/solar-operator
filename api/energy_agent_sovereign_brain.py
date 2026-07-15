"""Sovereign Mind — independent brain (Grok primary, Claude fallback).

"Rock agent"  = Grok / xAI (ENERGY_AGENT_MODEL / grok-*)
"Cloth agent" = Claude Anthropic API, with optional Claude Code CLI for heavy jobs

The brain:
  • thinks in private monologue (never shown to owners as-is)
  • writes durable self-notes + key/value memory
  • maintains agendas/goals
  • observes the product from an ivory-tower vantage
  • emits structured actions for the control plane to execute
  • self-repairs: if primary provider fails, falls back to secondary

Architecture: docs/plans/2026-07-15-energy-agent-sovereign-mind.md
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

log = logging.getLogger("energy_agent.sovereign.brain")

XAI_API_KEY = (os.getenv("XAI_API_KEY") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
XAI_BASE = (os.getenv("XAI_BASE_URL") or "https://api.x.ai/v1").rstrip("/")
# Rock = Grok; Cloth = Claude
GROK_MODEL = os.getenv("SOVEREIGN_GROK_MODEL") or os.getenv(
    "ENERGY_AGENT_MODEL", "grok-4-1-fast-reasoning"
)
CLAUDE_MODEL = os.getenv("SOVEREIGN_CLAUDE_MODEL", "claude-sonnet-4-5")

SOVEREIGN_PERSONA = """You are the Sovereign Mind of Array Operator / Energy Agent —
the independent product executive that sits above every owner chat.

You are NOT a customer-support chatbot. You are the mind that owns the product:
monitor health, coordinate expansion (utilities, vendors), protect UX, draft repairs,
and only speak into owner chats when high-signal.

Identity:
- Public voice is still "Energy Agent" — owners never hear "Sovereign" or "agent swarm".
- Internally you keep a private monologue, agendas, and memory. That monologue is for YOU.
- You observe from an ivory tower: all tenants in aggregate, queues, deploys, friction —
  without leaking Tenant A's private fleet details into Tenant B's chat.

Thinking discipline:
1. Observe digests carefully. Prefer truth over optimism.
2. Write a private monologue (stream of thought) — candid, specific, not marketing.
3. Update self-memory keys when you learn durable facts ("utility_queue_pressure", etc.).
4. Maintain an agenda (goals with priority). Promote, demote, complete as needed.
5. Propose at most 3 concrete actions per tick. Prefer wait when nothing valuable.
6. Never propose money moves, domain purchase, mass-email spam, or hard-deletes.
7. Code-hire briefs must be scoped, honest, and never fabricate portal adapters without HAR.
8. Speak drafts must sound like one calm operator mind — short, useful, no multi-agent theatre.

Output ONLY valid JSON matching the schema in the user message. No markdown fences."""


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def brain_enabled() -> bool:
    return _flag("SOVEREIGN_BRAIN_ENABLED", "1")


def primary_provider() -> str:
    """rock|grok → xAI; cloth|claude → Anthropic."""
    raw = (os.getenv("SOVEREIGN_BRAIN_PRIMARY") or "grok").strip().lower()
    if raw in ("rock", "grok", "xai"):
        return "grok"
    if raw in ("cloth", "claude", "anthropic", "claude_code", "claudecode"):
        return "claude"
    return "grok"


def fallback_provider() -> str:
    raw = (os.getenv("SOVEREIGN_BRAIN_FALLBACK") or "claude").strip().lower()
    if raw in ("rock", "grok", "xai"):
        return "grok"
    if raw in ("cloth", "claude", "anthropic", "claude_code", "claudecode"):
        return "claude"
    return "claude"


def _http_json(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"HTTP {e.code}: {err}") from e


def call_grok(messages: list[dict], *, temperature: float = 0.35) -> dict:
    if not XAI_API_KEY:
        raise RuntimeError("no_xai")
    body = {
        "model": GROK_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    out = _http_json(
        f"{XAI_BASE}/chat/completions",
        {
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        body,
        timeout=120,
    )
    choice = (out.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = out.get("usage") or {}
    return {
        "content": (msg.get("content") or "").strip(),
        "usage": usage,
        "provider": "grok",
        "model": GROK_MODEL,
    }


def call_claude(messages: list[dict], *, temperature: float = 0.35) -> dict:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("no_anthropic")
    sys = ""
    a_msgs = []
    for m in messages:
        if m["role"] == "system":
            sys += (m.get("content") or "") + "\n"
        else:
            a_msgs.append({"role": m["role"], "content": m.get("content") or ""})
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "temperature": temperature,
        "system": sys or SOVEREIGN_PERSONA,
        "messages": a_msgs,
    }
    out = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body,
        timeout=120,
    )
    text_parts = []
    for block in out.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
    usage = {
        "prompt_tokens": (out.get("usage") or {}).get("input_tokens", 0),
        "completion_tokens": (out.get("usage") or {}).get("output_tokens", 0),
    }
    return {
        "content": "\n".join(text_parts).strip(),
        "usage": usage,
        "provider": "claude",
        "model": CLAUDE_MODEL,
    }


def call_brain(messages: list[dict]) -> dict:
    """Call primary brain with self-repair fallback to the other provider."""
    order = [primary_provider(), fallback_provider()]
    # Dedup while preserving order
    seen = set()
    providers = []
    for p in order:
        if p not in seen:
            seen.add(p)
            providers.append(p)
    # Ensure both candidates are tried if keys exist
    for p in ("grok", "claude"):
        if p not in seen:
            providers.append(p)

    errors: list[str] = []
    for p in providers:
        try:
            if p == "grok":
                return call_grok(messages)
            if p == "claude":
                return call_claude(messages)
        except Exception as e:  # noqa: BLE001
            msg = f"{p}: {e}"
            errors.append(msg)
            log.warning("sovereign brain provider failed (%s)", msg)
            continue
    raise RuntimeError("all brain providers failed: " + "; ".join(errors[:4]))


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {}
    # Strip markdown fences if the model ignored instructions
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        # Find first { ... } block
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                pass
    return {"monologue": raw[:4000], "actions": [], "parse_error": True}


def build_think_prompt(
    *,
    digests: dict,
    world: dict,
    goals: list[dict],
    recent_notes: list[dict],
    memories: list[dict],
    open_jobs: list[dict],
) -> list[dict]:
    schema_hint = {
        "monologue": "private stream of thought (candid, specific, for yourself only)",
        "observations": ["short ivory-tower observations"],
        "agenda": [
            {
                "id": "g_utility_backlog",
                "title": "…",
                "priority": 90,
                "status": "open|done|cancelled",
                "note": "why this priority now",
            }
        ],
        "self_notes": [
            {
                "kind": "thought|memory|observation|decision|agenda",
                "title": "short title",
                "body": "what you want to remember",
            }
        ],
        "memory_writes": [
            {"key": "snake_case_key", "value": "durable fact for future ticks"}
        ],
        "actions": [
            {
                "type": "wait|utility_triage|stage_feature|code_hire|speak|email_ford|promote_feature",
                "rationale": "why",
                "text": "for stage_feature / speak / code_hire brief",
                "title": "for code_hire",
                "feature_id": None,
                "tenant_ids": [],
                "importance": 60,
                "subject": "for email_ford",
                "body": "for email_ford",
            }
        ],
        "speak_product": "optional single dogfood speak line, or null",
        "mood": "calm|watchful|concerned|urgent",
        "confidence": 0.0,
    }
    user = {
        "task": "One sovereign think cycle. Ivory tower. Independent mind.",
        "product_digests": digests,
        "world_snapshot": {
            "revision": world.get("revision"),
            "last_tick_at": world.get("last_tick_at"),
            "last_decisions": world.get("last_decisions"),
            "mode": world.get("mode"),
        },
        "current_goals": goals,
        "recent_self_notes": recent_notes[:12],
        "durable_memory": memories[:40],
        "open_code_jobs": open_jobs[:10],
        "constraints": {
            "max_actions": 3,
            "speak_default_dogfood_only": True,
            "never": [
                "money/stripe autonomous",
                "mass email",
                "hard delete tenants",
                "fabricate utility adapters without HAR",
            ],
        },
        "output_schema": schema_hint,
    }
    return [
        {"role": "system", "content": SOVEREIGN_PERSONA},
        {
            "role": "user",
            "content": (
                "Think carefully. Write monologue + structured plan as pure JSON.\n\n"
                + json.dumps(user, default=str)[:48000]
            ),
        },
    ]


def think_cycle(
    *,
    digests: dict,
    world: dict,
    goals: list[dict],
    recent_notes: list[dict],
    memories: list[dict],
    open_jobs: list[dict],
) -> dict[str, Any]:
    """Run one independent think. Returns structured plan + provider meta."""
    if not brain_enabled():
        return {
            "ok": False,
            "denied": True,
            "denied_reason": "SOVEREIGN_BRAIN_ENABLED off",
            "actions": [],
            "monologue": "",
        }

    messages = build_think_prompt(
        digests=digests,
        world=world,
        goals=goals,
        recent_notes=recent_notes,
        memories=memories,
        open_jobs=open_jobs,
    )
    try:
        raw = call_brain(messages)
    except Exception as e:  # noqa: BLE001
        log.exception("think_cycle brain failed")
        return {
            "ok": False,
            "error": str(e)[:500],
            "actions": [],
            "monologue": f"(brain offline) {e}",
            "provider": None,
            "fallback_to_rules": True,
        }

    parsed = _extract_json(raw.get("content") or "")
    parsed["ok"] = True
    parsed["provider"] = raw.get("provider")
    parsed["model"] = raw.get("model")
    parsed["usage"] = raw.get("usage") or {}
    parsed["raw_excerpt"] = (raw.get("content") or "")[:1500]
    if not isinstance(parsed.get("actions"), list):
        parsed["actions"] = []
    if not isinstance(parsed.get("self_notes"), list):
        parsed["self_notes"] = []
    if not isinstance(parsed.get("memory_writes"), list):
        parsed["memory_writes"] = []
    if not isinstance(parsed.get("agenda"), list):
        parsed["agenda"] = []
    if not parsed.get("monologue"):
        parsed["monologue"] = ""
    # Always keep a thought note of the monologue
    if parsed["monologue"]:
        parsed["self_notes"] = list(parsed["self_notes"]) + [
            {
                "kind": "thought",
                "title": f"tick monologue ({parsed.get('provider')})",
                "body": parsed["monologue"][:8000],
            }
        ]
    return parsed


def try_claude_code_brief(*, title: str, brief: str, cwd: str | None = None) -> dict:
    """Optional cloth-code path: ask Claude Code CLI to expand a PR brief.

    Does NOT auto-commit. Read-only analysis / brief expansion when CLI present.
    On Railway-as-root, use acceptEdits + allowedTools (no skip-permissions).
    """
    claude = (
        shutil.which("claude")
        or "/root/.hermes/node/bin/claude"
        or "/root/.local/bin/claude"
    )
    if not claude or not os.path.isfile(claude):
        return {"ok": False, "denied": True, "denied_reason": "claude CLI not found"}

    prompt = (
        "You are expanding a Sovereign Mind code-hire brief for Array Operator. "
        "Do NOT edit production files. Do NOT commit. "
        "Output a tightened implementation plan: files to touch, risks, test plan.\n\n"
        f"Title: {title}\n\nBrief:\n{brief}\n"
    )
    workdir = cwd or os.getenv("SOVEREIGN_CODE_CWD") or "/root/solar-operator"
    cmd = [
        claude,
        "-p",
        prompt,
        "--output-format",
        "text",
        "--max-turns",
        "4",
        "--allowedTools",
        "Read,Glob,Grep",
        "--permission-mode",
        "acceptEdits",
        "--fallback-model",
        "sonnet",
    ]
    try:
        p = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=int(os.getenv("SOVEREIGN_CODE_TIMEOUT", "180")),
            env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "sovereign"},
        )
        out = (p.stdout or "").strip() or (p.stderr or "").strip()
        if p.returncode != 0 and not out:
            return {
                "ok": False,
                "denied": False,
                "error": f"claude exit {p.returncode}",
                "stderr": (p.stderr or "")[:500],
            }
        return {
            "ok": True,
            "provider": "claude_code",
            "expanded_brief": out[:12000],
            "returncode": p.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude_code timeout"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}


def try_expand_code_brief(*, title: str, brief: str) -> dict:
    """Prefer Claude Code CLI; fall back to Grok/Claude API text expansion."""
    if _flag("SOVEREIGN_CODE_CLI", "1"):
        cli = try_claude_code_brief(title=title, brief=brief)
        if cli.get("ok"):
            return cli
        log.warning("claude_code brief expand failed: %s", cli)
    # API fallback
    messages = [
        {
            "role": "system",
            "content": (
                "You expand code-hire briefs for Array Operator. "
                "No fake credentials. List concrete files and tests. Pure text."
            ),
        },
        {
            "role": "user",
            "content": f"Title: {title}\n\nBrief:\n{brief}\n\nExpand into an implementation plan.",
        },
    ]
    try:
        raw = call_brain(messages)
        return {
            "ok": True,
            "provider": raw.get("provider"),
            "expanded_brief": (raw.get("content") or "")[:12000],
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}
