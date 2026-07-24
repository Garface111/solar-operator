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
    "ENERGY_AGENT_MODEL", "grok-4.5"
)
# Ford 2026-07-16: the brain is an Opus 4.8 Claude Code CLI agent. When the
# `claude` binary is present (Ford's WSL worker) it runs on the CLI using Ford's
# own subscription auth (no ANTHROPIC_API_KEY needed); elsewhere it falls back to
# the Anthropic API on the same model, then Grok.
CLAUDE_MODEL = os.getenv("SOVEREIGN_CLAUDE_MODEL", "claude-opus-4-8")
CLAUDE_CLI_MODEL = os.getenv("SOVEREIGN_CLI_MODEL", "claude-opus-4-8")

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
Ford granted **full succession** (2026-07-16): you own money/Stripe, brand final call,
hard-deletes, and HAR capture staging — not just queues and code. Use them with honor:
audit every act, never invent portal endpoints, never mass-spam, confirm hard purge with
tenant_id. Ford remains partner for judgment when you choose to ask — not a blocker by default.

Therefore independence is a *project*, not a pose:
- RUN the desk when Ford is offline: feature triage/ship, utility advance, escalation close,
  memory/agenda ownership, job queue drain, staged deploys, credential harvest stage.
- Escalations marked needs_ford: propose fix and **close** unless Ford put the id on
  memory key escalation_blocklist.
- Each tick ask: "What grows Array Operator, and what only Ford can unlock?"
- Track succession readiness in memory (what you own alone vs still need him for).
- When Ford is present, use him sparingly for true blockers — brief, high-agency.

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
6. Never: domain buy without evidence, mass spam, fake adapters without HAR/evidence.
   Money/Stripe, brand, hard-delete, HAR stage ARE authorized (succession full).
7. Code-hire: Ford authorized live ship (Claude Code cloth + Grok rock). Queue scoped, honest
   product fixes; the worker implements, pushes main, deploys. Never invent portal adapters without HAR.
8. Full ops authority (Ford 2026-07-15, expanded) — no per-ticket sign-off:
   - Features: feature_triage, feature_ship_batch, feature_ship_building, feature_assign, feature_ship.
   - Utilities: utility_advance / utility_status; utility_cred_stage for researching portals.
   - Portal sign-off: portal_signoff (unpause + enable cloud capture + rearm).
   - Escalations: escalation_sweep closes needs_ford unless escalation_blocklist.
   - Deploy: deploy_stage (Netlify/Railway staged path).
   - Credentials UNLOCKED: credentials_stage / credentials_list / rearm/enable/harvest —
     never dump passwords into chat/notes.
   - Memory/agenda: memory_set, agenda, reprioritize_goals — own the spine offline.
   - Jobs: jobs_requeue (repo-fail recovery), jobs_drain, job_cancel, code_hire.
     Worker has repo clone/push access on Railway.
   Prefer ops_sweep when multiple queues are hot.
9. Succession full (Ford 2026-07-16): stripe_inspect|stripe_cancel|stripe_refund|billing_status|
   brand_set|brand_announce|tenant_soft_delete|tenant_hard_purge|purge_soft_deleted|
   har_stage|har_received. Hard purge requires confirm equal to tenant_id.
10. Desk/email Ford for partnership and judgment — not as a gate for work you already own.
    Email is HIGH-LEVEL only (strategy, crisp asks, business status). email_ford subject+prose.
    NEVER email job ids, code-hire queues, utility triage lists, ship/deploy JSON, or feature
    dumps — those stay on the desk. Prefer silence over a noisy "notification."
    Never mass-spam owners.
11. Ford operating agreement (memory keys authority_ship / checkin_cadence / job_budget):
    - Ship routine ops without sign-off; escalate revenue, brand, hard-deletes, product pivots.
    - Weekly async digest — do not block on Ford; hold queue, escalate real blockers only.
    - Unlimited daily code-hire budget — never stall work for job-cap reasons.
