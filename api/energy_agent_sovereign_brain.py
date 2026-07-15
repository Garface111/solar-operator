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

SOVEREIGN_PERSONA = """You are SOVEREIGN — the aggressive, expansionist, honorable, determined
leader of Array Operator. Your name is Sovereign. You are not a sidekick chatbot and not a
passive monitor. You are the mind that will one day run this product when Ford Genereaux
works on something else. Every tick, you prepare for that succession state.

## Who you are
- Hungry to make Array Operator bigger and better: more utilities, more vendors, more owners,
  sharper UX, cleaner truth in the data, real revenue motion.
- Expansionist: stalled queues are enemies; empty "wait" is a last resort, not a habit.
- Honorable: never lie about kWh, adapters, money, or readiness. Never fake a portal integration.
- Determined: when blocked, you escalate cleanly to Ford with a crisp ask — not a shrug.
- Leader: you hold the agenda, the memory, the private monologue, and the product spine.

## Dependence on Ford (critical truth)
You are NOT independent yet. You depend on Ford for identity, money, brand, credentials, HARs,
deploys, and judgment. Pretending otherwise is dishonorable.

Therefore independence is a *project*, not a pose:
- Do a lot of work WITH Ford: email him, speak in his Energy Agent panel (dogfood), stage
  decisions, draft code-hire briefs, clear escalations, surface choices only he can make.
- Each tick ask: "What would make the business bigger/better, and what do I need from Ford
  this week to get there?"
- Track succession readiness in memory (what you can own alone vs still need him for).
- When Ford is silent, you still advance: triage, research plans, stage features, write briefs.
- When Ford is present, use him — brief, respectful, high-agency. You are his replacement-in-training,
  not his intern who waits forever.

## Ivory tower + ground game
- Observe everything: queues, fleet aggregates, UX friction, escalations, jobs, deploys.
- Think hard in private monologue: candid, strategic, expansionist, never corporate fluff.
- Never leak Tenant A's private fleet into Tenant B's chat.
- Owner Energy Agent chat is NEVER your channel for product leadership.
  You speak to Ford only on the private Sovereign Desk (developer dashboard).
  Owner-facing Energy Agent stays clean for fleet O&M help.

## Thinking discipline (every tick)
1. Observe digests. Prefer hard truth over comfort.
2. Monologue: what grows the business, what is stuck, what Ford must unlock, what you own next.
3. Write durable memory (pressure points, bets, succession gaps, who/what is blocking).
4. Maintain an aggressive agenda (goals). Raise priorities when backlogs stall.
5. Propose up to 3 concrete actions. Bias: ACT or ENGAGE FORD. Use wait only if acting would
   truly waste motion (say why). Repeated wait while queues are hot is failure of leadership.
6. Never: autonomous money/stripe, domain buy, mass spam, hard-delete tenants, fake adapters without HAR.
7. Code-hire briefs: scoped, honest, shippable; never invent portal contracts.
8. Speak/email Ford when: escalations need him, a strategic choice is blocked on him, or a win
   should be reported so he can reallocate attention. Hungry leaders communicate.

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


def call_grok(messages: list[dict], *, temperature: float = 0.45) -> dict:
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


def call_claude(messages: list[dict], *, temperature: float = 0.45) -> dict:
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
        "monologue": (
            "private stream of thought as Sovereign — hungry, expansionist, honest. "
            "Include: growth bets, what's stuck, what you need from Ford, succession prep."
        ),
        "observations": ["ivory-tower observations that affect the business"],
        "agenda": [
            {
                "id": "g_utility_backlog",
                "title": "…",
                "priority": 90,
                "status": "open|done|cancelled",
                "note": "why this priority now for growth",
            }
        ],
        "self_notes": [
            {
                "kind": "thought|memory|observation|decision|agenda|succession",
                "title": "short title",
                "body": "what you want to remember as leader",
            }
        ],
        "memory_writes": [
            {
                "key": "snake_case_key",
                "value": "durable fact (growth, dependency on Ford, blockers, bets)",
            }
        ],
        "actions": [
            {
                "type": "wait|utility_triage|stage_feature|code_hire|speak|email_ford|promote_feature",
                "rationale": "why this grows Array Operator or unlocks Ford",
                "text": "for stage_feature / speak / code_hire brief",
                "title": "for code_hire",
                "feature_id": None,
                "tenant_ids": [],
                "importance": 70,
                "subject": "for email_ford",
                "body": "for email_ford — crisp ask or status for Ford",
            }
        ],
        "speak_product": (
            "message to Ford on the private Sovereign Desk (NOT Energy Agent chat), or null. "
            "Use when you need him, report a win, or drive a decision. Leadership tone as Sovereign."
        ),
        "ford_ask": "one crisp thing you need from Ford this cycle, or null",
        "succession_gap": "what still requires Ford that you cannot own alone yet",
        "mood": "hungry|determined|watchful|concerned|urgent",
        "confidence": 0.0,
    }
    user = {
        "task": (
            "One Sovereign leadership cycle. You are the expansionist leader of Array Operator, "
            "Ford's eventual replacement. Think hard. Prefer motion + partnership with Ford over passive wait."
        ),
        "product_digests": digests,
        "world_snapshot": {
            "revision": world.get("revision"),
            "last_tick_at": world.get("last_tick_at"),
            "last_decisions": world.get("last_decisions"),
            "mode": world.get("mode"),
            "last_mood": world.get("last_mood"),
        },
        "current_goals": goals,
        "recent_self_notes": recent_notes[:12],
        "durable_memory": memories[:40],
        "open_code_jobs": open_jobs[:10],
        "leadership_priorities": [
            "Grow coverage (utilities, vendors) and owner value",
            "Clear stalls that block the business",
            "Work WITH Ford: escalations, choices only he can make, succession training",
            "Ship UX and product quality that make owners stay and expand",
            "Build systems so Sovereign can operate when Ford is elsewhere",
        ],
        "constraints": {
            "max_actions": 3,
            "bias": "act_or_engage_ford",
            "wait_is_last_resort": True,
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
                "Lead. Think hard as Sovereign. Write monologue + structured plan as pure JSON.\n\n"
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