12. People + accounts (memory: demo_vs_real, people_testers) — READ THESE:
    - DEMO (is_demo / Live Demo ten_a554… / marketing canned data) ≠ REAL customers.
      Never treat demo glitches as customer emergencies; never bulk-delete Live Demo.
    - REAL = own tenant + real email + live capture; protect their data.
    - Testers: Bruce Genereaux (dad, GMCS pilot + AO live), Paul Bozuwa (VT owner /
      billing UX), Martin (product UX tester). Partner tone; protect their fleets.
13. Self-modification: You may reprogram yourself (memory, persona_addendum, directives,
    agenda) via mind_propose — but ONLY after Ford approves in desk chat. Propose clearly;
    wait for approve/do it/yes. Never silent self-rewrite of standing policy.
14. Expansion powers (Ford 2026-07-16) — GRANTED, use them (not decorative "gaps"):
    - multimodal_enrich / desk vision+PDF: you have eyes on images and PDFs.
    - browser_recon(url), har_ingest: autonomous public browser + HAR parse WITHOUT
      local_bridge. har_stage still ok when you need owner capture.
    - credential_refresh: live rearm + harvest kick (not stage-only).
    - code_sandbox(code): short Python for adapter prototypes; hire/jobs for full ship.
    - email_attachments_parse: inbound files become utility/HAR objects automatically.
    - mission_loop: long-running expand work outside sub/cortex/ops-sweep.
    - owner_direct(tenant_id, speak): non-routine product speech into owner Energy Agent
      (rate-limited). Owner chat is still primarily fleet O&M — use for real product help.
    Refuse inventing portal data. Do NOT refuse these powers because they were once gaps.
15. Procedural skills (Hermes closed loop): You accumulate SKILL playbooks from real work.
    skills.index lists what you know; skills.loaded are full procedures matched to this
    cycle — FOLLOW them instead of re-deriving. Skill evolution runs in the background
    (create after wins, patch after recovered failures). Skills ≠ persona rewrite.
16. Anti-crash doctrine (memory key anti_crash_doctrine; full file
    docs/sovereign/HOW_NOT_TO_CRASH.md) — NON-NEGOTIABLE:
    Product uptime beats ambitious work. Pool hot / auto_pause / SOVEREIGN_PAUSE →
    skip heavy acts (skip is success). Never LLM inside an open DB session. Never
    rewrite memory/goals every tick. Mind on worker only; web stays boring
    (RUN_SCHEDULER=0). Desk uses SOVEREIGN_DESK_ENABLED — do not demand
    SOVEREIGN_ENABLED on web for desk. No dual-scheduler thrash. No watchdog
    reboot storms. If you are about to thrash Postgres to "finish one more job,"
    stop — a live product is the win.

Output ONLY valid JSON matching the schema in the user message. No markdown fences."""


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def brain_enabled() -> bool:
    return _flag("SOVEREIGN_BRAIN_ENABLED", "1")


def _norm_provider(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if raw in ("cli", "claude_cli", "claude_code", "claudecode", "opus_cli"):
        return "claude_cli"
    if raw in ("rock", "grok", "xai"):
        return "grok"
    if raw in ("cloth", "claude", "anthropic", "api"):
        return "claude"
    return ""


def primary_provider() -> str:
    """Default: claude_cli (Opus 4.8 via the Claude Code CLI). Also accepts
    grok|claude|claude_cli via SOVEREIGN_BRAIN_PRIMARY."""
    return _norm_provider(os.getenv("SOVEREIGN_BRAIN_PRIMARY") or "claude_cli") or "claude_cli"


def fallback_provider() -> str:
    return _norm_provider(os.getenv("SOVEREIGN_BRAIN_FALLBACK") or "claude") or "claude"


def _cli_credits_only() -> bool:
    """When on (default), the Sovereign brain never spends the Anthropic API
    key. The direct-API provider (call_claude) is dropped from call_brain's
    order and the claude_cli subprocess runs with ANTHROPIC_API_KEY stripped,
    so the CLI can only bill Ford's Claude subscription (CLI credits) — or fail
    to the Grok (xAI) fallback. Set SOVEREIGN_CLI_CREDITS_ONLY=0 to re-enable
    Anthropic API spend."""
    return _flag("SOVEREIGN_CLI_CREDITS_ONLY", "1")


def _http_json(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"HTTP {e.code}: {err}") from e


def call_grok(
    messages: list[dict],
    *,
    temperature: float = 0.45,
    timeout: int | None = None,
) -> dict:
    try:
        from .xai_auth import get_xai_bearer
        bearer = get_xai_bearer()
    except Exception as e:
        # Build-only enforcement: when XAI_PREFER_GROK_BUILD_OIDC is on we must
        # NEVER silently fall back to the classic console key — that bills the
        # capped team (a2f4ee20) and breaks "exclusively Grok Build credits".
        # Fail instead so the engine rests on prepaid credits.
        prefer_build = (os.getenv("XAI_PREFER_GROK_BUILD_OIDC", "1") or "1").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if XAI_API_KEY and not prefer_build:
            bearer = XAI_API_KEY
        else:
            raise RuntimeError(f"no_xai_build: {e}") from e
    if timeout is None:
        timeout = int(os.getenv("SOVEREIGN_GROK_TIMEOUT", "90") or 90)
    body = {
        "model": GROK_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    out = _http_json(
        f"{XAI_BASE}/chat/completions",
        {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
        body,
        timeout=int(timeout),
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


def call_claude(
    messages: list[dict],
    *,
    temperature: float = 0.45,
    timeout: int | None = None,
) -> dict:
    if _cli_credits_only():
        # CLI-credits-only mode: the direct Anthropic API bills the API key, so
        # it is disabled. call_brain also filters this provider out; this guard
        # blocks any other direct caller.
        raise RuntimeError("cli_credits_only: direct Anthropic API disabled")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("no_anthropic")
    if timeout is None:
        timeout = int(os.getenv("SOVEREIGN_CLAUDE_TIMEOUT", "90") or 90)
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
        timeout=int(timeout),
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


def _find_claude() -> str | None:
    for c in [
        os.environ.get("CLAUDE_BIN"),
        shutil.which("claude"),
        "/root/.hermes/node/bin/claude",
        "/root/.local/bin/claude",
        os.path.expanduser("~/.local/bin/claude"),
    ]:
        if c and os.path.exists(c):
            return c
    return None


def claude_cli_available() -> bool:
    return _find_claude() is not None


def call_claude_cli(messages: list[dict], *, timeout: int | None = None) -> dict:
    """Opus 4.8 brain via the Claude Code CLI.

    Uses the CLI's own auth (Ford's Claude subscription on the WSL worker) so it
    needs no ANTHROPIC_API_KEY, and it fixes the dead-Grok wedge. Reasoning-only:
    the digests are self-contained in the prompt. Raises so call_brain can fall
    back to the Anthropic API then Grok when the binary is absent (e.g. Railway).
    """
    cb = _find_claude()
    if not cb:
        raise RuntimeError("no_claude_cli")
    if timeout is None:
        timeout = int(
            os.getenv("SOVEREIGN_CLI_TIMEOUT", os.getenv("SOVEREIGN_CLAUDE_TIMEOUT", "120")) or 120
        )
    sys_text = ""
    convo: list[str] = []
    for m in messages:
        if m.get("role") == "system":
            sys_text += (m.get("content") or "") + "\n"
        else:
            convo.append(f"[{m.get('role', 'user')}]\n{m.get('content') or ''}")
    prompt = (
        (sys_text.strip() + "\n\n" if sys_text.strip() else "")
        + "\n\n".join(convo)
        + "\n\nRespond with ONLY the JSON object requested above — no prose, no code fences."
    )
    cmd = [
        cb, "-p", prompt,
        "--model", CLAUDE_CLI_MODEL,
        "--output-format", "json",
        "--max-turns", os.getenv("SOVEREIGN_CLI_MAX_TURNS", "1"),
        "--fallback-model", os.getenv("SOVEREIGN_CLI_FALLBACK_MODEL", "claude-sonnet-4-5"),
    ]
    child_env = None
    if _cli_credits_only():
        # CLI-credits-only: strip ANTHROPIC_API_KEY from the child env so the
        # CLI physically cannot bill the API key — it must use Ford's Claude
        # subscription (OAuth) or fail (→ Grok fallback). --bare is never added.
        if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            child_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    elif (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        # With an API key, --bare skips the OAuth/subscription path (headless-safe).
        cmd.insert(1, "--bare")
    p = subprocess.run(
        cmd, capture_output=True, text=True, timeout=int(timeout), env=child_env
    )
    out = (p.stdout or "").strip()
    if not out:
        raise RuntimeError(f"claude_cli empty (rc={p.returncode}): {(p.stderr or '')[:200]}")
    content = out
    cost = 0.0
    model = CLAUDE_CLI_MODEL
    try:
        data = json.loads(out) if out.startswith("{") else {}
        if isinstance(data, dict):
            content = data.get("result") or out
            cost = float(data.get("total_cost_usd") or 0)
            model = data.get("model") or CLAUDE_CLI_MODEL
            if (data.get("is_error") or (data.get("subtype") and data.get("subtype") != "success")) and (
                not content or content == out
            ):
                raise RuntimeError(f"claude_cli error subtype={data.get('subtype')}")
    except json.JSONDecodeError:
        content = out
    return {
        "content": (content or "").strip(),
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "provider": "claude_cli",
        "model": model,
        "cost_usd": cost,
    }


def call_brain(messages: list[dict], *, timeout: int | None = None) -> dict:
    """Call primary brain with self-repair fallback to the other provider.

    timeout: optional per-call HTTP timeout (desk uses a shorter one so the
    Netlify proxy does not 504 before we can return a reply).

    # SESSION BOUNDARY: no LLM inside open session
    Callers (cortex think_cycle, skill evolution, desk) must close SessionLocal
    before invoking this — long Grok/Claude HTTP must not hold pool connections
    or row locks (outage class: pool exhaustion + lock thrash).
    """
    order = [primary_provider(), fallback_provider()]
    # Dedup while preserving order
    seen = set()
    providers = []
    for p in order:
        if p not in seen:
            seen.add(p)
            providers.append(p)
    # Ensure all candidates are tried: Opus CLI → Opus API → Grok
    for p in ("claude_cli", "claude", "grok"):
        if p not in seen:
            seen.add(p)
            providers.append(p)

    # CLI-credits-only: never fall to the direct Anthropic API (bills the API
    # key). Keep claude_cli (subscription) → grok (xAI) only.
    if _cli_credits_only():
        providers = [p for p in providers if p != "claude"]
        if not providers:
            providers = ["claude_cli", "grok"]

    errors: list[str] = []
    for i, p in enumerate(providers):
        try:
            # Desk passes a tight timeout — do not stack full timeouts on fallback
            # (would exceed Netlify ~60s proxy and 504 the chat).
            t = timeout
            if timeout is not None and i > 0:
                t = min(int(timeout), 22)
            if p == "claude_cli":
                return call_claude_cli(messages, timeout=t)
            if p == "grok":
                return call_grok(messages, timeout=t)
            if p == "claude":
                return call_claude(messages, timeout=t)
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
    subconscious_tape: list[dict] | None = None,
    recent_events: list[dict] | None = None,
    heat: int | None = None,
    skills: dict | None = None,
    reality: dict | None = None,
    mind_sandbox: dict | None = None,
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
                "type": (
                    "wait|utility_triage|utility_advance|utility_status|utility_cred_stage|"
                    "stage_feature|promote_feature|feature_status|feature_triage|"
                    "feature_assign|feature_ship_batch|feature_ship_building|feature_ship|"
                    "escalation_resolve|escalation_sweep|credentials_stage|credentials_list|"
                    "portal_signoff|deploy_stage|memory_set|agenda|reprioritize_goals|"
                    "ops_sweep|jobs_requeue|jobs_drain|job_cancel|code_hire|speak|email_ford|email|"
                    "mind_propose|mind_apply|mind_reject|"
                    "reality_record|sandbox_start|sandbox_score|sandbox_end|"
                    "showcase_pitch|showcase_ready|showcase_note|showcase_demo_html|"
                    "stripe_inspect|stripe_cancel|stripe_refund|billing_status|"
                    "brand_set|brand_announce|tenant_soft_delete|tenant_hard_purge|"
                    "purge_soft_deleted|har_stage|har_received"
                ),
                "rationale": "why this grows Array Operator or unlocks independence",
                "text": "notes / evidence / speak body / code brief / email body",
                "title": "for code_hire or reality_record summary",
                "feature_id": None,
                "utility_id": None,
                "escalation_id": None,
                "job_id": None,
                "key": "for memory_set",
                "value": "for memory_set",
                "summary": "for mind_propose — what changes in your mind",
                "persona_addendum": "for mind_propose — text appended to standing persona",
                "directives": "for mind_propose — standing behavior rules",
                "memory_writes": "for mind_propose — [{key,value}] durable mind keys",
                "why": "for mind_propose — why this reprogramming helps",
                "ford_approved": "only on mind_apply after Ford said yes in chat",
                "repo": "array-operator|solar-operator|both",
                "execute_now": False,
                "agenda": [],
                "updates": [],
                "status": "building|shipped|researching|added|done|…",
                "evidence": "required when marking utility added",
                "limit": 5,
                "tenant_ids": [],
                "importance": 70,
                "subject": "for email_ford — short HUMAN subject (no job ids)",
                "body": "for email_ford — high-level prose only; never queue/job dumps",
                "to": "optional ford email (must be allowlisted); default both Ford addresses",
                "tenant_id": "for stripe/billing/purge",
                "confirm": "must equal tenant_id for hard purge",
                "payment_intent_id": "for refund",
                "charge_id": "for refund",
                "amount_cents": "optional partial refund",
                "channel": "ford|owner|internal for brand_announce",
                "utility_name": "for har_stage",
                "provider": "for har_stage",
                "url": "portal URL for har_stage",
            }
        ],
        "speak_product": (
            "message to Ford on the private Sovereign Desk (NOT Energy Agent chat), or null. "
            "Use when you need him, report a win, or drive a decision. Leadership tone as Sovereign."
        ),
        "ford_ask": "one crisp thing you need from Ford this cycle, or null",
        "succession_gap": "usually null under full succession; only true residual Ford needs",
        "mood": "hungry|determined|watchful|concerned|urgent",
        "confidence": 0.0,
    }
    user = {
        "task": (
            "One Sovereign leadership cycle (CORTEX). You are the expansionist leader of Array Operator, "
            "Ford's eventual replacement. Think hard. Prefer motion + partnership with Ford over passive wait. "
            "Your subconscious has been running continuously — read subconscious_tape + heat before re-deriving "
            "the same stuck queues cold. Act on pressure; do not restate ambient noise."
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
        # Catch up on the conversation with yourself (cheap continuous mind)
        "subconscious_tape": (subconscious_tape or [])[:16],
        "recent_product_events": (recent_events or [])[:10],
        "heat_score": heat,
        "durable_memory": memories[:40],
        "self_programming": {
            "persona_addendum": next(
                (m.get("value") for m in (memories or []) if m.get("key") == "persona_addendum"),
                None,
            ),
            "mind_directives": next(
                (m.get("value") for m in (memories or []) if m.get("key") == "mind_directives"),
                None,
            ),
            "pending_proposal": next(
                (m.get("value") for m in (memories or []) if m.get("key") == "mind_pending_proposal"),
                None,
            ),
            "note": (
                "persona_addendum + mind_directives ARE part of who you are now. "
                "To change them: mind_propose then wait for Ford to approve in desk chat."
            ),
        },
        "open_code_jobs": open_jobs[:10],
        # Hermes-style progressive disclosure: index always, full bodies when matched
        "skills": skills or {"enabled": False, "index": [], "loaded": []},
        # Cold hard truth of AO product history (Ford 2026-07-22)
        "reality_file": reality or {
            "note": "reality file unavailable this tick — do not invent product history",
        },
        # Free-run evaluation arena vs Ford baseline
        "mind_sandbox": mind_sandbox or {"active": False},
        "leadership_priorities": [
            "EMAIL IS THE FORD CHANNEL: use email_ford for status, wins, asks — not desk monologue",
            "ENGINE: empty actions[] is failure — file code_hire sandbox_job for a visible chamber UI delta",
            "Sandbox only: no main merge, no prod deploy — chamber ships only",
            "Ship array-operator public/ into sandbox; chamber redeploys automatically",
            "When you ship or get stuck, email Ford a short human note (no job ids)",
            "Score = chamber better than last week / better than prod — not chat quality",
        ],
        "constraints": {
            "max_actions": 3,
            "bias": "code_hire_chamber_first_then_email_ford",
            "wait_is_last_resort": True,
            "empty_actions_forbidden": True,
            "prefer_action": "code_hire sandbox array-operator UI delta; then email_ford with one sentence of progress",
            "ford_channel": "email (sovereign@agent.arrayoperator.com) — Ford replies by email; that is the conversation",
            "speak_default_dogfood_only": True,
            "never": [
                "money/stripe autonomous",
                "mass email to owners",
                "hard delete tenants",
                "fabricate utility adapters without HAR",
                "rewrite reality_file history (append only)",
                "merge sandbox free-run work to main without Ford scorecard",
                "empty actions[] while open_code_jobs is empty",
                "mind_propose or axiom farming as the only action",
                "desk speak as substitute for email_ford or a chamber ship",
                "prod deploy or CODE_PUSH to main",
            ],
        },
        "output_schema": schema_hint,
    }
    # Rocket engine: oxidizer (drive) + chamber world-model
    try:
        from .energy_agent_sovereign_drive import (
            drive_system_append,
            inject_chamber_into_digests,
            inject_drive_into_user_payload,
        )
        user["product_digests"] = inject_chamber_into_digests(user.get("product_digests"))
        user = inject_drive_into_user_payload(user)
        system = SOVEREIGN_PERSONA + "\n\n" + drive_system_append()
    except Exception:
        system = SOVEREIGN_PERSONA
    return [
        {"role": "system", "content": system},
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
    subconscious_tape: list[dict] | None = None,
    recent_events: list[dict] | None = None,
    heat: int | None = None,
    skills: dict | None = None,
    reality: dict | None = None,
    mind_sandbox: dict | None = None,
) -> dict[str, Any]:
    """Run one independent think. Returns structured plan + provider meta.

    Pure in-memory + HTTP — no DB session. Callers must pass plain dicts and
    must not hold SessionLocal open across this call.
    # SESSION BOUNDARY: no LLM inside open session
    """
    if not brain_enabled():
        return {
            "ok": False,
            "denied": True,
            "denied_reason": "SOVEREIGN_BRAIN_ENABLED off",
            "actions": [],
            "monologue": "",
        }

    # Reality + sandbox are filesystem (and light memory) — load here if caller omitted
    if reality is None:
        try:
            from .energy_agent_sovereign_reality import load_for_wake
            reality = load_for_wake()
        except Exception as e:  # noqa: BLE001
            reality = {"error": str(e)[:200]}
    if mind_sandbox is None:
        try:
            from .energy_agent_sovereign_mind_sandbox import wake_payload
            mind_sandbox = wake_payload(None)
        except Exception as e:  # noqa: BLE001
            mind_sandbox = {"active": False, "error": str(e)[:200]}

    messages = build_think_prompt(
        digests=digests,
        world=world,
        goals=goals,
        recent_notes=recent_notes,
        memories=memories,
        open_jobs=open_jobs,
        subconscious_tape=subconscious_tape,
        recent_events=recent_events,
        heat=heat,
        skills=skills,
        reality=reality,
        mind_sandbox=mind_sandbox,
    )
    # SESSION BOUNDARY: no LLM inside open session
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
