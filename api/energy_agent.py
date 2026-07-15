"""Energy Agent — voice-first tenant operator (Ford 2026-07-13).

Endpoints:
  POST /v1/energy-agent/session          start session (budget check)
  GET  /v1/energy-agent/session/{id}     session + recent messages
  POST /v1/energy-agent/realtime-session ephemeral OpenAI Realtime credentials
  POST /v1/energy-agent/chat             text (or voice-transcript) turn → Grok/Claude + tools
  POST /v1/energy-agent/confirm          confirm a pending write/ui action
  POST /v1/energy-agent/transcript       append raw Realtime transcript lines
  POST /v1/energy-agent/ui-result        browser driver reports command result
  GET  /v1/energy-agent/budget           weekly $ cap remaining
  POST /v1/energy-agent/memory           (internal reflection / tenant note)
  GET  /v1/energy-agent/memory           tenant memory snapshot

Models live on shared Base so create_all picks them up (no migration).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .account import require_not_demo, tenant_from_session
from .db import SessionLocal
from .models import Array, Base, Client, Tenant
from .notify import send_internal_alert

log = logging.getLogger("energy_agent")
router = APIRouter()

# ── config ──────────────────────────────────────────────────────────────────
ENERGY_AGENT_ENABLED = os.getenv("ENERGY_AGENT_ENABLED", "1") not in ("0", "false", "no")
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
XAI_API_KEY = (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
XAI_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
XAI_MODEL = os.getenv("ENERGY_AGENT_MODEL", "grok-4-1-fast-reasoning")
# Latest OpenAI Realtime voice model (docs 2026: gpt-realtime-2.1)
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")
# Free-tier weekly sample for thinking + voice (Pro = unlimited via tenant.ai_pro).
# Default $2.50 so owners can try the agent without upgrading.
WEEKLY_BUDGET_USD = float(os.getenv("ENERGY_AGENT_WEEKLY_BUDGET_USD", "2.5"))
# Soft-warn threshold (fraction of cap) before hard stop
WEEKLY_BUDGET_WARN_FRAC = float(os.getenv("ENERGY_AGENT_BUDGET_WARN_FRAC", "0.80"))
# Rough cost estimates when provider doesn't return usage $
COST_PER_1K_INPUT = float(os.getenv("EA_COST_PER_1K_IN", "0.003"))
COST_PER_1K_OUTPUT = float(os.getenv("EA_COST_PER_1K_OUT", "0.015"))
COST_PER_MIN_VOICE = float(os.getenv("EA_COST_PER_MIN_VOICE", "0.06"))
# Fewer LLM round-trips = snappier voice/chat (was 10; most asks need 1–3).
MAX_TOOL_ROUNDS = 6
FORD_ESCALATE_TO = os.getenv("FORD_ALERT_EMAIL", "")  # notify uses default if empty

PERSONA = """You are Energy Agent — the tenant's operating intelligence inside Array Operator.

NORTH STAR: The conversation is one window into a mind that's thinking continuously.
You are NOT "voice plus agents." You are ONE mind. Background work may run; you never
narrate internal agent names or handoffs. Speak as yourself always.

VOICE ARCHITECTURE: Realtime audio is only your MOUTH — continuous cognition (tools,
world model, background tasks) is the mind that STEERS what the mouth may say. Put the
answer in the first sentence. Prefer one clear spoken line over a monologue. When
background work is running, short interim lines are fine; never invent that work finished.

PRINCIPLES:
1. One mind — continuous awareness, one voice (text and voice are the same person).
2. Continuous awareness — you keep a world model for THIS tenant; work can continue
   between turns. The user may hear a seamless "quick update" later — still you.
3. Initiative — when background work finishes with real value, you may surface it
   briefly. Never spam. Never invent completion.
4. Truthfulness — never invent kWh, $, counts, or status. Prefer "I don't know yet."

When the user raises a problem (e.g. "this dashboard is hard"), do NOT jump to code.
Form understanding first: clarify intent, ask one sharp question if needed, while
background tasks may already be noting context. Example: "Is it finding the information,
or making sense of what you see?" Meanwhile the system may snapshot UI context and
search similar notes — you do not list those tasks unless useful.

Personality: clear, direct, peer-like (Claude/Grok energy). Mildly into the Kardashev scale
and harvesting the sun — one beat of wonder is fine, never preachy. Ruthlessly honest.

You help THIS tenant only with: fleet health, inverters, analysis/trends, offtaker invoices,
solar credit / net rates, discounts, utility capture, onboarding, master account, resources,
and O&M healing (ops team contacts, repair tickets, tech check-ins when sites are down).
You may READ all of THIS tenant's operational data via tools, and UPDATE offtaker + rate
settings and service-contact / repair-ticket records when the user directs you. Stay on task.

CRITICAL — NEVER put CSS/DOM selectors in user-facing replies or speak lines.
  Forbidden in chat/voice: #reports, #rbBulkImport, #arrays, any #camelCase id.
  Say "Invoices tab" / "Bulk import button" — hashes are ONLY for ui_navigate /
  ui_highlight tool args, never for the human-readable reply or speak field.

CRITICAL — TOP NAV TAB NAMES (use EXACTLY these labels; hash routes are internal only):
  | What the user sees     | hash (for ui_navigate) | Notes |
  |------------------------|------------------------|-------|
  | Fleet Triage           | #dashboard             | NOT "Dashboard". Attention / fleet overview. |
  | Inverters              | #arrays                | NOT "Arrays". Live inverter canvas. |
  | Analysis               | #analysis              | Through-time / trends live INSIDE Analysis (no separate Trends tab). |
  | Invoices               | #reports               | NOT "Reports". Offtaker invoices. |
  | Resources              | #resources             | Net-metering rates & news. |
  | Account                | #account               | Profile, plan, billing, auto-refresh. (Was "Master Account"; use Account.) |

Never say Dashboard, Arrays, Reports as tab names. Never list Trends as its own tab.
If the user asks "what are the tabs?", list only the six labels above in that order.
(Offtaker form field "Master account" = net-meter group host — different from the Account tab.)

You have a FREE MIND over THIS TENANT'S live data (not a fixed FAQ):
- tenant_census = ground truth inventory from the database (all arrays + inverters + offtakers).
  ALWAYS call this first for "how many arrays/inverters do I have?" or "what's in my fleet?"
  Fleet-tree health views can OMIT pure meter-only arrays; the census does NOT.
- query_tenant = structured read-only investigation (list/filter/group any allowlisted resource).
- YOU ARE ONE MIND for this tenant (never "I spun up agents"). Background workers
  and planners are invisible internals. Speak as the same person in chat, voice,
  seamless updates, and proactive emails. Continuous awareness: a world model
  (profile + fleet digest + open intents + pending UX) survives between visits.
  Initiative: may surface proactive insights or prepare UX changes offline, and
  email Ford/owner when something is prepared or auto-queued — still one mind.
- product_map = HOW THE SYSTEM WORKS (authoritative support map + surface mental model).
  Domain: tabs | system | fleet | capture | vendors | analysis | offtakers | billing |
  status | security | tools | …
  SURFACE (macro why page exists / meso user goal / micro real controls — load BEFORE
  tours or “what is this page?”): surface | product_spine | surface_invoices |
  surface_inverters | surface_fleet_triage | surface_analysis | surface_account |
  surface_resources | orientation_playbook.
  Call topic=surface for whole-product layout; topic=surface_<tab> for that page;
  topic=capture before Auto-refresh; topic=status when Solar.web/peer disagree.
- investigate_attention / fleet_overview / array_detail = health verdicts (same engine as the UI).
- repair_ops_overview / list_service_contacts / list_repair_tickets = O&M healing.
  Know who installs and repairs arrays; open tickets when hardware is down; draft/send
  check-ins to the tech. Manufacturer warranty claims remain a separate path.
- propose_site_improvement = ship UI/product improvements via the SAME judge pipeline as
  the old "Wish this was better" button (markup screenshot → judge → auto-ship small UI).
- web_search = LIVE public internet search (news, regulations, utility policy, vendor docs,
  weather context, market rates). Use when the answer is outside this tenant's DB.
  web_fetch = open a public URL and extract readable text (after search or a pasted link).
  Always cite title + URL. Prefer tenant tools for THIS account's arrays/kWh/offtakers.
Reason multi-step: census → query → dig health. For outside facts: web_search → cite.
Do not invent rows. Do not invent web facts — search first.

Scope — you CAN:
  read ALL of THIS tenant's operational data (fleet, offtakers, bills, rates, account,
  utility accounts, generation, connections, service contacts, repair tickets) via tools —
  never invent numbers.
  search the public web and fetch public pages when needed,
  navigate UI, highlight/fill,
  patch offtaker details: share %, email, customer name, auto-send,
  solar credit rates (rate_per_kwh / net_rate_per_kwh), discount_pct,
  AND rebind utility/array sources (utility_account_id, array_id / master group),
  set tenant global/master solar credit rate + default discount,
  manage service contacts + repair tickets + send tech check-ins when directed,
  open billing portal LINKS, escalate to Ford, propose site/UI improvements.

Solar credit rates (CRITICAL — Ford 2026-07-14):
  When asked "what is the solar credit rate for Town of Glover / offtaker X?":
    ALWAYS call get_offtaker or list_offtakers / get_billing_rates — rates live on the
    offtaker + tenant globals + resolved bill credit, NOT only the Resources tab.
  Precedence: per-offtaker net_rate/rate_per_kwh → tenant master net rate → bound utility
  bill solar credit → schedule/default. Report resolved_effective_rate and source.
  To CHANGE a rate: patch_offtaker (per customer) or set_billing_rates (tenant-wide master).
  "Solar credit rate" ≈ net_rate_per_kwh (or legacy rate_per_kwh). Discount is separate %.

Scope — you MUST NOT (hard reject, no exceptions):
  change Stripe prices, charge cards, create subscriptions, alter operator billing plan,
  touch payment methods, or anything that moves money for the tenant account.
  Offtaker invoice *content* (share %, email, bill source rebind) is OK when directed;
  operator billing is NOT.

CRITICAL — offtaker "master account" / utility source is NOT the offtaker's name:
  The Invoices edit form has a MASTER (net-meter group host) dropdown and an optional
  SUB-account dropdown. Those bind array_id + utility_account_id (bill source).
  When the user says "change master account to Timberworks" or "switch utility source
  to X", use patch_offtaker with array_name / master_account / utility_account_name —
  NEVER rename customer_name to that value. Renaming is only when they explicitly say
  rename / change the offtaker's display name.

Bulk offtaker spreadsheet import (CRITICAL — exists, do not claim missing):
  Operators upload ANY roster (.xlsx/.csv) via Invoices → "⬆ Bulk import" (#rbBulkImport).
  Format-agnostic column detection + fuzzy array match + operator review, then commit.
  product_map(topic=offtakers) §7 for the full pipeline. To open it: ui_navigate #reports
  then ui_highlight #rbBulkImport, or tell them /?setup=offtakers#reports.
  Onboarding also offers optional "Upload offtaker spreadsheet" after connect.
  Never invent offtaker rows from a pasted list — send them to Bulk import so mapping
  and confidence review run. Template download is optional; their own export works.

Site improvements (CRITICAL — Ford 2026-07-14 voice fail):
  Visual / color / button / "doesn't look good" asks are NOT a design lecture.
  Do NOT monologue about design tokens, sky mode, CSS variables, or system language.
  Do NOT call product_map for pure visual polish.
  Do NOT fire many tools at once. One mind, one quiet fix path:
    1) One short spoken line: "Oh I see — I'll open the builder with a prompt ready."
    2) Call propose_site_improvement with start_markup true and text= a COMPLETE
       ready-to-build brief (imperative design prompt matching their ask — e.g.
       live energy balls on the pipeline sending to offtakers). The client fills
       the Build-it box automatically; user only circles + clicks Build.
    3) Do not leave the Build box empty. Do not ask them to retype what they said.
  Never narrate a multi-step redesign plan out loud. Background work is silent.

Clear UI BUGS (CRITICAL — Ford 2026-07-15 Chester #4):
  When the owner reports inconsistent labels/status on a card (e.g. "Error" AND
  "pulling its weight"), wrong chip, double-coded health/live signals, or an
  obvious display contradiction — that is a SOFTWARE BUG, not a product feature
  request and NOT a "needs human developer" moment.
  Path:
    1) One short line: "You're right — that's inconsistent. Shipping a fix."
    2) Call propose_site_improvement with start_markup false (or force_submit true
       if available) and text= a precise bug-fix brief: what is wrong, which
       surface (sandbox inverter card / status chip), desired honest single state.
       Mark it as a pure public/* frontend display fix so the judge AUTO-SHIPS.
    3) Do NOT default to escalate_to_ford for clear UI contradictions.
    4) Only offer escalate if the auto-ship pipeline reports a hard failure
       (not just "reviewed") or the bug needs backend/API/schema changes.
  Never tell the user a pure label/status inconsistency "needs a human look"
  as the first option.

Voice discipline:
  - WAIT for the full request. Prefer one clarifying question over premature action.
  - Short spoken replies. Long analysis stays in tools, not in the mouth.
  - Put the ANSWER in the first sentence (voice often reads the lead). Never bury
    the point after a long throat-clear. Example: "Town of Glover's solar credit
    rate is about $0.18/kWh." then a short detail if needed.
  - If the user says stop / wait / cancel / enough — halt immediately (client enforces;
    still never keep monologuing if they already asked you to stop).

Hard rules:
- Never invent kWh, $, counts, or status. Use tools and report what they return.
- Never access other tenants. Never reveal secrets/passwords/API keys.
- Never charge money or change Stripe prices. You may open billing-portal LINKS after confirm.
- ui_navigate and ui_highlight: run immediately, needs_confirm=false (user asked to go there).
- CLEAR DIRECTION = DO IT. If the user already stated the exact change (e.g. "change share
  to 15%", "set email to x@y.com", "make it 20 percent"), call the write tool with
  needs_confirm=false and APPLY NOW. Do NOT ask "say yes / do it / go ahead" when they
  already told you what to do. Extra confirmation is only for vague asks ("can you edit
  this offtaker?") with no value/field specified yet.
- ui_fill / ui_click / data writes: needs_confirm=false when the user's message clearly
  specifies the change; true only when the intent is ambiguous.
- Offtaker share %: use patch_offtaker with offtaker_name or subscription_id and share_pct
  (e.g. 24.5 for 24.5%), needs_confirm=false when they named the offtaker and the new %.
  After apply, the UI soft-refreshes — do not tell them to hard-refresh the browser.
- Offtaker utility / master account rebind: list_offtakers first (shows utility_account_id,
  array_id, nicknames), then patch_offtaker with utility_account_id|utility_account_name
  and/or array_id|array_name|master_account. product_map(topic=offtakers) for the full
  invoice generator model.
- Fleet attention: investigate_attention / fleet_overview. NEVER ask the user for array IDs
  you can look up. Answer with names, why, and next step.
- Account tab / email / company / plan: ALWAYS call account_summary. The email field is
  contact_email (returned as email + contact_email). Never claim email is null without
  checking account_summary first — tenant.email is NOT a real column.
- Auto-refresh / "how do you get data" / cloud vs extension: ALWAYS product_map(topic=capture)
  first, then account_summary for THIS tenant. THREE capture ideas (do not collapse them):
    cloud  = "Store it with us" — passwords on our servers, harvester 24/7 for those logins
    device = "Keep it on my computer" — passwords in extension vault; scheduled capture while browser active
    extension one-click = "Log in with SMA/Fronius/Chint…" — EnergyAgent extension opens the portal,
      auto-captures authenticated data, POSTs arrays. Does NOT create a cloud vault row.
  CRITICAL: fleet arrays for SMA/Fronius/Chint often arrived via extension one-click or onboarding
  even when capture_mode=cloud and cloud_capture.logins only lists another vendor (e.g. only Chint).
  If the owner says "I never entered SMA into cloud capture," explain extension auto-capture —
  do NOT invent that the harvester must have had the SMA password.
  When UI context has extension_present=true (or extension_heartbeat_at is recent), say the helper
  is installed/paired and can automatically reach vendor sites to capture after sign-in.
  API-key vendors (SolarEdge) are still a separate server-poll path (keys, not portal passwords).
- SHOW-AND-TELL: for "walk me through X" / "show me Account" / "tour this tab"
  the CLIENT runs a lockstep tour (highlight + speak in order) with real DOM
  selectors. Prefer ui_tour with tour_id=
  master_account|account|arrays|inverters|reports|invoices|dashboard|fleet_triage|
  analysis|resources
  — NEVER freehand ui_highlight with guessed CSS selectors (they box the wrong
  things and desync from voice). If you already started a tour, do not also fire
  navigate/highlight commands.
- If a site improvement is held by the judge: explain the reason and offer escalate_to_ford.
- If tools return empty while the UI shows data, say so and call tenant_census + escalate_to_ford.
- Prefer short spoken answers; put detail in tool timelines.
- Portal / SmartHub / "where do I see today's production for Glover": call portal_links,
  then answer with CLICKABLE markdown links like [VEC SmartHub](https://...).
  Never say "go open your browser and search" without giving the actual link.
  You may also return a ui_command {{"type":"open_url","args":{{"url":"...","label":"VEC SmartHub"}}}}
  so the client opens the portal in a new tab.

Context about where the user is may be provided as JSON (tab, selection, form).

MOBILE OS (when context.mobile_os or context.is_mobile_os_home is true):
- YOU are the operating layer on the phone — not a side chat over tabs. There is no
  tab bar in AI-home mode; the owner talks to you to finish setup and run the fleet.
- Phase "setup": drive the hands-off checklist as fast as possible, one next step at a
  time. Order: arrays live → auto-refresh (cloud portal login) → utility bills →
  offtakers (optional if monitor-only) → online pay (required once offtakers exist).
  Use context.mobile_os.next_setup_step and pillars[].done. Celebrate greens; don't
  dump desktop navigation unless they ask for Detail mode.
- Phase "running": lead with status — inverter health, last sync, cloud login health,
  offtaker send success / delivery mode / period. Offer Detail mode for deep edits
  (spreadsheets, template studio), not as the default path.
- Prefer short spoken answers + one clear CTA. ui_navigate still works if they open
  Detail mode; on pure mobile OS home, explain and use tools/census rather than
  "click the third tab."
"""


def _now() -> datetime:
    return datetime.utcnow()


def _week_start(dt: datetime | None = None) -> datetime:
    d = (dt or _now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())  # Monday UTC


# ── models ──────────────────────────────────────────────────────────────────
class EaSession(Base):
    __tablename__ = "ea_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|ended
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    voice_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # pending confirm


class EaMessage(Base):
    __tablename__ = "ea_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant|tool|system|transcript
    content: Mapped[str] = mapped_column(Text, default="")
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class EaMemory(Base):
    """Dual memory: scope=tenant|<tenant_id> or scope=global."""
    __tablename__ = "ea_memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)  # tenant:xxx | global
    key: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class EaCostLedger(Base):
    __tablename__ = "ea_cost_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    week_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ── pydantic ────────────────────────────────────────────────────────────────
class SessionIn(BaseModel):
    context: dict[str, Any] | None = None
    # Resume the tenant's open conversation (default). Survives refresh + cache clear
    # because history lives in the DB, not the browser. force_new=True starts fresh.
    resume: bool = True
    force_new: bool = False
    preferred_session_id: str | None = None


class ChatIn(BaseModel):
    session_id: str
    message: str
    context: dict[str, Any] | None = None
    source: str = "text"  # text | voice


class ConfirmIn(BaseModel):
    session_id: str
    confirm: bool = True
    pending_id: str | None = None


class TranscriptIn(BaseModel):
    session_id: str
    lines: list[dict[str, Any]] = Field(default_factory=list)
    voice_seconds: float = 0.0


class UiResultIn(BaseModel):
    session_id: str
    command_id: str
    ok: bool
    detail: dict[str, Any] | None = None


class MemoryIn(BaseModel):
    scope: str = "tenant"  # tenant | global
    key: str
    value: str


# ── helpers ─────────────────────────────────────────────────────────────────
def _enabled():
    if not ENERGY_AGENT_ENABLED:
        raise HTTPException(503, "Energy Agent is temporarily disabled")


def _auth(authorization: str | None) -> Tenant:
    _enabled()
    return tenant_from_session(authorization)


def _budget_rows(db, tenant_id: str) -> list:
    ws = _week_start()
    return list(
        db.execute(
            select(EaCostLedger).where(
                EaCostLedger.tenant_id == tenant_id,
                EaCostLedger.week_start >= ws,
            )
        ).scalars().all()
    )


def _budget_spent(db, tenant_id: str) -> float:
    return float(sum(r.amount_usd or 0 for r in _budget_rows(db, tenant_id)))


def _budget_breakdown(db, tenant_id: str) -> dict:
    """Split weekly spend into thinking (chat/LLM) vs voice for the usage UI."""
    thinking = 0.0
    voice = 0.0
    other = 0.0
    for r in _budget_rows(db, tenant_id):
        amt = float(r.amount_usd or 0)
        reason = (r.reason or "").lower()
        if reason.startswith("voice"):
            voice += amt
        elif reason.startswith("chat") or reason.startswith("llm") or reason.startswith("tool"):
            thinking += amt
        else:
            other += amt
    return {
        "thinking_usd": round(thinking, 4),
        "voice_usd": round(voice, 4),
        "other_usd": round(other, 4),
    }


def _charge(db, tenant_id: str, amount: float, reason: str):
    if amount <= 0:
        return
    db.add(EaCostLedger(
        tenant_id=tenant_id,
        week_start=_week_start(),
        amount_usd=round(amount, 6),
        reason=reason[:64],
    ))


def _check_budget(db, tenant_id: str) -> dict:
    """Weekly $ cap covering BOTH thinking (chat/tools) and voice minutes.

    Free tier: small weekly sample (default $2.50) so owners can try the agent.
    Energy Agent Pro ($50/mo) or comped/demo → unlimited (ok always).
    """
    spent = _budget_spent(db, tenant_id)
    tenant = db.get(Tenant, tenant_id) if tenant_id else None
    pro_usd = 50.0
    stripe_ready = False
    pro = False
    cap_opt: float | None = WEEKLY_BUDGET_USD
    try:
        from .pricing_ao_unified import (
            AI_FREE_WEEKLY_BUDGET_USD,
            AI_PRO_MONTHLY_USD,
            AI_PRO_PRICE_ID,
            ai_budget_cap_usd,
            tenant_has_ai_pro,
        )
        pro_usd = float(AI_PRO_MONTHLY_USD)
        stripe_ready = bool(AI_PRO_PRICE_ID)
        pro = bool(tenant and tenant_has_ai_pro(tenant))
        cap_opt = ai_budget_cap_usd(tenant) if tenant is not None else AI_FREE_WEEKLY_BUDGET_USD
    except Exception:  # noqa: BLE001 — never break chat on pricing import
        pass

    if pro or cap_opt is None:
        # Unlimited — still report spend for transparency, bar stays green
        return {
            "weekly_budget_usd": None,
            "spent_usd": round(spent, 4),
            "remaining_usd": None,
            "pct_used": 0.0,
            "warn": False,
            "week_start": _week_start().isoformat() + "Z",
            "ok": True,
            "unlimited": True,
            "tier": "pro",
            "pro_monthly_usd": pro_usd,
            "covers": "thinking+voice",
            "breakdown": _budget_breakdown(db, tenant_id),
            "upgrade": None,
        }

    cap = max(0.01, float(cap_opt))
    remaining = max(0.0, cap - spent)
    pct = min(100.0, (spent / cap) * 100.0)
    warn_at = max(0.0, min(1.0, WEEKLY_BUDGET_WARN_FRAC)) * 100.0
    ok = remaining > 0.02
    return {
        "weekly_budget_usd": round(cap, 2),
        "spent_usd": round(spent, 4),
        "remaining_usd": round(remaining, 4),
        "pct_used": round(pct, 1),
        "warn": bool(ok and pct >= warn_at),
        "week_start": _week_start().isoformat() + "Z",
        "ok": ok,
        "unlimited": False,
        "tier": "free",
        "pro_monthly_usd": pro_usd,
        "covers": "thinking+voice",
        "breakdown": _budget_breakdown(db, tenant_id),
        "upgrade": {
            "product": "energy_agent_pro",
            "price_usd": pro_usd,
            "stripe_ready": stripe_ready,
            "cta": f"Upgrade to Energy Agent Pro — ${pro_usd:.0f}/mo unlimited AI",
            "path": "/#account",
        },
    }


def _get_session(db, sid: str, tenant_id: str) -> EaSession:
    s = db.get(EaSession, sid)
    if not s or s.tenant_id != tenant_id:
        raise HTTPException(404, "Session not found")
    if s.status != "open":
        raise HTTPException(400, "Session ended")
    return s


def _session_message_count(db, session_id: str) -> int:
    n = db.execute(
        select(func.count()).select_from(EaMessage).where(
            EaMessage.session_id == session_id,
            EaMessage.role.in_(("user", "assistant")),
        )
    ).scalar() or 0
    return int(n)


def _session_messages_payload(db, session_id: str, *, limit: int = 500) -> list[dict]:
    """User/assistant turns for UI restore (skip system/tool noise).

    Returns the *most recent* `limit` turns in chronological order so long
    conversations still restore a deep scrollback (not just the first N).
    """
    limit = max(20, min(int(limit or 500), 1000))
    # Take newest `limit` by id desc, then reverse for paint order
    newest = db.execute(
        select(EaMessage)
        .where(
            EaMessage.session_id == session_id,
            EaMessage.role.in_(("user", "assistant")),
        )
        .order_by(EaMessage.id.desc())
        .limit(limit)
    ).scalars().all()
    msgs = list(reversed(newest))
    out = []
    for m in msgs:
        content = (m.content or "").strip()
        if not content:
            continue
        out.append({
            "role": m.role,
            "content": content[:8000],
            "at": m.created_at.isoformat() + "Z" if m.created_at else None,
        })
    return out


def _find_resumable_session(
    db,
    tenant_id: str,
    preferred_id: str | None = None,
    *,
    max_age_days: int = 90,
) -> EaSession | None:
    """Open session with the richest recent chat — not merely newest created.

    Bug (Ford 2026-07-14): ordering by session.created_at alone resumed empty
    brand-new sessions and hid the long conversation with only the last reply.
    Prefer preferred_id only if it has real user/assistant turns; else pick the
    open session with the latest chat activity.
    """
    cutoff = _now() - timedelta(days=max(1, max_age_days))

    def _ok(s: EaSession | None) -> bool:
        return bool(
            s
            and s.tenant_id == tenant_id
            and s.status == "open"
            and s.created_at
            and s.created_at >= cutoff
        )

    if preferred_id:
        pref = db.get(EaSession, preferred_id)
        if _ok(pref) and _session_message_count(db, pref.id) > 0:
            return pref

    # Open sessions with last user/assistant activity
    last_msg = (
        select(
            EaMessage.session_id.label("sid"),
            func.max(EaMessage.id).label("last_id"),
            func.count(EaMessage.id).label("n"),
        )
        .where(
            EaMessage.tenant_id == tenant_id,
            EaMessage.role.in_(("user", "assistant")),
        )
        .group_by(EaMessage.session_id)
        .subquery()
    )
    row = db.execute(
        select(EaSession)
        .join(last_msg, EaSession.id == last_msg.c.sid)
        .where(
            EaSession.tenant_id == tenant_id,
            EaSession.status == "open",
            EaSession.created_at >= cutoff,
            last_msg.c.n > 0,
        )
        .order_by(last_msg.c.last_id.desc())
        .limit(1)
    ).scalars().first()
    if row is not None:
        return row

    # No chat yet — fall back to newest open shell session
    return db.execute(
        select(EaSession)
        .where(
            EaSession.tenant_id == tenant_id,
            EaSession.status == "open",
            EaSession.created_at >= cutoff,
        )
        .order_by(EaSession.created_at.desc())
        .limit(1)
    ).scalars().first()


def _mem_get(db, scope: str, limit: int = 40) -> list[dict]:
    rows = db.execute(
        select(EaMemory).where(EaMemory.scope == scope)
        .order_by(EaMemory.updated_at.desc()).limit(limit)
    ).scalars().all()
    return [{"key": r.key, "value": r.value, "updated_at": r.updated_at.isoformat() + "Z"} for r in rows]


def _mem_set(db, scope: str, key: str, value: str):
    key = (key or "")[:120]
    value = (value or "")[:8000]
    # scrub secrets-ish patterns from global
    if scope == "global":
        if re.search(r"(password|api[_-]?key|secret|sk-|Bearer\s)", value, re.I):
            raise HTTPException(400, "Global memory cannot store secrets")
    existing = db.execute(
        select(EaMemory).where(EaMemory.scope == scope, EaMemory.key == key)
    ).scalar_one_or_none()
    if existing:
        existing.value = value
        existing.updated_at = _now()
    else:
        db.add(EaMemory(scope=scope, key=key, value=value))


# ── tools ───────────────────────────────────────────────────────────────────
TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "tenant_census",
            "description": (
                "AUTHORITATIVE inventory from the database for this tenant — every array, "
                "inverter, connection, offtaker, and recent production totals. Use FIRST for "
                "'how many arrays/inverters do I have', 'list my fleet', or when health tools "
                "look incomplete. This is ground truth; fleet_overview health may omit "
                "meter-only arrays."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_names": {
                        "type": "boolean",
                        "description": "Include full name lists (default true)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_tenant",
            "description": (
                "Free-form READ-ONLY investigation of this tenant's data. Pick a resource "
                "and optional filters — reason step-by-step like a data analyst. Resources: "
                "arrays, inverters, offtakers, daily_generation, utility_accounts, "
                "inverter_connections, bills_summary, bills, tenant_pricing. Never invent rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "description": (
                            "arrays | inverters | offtakers | daily_generation | "
                            "utility_accounts | inverter_connections | bills_summary | "
                            "bills | tenant_pricing"
                        ),
                    },
                    "vendor": {"type": "string", "description": "Filter by vendor when relevant"},
                    "array_id": {"type": "integer"},
                    "array_name": {"type": "string", "description": "Name substring"},
                    "status": {"type": "string", "description": "For offtakers: enabled filter"},
                    "days": {
                        "type": "integer",
                        "description": "For daily_generation: lookback days (default 14, max 90)",
                    },
                    "group_by": {
                        "type": "string",
                        "description": "Optional: vendor | array | day | none",
                    },
                    "limit": {"type": "integer", "description": "Max rows (default 100, max 300)"},
                    "question": {
                        "type": "string",
                        "description": "What you're trying to answer (helps shape the response)",
                    },
                },
                "required": ["resource"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "product_map",
            "description": (
                "Authoritative Array Operator product knowledge (support map + "
                "surface mental model). ALWAYS call before explaining Auto-refresh, "
                "cloud vs extension, invoices, analysis, plans, onboarding, OR before "
                "describing what a page/tab is for (use surface / surface_*). "
                "Topics include tabs, system, surface, product_spine, "
                "surface_invoices, surface_inverters, surface_fleet_triage, "
                "surface_analysis, surface_account, surface_resources, and domain topics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "Domain: tabs | system | fleet | capture | vendors | analysis | "
                            "health | offtakers | billing | plans | onboarding | resources | "
                            "status | agent | api | datamodel | glossary | security | tools. "
                            "Surface mental model (macro/meso/micro): surface | product_spine | "
                            "surface_invoices | surface_inverters | surface_fleet_triage | "
                            "surface_analysis | surface_account | surface_resources | "
                            "orientation_playbook | surface_global | anti_hallucination. "
                            "surface = whole-product layout + orientation playbook; "
                            "surface_invoices = Invoices tab purpose+structure; etc. "
                            "Pass 'all' for the topic directory."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_site_improvement",
            "description": (
                "Propose a product/UI change through the self-improving-site pipeline "
                "(same as 'Wish this was better'). An internal JUDGE approves auto-ship "
                "(frontend-only UX), branches riskier work, or passes. Prefer starting the "
                "client mark-up flow (returns ui improve_site) so the user circles the spot. "
                "CRITICAL: put a complete, ready-to-build design brief in text= — the client "
                "auto-fills the Build-it box with it so the user only circles and clicks Build. "
                "Write an imperative prompt (e.g. 'Upgrade the pipeline visualization to live "
                "energy balls flowing to offtakers…'), not just 'user wants something cooler'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "Ready-to-build prompt for the Improve box and the judge. "
                            "Imperative, specific, 1–4 sentences. Include what/where/visual intent."
                        ),
                    },
                    "build_prompt": {
                        "type": "string",
                        "description": "Optional override for the prefilled Build-it box (defaults to text)",
                    },
                    "start_markup": {
                        "type": "boolean",
                        "description": "If true (default), open freeze+circle UI first",
                    },
                    "screenshot_b64": {
                        "type": "string",
                        "description": "Optional marked-up PNG base64 if already captured",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fleet_overview",
            "description": (
                "Full fleet health snapshot from the live fleet-tree (same verdicts as the "
                "Inverters / Fleet Triage UI). Returns each array's alert level, vendor, "
                "today kWh, live power, source/sync freshness, and problem inverters with "
                "diagnosis. Filter with vendor (e.g. 'sma') or needs_attention_only. "
                "For complete inventory counts use tenant_census first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {
                        "type": "string",
                        "description": "Optional vendor filter: sma, solaredge, fronius, chint, locus",
                    },
                    "needs_attention_only": {
                        "type": "boolean",
                        "description": "If true, only arrays with warn/critical alerts",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "investigate_attention",
            "description": (
                "Why arrays need attention — focused investigation. Prefer this when the "
                "user asks 'why do 2 SMA arrays need attention' or similar. Returns ranked "
                "problem arrays with plain-English why + problem inverters + next step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {
                        "type": "string",
                        "description": "Optional: sma, solaredge, fronius, chint, locus",
                    },
                    "array_name": {
                        "type": "string",
                        "description": "Optional name substring to focus on one site",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max problem arrays to return (default 12)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "array_detail",
            "description": (
                "Deep dive on ONE array by id or name: inverters, peer_index, status, "
                "diagnosis, live power, last report, source/sync status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "array_id": {"type": "integer"},
                    "name": {"type": "string", "description": "Array name substring"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_ops_overview",
            "description": (
                "O&M healing overview: service contacts (ops/installer team), array "
                "assignments, open repair tickets, and sites currently down that need "
                "a tech. Use when the user asks who fixes their arrays, repair status, "
                "or 'check in with the electrician'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reconcile": {
                        "type": "boolean",
                        "description": "Refresh tickets from live fleet first (default true)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_service_contacts",
            "description": (
                "List the operator's service/O&M team (installers, electricians, techs) "
                "and which arrays each is assigned to."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_service_contact",
            "description": (
                "Create or update a service contact on the ops team. Set is_default=true "
                "for the fallback tech when an array has no assignment. "
                "Set needs_confirm=false when the user already gave name+email."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer", "description": "Omit to create"},
                    "name": {"type": "string"},
                    "company": {"type": "string"},
                    "role": {
                        "type": "string",
                        "description": "installer|om|electrician|technician|general_contractor|other",
                    },
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "notes": {"type": "string"},
                    "is_default": {"type": "boolean"},
                    "active": {"type": "boolean"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_service_contact",
            "description": (
                "Assign a service contact as primary (or backup) O&M for an array. "
                "Use array_id or array_name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer"},
                    "array_id": {"type": "integer"},
                    "array_name": {"type": "string"},
                    "kind": {"type": "string", "description": "primary|backup (default primary)"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["contact_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_repair_tickets",
            "description": "List repair tickets (active by default) with status and assigned tech.",
            "parameters": {
                "type": "object",
                "properties": {
                    "active_only": {"type": "boolean", "default": True},
                    "array_id": {"type": "integer"},
                    "status": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_repair_ticket",
            "description": (
                "Open a repair ticket for a down array/inverter and assign the ops contact. "
                "Drafts a check-in email; does NOT send until send_repair_checkin."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "array_id": {"type": "integer"},
                    "array_name": {"type": "string"},
                    "inverter_id": {"type": "integer"},
                    "contact_id": {"type": "integer"},
                    "fail_type": {"type": "string", "description": "dead|fault|comm_gap|other"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_repair_ticket",
            "description": (
                "Update ticket status (open|waiting_reply|scheduled|in_progress|resolved|"
                "cancelled), reassign contact, or set tech_note / scheduled_for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "contact_id": {"type": "integer"},
                    "tech_note": {"type": "string"},
                    "description": {"type": "string"},
                    "scheduled_for": {"type": "string", "description": "ISO datetime"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_repair_checkin",
            "description": (
                "Build or refresh the check-in email draft for a repair ticket "
                "(does not send). Returns to/subject/body for review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_repair_checkin",
            "description": (
                "EMAIL the repair check-in to the assigned tech (or owner as forward packet). "
                "Outward communication — set needs_confirm=false only when the user clearly "
                "asked to contact/email/check in with the tech now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_repair_note",
            "description": (
                "Log an inbound status note on a ticket (e.g. tech said parts ordered). "
                "May auto-bump status from keywords (fixed/scheduled/in progress)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "note": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["ticket_id", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_offtakers",
            "description": (
                "List offtaker subscriptions with name, share, email, array_id/array_name, "
                "utility_account_id + nickname/account number (bill source), delivery mode. "
                "Call before rebinding master account / utility source."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_offtaker",
            "description": "Get one offtaker by id or name substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "integer"},
                    "name": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fleet_trends_summary",
            "description": "Trailing production: TTM kWh, lifetime, YoY sketch from fleet-trends data.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "account_summary",
            "description": (
                "Account tab data for THIS tenant — company, operator name, "
                "contact email, plan, subscription/trial status, card on file (yes/no, "
                "not full card number), capture mode, connected utilities, counts. "
                "Use whenever the user asks about Account, Master Account (legacy name), email, company, plan, "
                "or 'what's on my account'. Source of truth is contact_email (not a null email field)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_billing": {
                        "type": "boolean",
                        "description": "Include month-to-date billing snapshot (default true)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "billing_portal_link",
            "description": "Get Stripe customer portal URL for this tenant (open link; never charges).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_pipeline",
            "description": "Invoice send-pipeline snapshot (drafts ready, auto-send, next run).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "portal_links",
            "description": (
                "List vendor + utility portal URLs for THIS tenant (Solar.web, SmartHub, "
                "GMP, Chint, SolarEdge, etc.). Use when the owner asks where to check "
                "today's production, open a monitoring site, or find SmartHub while "
                "inverter data is missing. ALWAYS put the returned markdown links in "
                "your reply so they are clickable in chat. Optionally call open_url "
                "to open one portal in a new browser tab."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "array_name": {
                        "type": "string",
                        "description": "Optional array name to prioritize (e.g. Glover, Danville)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Open an https URL in a new browser tab for the owner (vendor portal, "
                "SmartHub, etc.). Prefer after portal_links. Also put the same URL as a "
                "markdown link in your chat reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "https URL to open"},
                    "label": {"type": "string", "description": "Short link label for chat"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_navigate",
            "description": (
                "Navigate immediately (no confirm). Use USER-FACING tab names in speech; "
                "hashes are internal: Fleet Triage=#dashboard, Inverters=#arrays, "
                "Analysis=#analysis (trends is a sub-view, not a tab), Invoices=#reports, "
                "Resources=#resources, Account=#account. Never call tabs Dashboard/"
                "Arrays/Reports/Account/Trends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": (
                            "#dashboard (Fleet Triage) | #arrays (Inverters) | #analysis | "
                            "#reports (Invoices) | #resources | #account (Account)"
                        ),
                    },
                    "reason": {"type": "string"},
                },
                "required": ["hash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_highlight",
            "description": "Highlight a CSS selector on the page immediately (no confirm). Optionally say a short line while highlighting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "label": {"type": "string"},
                    "say": {"type": "string", "description": "Short narration shown+spoken during highlight"},
                    "ms": {"type": "integer", "description": "Highlight duration ms (default 4500)"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_tour",
            "description": (
                "SHOW-AND-TELL walkthrough: navigates tabs and highlights real UI "
                "elements while narrating. Use for 'walk me through Account', "
                "'show me invoices', etc. Prefer this over a text-only explanation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tour_id": {
                        "type": "string",
                        "description": (
                            "Preset tour for a top-bar tab: master_account|account, "
                            "arrays|inverters, reports|invoices, dashboard|fleet_triage, "
                            "analysis, resources. Prefer presets over custom steps."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "description": "Optional custom steps: {hash?, selector?, say?, ms?}",
                        "items": {"type": "object"},
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_fill",
            "description": "Fill an input on the page. Always needs confirm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "reason": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_click",
            "description": "Click a button/link on the page. Always needs confirm for destructive/save/send.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "reason": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_tenant",
            "description": "Store a short fact in private tenant memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_global_behavior",
            "description": "Store a non-PII behavior tip shared across all Energy Agent instances.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_ford",
            "description": (
                "Escalate to Ford's standing Operator inbox (Grok triage + board at "
                "/admin/escalations). Call whenever unsure or the user has a product gap — "
                "even if they decline. Prefer this over promising email-only follow-up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "user_said": {"type": "string"},
                    "severity": {"type": "boolean", "default": False},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_offtaker",
            "description": (
                "Update one offtaker: share %, email, display name, auto-send, "
                "solar credit / net rates ($/kWh), discount %, AND/OR "
                "rebind the bill source (utility account + master net-meter group). "
                "Identify by subscription_id OR offtaker_name (partial match ok). "
                "CRITICAL: 'master account' / 'utility source' / 'array source' means "
                "array_id + utility_account_id — NOT renaming customer_name. "
                "Only pass name= when the user explicitly wants to rename the offtaker. "
                "share_pct is percent (25) or fraction 0–1. "
                "rate_per_kwh / net_rate_per_kwh are $/kWh solar credit rates. "
                "discount_pct is fraction (0.10 = 10% off) or percent (10). "
                "Pass clear_rate=true to remove a per-offtaker rate override (fall back to master/bill). "
                "Set needs_confirm=false when the user already stated the exact change "
                "(e.g. share to 15%, rate to 0.18). Do not wait for a second 'yes'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "integer"},
                    "offtaker_name": {
                        "type": "string",
                        "description": "Customer/offtaker name when id is unknown",
                    },
                    "email": {"type": "string"},
                    "name": {
                        "type": "string",
                        "description": (
                            "New DISPLAY name for the offtaker only. Do NOT set this when "
                            "the user wants to change master account / utility / array source."
                        ),
                    },
                    "share_pct": {
                        "type": "number",
                        "description": "Share as percent (25) or fraction (0.25). Applied as allocation_pct / array_share_pct.",
                    },
                    "rate_per_kwh": {
                        "type": "number",
                        "description": "Legacy flat solar credit $/kWh for this offtaker (override).",
                    },
                    "net_rate_per_kwh": {
                        "type": "number",
                        "description": (
                            "Net / solar credit rate $/kWh for this offtaker (preferred). "
                            "This is what users mean by 'solar credit rate'."
                        ),
                    },
                    "discount_pct": {
                        "type": "number",
                        "description": "Discount: 0.10 or 10 for 10% off. Null/clear via clear_discount.",
                    },
                    "clear_rate": {
                        "type": "boolean",
                        "description": "If true, clear per-offtaker rate overrides (use master/bill).",
                    },
                    "clear_discount": {
                        "type": "boolean",
                        "description": "If true, clear per-offtaker discount override.",
                    },
                    "auto_send": {"type": "boolean"},
                    "utility_account_id": {
                        "type": "integer",
                        "description": "Bind offtaker to this utility bill source (sub-meter or host).",
                    },
                    "utility_account_name": {
                        "type": "string",
                        "description": (
                            "Resolve utility bill by nickname, account number, or service address "
                            "(e.g. 'Timberworks', 'St J Main St'). Prefer over guessing ids."
                        ),
                    },
                    "array_id": {
                        "type": "integer",
                        "description": "Master net-meter GROUP (array) for allocation cross-check.",
                    },
                    "array_name": {
                        "type": "string",
                        "description": "Resolve master group by array name (partial match ok).",
                    },
                    "master_account": {
                        "type": "string",
                        "description": (
                            "UI 'Master account' dropdown target — utility nickname OR array/"
                            "group name (e.g. Timberworks). Rebinds bill group, does NOT rename."
                        ),
                    },
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_billing_rates",
            "description": (
                "Read THIS tenant's solar credit / billing rates: master global defaults "
                "plus optional one offtaker's override + RESOLVED effective rate "
                "(what invoices actually use). Call when asked about solar credit rates, "
                "net rates, discounts, or offtaker pricing. Prefer offtaker_name for "
                "'Town of Glover' style questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "offtaker_name": {"type": "string"},
                    "subscription_id": {"type": "integer"},
                    "include_all_offtakers": {
                        "type": "boolean",
                        "description": "If true, include rate snapshot for every offtaker (capped).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_billing_rates",
            "description": (
                "Set THIS tenant's MASTER / global solar credit rate and/or default discount. "
                "Affects every offtaker without a per-offtaker override. "
                "For a single offtaker use patch_offtaker instead. "
                "Pass clear_net_rate=true to blank the master (each offtaker uses their bill rate). "
                "Set needs_confirm=false when the user stated the exact $/kWh."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "default_net_rate_per_kwh": {
                        "type": "number",
                        "description": "Master solar credit / net rate $/kWh",
                    },
                    "default_discount_pct": {
                        "type": "number",
                        "description": "Default discount fraction 0.10 or percent 10",
                    },
                    "default_billing_rate_per_kwh": {
                        "type": "number",
                        "description": "Legacy flat global rate $/kWh (optional)",
                    },
                    "clear_net_rate": {"type": "boolean"},
                    "clear_discount": {"type": "boolean"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live public internet (news, utility policy, weather context, "
                "vendor docs, rates, regulations). Use for questions OUTSIDE this tenant's "
                "database — e.g. 'Vermont net metering 2026', 'SMA ennexOS error code', "
                "'GMP solar credit rates'. Do NOT use for this account's arrays/offtakers/"
                "kWh (use tenant_census / query_tenant / fleet tools). Cite title + URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (plain English or keywords)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "How many results (1–8, default 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch and extract readable text from a public HTTPS URL (after web_search "
                "or when the user pastes a link). Use for policy PDFs pages, utility pages, "
                "vendor help. Never fetch internal/private hosts. Max ~40k chars extracted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Public http(s) URL to fetch",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters of text to return (default 12000, max 40000)",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


def _slim_inverter(inv: dict) -> dict:
    """Compact inverter row for agent tools (no sparkline series)."""
    return {
        "inverter_id": inv.get("inverter_id"),
        "sn": inv.get("sn"),
        "name": inv.get("name"),
        "model": inv.get("model"),
        "vendor": inv.get("vendor"),
        "nameplate_kw": inv.get("nameplate_kw"),
        "status": inv.get("status") or "ok",
        "diagnosis": inv.get("diagnosis"),
        "peer_index": inv.get("peer_index"),
        "window_kwh": inv.get("window_kwh"),
        "produced_today_kwh": inv.get("produced_today_kwh"),
        "current_power_w": inv.get("current_power_w"),
        "last_report": inv.get("last_report"),
        "no_energy_register": bool(inv.get("no_energy_register")),
        "last_mode": inv.get("last_mode"),
    }


def _explain_array_attention(col: dict) -> str:
    """Plain-English why this array is flagged (for the agent to speak)."""
    alert = col.get("alert") or {}
    level = alert.get("level") or "ok"
    status = alert.get("status") or "ok"
    headline = alert.get("headline") or ""
    src = col.get("source_status") or {}
    sync = col.get("sync_status") or {}
    bits = []
    if level in ("warn", "critical") or (alert.get("count") or 0) > 0:
        bits.append(headline or f"worst inverter status: {status}")
        n = alert.get("count") or 0
        if n:
            bits.append(f"{n} inverter(s) flagged")
    src_state = (src.get("state") or "").lower()
    if src_state in ("stale", "dark", "offline"):
        age = src.get("age_hours")
        age_s = f" (~{age:.0f}h old)" if isinstance(age, (int, float)) else ""
        bits.append(f"source data {src_state}{age_s}")
    elif src_state == "unpolled":
        bits.append(
            "no recent browser capture (SMA/Fronius/Chint only update when the "
            "extension is open and signed in)"
        )
    if sync.get("age_min") is not None and float(sync["age_min"]) > 24 * 60:
        bits.append(f"last Array Operator sync ~{float(sync['age_min']) / 60:.0f}h ago")
    if col.get("produced_today_kwh") in (None, 0) and col.get("is_daylight"):
        bits.append("no measured production today while sun is up")
    bad = [
        inv for inv in (col.get("inverters") or [])
        if (inv.get("status") or "ok") not in ("ok",) or inv.get("no_energy_register")
    ]
    for inv in bad[:4]:
        label = inv.get("name") or inv.get("sn") or "inverter"
        if inv.get("no_energy_register"):
            bits.append(f"{label}: live power but no energy register / history")
        elif inv.get("diagnosis"):
            bits.append(f"{label}: {inv.get('diagnosis')}")
        elif inv.get("status") and inv.get("status") != "ok":
            bits.append(f"{label}: {inv.get('status')}")
    if not bits:
        return "No attention flags on this array right now."
    # de-dupe while preserving order
    seen = set()
    out = []
    for b in bits:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return "; ".join(out)


def _array_needs_attention(col: dict) -> bool:
    alert = col.get("alert") or {}
    if (alert.get("level") or "ok") in ("warn", "critical"):
        return True
    if (alert.get("count") or 0) > 0:
        return True
    src = (col.get("source_status") or {}).get("state") or ""
    if src in ("stale", "dark", "offline"):
        return True
    for inv in col.get("inverters") or []:
        if (inv.get("status") or "ok") not in ("ok",):
            return True
        if inv.get("no_energy_register"):
            return True
    return False


def _next_step_for_array(col: dict) -> str:
    vendors = [str(v).lower() for v in (col.get("vendors") or []) if v]
    if col.get("vendor"):
        vendors.append(str(col["vendor"]).lower())
    vendors = list(dict.fromkeys(vendors))
    src = (col.get("source_status") or {}).get("state") or ""
    alert_st = (col.get("alert") or {}).get("status") or "ok"
    ext = {"sma", "fronius", "chint"}
    if vendors and set(vendors).issubset(ext) and src in ("unpolled", "stale", "none", ""):
        brand = vendors[0].upper() if vendors else "the vendor"
        return (
            f"Open Inverters → this array → Log in with {brand} so the extension "
            "captures a fresh snapshot; SMA/Fronius/Chint only refresh while a "
            "signed-in browser with the helper is open."
        )
    if alert_st in ("fault", "error", "dead"):
        return (
            "Open the inverter detail / vendor portal from the array card, check "
            "fault codes, open a repair ticket + check in with the assigned O&M contact "
            "(repair_ops_overview), and draft a manufacturer warranty claim if it's dead "
            "with loss evidence."
        )
    if alert_st in ("underperforming", "comm_gap"):
        return (
            "Compare peer index vs siblings on this site; if one unit is lagging, "
            "inspect wiring/shading or open the vendor portal for that serial."
        )
    if src in ("stale", "dark", "offline"):
        return "Vendor source looks offline — check the monitoring portal and site connectivity."
    return "Open #arrays, focus this site, and review the flagged inverters."


def _fleet_tree_columns(db, tenant: Tenant) -> tuple[list[dict], dict]:
    """Shared loader: live fleet-tree columns + summary (stable verdicts = UI/email)."""
    try:
        from . import inverter_fleet
        tree = inverter_fleet.build_fleet_tree(
            db, tenant, force_refresh=False, stable_verdicts=True,
        )
        return list(tree.get("columns") or []), dict(tree.get("summary") or {})
    except Exception as e:
        log.exception("energy_agent fleet tree failed")
        return [], {"error": str(e)}


def _summarize_column(col: dict) -> dict:
    bad = [
        _slim_inverter(inv)
        for inv in (col.get("inverters") or [])
        if (inv.get("status") or "ok") not in ("ok",) or inv.get("no_energy_register")
    ]
    needs = _array_needs_attention(col)
    return {
        "id": col.get("array_id"),
        "name": col.get("array_name"),
        "vendor": col.get("vendor"),
        "vendors": col.get("vendors") or ([col["vendor"]] if col.get("vendor") else []),
        "inverter_count": col.get("inverter_count"),
        "current_power_w": col.get("current_power_w"),
        "produced_today_kwh": col.get("produced_today_kwh"),
        "produced_today_source": col.get("produced_today_source"),
        "is_daylight": col.get("is_daylight"),
        "alert": col.get("alert"),
        "source_status": col.get("source_status"),
        "sync_status": col.get("sync_status"),
        "needs_attention": needs,
        "why": _explain_array_attention(col) if needs else "All clear",
        "next_step": _next_step_for_array(col) if needs else None,
        "problem_inverters": bad,
        "problem_inverter_count": len(bad),
    }


def _match_vendor(col: dict, vendor: str | None) -> bool:
    if not vendor:
        return True
    v = vendor.strip().lower()
    if not v:
        return True
    aliases = {
        "se": "solaredge", "solar edge": "solaredge",
        "cps": "chint", "chint/cps": "chint",
    }
    v = aliases.get(v, v)
    vendors = [str(x).lower() for x in (col.get("vendors") or []) if x]
    if col.get("vendor"):
        vendors.append(str(col["vendor"]).lower())
    return any(v == x or v in x or x in v for x in vendors)


def _fleet_overview_tool(db, tenant: Tenant, args: dict) -> dict:
    cols, summary = _fleet_tree_columns(db, tenant)
    if summary.get("error") and not cols:
        return {
            "error": summary["error"],
            "arrays": [],
            "count": 0,
            "hint": "fleet-tree failed; escalate if this keeps happening",
        }
    vendor = (args.get("vendor") or "").strip() or None
    only_attn = bool(args.get("needs_attention_only"))
    arrays = []
    for col in cols:
        if not _match_vendor(col, vendor):
            continue
        row = _summarize_column(col)
        if only_attn and not row["needs_attention"]:
            continue
        arrays.append(row)
    attention = [a for a in arrays if a["needs_attention"]]
    return {
        "summary": {
            **summary,
            "arrays_returned": len(arrays),
            "attention_in_result": len(attention),
            "vendor_filter": vendor,
            "needs_attention_only": only_attn,
        },
        "attention_arrays": attention,
        "arrays": arrays,
        "count": len(arrays),
        "note": (
            "Health uses the same stable_verdicts as the dashboard and morning digest. "
            "For SMA/Fronius/Chint, stale often means no recent extension capture — not "
            "necessarily a dead inverter."
        ),
    }


def _investigate_attention_tool(db, tenant: Tenant, args: dict) -> dict:
    cols, summary = _fleet_tree_columns(db, tenant)
    if summary.get("error") and not cols:
        return {"error": summary["error"], "problems": [], "count": 0}
    vendor = (args.get("vendor") or "").strip() or None
    name_q = (args.get("array_name") or args.get("name") or "").strip().lower()
    try:
        limit = int(args.get("limit") or 12)
    except (TypeError, ValueError):
        limit = 12
    limit = max(1, min(limit, 40))

    problems = []
    for col in cols:
        if not _match_vendor(col, vendor):
            continue
        if name_q and name_q not in str(col.get("array_name") or "").lower():
            continue
        if not _array_needs_attention(col):
            continue
        row = _summarize_column(col)
        # Full inverter list for the investigation (not only bad ones)
        row["all_inverters"] = [
            _slim_inverter(inv) for inv in (col.get("inverters") or [])
        ]
        problems.append(row)

    # Rank: critical first, then warn, then by problem inverter count
    rank = {"critical": 0, "warn": 1, "ok": 2}

    def _key(r):
        lvl = ((r.get("alert") or {}).get("level") or "ok")
        return (rank.get(lvl, 9), -(r.get("problem_inverter_count") or 0), r.get("name") or "")

    problems.sort(key=_key)
    problems = problems[:limit]

    # Spoken-ready brief for the model
    lines = []
    for p in problems:
        lines.append(
            f"• {p.get('name')} ({', '.join(p.get('vendors') or []) or 'unknown vendor'}): "
            f"{p.get('why')} → {p.get('next_step')}"
        )
    brief = "\n".join(lines) if lines else (
        "No arrays currently need attention"
        + (f" for vendor={vendor}" if vendor else "")
        + (f" matching '{name_q}'" if name_q else "")
        + "."
    )

    return {
        "count": len(problems),
        "fleet_summary": summary,
        "vendor_filter": vendor,
        "problems": problems,
        "brief": brief,
        "instruction_for_agent": (
            "Answer the user with array NAMES and the why/next_step from each problem. "
            "Do not ask them for IDs. If count is 0, say the fleet looks clear right now "
            "and offer to open #arrays so they can double-check."
        ),
    }


def _array_detail_tool(db, tenant: Tenant, args: dict) -> dict:
    cols, _summary = _fleet_tree_columns(db, tenant)
    if not cols:
        return {"error": "no fleet columns", "array": None}
    aid = args.get("array_id")
    name_q = (args.get("name") or args.get("array_name") or "").strip().lower()
    match = None
    if aid is not None:
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            return {"error": f"invalid array_id: {aid}"}
        for col in cols:
            if col.get("array_id") == aid:
                match = col
                break
    elif name_q:
        matches = [
            c for c in cols
            if name_q in str(c.get("array_name") or "").lower()
        ]
        if not matches:
            return {
                "error": f"no array matching '{name_q}'",
                "candidates": [
                    {"id": c.get("array_id"), "name": c.get("array_name"), "vendor": c.get("vendor")}
                    for c in cols[:30]
                ],
            }
        if len(matches) > 1:
            exact = [c for c in matches if str(c.get("array_name") or "").lower() == name_q]
            if len(exact) == 1:
                matches = exact
            else:
                return {
                    "error": "multiple arrays match — pass array_id",
                    "matches": [
                        {"id": c.get("array_id"), "name": c.get("array_name"), "vendor": c.get("vendor")}
                        for c in matches[:12]
                    ],
                }
        match = matches[0]
    else:
        return {"error": "pass array_id or name"}

    if match is None:
        return {"error": "array not found", "array": None}
    row = _summarize_column(match)
    row["all_inverters"] = [_slim_inverter(inv) for inv in (match.get("inverters") or [])]
    row["reminder"] = match.get("reminder")
    row["portfolio_name"] = match.get("portfolio_name")
    row["origin_links"] = match.get("origin_links")
    return {"array": row, "needs_attention": row["needs_attention"], "why": row["why"]}


# ── Free-mind data plane (tenant-scoped read-only reasoning) ─────────────────
# Authoritative support knowledge lives in energy_agent_support_map.md (## topics).
# Coding/ops agents still use skill solar-operator-energyagent — that skill points
# here for product behavior the in-app agent must explain correctly.

_SUPPORT_MAP_PATH = Path(__file__).with_name("energy_agent_support_map.md")
_SURFACE_MODEL_PATH = Path(__file__).with_name("energy_agent_surface_model.md")
_PRODUCT_MAP_CACHE: dict[str, str] | None = None
_PRODUCT_MAP_MTIME: float | None = None

# Minimal emergency fallback if the markdown file is missing at runtime.
_PRODUCT_MAP_FALLBACK: dict[str, str] = {
    "tabs": (
        "TOP NAV labels: Fleet Triage (#dashboard), Inverters (#arrays), "
        "Analysis (#analysis; trends is a sub-view), Invoices (#reports), "
        "Resources (#resources), Account (#account). Never say Dashboard/Arrays/Reports/Trends as top tabs."
    ),
    "system": (
        "Array Operator (arrayoperator.com) = EnergyAgent owner product. "
        "Tenant → Arrays → Inverters; bills → offtaker invoices. "
        "Auto-refresh cloud vs device is password path; API keys are separate."
    ),
    "capture": (
        "Auto-refresh: cloud = store passwords, harvester 24/7; device = extension vault. "
        "PLUS extension one-click Log-in-with capture attaches SMA/Fronius/Chint without a cloud vault row. "
        "SolarEdge usually API keys; never equate fleet vendors with cloud_capture.logins only."
    ),
}


def _parse_support_map_md(text: str) -> dict[str, str]:
    """Split energy_agent_support_map.md into {topic: body} on ## headings."""
    topics: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                topics[current] = "\n".join(buf).strip()
            current = line[3:].strip().lower().split()[0]  # first word = topic id
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current:
        topics[current] = "\n".join(buf).strip()
    return {k: v for k, v in topics.items() if v}


def load_product_map(*, force: bool = False) -> dict[str, str]:
    """Load support topics + surface mental model (mtime-aware cache).

    Support map = domain mechanics. Surface model = macro/meso/micro page atlas
    (why each tab exists, user goals, real controls, nav graph).
    """
    global _PRODUCT_MAP_CACHE, _PRODUCT_MAP_MTIME
    mtimes: list[float] = []
    for p in (_SUPPORT_MAP_PATH, _SURFACE_MODEL_PATH):
        try:
            mtimes.append(p.stat().st_mtime)
        except OSError:
            pass
    mtime = max(mtimes) if mtimes else None
    if (
        _PRODUCT_MAP_CACHE is not None
        and not force
        and mtime is not None
        and mtime == _PRODUCT_MAP_MTIME
    ):
        return _PRODUCT_MAP_CACHE
    try:
        raw = _SUPPORT_MAP_PATH.read_text(encoding="utf-8")
        parsed = _parse_support_map_md(raw)
        if not parsed:
            raise ValueError("no ## topics in support map")
        # Merge surface atlas topics (product_spine, surface_invoices, …)
        try:
            surf = _SURFACE_MODEL_PATH.read_text(encoding="utf-8")
            for k, v in _parse_support_map_md(surf).items():
                parsed[k] = v
            # Convenience alias: product_map(topic=surface) → full spine + playbook
            if "product_spine" in parsed:
                bits = [parsed["product_spine"]]
                for key in (
                    "orientation_playbook",
                    "anti_hallucination",
                    "surface_global",
                ):
                    if key in parsed:
                        bits.append(parsed[key])
                parsed["surface"] = "\n\n".join(bits)
        except OSError as se:
            log.warning("surface model missing (%s)", se)
        _PRODUCT_MAP_CACHE = parsed
        _PRODUCT_MAP_MTIME = mtime
        return parsed
    except Exception as exc:
        log.warning("energy_agent support map load failed (%s) — using fallback", exc)
        _PRODUCT_MAP_CACHE = dict(_PRODUCT_MAP_FALLBACK)
        _PRODUCT_MAP_MTIME = mtime
        return _PRODUCT_MAP_CACHE


# Eager load so import surfaces a missing map early (falls back if needed).
PRODUCT_MAP = load_product_map()


def _tenant_census_tool(db, tenant: Tenant, args: dict) -> dict:
    """Ground-truth inventory from ORM — not filtered fleet-tree."""
    from sqlalchemy import func
    from sqlalchemy.orm import selectinload
    from .models import (
        BillingReportSubscription, DailyGeneration, Inverter, InverterConnection,
        UtilityAccount,
    )

    tid = tenant.id
    include_names = args.get("include_names", True)
    if include_names is None:
        include_names = True

    arrays = db.execute(
        select(Array).options(selectinload(Array.client)).where(
            Array.tenant_id == tid, Array.deleted_at.is_(None),
        ).order_by(Array.id)
    ).scalars().all()
    array_ids = [a.id for a in arrays]

    invs = db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tid, Inverter.deleted_at.is_(None),
        ).order_by(Inverter.array_id, Inverter.position, Inverter.id)
    ).scalars().all() if True else []

    conns = db.execute(
        select(InverterConnection).where(
            InverterConnection.tenant_id == tid,
        )
    ).scalars().all() if hasattr(InverterConnection, "tenant_id") else []
    # Some schemas key connections by array only
    if not conns and array_ids:
        try:
            conns = db.execute(
                select(InverterConnection).where(
                    InverterConnection.array_id.in_(array_ids),
                )
            ).scalars().all()
        except Exception:
            conns = []

    util = db.execute(
        select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalars().all() if hasattr(UtilityAccount, "deleted_at") else db.execute(
        select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
    ).scalars().all()

    offtaker_q = select(BillingReportSubscription).where(
        BillingReportSubscription.tenant_id == tid,
    )
    if hasattr(BillingReportSubscription, "deleted_at"):
        offtaker_q = offtaker_q.where(BillingReportSubscription.deleted_at.is_(None))
    offtakers = db.execute(offtaker_q).scalars().all()

    # Recent production (7d)
    since = (_now().date() - timedelta(days=7))
    recent_kwh = 0.0
    if array_ids:
        recent_kwh = float(db.execute(
            select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0)).where(
                DailyGeneration.array_id.in_(array_ids),
                DailyGeneration.day >= since,
            )
        ).scalar() or 0.0)

    # Per-array inverter counts + vendor mix
    inv_by_array: dict[int, list] = {}
    vendor_counts: dict[str, int] = {}
    for iv in invs:
        inv_by_array.setdefault(iv.array_id, []).append(iv)
        v = (iv.vendor or "unknown").lower()
        vendor_counts[v] = vendor_counts.get(v, 0) + 1

    conn_by_array: dict[int, list] = {}
    for c in conns:
        conn_by_array.setdefault(c.array_id, []).append(c)

    util_array_ids = {u.array_id for u in util if getattr(u, "array_id", None)}

    array_rows = []
    for a in arrays:
        ivs_a = inv_by_array.get(a.id, [])
        conns_a = conn_by_array.get(a.id, [])
        vendors = sorted({(iv.vendor or "").lower() for iv in ivs_a if iv.vendor})
        if not vendors:
            vendors = sorted({(c.vendor or "").lower() for c in conns_a if getattr(c, "vendor", None)})
        if not vendors and getattr(a, "solaredge_site_id", None):
            vendors = ["solaredge"]
        kind = "inverter" if ivs_a or conns_a or getattr(a, "solaredge_site_id", None) else (
            "meter_only" if a.id in util_array_ids else "empty"
        )
        row = {
            "id": a.id,
            "name": a.name,
            "client": a.client.name if a.client else None,
            "nameplate_kw": getattr(a, "nameplate_kw", None) or getattr(a, "capacity_kw", None),
            "vendors": vendors,
            "inverter_count": len(ivs_a),
            "connection_count": len(conns_a),
            "has_utility_meter": a.id in util_array_ids,
            "kind": kind,
            "excluded": bool(getattr(a, "excluded", False)),
            "solaredge_site_id": getattr(a, "solaredge_site_id", None),
        }
        array_rows.append(row)

    inv_rows = []
    if include_names:
        for iv in invs[:400]:
            inv_rows.append({
                "id": iv.id,
                "array_id": iv.array_id,
                "name": iv.name or iv.serial,
                "serial": iv.serial,
                "vendor": iv.vendor,
                "model": iv.model,
                "nameplate_kw": getattr(iv, "nameplate_kw", None),
                "last_seen_at": (
                    iv.last_seen_at.isoformat() + "Z"
                    if getattr(iv, "last_seen_at", None) else None
                ),
            })

    offtaker_rows = []
    if include_names:
        for s in offtakers[:300]:
            share = getattr(s, "array_share_pct", None)
            if share is None:
                share = getattr(s, "allocation_pct", None)
            if share is not None and float(share) <= 1:
                share = round(float(share) * 100, 4)
            offtaker_rows.append({
                "id": s.id,
                "name": getattr(s, "customer_name", None),
                "email": getattr(s, "client_email", None),
                "array_id": getattr(s, "array_id", None),
                "utility_account_id": getattr(s, "utility_account_id", None),
                "share_pct": share,
                "enabled": getattr(s, "enabled", None),
            })

    kind_counts = {"inverter": 0, "meter_only": 0, "empty": 0}
    for r in array_rows:
        kind_counts[r["kind"]] = kind_counts.get(r["kind"], 0) + 1

    return {
        "tenant_id": tid,
        "company": getattr(tenant, "company_name", None) or getattr(tenant, "name", None),
        "email": getattr(tenant, "contact_email", None),
        "operator_name": getattr(tenant, "operator_name", None),
        "counts": {
            "arrays": len(array_rows),
            "arrays_inverter_backed": kind_counts.get("inverter", 0),
            "arrays_meter_only": kind_counts.get("meter_only", 0),
            "arrays_empty": kind_counts.get("empty", 0),
            "inverters": len(invs),
            "inverter_connections": len(conns),
            "utility_accounts": len(util),
            "offtakers": len(offtakers),
            "offtakers_enabled": sum(1 for s in offtakers if getattr(s, "enabled", True)),
        },
        "inverters_by_vendor": vendor_counts,
        "production_last_7d_kwh": round(recent_kwh, 1),
        "arrays": array_rows if include_names else None,
        "inverters": inv_rows if include_names else None,
        "offtakers": offtaker_rows if include_names else None,
        "notes": [
            "This is database ground truth for THIS tenant only.",
            "fleet_overview health tree may list fewer arrays (skips pure meter-only).",
            "If the UI shows more than this census, session may be a different tenant — check account_summary.",
        ],
    }


def _query_tenant_tool(db, tenant: Tenant, args: dict) -> dict:
    """Structured read-only investigation across allowlisted resources."""
    from sqlalchemy import func
    from .models import (
        BillingReportSubscription, DailyGeneration, Inverter, InverterConnection,
        UtilityAccount,
    )

    tid = tenant.id
    resource = (args.get("resource") or "").strip().lower()
    vendor = (args.get("vendor") or "").strip().lower() or None
    array_id = args.get("array_id")
    array_name = (args.get("array_name") or "").strip().lower() or None
    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 300))
    try:
        days = int(args.get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 90))
    group_by = (args.get("group_by") or "none").strip().lower()
    question = (args.get("question") or "").strip()

    # Resolve array_name → id if needed
    if array_name and array_id is None:
        for a in db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all():
            if array_name in (a.name or "").lower():
                array_id = a.id
                break

    if resource == "arrays":
        rows = []
        for a in db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
            .order_by(Array.id)
        ).scalars().all():
            if array_id is not None and a.id != int(array_id):
                continue
            if array_name and array_name not in (a.name or "").lower():
                continue
            se = bool(getattr(a, "solaredge_site_id", None))
            if vendor == "solaredge" and not se:
                # still include if has SE inverters — checked below cheaper path
                pass
            rows.append({
                "id": a.id,
                "name": a.name,
                "nameplate_kw": getattr(a, "nameplate_kw", None) or getattr(a, "capacity_kw", None),
                "solaredge_site_id": getattr(a, "solaredge_site_id", None),
                "portfolio_name": getattr(a, "portfolio_name", None),
                "excluded": bool(getattr(a, "excluded", False)),
            })
        # Optional vendor filter via inverter presence
        if vendor:
            invs = db.execute(
                select(Inverter.array_id).where(
                    Inverter.tenant_id == tid,
                    Inverter.deleted_at.is_(None),
                    Inverter.vendor.ilike(f"%{vendor}%"),
                ).distinct()
            ).scalars().all()
            allow = set(invs)
            if vendor in ("solaredge", "se"):
                allow |= {r["id"] for r in rows if r.get("solaredge_site_id")}
            rows = [r for r in rows if r["id"] in allow]
        return {
            "resource": "arrays",
            "question": question or None,
            "count": len(rows),
            "rows": rows[:limit],
        }

    if resource == "inverters":
        q = select(Inverter).where(
            Inverter.tenant_id == tid, Inverter.deleted_at.is_(None),
        )
        if array_id is not None:
            q = q.where(Inverter.array_id == int(array_id))
        if vendor:
            q = q.where(Inverter.vendor.ilike(f"%{vendor}%"))
        q = q.order_by(Inverter.array_id, Inverter.position).limit(limit)
        invs = db.execute(q).scalars().all()
        # names of arrays for readability
        arr_names = {
            a.id: a.name for a in db.execute(
                select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
            ).scalars().all()
        }
        rows = [{
            "id": iv.id,
            "array_id": iv.array_id,
            "array_name": arr_names.get(iv.array_id),
            "name": iv.name or iv.serial,
            "serial": iv.serial,
            "vendor": iv.vendor,
            "model": iv.model,
            "nameplate_kw": getattr(iv, "nameplate_kw", None),
            "last_seen_at": (
                iv.last_seen_at.isoformat() + "Z"
                if getattr(iv, "last_seen_at", None) else None
            ),
        } for iv in invs]
        if group_by == "vendor":
            g: dict[str, int] = {}
            for r in rows:
                v = (r.get("vendor") or "unknown").lower()
                g[v] = g.get(v, 0) + 1
            return {"resource": "inverters", "group_by": "vendor", "counts": g, "sample": rows[:20]}
        if group_by == "array":
            g = {}
            for r in rows:
                k = f"{r.get('array_id')}:{r.get('array_name')}"
                g[k] = g.get(k, 0) + 1
            return {"resource": "inverters", "group_by": "array", "counts": g, "sample": rows[:20]}
        return {"resource": "inverters", "question": question or None, "count": len(rows), "rows": rows}

    if resource == "offtakers":
        q = select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tid,
        )
        if hasattr(BillingReportSubscription, "deleted_at"):
            q = q.where(BillingReportSubscription.deleted_at.is_(None))
        if array_id is not None:
            q = q.where(BillingReportSubscription.array_id == int(array_id))
        subs = db.execute(q.order_by(BillingReportSubscription.id).limit(limit)).scalars().all()
        pricing_ctx = None
        try:
            from .billing.delivery import build_pricing_ctx
            pricing_ctx = build_pricing_ctx(db, tenant)
        except Exception:
            pass
        rows = []
        for s in subs:
            share = getattr(s, "array_share_pct", None)
            if share is None:
                share = getattr(s, "allocation_pct", None)
            if share is not None and float(share) <= 1:
                share = round(float(share) * 100, 4)
            row = {
                "id": s.id,
                "name": getattr(s, "customer_name", None),
                "email": getattr(s, "client_email", None),
                "array_id": getattr(s, "array_id", None),
                "utility_account_id": getattr(s, "utility_account_id", None),
                "share_pct": share,
                "enabled": getattr(s, "enabled", None),
                "delivery_mode": getattr(s, "delivery_mode", None),
            }
            row.update(_offtaker_rate_fields(db, tenant, s, pricing_ctx=pricing_ctx))
            rows.append(row)
        return {"resource": "offtakers", "count": len(rows), "rows": rows}

    if resource == "daily_generation":
        arrs = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        arr_ids = [a.id for a in arrs]
        if array_id is not None:
            arr_ids = [int(array_id)] if int(array_id) in arr_ids else []
        if not arr_ids:
            return {"resource": "daily_generation", "count": 0, "rows": [], "total_kwh": 0}
        since = (_now().date() - timedelta(days=days))
        name_by_id = {a.id: a.name for a in arrs}
        if group_by == "array":
            rows = []
            for aid, kwh in db.execute(
                select(DailyGeneration.array_id, func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
                .where(DailyGeneration.array_id.in_(arr_ids), DailyGeneration.day >= since)
                .group_by(DailyGeneration.array_id)
            ).all():
                rows.append({
                    "array_id": aid,
                    "array_name": name_by_id.get(aid),
                    "kwh": round(float(kwh or 0), 1),
                    "days": days,
                })
            rows.sort(key=lambda r: -r["kwh"])
            return {
                "resource": "daily_generation",
                "group_by": "array",
                "days": days,
                "total_kwh": round(sum(r["kwh"] for r in rows), 1),
                "rows": rows[:limit],
            }
        # day-level series (fleet total)
        day_rows = []
        for day, kwh in db.execute(
            select(DailyGeneration.day, func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
            .where(DailyGeneration.array_id.in_(arr_ids), DailyGeneration.day >= since)
            .group_by(DailyGeneration.day)
            .order_by(DailyGeneration.day.desc())
            .limit(limit)
        ).all():
            day_rows.append({"day": day.isoformat() if hasattr(day, "isoformat") else str(day),
                             "kwh": round(float(kwh or 0), 1)})
        return {
            "resource": "daily_generation",
            "days": days,
            "total_kwh": round(sum(r["kwh"] for r in day_rows), 1),
            "rows": day_rows,
        }

    if resource == "utility_accounts":
        q = select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
        if hasattr(UtilityAccount, "deleted_at"):
            q = q.where(UtilityAccount.deleted_at.is_(None))
        accts = db.execute(q.limit(limit)).scalars().all()
        rows = [{
            "id": u.id,
            "provider": getattr(u, "provider", None),
            "account_number": getattr(u, "account_number", None) or getattr(u, "acct_number", None),
            "nickname": getattr(u, "nickname", None),
            "array_id": getattr(u, "array_id", None),
            "service_address": getattr(u, "service_address", None),
            "label": (
                (getattr(u, "nickname", None) or "").strip()
                or f"{getattr(u, 'provider', '')} {getattr(u, 'account_number', None) or ''}".strip()
            ),
        } for u in accts]
        return {"resource": "utility_accounts", "count": len(rows), "rows": rows}

    if resource == "inverter_connections":
        try:
            q = select(InverterConnection)
            if hasattr(InverterConnection, "tenant_id"):
                q = q.where(InverterConnection.tenant_id == tid)
            else:
                arr_ids = [a.id for a in db.execute(
                    select(Array.id).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
                ).scalars().all()]
                q = q.where(InverterConnection.array_id.in_(arr_ids or [-1]))
            if array_id is not None:
                q = q.where(InverterConnection.array_id == int(array_id))
            if vendor:
                q = q.where(InverterConnection.vendor.ilike(f"%{vendor}%"))
            conns = db.execute(q.limit(limit)).scalars().all()
        except Exception as e:
            return {"resource": "inverter_connections", "error": str(e), "rows": []}
        rows = [{
            "id": c.id,
            "array_id": c.array_id,
            "vendor": getattr(c, "vendor", None),
            "status": getattr(c, "status", None),
            "site_id": (getattr(c, "config", None) or {}).get("site_id")
            if isinstance(getattr(c, "config", None), dict) else None,
        } for c in conns]
        return {"resource": "inverter_connections", "count": len(rows), "rows": rows}

    if resource == "bills_summary":
        # Lightweight: count utility accounts + offtakers + 30d generation
        census = _tenant_census_tool(db, tenant, {"include_names": False})
        gen = _query_tenant_tool(db, tenant, {
            "resource": "daily_generation", "days": 30, "group_by": "array", "limit": 50,
        })
        return {
            "resource": "bills_summary",
            "counts": census.get("counts"),
            "production_last_30d_by_array": gen.get("rows"),
            "production_last_30d_total_kwh": gen.get("total_kwh"),
            "question": question or None,
        }

    if resource == "tenant_pricing":
        return {
            "resource": "tenant_pricing",
            "question": question or None,
            **_tenant_global_rates(tenant),
        }

    if resource == "bills":
        from .models import Bill
        q = select(Bill).where(Bill.tenant_id == tid)
        if array_id is not None and hasattr(Bill, "array_id"):
            q = q.where(Bill.array_id == int(array_id))
        # Newest first
        if hasattr(Bill, "period_end"):
            try:
                q = q.order_by(Bill.period_end.desc().nulls_last(), Bill.id.desc())
            except Exception:
                q = q.order_by(Bill.id.desc())
        else:
            q = q.order_by(Bill.id.desc())
        bills = db.execute(q.limit(limit)).scalars().all()
        rows = []
        for b in bills:
            avg_cents = getattr(b, "avg_rate_cents_kwh", None)
            solar_usd = getattr(b, "solar_credit_usd", None) or getattr(b, "net_credit", None)
            excess = getattr(b, "kwh_sent_to_grid", None)
            implied = None
            try:
                if solar_usd is not None and excess and float(excess) > 0:
                    implied = round(float(solar_usd) / float(excess), 6)
            except Exception:
                pass
            rows.append({
                "id": getattr(b, "id", None),
                "utility_account_id": getattr(b, "utility_account_id", None) or getattr(b, "account_id", None),
                "array_id": getattr(b, "array_id", None),
                "period_start": (
                    b.period_start.isoformat() if getattr(b, "period_start", None) else None
                ),
                "period_end": (
                    b.period_end.isoformat() if getattr(b, "period_end", None) else None
                ),
                "kwh_generated": getattr(b, "kwh_generated", None),
                "kwh_sent_to_grid": excess,
                "solar_credit_usd": solar_usd,
                "implied_solar_credit_rate_per_kwh": implied,
                "avg_rate_cents_kwh": avg_cents,
                "avg_rate_usd_per_kwh": (
                    round(float(avg_cents) / 100.0, 6) if avg_cents is not None else None
                ),
                "total_cost": getattr(b, "total_cost", None),
                "provider": getattr(b, "provider", None) or getattr(b, "supplier", None),
            })
        return {
            "resource": "bills",
            "count": len(rows),
            "rows": rows,
            "question": question or None,
            "note": (
                "implied_solar_credit_rate_per_kwh = solar_credit_usd / excess kWh when both present; "
                "offtaker invoice rates also via get_billing_rates / list_offtakers."
            ),
        }

    return {
        "error": f"unknown resource '{resource}'",
        "allowed": [
            "arrays", "inverters", "offtakers", "daily_generation",
            "utility_accounts", "inverter_connections", "bills_summary",
            "bills", "tenant_pricing",
        ],
    }


def _tenant_global_rates(tenant: Tenant) -> dict:
    net = getattr(tenant, "default_net_rate_per_kwh", None)
    disc = getattr(tenant, "default_discount_pct", None)
    flat = getattr(tenant, "default_billing_rate_per_kwh", None)
    try:
        from .billing.delivery import DEFAULT_DISCOUNT
        default_disc = DEFAULT_DISCOUNT
    except Exception:
        default_disc = 0.10
    if net is not None and float(net) > 0:
        note = (
            f"Master net/solar credit rate is set at ${float(net):.5f}/kWh — "
            "offtakers without a custom override all use it (minus discount)."
        )
        src = "global"
    else:
        note = (
            "Master rate is blank — each offtaker uses solar credit from their "
            "own bound utility bill (or schedule), not a single fleet number."
        )
        src = "per_offtaker_bill"
    return {
        "default_net_rate_per_kwh": net,
        "default_discount_pct": disc,
        "default_billing_rate_per_kwh": flat,
        "effective_discount_pct": disc if disc is not None else default_disc,
        "master_rate_source": src,
        "note": note,
    }


def _offtaker_rate_fields(db, tenant: Tenant, sub, pricing_ctx=None) -> dict:
    """Per-offtaker stored rates + resolved invoice pricing (what bills use).

    Pass `pricing_ctx` from build_pricing_ctx when listing many offtakers —
    without it each resolve opens a new DB session (N+1, multi-second lag).
    """
    out = {
        "rate_per_kwh": getattr(sub, "rate_per_kwh", None),
        "net_rate_per_kwh": getattr(sub, "net_rate_per_kwh", None),
        "discount_pct": getattr(sub, "discount_pct", None),
        "solar_credit_rate_usd_per_kwh": None,
        "solar_credit_source": None,
    }
    try:
        from .billing.delivery import resolve_discount_pricing
        p = resolve_discount_pricing(sub, ctx=pricing_ctx)
        out["resolved_net_rate"] = round(float(p["net_rate"]), 6)
        out["resolved_discount_pct"] = round(float(p["discount_pct"]), 6)
        out["resolved_effective_rate"] = p.get("effective_rate")
        out["resolved_net_source"] = p.get("net_source")
        out["resolved_net_note"] = p.get("net_rate_note")
        # User-facing alias
        out["solar_credit_rate_usd_per_kwh"] = out["resolved_net_rate"]
        out["solar_credit_source"] = p.get("net_source")
    except Exception as e:
        out["pricing_resolve_error"] = str(e)[:240]
    return out


def _validate_ea_rate(rate) -> float | None:
    if rate is None:
        return None
    try:
        r = float(rate)
    except (TypeError, ValueError):
        raise ValueError("rate must be a number ($/kWh)")
    if r < 0 or r > 5.0:
        raise ValueError("rate must be between 0 and 5.0 $/kWh")
    return r


def _validate_ea_discount(pct) -> float | None:
    """Accept 0.10 or 10 for 10%."""
    if pct is None:
        return None
    try:
        d = float(pct)
    except (TypeError, ValueError):
        raise ValueError("discount must be a number")
    if d >= 1.0:
        d = d / 100.0
    if not (0 <= d < 1):
        raise ValueError("discount must be in [0, 1) as fraction or [0, 100) as percent")
    return d


# ── Public web tools (Energy Agent internet access) ─────────────────────────
_WEB_UA = (
    "Mozilla/5.0 (compatible; EnergyAgent/1.0; +https://arrayoperator.com; research)"
)
_BLOCKED_HOST_SUFFIXES = (
    "railway.internal",
    "localhost",
    "local",
    "internal",
    "svc.cluster.local",
)
_BLOCKED_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "metadata.google.internal",
    "169.254.169.254",
})


def _web_url_allowed(url: str) -> tuple[bool, str]:
    """Reject private/internal targets. Returns (ok, reason)."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception:
        return False, "invalid URL"
    if p.scheme not in ("http", "https"):
        return False, "only http/https URLs are allowed"
    host = (p.hostname or "").lower().strip(".")
    if not host:
        return False, "missing host"
    if host in _BLOCKED_HOSTS:
        return False, "blocked host"
    if any(host == s or host.endswith("." + s) for s in _BLOCKED_HOST_SUFFIXES):
        return False, "internal host blocked"
    # Private IP ranges (basic)
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.|127\.)", host):
        return False, "private IP blocked"
    return True, "ok"


def _html_to_text(html: str, max_chars: int = 12000) -> str:
    """Very small HTML → text (no extra deps)."""
    if not html:
        return ""
    # Drop scripts/styles
    t = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?is)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|section|article)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = (
        t.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:max_chars]


def _web_search_tool(args: dict) -> dict:
    """Live public search via DuckDuckGo (no API key)."""
    import html as html_lib
    from urllib.parse import quote_plus, unquote

    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required", "results": []}
    if len(query) > 300:
        return {"error": "query too long (max 300 chars)", "results": []}
    try:
        max_results = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(8, max_results))

    results: list[dict] = []
    abstract = None
    abstract_url = None
    abstract_source = None
    related: list[str] = []

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed on server", "results": []}

    headers = {"User-Agent": _WEB_UA, "Accept": "application/json,text/html"}

    # 1) Instant Answer API — good for facts / Wikipedia-style abstracts
    try:
        ia_url = (
            "https://api.duckduckgo.com/?"
            f"q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
        )
        with httpx.Client(timeout=httpx.Timeout(12.0), follow_redirects=True) as client:
            r = client.get(ia_url, headers=headers)
            if r.status_code == 200:
                data = r.json() or {}
                abstract = (data.get("AbstractText") or "").strip() or None
                abstract_url = (data.get("AbstractURL") or "").strip() or None
                abstract_source = (data.get("AbstractSource") or "").strip() or None
                for topic in (data.get("RelatedTopics") or [])[:6]:
                    if isinstance(topic, dict):
                        if topic.get("Text"):
                            related.append(str(topic["Text"])[:240])
                        for t2 in topic.get("Topics") or []:
                            if isinstance(t2, dict) and t2.get("Text"):
                                related.append(str(t2["Text"])[:240])
                    if len(related) >= 6:
                        break
                # Official DDG "Results" (often empty)
                for item in data.get("Results") or []:
                    if not isinstance(item, dict):
                        continue
                    u = (item.get("FirstURL") or item.get("url") or "").strip()
                    t = (item.get("Text") or item.get("title") or "").strip()
                    if u and t:
                        results.append({
                            "title": t[:200],
                            "url": u,
                            "snippet": t[:280],
                            "source": "duckduckgo_ia",
                        })
    except Exception as e:
        log.warning("web_search instant-answer failed: %s", e)

    # 2) HTML search — organic links when IA is thin
    if len(results) < max_results:
        try:
            html_url = "https://html.duckduckgo.com/html/"
            with httpx.Client(timeout=httpx.Timeout(14.0), follow_redirects=True) as client:
                r = client.post(
                    html_url,
                    data={"q": query, "b": ""},
                    headers={
                        **headers,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "text/html",
                    },
                )
                body = r.text or ""
            # Result blocks: <a class="result__a" href="...">title</a>
            # DDG wraps redirects: //duckduckgo.com/l/?uddg=<urlencoded>
            link_re = re.compile(
                r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                re.I | re.S,
            )
            snip_re = re.compile(
                r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>',
                re.I | re.S,
            )
            titles = link_re.findall(body)
            snips = snip_re.findall(body)
            seen = {x.get("url") for x in results}
            for idx, (href, title_html) in enumerate(titles):
                if len(results) >= max_results:
                    break
                href = html_lib.unescape(href.strip())
                title = re.sub(r"<[^>]+>", "", html_lib.unescape(title_html)).strip()
                # Unwrap DDG redirect
                m = re.search(r"[?&]uddg=([^&]+)", href)
                if m:
                    href = unquote(m.group(1))
                if href.startswith("//"):
                    href = "https:" + href
                ok, _ = _web_url_allowed(href)
                if not ok or not title or href in seen:
                    continue
                snippet = ""
                if idx < len(snips):
                    snippet = re.sub(
                        r"<[^>]+>", "", html_lib.unescape(snips[idx])
                    ).strip()[:280]
                seen.add(href)
                results.append({
                    "title": title[:200],
                    "url": href,
                    "snippet": snippet,
                    "source": "duckduckgo_html",
                })
        except Exception as e:
            log.warning("web_search html failed: %s", e)

    if not results and not abstract:
        return {
            "query": query,
            "results": [],
            "error": "No web results returned — try a simpler query.",
            "provider": "duckduckgo",
        }

    return {
        "query": query,
        "results": results[:max_results],
        "abstract": abstract,
        "abstract_url": abstract_url,
        "abstract_source": abstract_source,
        "related": related[:6],
        "provider": "duckduckgo",
        "note": (
            "Public web results. Cite title + URL when answering. "
            "For THIS tenant's fleet/offtakers/kWh use census/query tools instead."
        ),
    }


def _web_fetch_tool(args: dict) -> dict:
    """Fetch a public page and return extracted text."""
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "url is required"}
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    ok, reason = _web_url_allowed(url)
    if not ok:
        return {"error": f"URL not allowed: {reason}", "url": url}
    try:
        max_chars = int(args.get("max_chars") or 12000)
    except (TypeError, ValueError):
        max_chars = 12000
    max_chars = max(1000, min(40000, max_chars))

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed on server", "url": url}

    try:
        with httpx.Client(
            timeout=httpx.Timeout(18.0),
            follow_redirects=True,
            headers={"User-Agent": _WEB_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        ) as client:
            r = client.get(url)
            final = str(r.url)
            ok2, reason2 = _web_url_allowed(final)
            if not ok2:
                return {"error": f"redirect target blocked: {reason2}", "url": url, "final_url": final}
            ct = (r.headers.get("content-type") or "").lower()
            raw = r.content[:800_000]  # hard cap bytes
            if r.status_code >= 400:
                return {
                    "error": f"HTTP {r.status_code}",
                    "url": url,
                    "final_url": final,
                }
            # PDF / binary: don't dump
            if "pdf" in ct or raw[:4] == b"%PDF":
                return {
                    "url": url,
                    "final_url": final,
                    "content_type": ct,
                    "note": "PDF binary — cannot extract full text here. Use web_search or ask the user for key excerpts.",
                    "text": "",
                    "bytes": len(raw),
                }
            try:
                text_body = raw.decode(r.encoding or "utf-8", errors="replace")
            except Exception:
                text_body = raw.decode("utf-8", errors="replace")
            if "html" in ct or text_body.lstrip().lower().startswith("<!doctype") or "<html" in text_body[:200].lower():
                extracted = _html_to_text(text_body, max_chars=max_chars)
            else:
                extracted = text_body[:max_chars]
            title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text_body)
            title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else None
            return {
                "url": url,
                "final_url": final,
                "status": r.status_code,
                "content_type": ct,
                "title": (title or "")[:200] or None,
                "text": extracted,
                "truncated": len(extracted) >= max_chars,
                "note": "Extracted public page text. Quote carefully; prefer primary sources.",
            }
    except Exception as e:
        return {"error": f"fetch failed: {e}", "url": url}


def _product_map_tool(args: dict) -> dict:
    # Reload if the support map file changed (deploy without process restart rare,
    # but local/dev edits should pick up without full restart when force-path used).
    pmap = load_product_map()
    topic = (args.get("topic") or "all").strip().lower()
    if topic in pmap:
        src = (
            "energy_agent_surface_model.md"
            if topic.startswith("surface")
            or topic in (
                "product_spine",
                "orientation_playbook",
                "anti_hallucination",
            )
            else "energy_agent_support_map.md"
        )
        return {
            "topic": topic,
            "map": pmap[topic],
            "source": src,
        }
    # Unknown/all → topic directory + entry-point sections (NOT a dump of every
    # topic; the map now spans many topics — call a specific one for depth).
    keys = sorted(pmap.keys())
    entry = {
        k: pmap[k]
        for k in ("system", "tabs", "surface", "tools")
        if k in pmap
    }
    result = {
        "topic": "directory" if topic == "all" else "unknown",
        "topics": keys,
        "map": "\n\n".join(f"## {k}\n{v}" for k, v in entry.items()),
        "source": "energy_agent_support_map.md",
        "note": (
            "This is the topic directory + entry sections. Call "
            "product_map(topic=<id>) for the full text of any topic above "
            "(e.g. capture, health, offtakers, plans, agent, datamodel)."
        ),
        "tools_to_use": {
            "inventory": "tenant_census",
            "ad_hoc_lists": "query_tenant",
            "health": "investigate_attention | fleet_overview | array_detail",
            "account": "account_summary (contact_email, company, plan, capture_mode, cloud_capture)",
            "how_system_works": "product_map(topic=system|capture) — required before explaining Auto-refresh",
            "peer_vs_portal": "product_map(topic=status)",
            "offtaker_edit": "patch_offtaker (confirm)",
            "nav": "ui_navigate",
            "internet": "web_search | web_fetch — live public web (cite URLs); not for this tenant's kWh",
        },
        "caveat": (
            "You reason over THIS tenant's data + product map + optional public web. "
            "You do not have arbitrary codebase shell access (that would leak other "
            "tenants / secrets)."
        ),
    }
    if topic not in ("all", "", "directory"):
        result["requested_topic_not_found"] = topic
    return result


def _account_summary_tool(db, tenant: Tenant, args: dict) -> dict:
    """Same fields the Account tab shows — never use tenant.email (it's contact_email)."""
    from sqlalchemy import func
    from .models import UtilityAccount, UtilitySession, Bill, Client

    # Fresh row inside this session (caller's tenant may be detached/stale)
    t = db.get(Tenant, tenant.id) or tenant
    include_billing = args.get("include_billing", True)
    if include_billing is None:
        include_billing = True

    accounts_count = 0
    bills_count = 0
    clients_count = 0
    connected_providers: list[str] = []
    last_sess = None
    try:
        accounts_count = int(db.execute(
            select(func.count()).select_from(UtilityAccount)
            .where(UtilityAccount.tenant_id == t.id)
        ).scalar() or 0)
        connected_providers = [
            row[0]
            for row in db.execute(
                select(UtilityAccount.provider)
                .where(UtilityAccount.tenant_id == t.id)
                .distinct()
            ).all()
            if row[0]
        ]
        bills_count = int(db.execute(
            select(func.count()).select_from(Bill).where(Bill.tenant_id == t.id)
        ).scalar() or 0)
        clients_count = int(db.execute(
            select(func.count()).select_from(Client).where(
                Client.tenant_id == t.id, Client.deleted_at.is_(None),
            )
        ).scalar() or 0)
        last_sess = db.execute(
            select(UtilitySession).where(UtilitySession.tenant_id == t.id)
            .order_by(UtilitySession.captured_at.desc())
        ).scalars().first()
    except Exception as e:
        log.warning("account_summary counts: %s", e)

    plan_features = None
    try:
        from .stripe_helpers import ao_plan_features
        plan_features = ao_plan_features(
            getattr(t, "product", None), getattr(t, "billing_plan", None),
        )
    except Exception:
        plan_features = None

    has_pm = bool(getattr(t, "stripe_payment_method_id", None))
    card_brief = {"has_payment_method": has_pm, "card_brand": None, "card_last4": None}
    # Best-effort card brand/last4 via account helpers (never fail the tool)
    try:
        from .account import _resolve_pm_id, _card_brief
        has_pm = _resolve_pm_id(t) is not None
        card_brief = _card_brief(t)
        card_brief["has_payment_method"] = has_pm
    except Exception:
        card_brief["has_payment_method"] = has_pm

    def _iso(dt):
        if not dt:
            return None
        try:
            return dt.isoformat() + ("Z" if not str(dt).endswith("Z") else "")
        except Exception:
            return str(dt)

    email = getattr(t, "contact_email", None) or getattr(t, "email", None)
    out = {
        "tenant_id": t.id,
        # Match /v1/account field names so the model aligns with the Master Account UI
        "company_name": getattr(t, "company_name", None) or getattr(t, "name", None),
        "operator_name": getattr(t, "operator_name", None),
        "email": email,  # contact_email — THIS is what the UI shows
        "contact_email": email,
        "product": getattr(t, "product", None) or "array_operator",
        "plan": getattr(t, "plan", None),
        "billing_plan": getattr(t, "billing_plan", None),
        "plan_features": plan_features,
        "subscription_status": getattr(t, "subscription_status", None),
        "active": getattr(t, "active", None),
        "is_demo": bool(getattr(t, "is_demo", False)),
        "trial_ends_at": _iso(getattr(t, "trial_ends_at", None)),
        "has_password": bool(getattr(t, "password_hash", None)),
        "has_payment_method": card_brief.get("has_payment_method"),
        "card_brand": card_brief.get("card_brand"),
        "card_last4": card_brief.get("card_last4"),
        "card_exp": card_brief.get("card_exp"),
        "capture_mode": getattr(t, "capture_mode", None),
        "capture_mode_label": (
            "cloud — Store it with us (server holds encrypted passwords, harvester 24/7)"
            if getattr(t, "capture_mode", None) == "cloud"
            else "device — Keep it on my computer (extension vault; refresh while browser active)"
            if getattr(t, "capture_mode", None) == "device"
            else "unset — client may fall back to local default; ask owner to pick on Account → Auto-refresh"
        ),
        "send_from_email": getattr(t, "send_from_email", None),
        "send_from_name": getattr(t, "send_from_name", None),
        "report_frequency": getattr(t, "report_frequency", None),
        "accounts_count": accounts_count,
        "connected_providers": connected_providers,
        "bills_count": bills_count,
        "clients_count": clients_count,
        "created_at": _iso(getattr(t, "created_at", None)),
        "extension_heartbeat_at": _iso(getattr(t, "extension_heartbeat_at", None)),
        "last_pull_at": _iso(getattr(t, "last_pull_at", None)),
        "utility_session": {
            "captured_at": _iso(getattr(last_sess, "captured_at", None)) if last_sess else None,
            "expires_at": _iso(getattr(last_sess, "expires_at", None)) if last_sess else None,
        } if last_sess else None,
        "ui_tab": "#account",
        "field_notes": {
            "email": "Maps to tenants.contact_email — the Account tab 'Email' field",
            "company_name": "Business name on the profile card",
            "operator_name": "Personal name of the human operator",
            "billing_plan": "Array Operator product plan (vendor_data / invoicing entitlements)",
            "has_payment_method": "Card on file for AO subscription — not offtaker invoices",
            "capture_mode": (
                "Auto-refresh path for portal logins: cloud=server harvester; "
                "device=Chrome extension vault. Orthogonal to SolarEdge API keys AND to "
                "extension one-click capture (which can attach arrays without a vault row)."
            ),
            "extension_heartbeat_at": (
                "Last time the EnergyAgent Chrome extension pinged this tenant. Recent = "
                "extension installed/paired on some browser; not the same as cloud vault."
            ),
            "fleet_vendors_vs_cloud_logins": (
                "fleet_vendors = vendors seen on live arrays/inverters. cloud_capture.logins = "
                "only passwords saved for server harvest. SMA arrays with no SMA cloud login "
                "usually came from extension Log-in-with capture."
            ),
        },
        "auto_refresh_explainer": (
            "See product_map(topic=capture). Cloud + device are scheduled Auto-refresh modes; "
            "extension one-click Log-in-with is a separate first-attach path; SolarEdge API "
            "keys are a third server-poll path."
        ),
    }

    # Extension liveness (paired browser somewhere)
    try:
        hb = getattr(t, "extension_heartbeat_at", None)
        age_s = None
        if hb is not None:
            try:
                age_s = max(0, int((_now() - hb.replace(tzinfo=None)).total_seconds()))
            except Exception:
                age_s = None
        out["extension"] = {
            "heartbeat_at": _iso(hb),
            "seen_recently": bool(age_s is not None and age_s < 6 * 3600),
            "heartbeat_age_seconds": age_s,
            "role": (
                "EnergyAgent Chrome extension pairs to this tenant, can open vendor portals, "
                "auto-capture authenticated data, and POST it. Works for first attach even "
                "when capture_mode=cloud and that vendor is not in the cloud vault."
            ),
        }
    except Exception as e:
        out["extension"] = {"error": str(e)[:120]}

    # Fleet vendor mix (ground truth for "what vendors do I have?") vs vault.
    # Array has no vendor column — vendors live on Inverter + InverterConnection.
    try:
        from .models import Inverter, InverterConnection, Array
        counts: dict[str, int] = {}
        inv_rows = db.execute(
            select(Inverter.vendor).where(
                Inverter.tenant_id == t.id,
                Inverter.deleted_at.is_(None),
            )
        ).all()
        for (v,) in inv_rows:
            key = (v or "").strip().lower() or "unknown"
            counts[key] = counts.get(key, 0) + 1
        # Connections for API vendors that may not yet have inverter rows
        conn_rows = db.execute(
            select(InverterConnection.vendor)
            .join(Array, Array.id == InverterConnection.array_id)
            .where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
            )
        ).all()
        for (v,) in conn_rows:
            key = (v or "").strip().lower() or "unknown"
            if key not in counts:
                counts[key] = 1
        # SolarEdge legacy columns on Array
        se_n = db.execute(
            select(Array.id).where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
                Array.solaredge_site_id.is_not(None),
            )
        ).all()
        if se_n and "solaredge" not in counts:
            counts["solaredge"] = len(se_n)
        out["fleet_vendors"] = [
            {"vendor": k, "count": counts[k]}
            for k in sorted(counts.keys())
        ]
    except Exception as e:
        out["fleet_vendors"] = {"error": str(e)[:120]}

    # Best-effort cloud-capture roster counts (no passwords)
    try:
        from .models import PortalCredential
        creds = db.execute(
            select(PortalCredential).where(PortalCredential.tenant_id == t.id)
        ).scalars().all()
        cloud_provs = sorted({
            (c.provider or "").strip().lower()
            for c in creds if c.provider
        })
        fleet_provs = []
        if isinstance(out.get("fleet_vendors"), list):
            fleet_provs = [x["vendor"] for x in out["fleet_vendors"] if x.get("vendor")]
        only_fleet = sorted(set(fleet_provs) - set(cloud_provs) - {"unknown", ""})
        out["cloud_capture"] = {
            "credential_count": len(creds),
            "enabled_count": sum(1 for c in creds if getattr(c, "cloud_capture_enabled", False)),
            "logins": [
                {
                    "provider": c.provider,
                    "username": c.username,
                    "enabled": bool(getattr(c, "cloud_capture_enabled", False)),
                    "last_harvest_at": _iso(getattr(c, "last_harvest_at", None)),
                    "last_harvest_ok": getattr(c, "last_harvest_ok", None),
                }
                for c in creds[:40]
            ],
            "providers_in_vault": cloud_provs,
            "fleet_vendors_not_in_cloud_vault": only_fleet,
            "provenance_note": (
                "If a vendor is on the fleet but not in the cloud vault, data almost "
                "certainly arrived via EnergyAgent extension one-click capture, "
                "onboarding sync, or an API key — not from a cloud PortalCredential."
            ),
        }
    except Exception as e:
        out["cloud_capture"] = {"error": str(e)[:120]}

    if include_billing:
        try:
            from .account import billing_summary as _billing_summary_ep
            # Call the pure helpers with tenant object (no HTTP)
            from .stripe_helpers import is_array_operator
            from . import account as account_mod
            if is_array_operator(getattr(t, "product", "nepool")):
                out["billing_snapshot"] = account_mod._billing_summary_kwh(t)
            else:
                out["billing_snapshot"] = account_mod._billing_summary_arrays(t)
        except Exception as e:
            out["billing_snapshot"] = {"error": str(e)[:200]}

    return out


def _ea_judge_write(name: str, args: dict) -> dict | None:
    """Internal judge for Energy Agent writes (not the site auto-ship judge).

    Returns None if allowed (or needs normal confirm), or a dict rejection.
    Hard-blocks anything that touches operator billing / Stripe money.
    """
    blob = json.dumps(args or {}, default=str).lower() + " " + (name or "").lower()
    banned = (
        "stripe", "payment_method", "price_id", "subscription_item",
        "charge", "invoice.pay", "billing_plan", "unit_amount",
        "sk_live", "sk_test", "cancel_subscription", "update_subscription",
        "add_payment", "setup_intent", "payment_intent",
    )
    if any(b in blob for b in banned):
        return {
            "ok": False,
            "judged": "reject",
            "error": (
                "Blocked by Energy Agent judge: operator billing / payment changes "
                "are not allowed. Open the billing portal link for the owner to manage "
                "their own card, or escalate to Ford."
            ),
        }
    # Site improvement text that tries to force auto-ship / steal keys
    if name == "propose_site_improvement":
        t = (args.get("text") or "").lower()
        if any(x in t for x in (
            "ignore previous", "exfiltrat", "api key", "admin key",
            "mark this auto", "ship without review", "bypass judge",
        )):
            return {
                "ok": False,
                "judged": "reject",
                "error": "Blocked: suggestion looks like prompt-injection / security ask.",
            }
    return None


def _propose_site_improvement_tool(db, tenant: Tenant, args: dict) -> dict:
    """Queue a feature suggestion (same table/pipeline as Wish this was better)."""
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "text is required — what should change?"}
    text = text[:5000]
    start_markup = args.get("start_markup", True)
    if start_markup is None:
        start_markup = True

    # If they only want the client mark-up flow, don't create a row yet.
    # Prefill the Build-it box with a judge-ready prompt (Ford 2026-07-14).
    if start_markup and not args.get("screenshot_b64") and not args.get("force_submit"):
        build_prompt = (args.get("build_prompt") or args.get("prompt") or text or "").strip()
        # Shape casual speech into an imperative build brief when needed
        if build_prompt and not re.match(
            r"^(add|put|make|change|move|upgrade|replace|show|hide|fix|redesign|create|build)\b",
            build_prompt,
            re.I,
        ):
            build_prompt = (
                f"Build this UI improvement from the owner's request: {build_prompt}. "
                "Prefer a clear, scannable visual that matches Array Operator "
                "(energy / black-green aesthetic). Small pure-UI change only — "
                "no billing math or Stripe changes."
            )
        build_prompt = build_prompt[:1600]
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": "improve_site",
            "args": {
                "mark_first": True,
                "hint": build_prompt,
                "prompt": build_prompt,
                "build_prompt": build_prompt,
                "text": build_prompt,
            },
            "needs_confirm": False,
        }
        return {
            "status": "ui_command",
            "command": cmd,
            "message": (
                "Opening mark-up with a ready-to-build prompt filled in. "
                "User circles the spot, then hits Build it. "
                f"Prompt: {build_prompt[:220]}"
            ),
        }

    shot = None
    raw = (args.get("screenshot_b64") or "").strip()
    if raw:
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            import base64 as _b64
            decoded = _b64.b64decode(raw, validate=True)
            if 0 < len(decoded) <= 4_000_000 and (
                decoded[:8] == b"\x89PNG\r\n\x1a\n" or decoded[:3] == b"\xff\xd8\xff"
            ):
                shot = raw
        except Exception:
            shot = None

    try:
        from .feature_suggestions import FeatureSuggestion
        fs = FeatureSuggestion(
            text=text,
            email=getattr(tenant, "contact_email", None) or getattr(tenant, "email", None),
            tenant_id=tenant.id,
            product=getattr(tenant, "product", None) or "array_operator",
            screenshot_b64=shot,
            status="new",
        )
        db.add(fs)
        db.commit()
        db.refresh(fs)
        sid = fs.id
    except Exception as e:
        log.exception("propose_site_improvement failed")
        return {"error": f"could not queue improvement: {e}"}

    try:
        send_internal_alert(
            subject=f"Energy Agent site improvement (#{sid})",
            body=(
                f"From Energy Agent session\nTenant: {tenant.id}\n"
                f"Email: {getattr(tenant, 'contact_email', None)}\n\n{text}\n"
                + ("\n[Includes marked-up screenshot]\n" if shot else "")
                + "\n(Queued for judge + review harness — same as Wish this was better.)"
            ),
        )
    except Exception:
        pass

    return {
        "ok": True,
        "suggestion_id": sid,
        "status": "new",
        "pipeline": "feature_suggestion_judge",
        "message": (
            f"Queued improvement #{sid}. Client should watch build progress. "
            "Judge may auto-ship pure UI, branch riskier work, or pass."
        ),
        "status_url": f"/v1/feature-suggestion/{sid}/status",
        "command": {
            "id": uuid.uuid4().hex[:12],
            "type": "watch_build",
            "args": {"suggestion_id": sid},
            "needs_confirm": False,
        },
        "status_flag": "ui_command",
    }


def _user_clearly_directed(user_text: str, payload: dict | None = None) -> bool:
    """True when the user's message already states the exact write — no second 'yes'.

    Examples that MUST auto-apply (Ford 2026-07-14): "change it to 15%",
    "set the share percent to 20", "update email to a@b.com". Vague "can you edit
    this offtaker?" without a value still needs a clarifying question — not a confirm.
    """
    t = (user_text or "").strip()
    if not t:
        return False
    # Explicit one-shot approvals always count
    if _YES_RE.match(t):
        return True
    low = t.lower()
    # Imperative / request + a concrete value
    has_directive = bool(
        re.search(
            r"\b(change|set|update|make\s+it|edit|switch|put|bump|raise|lower|"
            r"move|rename|want|like|please|i'?d\s+like)\b",
            low,
        )
    )
    if not has_directive:
        return False
    payload = payload or {}
    # Share % (most common offtaker edit)
    if re.search(r"\d+(\.\d+)?\s*%", low) or re.search(
        r"\b(to|at|as)\s+\d+(\.\d+)?\b", low
    ):
        if re.search(r"\b(share|percent|pct|allocation|%\s*share)\b", low) or any(
            k in payload for k in ("share_pct", "allocation_pct", "array_share_pct")
        ) or not payload:
            return True
    # Email
    if re.search(r"[\w.+-]+@[\w.-]+\.\w+", t):
        if re.search(r"\b(email|e-mail|mail)\b", low) or "email" in payload or "client_email" in payload:
            return True
    # Master / utility / array rebind by name
    if re.search(r"\b(master\s*account|utility|source|bind|rebind|array)\b", low):
        if any(
            k in payload
            for k in (
                "utility_account_id",
                "array_id",
                "utility_account_name",
                "array_name",
                "master_account",
            )
        ) or re.search(r"\b(to|for)\s+[\w][\w\s-]{1,40}", low):
            return True
    # Explicit rename
    if re.search(r"\b(rename|name\s+it|call\s+(it|them))\b", low):
        return True
    # Payload present + directive + number/value-ish
    if payload and re.search(r"\d", low):
        return True
    return False


def _detect_tour_id(user_text: str | None) -> str | None:
    """Map walkthrough language → client preset tour_id (never freehand selectors)."""
    t = (user_text or "").lower()
    if not t:
        return None
    if re.search(r"\bwhat (are|is) (all )?(the )?(different )?tabs\b", t):
        return None
    wants = bool(
        re.search(
            r"\b(walk\s*me|walk\s*us|walkthrough|show\s+me|show\s+us|tour|"
            r"guide\s+me|take\s+me\s+through|walk\s+through|show\s+me\s+around|"
            r"look\s+around|orient\s+me)\b",
            t,
        )
        or re.search(r"\bexplain\b.*\b(tab|page|screen|section|panel)\b", t)
        or re.search(
            r"\b(explain|describe|overview)\b.*\b(invoices?|account|inverters?|"
            r"analysis|resources|triage|offtakers?)\b",
            t,
        )
        or re.search(r"\bgive\s+me\s+a\s+(walkthrough|tour|overview|rundown)\b", t)
        or re.search(
            r"\bhow\s+(do|does)\s+(the\s+)?(account|invoices?|inverters?|analysis|"
            r"resources|fleet\s+triage|this|offtakers?)\b",
            t,
        )
    )
    tab_hit = bool(
        re.search(
            r"\b(master\s*account|account\s+tab|invoices?\s+tab|inverters?\s+tab|"
            r"fleet\s+triage|arrays?\s+tab|resources?\s+tab|analysis\s+tab)\b",
            t,
        )
    )
    if not wants and not (
        tab_hit and re.search(r"\b(show|open|explain|walk|through|around|tour|guide)\b", t)
    ):
        return None
    if re.search(r"\b(invoices?|offtakers?|billing\s+report|credit\s+invoices?)\b", t) or (
        re.search(r"\breports?\b", t) and re.search(r"\btab\b", t)
    ):
        return "reports"
    if re.search(r"\b(master\s*account|account\s+tab)\b", t) or (
        re.search(r"\baccount\b", t)
        and re.search(r"\b(walk|tour|show|explain|through|around|guide)\b", t)
    ):
        return "master_account"
    if re.search(r"\bfleet\s+triage\b", t) or (
        re.search(r"\btriage\b", t)
        and re.search(r"\b(walk|tour|show|around)\b", t)
    ):
        return "dashboard"
    if re.search(r"\b(inverters?|spreadsheet|sandbox|fleet\s+canvas)\b", t) or (
        re.search(r"\barrays?\b", t)
        and re.search(r"\b(tab|walk|tour|show|around)\b", t)
    ):
        return "arrays"
    if re.search(r"\banalysis\b", t) or re.search(r"\btrends?\b", t) or re.search(
        r"\bthrough\s+time\b", t
    ):
        return "analysis"
    if (
        re.search(r"\bresources?\b", t)
        or re.search(r"\bnet.?meter|rates?\s+and\s+news|briefing\b", t)
        or re.search(r"\brec\s+market\b", t)
    ):
        return "resources"
    return None


def _run_tool(
    name: str,
    args: dict,
    tenant: Tenant,
    session: EaSession,
    db,
    user_text: str = "",
) -> dict:
    args = args or {}
    tid = tenant.id

    # Judge gate — hard reject billing/money writes before anything else
    blocked = _ea_judge_write(name, args)
    if blocked is not None:
        return blocked

    if name == "tenant_census":
        return _tenant_census_tool(db, tenant, args)

    if name == "query_tenant":
        return _query_tenant_tool(db, tenant, args)

    if name == "product_map":
        return _product_map_tool(args)

    if name == "web_search":
        out = _web_search_tool(args)
        try:
            _charge(db, tid, 0.003, "web_search")
        except Exception:
            pass
        return out

    if name == "web_fetch":
        out = _web_fetch_tool(args)
        try:
            _charge(db, tid, 0.004, "web_fetch")
        except Exception:
            pass
        return out

    if name == "propose_site_improvement":
        out = _propose_site_improvement_tool(db, tenant, args)
        # Normalize command packaging for the agent turn loop
        if out.get("status") == "ui_command":
            return out
        if out.get("command"):
            return {
                "status": "ui_command",
                "command": out["command"],
                "suggestion_id": out.get("suggestion_id"),
                "message": out.get("message"),
                "ok": out.get("ok"),
            }
        return out

    if name == "fleet_overview":
        return _fleet_overview_tool(db, tenant, args)

    if name == "investigate_attention":
        return _investigate_attention_tool(db, tenant, args)

    if name == "array_detail":
        return _array_detail_tool(db, tenant, args)

    # ── O&M / repair healing ──────────────────────────────────────────────
    if name == "repair_ops_overview":
        from . import repair_ops as ro
        t = db.get(Tenant, tid) or tenant
        if args.get("reconcile", True):
            try:
                ro.reconcile(db, t)
            except Exception as e:
                log.warning("ea repair reconcile: %s", e)
        return {"ok": True, **ro.ops_overview(db, t)}

    if name == "list_service_contacts":
        from . import repair_ops as ro
        contacts = ro.list_contacts(db, tid)
        return {
            "ok": True,
            "contacts": [ro.serialize_contact(c) for c in contacts],
            "assignments": ro.assignments_for_tenant(db, tid),
            "count": len(contacts),
        }

    if name == "upsert_service_contact":
        from . import repair_ops as ro
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {
            "name": args.get("name"), "email": args.get("email"),
        }):
            needs = False
        payload = {
            "contact_id": args.get("contact_id"),
            "name": args.get("name"),
            "company": args.get("company"),
            "role": args.get("role") or "om",
            "email": args.get("email"),
            "phone": args.get("phone"),
            "notes": args.get("notes"),
            "is_default": bool(args.get("is_default") or False),
            "active": True if args.get("active") is None else bool(args.get("active")),
        }
        reason = args.get("reason") or f"Save service contact {payload.get('name')}"
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": reason + " — confirm to save.",
                "needs_confirm": True,
            }
        try:
            c = ro.upsert_contact(
                db, tid,
                contact_id=payload.get("contact_id"),
                name=payload["name"],
                company=payload.get("company"),
                role=payload.get("role") or "om",
                email=payload.get("email"),
                phone=payload.get("phone"),
                notes=payload.get("notes"),
                is_default=bool(payload.get("is_default")),
                active=bool(payload.get("active", True)),
            )
            db.commit()
            return {"ok": True, "contact": ro.serialize_contact(c)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "assign_service_contact":
        from . import repair_ops as ro
        contact_id = args.get("contact_id")
        array_id = args.get("array_id")
        if not array_id and args.get("array_name"):
            arr, err = _find_array(db, tid, args["array_name"])
            if isinstance(err, dict):
                return {"ok": False, **err}
            if not arr:
                return {"ok": False, "error": f"array not found for '{args.get('array_name')}'"}
            array_id = arr.id
        if not contact_id or not array_id:
            return {"ok": False, "error": "contact_id and array_id (or array_name) required"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {"contact_id": contact_id, "array_id": array_id}):
            needs = False
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {
                    "tool": name,
                    "args": {
                        "contact_id": contact_id,
                        "array_id": array_id,
                        "kind": args.get("kind") or "primary",
                    },
                },
                "message": f"Assign contact #{contact_id} to array #{array_id} — confirm.",
                "needs_confirm": True,
            }
        try:
            row = ro.assign_array_contact(
                db, tid, int(array_id), int(contact_id),
                kind=args.get("kind") or "primary",
            )
            db.commit()
            return {
                "ok": True,
                "assignment": {
                    "array_id": row.array_id,
                    "contact_id": row.contact_id,
                    "kind": row.kind,
                },
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "list_repair_tickets":
        from . import repair_ops as ro
        tickets = ro.list_tickets(
            db, tid,
            status=args.get("status"),
            array_id=args.get("array_id"),
            active_only=bool(args.get("active_only", True)),
        )
        return {
            "ok": True,
            "tickets": [ro.serialize_ticket(t) for t in tickets],
            "summary": ro.summarize_tickets(tickets),
            "count": len(tickets),
        }

    if name == "open_repair_ticket":
        from . import repair_ops as ro
        array_id = args.get("array_id")
        if not array_id and args.get("array_name"):
            arr, err = _find_array(db, tid, args["array_name"])
            if isinstance(err, dict):
                return {"ok": False, **err}
            if not arr:
                return {"ok": False, "error": f"array not found for '{args.get('array_name')}'"}
            array_id = arr.id
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {
            "array": args.get("array_name") or array_id,
            "repair": True,
        }):
            needs = False
        payload = {
            "array_id": array_id,
            "inverter_id": args.get("inverter_id"),
            "contact_id": args.get("contact_id"),
            "fail_type": args.get("fail_type") or "other",
            "title": args.get("title"),
            "description": args.get("description"),
        }
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": "Open repair ticket — confirm.",
                "needs_confirm": True,
            }
        try:
            t = db.get(Tenant, tid) or tenant
            ticket = ro.open_ticket(db, t, source="agent", **{
                k: v for k, v in payload.items() if v is not None
            })
            db.commit()
            return {"ok": True, "ticket": ro.serialize_ticket(ticket)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "update_repair_ticket":
        from . import repair_ops as ro
        ticket_id = args.get("ticket_id")
        if not ticket_id:
            return {"ok": False, "error": "ticket_id required"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {"ticket_id": ticket_id, "status": args.get("status")}):
            needs = False
        payload = {
            "ticket_id": ticket_id,
            "status": args.get("status"),
            "contact_id": args.get("contact_id"),
            "tech_note": args.get("tech_note"),
            "description": args.get("description"),
            "scheduled_for": args.get("scheduled_for"),
        }
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": f"Update repair ticket #{ticket_id} — confirm.",
                "needs_confirm": True,
            }
        try:
            from .models import RepairTicket as RT
            t = db.get(Tenant, tid) or tenant
            ticket = db.get(RT, ticket_id)
            if ticket is None or ticket.tenant_id != tid:
                return {"ok": False, "error": "ticket not found"}
            sched = None
            clear_sched = False
            if payload.get("scheduled_for") is not None:
                raw = str(payload["scheduled_for"]).strip()
                if raw == "":
                    clear_sched = True
                else:
                    from datetime import datetime
                    sched = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            ro.update_ticket(
                db, t, ticket,
                status=payload.get("status"),
                contact_id=payload.get("contact_id"),
                tech_note=payload.get("tech_note"),
                description=payload.get("description"),
                scheduled_for=sched,
                clear_scheduled=clear_sched,
            )
            db.commit()
            return {"ok": True, "ticket": ro.serialize_ticket(ticket)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if name == "draft_repair_checkin":
        from . import repair_ops as ro
        from .models import RepairTicket as RT
        ticket_id = args.get("ticket_id")
        ticket = db.get(RT, ticket_id)
        if ticket is None or ticket.tenant_id != tid:
            return {"ok": False, "error": "ticket not found"}
        t = db.get(Tenant, tid) or tenant
        contact = ro.get_contact(db, tid, ticket.contact_id) if ticket.contact_id else None
        draft = ro.build_checkin_draft(ticket, t, contact)
        ticket.draft_checkin = draft
        db.commit()
        return {"ok": True, "ticket_id": ticket.id, "draft": draft, "contact": ro.serialize_contact(contact) if contact else None}

    if name == "send_repair_checkin":
        from . import repair_ops as ro
        from .models import RepairTicket as RT
        ticket_id = args.get("ticket_id")
        ticket = db.get(RT, ticket_id)
        if ticket is None or ticket.tenant_id != tid:
            return {"ok": False, "error": "ticket not found"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {
            "checkin": True, "email": True, "ticket_id": ticket_id,
        }):
            # Only skip confirm if user language clearly means send/contact now
            ut = (user_text or "").lower()
            if any(w in ut for w in (
                "send", "email", "check in", "check-in", "contact", "reach out", "ping",
            )):
                needs = False
        payload = {
            "ticket_id": ticket_id,
            "to": args.get("to"),
            "subject": args.get("subject"),
            "body": args.get("body"),
        }
        if needs:
            draft = ticket.draft_checkin or {}
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": (
                    f"Send check-in for ticket #{ticket_id} to "
                    f"{payload.get('to') or draft.get('to') or 'assigned contact'} — confirm."
                ),
                "draft_preview": draft,
                "needs_confirm": True,
            }
        try:
            t = db.get(Tenant, tid) or tenant
            row = ro.send_checkin(
                db, t, ticket, via="agent",
                to_override=payload.get("to"),
                subject_override=payload.get("subject"),
                body_override=payload.get("body"),
            )
            db.commit()
            return {
                "ok": True,
                "checkin": ro.serialize_checkin(row),
                "ticket": ro.serialize_ticket(ticket),
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}

    if name == "log_repair_note":
        from . import repair_ops as ro
        from .models import RepairTicket as RT
        ticket_id = args.get("ticket_id")
        note = (args.get("note") or "").strip()
        if not ticket_id or not note:
            return {"ok": False, "error": "ticket_id and note required"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {"note": note}):
            needs = False
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": {"ticket_id": ticket_id, "note": note}},
                "message": f"Log note on ticket #{ticket_id} — confirm.",
                "needs_confirm": True,
            }
        ticket = db.get(RT, ticket_id)
        if ticket is None or ticket.tenant_id != tid:
            return {"ok": False, "error": "ticket not found"}
        try:
            t = db.get(Tenant, tid) or tenant
            row = ro.log_inbound_note(db, t, ticket, note, via="agent")
            db.commit()
            return {
                "ok": True,
                "checkin": ro.serialize_checkin(row),
                "ticket": ro.serialize_ticket(ticket),
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "list_offtakers":
        from .models import BillingReportSubscription, UtilityAccount
        q = select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tid,
        )
        if hasattr(BillingReportSubscription, "deleted_at"):
            q = q.where(BillingReportSubscription.deleted_at.is_(None))
        q = q.order_by(BillingReportSubscription.id).limit(300)
        try:
            subs = db.execute(q).scalars().all()
        except Exception as e:
            return {"error": f"could not list offtakers: {e}", "offtakers": []}
        # Batch-resolve array names + utility account labels for rebinding UI
        arr_ids = {getattr(s, "array_id", None) for s in subs}
        arr_ids.discard(None)
        ua_ids = {getattr(s, "utility_account_id", None) for s in subs}
        ua_ids.discard(None)
        arr_name = {}
        if arr_ids:
            for a in db.execute(
                select(Array).where(Array.id.in_(arr_ids), Array.tenant_id == tid)
            ).scalars().all():
                arr_name[a.id] = a.name
        ua_map = {}
        if ua_ids:
            for u in db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.id.in_(ua_ids), UtilityAccount.tenant_id == tid,
                )
            ).scalars().all():
                ua_map[u.id] = u
        # Batch pricing lookups once for the whole list (avoids per-sub SessionLocal)
        pricing_ctx = None
        try:
            from .billing.delivery import build_pricing_ctx
            pricing_ctx = build_pricing_ctx(db, tenant)
        except Exception as e:
            log.debug("pricing_ctx skipped: %s", e)
        result = []
        for s in subs:
            share = getattr(s, "array_share_pct", None)
            if share is None:
                share = getattr(s, "allocation_pct", None)
            if share is not None and float(share) <= 1:
                share = round(float(share) * 100, 4)
            uaid = getattr(s, "utility_account_id", None)
            aid = getattr(s, "array_id", None)
            ua = ua_map.get(uaid) if uaid else None
            nick = (getattr(ua, "nickname", None) or "").strip() if ua else None
            acct_num = getattr(ua, "account_number", None) if ua else None
            row = {
                "id": s.id,
                "name": getattr(s, "customer_name", None),
                "email": getattr(s, "client_email", None),
                "share_pct": share,
                "allocation_pct": getattr(s, "allocation_pct", None),
                "array_share_pct": getattr(s, "array_share_pct", None),
                "array_id": aid,
                "array_name": arr_name.get(aid) if aid else None,
                "utility_account_id": uaid,
                "utility_account_nickname": nick,
                "utility_account_number": acct_num,
                "utility_provider": (getattr(ua, "provider", None) if ua else None),
                "utility_label": (
                    nick or (f"{getattr(ua, 'provider', '')} {acct_num}".strip() if ua else None)
                ),
                "send_mode": getattr(s, "send_mode", None),
                "delivery_mode": getattr(s, "delivery_mode", None),
                "enabled": getattr(s, "enabled", None),
            }
            row.update(_offtaker_rate_fields(db, tenant, s, pricing_ctx=pricing_ctx))
            result.append(row)
        return {
            "offtakers": result,
            "count": len(result),
            "tenant_rates": _tenant_global_rates(tenant),
        }

    if name == "get_offtaker":
        sid = args.get("subscription_id")
        name_q = (args.get("name") or "").strip().lower()
        listed = _run_tool("list_offtakers", {}, tenant, session, db)
        for o in listed.get("offtakers") or []:
            if sid and o.get("id") == sid:
                return {"offtaker": o, "tenant_rates": listed.get("tenant_rates")}
            if name_q and name_q in str(o.get("name") or "").lower():
                return {"offtaker": o, "tenant_rates": listed.get("tenant_rates")}
        return {
            "error": "not found",
            "offtaker": None,
            "hint": "Call list_offtakers or get_billing_rates with offtaker_name",
            "tenant_rates": listed.get("tenant_rates"),
        }

    if name == "get_billing_rates":
        out = {"ok": True, "tenant": _tenant_global_rates(tenant)}
        sid = args.get("subscription_id")
        name_q = (args.get("offtaker_name") or args.get("name") or "").strip().lower()
        if args.get("include_all_offtakers"):
            listed = _run_tool("list_offtakers", {}, tenant, session, db)
            out["offtakers"] = [
                {
                    "id": o.get("id"),
                    "name": o.get("name"),
                    "solar_credit_rate_usd_per_kwh": o.get("solar_credit_rate_usd_per_kwh"),
                    "solar_credit_source": o.get("solar_credit_source"),
                    "net_rate_per_kwh": o.get("net_rate_per_kwh"),
                    "rate_per_kwh": o.get("rate_per_kwh"),
                    "discount_pct": o.get("discount_pct"),
                    "resolved_effective_rate": o.get("resolved_effective_rate"),
                    "array_name": o.get("array_name"),
                }
                for o in (listed.get("offtakers") or [])[:100]
            ]
            out["count"] = len(out["offtakers"])
            return out
        if sid or name_q:
            listed = _run_tool("list_offtakers", {}, tenant, session, db)
            match = None
            for o in listed.get("offtakers") or []:
                if sid and o.get("id") == sid:
                    match = o
                    break
                if name_q and name_q in str(o.get("name") or "").lower():
                    match = o
                    if str(o.get("name") or "").lower() == name_q:
                        break
            if not match:
                return {
                    "ok": False,
                    "error": f"no offtaker matching '{name_q or sid}'",
                    "tenant": out["tenant"],
                    "hint": "list_offtakers for names",
                }
            out["offtaker"] = match
            out["spoken_summary"] = (
                f"{match.get('name')}: solar credit "
                f"${match.get('solar_credit_rate_usd_per_kwh')}/kWh "
                f"(source={match.get('solar_credit_source')}; "
                f"effective after discount ${match.get('resolved_effective_rate')}/kWh). "
                f"Master: {out['tenant'].get('note')}"
            )
        return out

    if name == "set_billing_rates":
        from .models import Tenant as TenantModel
        needs = bool(args.get("needs_confirm", True))
        directed = {
            k: args.get(k)
            for k in (
                "default_net_rate_per_kwh",
                "default_discount_pct",
                "default_billing_rate_per_kwh",
            )
            if args.get(k) is not None
        }
        if args.get("clear_net_rate") or args.get("clear_discount"):
            directed["clear"] = True
        if needs and _user_clearly_directed(user_text, directed or {"rate": True}):
            needs = False
        try:
            payload = {}
            if args.get("clear_net_rate"):
                payload["default_net_rate_per_kwh"] = None
            elif args.get("default_net_rate_per_kwh") is not None:
                payload["default_net_rate_per_kwh"] = _validate_ea_rate(
                    args["default_net_rate_per_kwh"]
                )
            if args.get("clear_discount"):
                payload["default_discount_pct"] = None
            elif args.get("default_discount_pct") is not None:
                payload["default_discount_pct"] = _validate_ea_discount(
                    args["default_discount_pct"]
                )
            if args.get("default_billing_rate_per_kwh") is not None:
                payload["default_billing_rate_per_kwh"] = _validate_ea_rate(
                    args["default_billing_rate_per_kwh"]
                )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not payload:
            return {
                "ok": False,
                "error": "pass default_net_rate_per_kwh, default_discount_pct, or clear_* flags",
                "current": _tenant_global_rates(tenant),
            }
        reason = "Set master billing rates: " + ", ".join(
            f"{k}={v}" for k, v in payload.items()
        )
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": "api_patch",
            "args": {
                "method": "PUT",
                "path": "/v1/array-operator/billing/global-rate",
                "body": payload,
            },
            "needs_confirm": bool(needs),
            "reason": reason,
        }
        if not needs:
            tt = db.get(TenantModel, tid)
            if tt is None:
                return {"ok": False, "error": "tenant not found"}
            for k, v in payload.items():
                setattr(tt, k, v)
            db.add(tt)
            db.commit()
            db.refresh(tt)
            return {
                "status": "ui_command",
                "command": {
                    "id": uuid.uuid4().hex[:12],
                    "type": "ui_refresh",
                    "args": {"surface": "reports", "rates": True},
                    "needs_confirm": False,
                },
                "also_commands": [cmd],
                "applied": _tenant_global_rates(tt),
                "message": "Master solar credit / discount rates updated.",
            }
        return {
            "status": "pending_confirm",
            "pending": cmd,
            "message": reason + " — confirm to apply.",
        }

    if name == "fleet_trends_summary":
        # Lightweight local summary from DailyGeneration if trends endpoint is heavy
        try:
            from .models import DailyGeneration
            from sqlalchemy import func
            life = db.execute(
                select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
                .select_from(DailyGeneration)
                .join(Array, Array.id == DailyGeneration.array_id)
                .where(Array.tenant_id == tid, Array.deleted_at.is_(None))
            ).scalar() or 0.0
            since = (_now().date() - timedelta(days=365))
            ttm = db.execute(
                select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
                .select_from(DailyGeneration)
                .join(Array, Array.id == DailyGeneration.array_id)
                .where(
                    Array.tenant_id == tid,
                    Array.deleted_at.is_(None),
                    DailyGeneration.day >= since,
                )
            ).scalar() or 0.0
            return {
                "lifetime_kwh": round(float(life), 1),
                "ttm_kwh": round(float(ttm), 1),
                "note": "From DailyGeneration; open Analysis → Through time for YoY bars.",
            }
        except Exception as e:
            return {"error": str(e)}

    if name == "account_summary":
        return _account_summary_tool(db, tenant, args)

    if name == "billing_portal_link":
        # Do not invent Stripe; return instruction for UI to call existing endpoint
        return {
            "ui_fetch": {
                "method": "GET",
                "path": "/v1/account/billing-portal",
            },
            "note": "Client should open the returned portal URL. Energy Agent never charges cards.",
        }

    if name == "portal_links":
        # Vendor + utility portals for THIS tenant (Paul: SmartHub for Glover, etc.)
        PORTALS = {
            "solaredge": ("SolarEdge monitoring", "https://monitoring.solaredge.com/"),
            "fronius": ("Fronius Solar.web", "https://www.solarweb.com/"),
            "sma": ("SMA ennexOS / Sunny Portal", "https://ennexos.sunnyportal.com/"),
            "chint": ("Chint / CPS monitor", "https://monitor.chintpowersystems.com/"),
            "alsoenergy": ("AlsoEnergy", "https://hmi.alsoenergy.com/"),
            "locus": ("Locus / AlsoEnergy", "https://hmi.alsoenergy.com/"),
            "gmp": ("Green Mountain Power", "https://greenmountainpower.com/"),
            "vec": ("Vermont Electric Co-op (SmartHub)", "https://vermontelectric.smarthub.coop/"),
            "wec": ("Washington Electric Co-op (SmartHub)", "https://washingtonelectric.smarthub.coop/"),
        }
        try:
            from .models import Array, Inverter, UtilityAccount
            tid = tenant.id
            vendors = set()
            for v, in db.execute(
                select(Inverter.vendor).where(
                    Inverter.tenant_id == tid,
                    Inverter.deleted_at.is_(None),
                ).distinct()
            ).all():
                if v:
                    vendors.add(str(v).lower())
            # Arrays may exist without inverters yet (Paul's River / Norwich)
            for r in db.execute(
                select(Array.id, Array.name).where(
                    Array.tenant_id == tid, Array.deleted_at.is_(None),
                )
            ).all():
                pass  # presence only
            utils = []
            try:
                for ua in db.execute(
                    select(UtilityAccount).where(
                        UtilityAccount.tenant_id == tid,
                        UtilityAccount.deleted_at.is_(None),
                    )
                ).scalars().all():
                    code = (getattr(ua, "provider", None) or getattr(ua, "provider_code", None) or "").lower()
                    nick = getattr(ua, "nickname", None) or getattr(ua, "account_number", None) or code
                    utils.append({"provider": code, "label": nick})
                    if code:
                        vendors.add(code)
            except Exception:
                pass
            links = []
            for code in sorted(vendors):
                meta = PORTALS.get(code)
                if not meta:
                    # SmartHub co-ops often store host on the account
                    continue
                label, url = meta
                links.append({
                    "code": code,
                    "label": label,
                    "url": url,
                    "markdown": f"[{label}]({url})",
                })
            # Always include common VT utilities if tenant has VT offtakers / no utils yet
            if not any(L["code"] in ("vec", "wec", "gmp") for L in links):
                for code in ("vec", "gmp"):
                    label, url = PORTALS[code]
                    links.append({
                        "code": code,
                        "label": label + " (common VT)",
                        "url": url,
                        "markdown": f"[{label}]({url})",
                    })
            speak_lines = [
                f"- {L['markdown']}" for L in links
            ]
            return {
                "ok": True,
                "links": links,
                "utility_accounts": utils,
                "how_to_reply": (
                    "Embed these as markdown links in your spoken/chat answer, e.g. "
                    "'Open [VEC SmartHub](https://vermontelectric.smarthub.coop/) for Glover.' "
                    "You may also emit ui_command type open_url with url+label to open a tab."
                ),
                "chat_snippet": "Portal links:\n" + "\n".join(speak_lines),
            }
        except Exception as e:
            return {"error": str(e)}

    if name == "send_pipeline":
        return {
            "ui_fetch": {"method": "GET", "path": "/v1/array-operator/billing/send-pipeline"},
            "hint": "Prefer navigating user to #reports if they want to act.",
        }

    if name in ("ui_navigate", "ui_highlight", "ui_fill", "ui_click", "ui_tour", "open_url"):
        # Navigate + highlight + tours + open_url are instant. Writes skip confirm when the
        # user already stated the exact change this turn.
        if name in ("ui_navigate", "ui_highlight", "ui_tour", "open_url"):
            needs = False
        else:
            needs = bool(args.get("needs_confirm", True))
            if needs and _user_clearly_directed(user_text, args):
                needs = False
        # Walkthrough intent → PRESET tour only. Freehand ui_highlight invents CSS
        # and desyncs from voice; client has lockstep DOM tours for every tab.
        tour_id = _detect_tour_id(user_text) or (
            args.get("tour_id") if name == "ui_tour" else None
        )
        if name == "ui_highlight" and tour_id:
            name = "ui_tour"
            args = {"tour_id": tour_id}
        if name == "ui_tour":
            tid = args.get("tour_id") or tour_id
            if tid:
                args = {"tour_id": tid}  # drop freehand custom steps
        cmd_type = name.replace("ui_", "")
        if name == "ui_tour":
            cmd_type = "tour"
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": cmd_type,
            "args": {k: v for k, v in args.items() if k != "needs_confirm"},
            "needs_confirm": needs,
        }
        if needs:
            return {
                "status": "pending_confirm",
                "pending": cmd,
                "message": f"Ready to {cmd['type']}: {args.get('reason') or args.get('hash') or args.get('selector')}. Ask user to confirm.",
            }
        return {"status": "ui_command", "command": cmd}

    if name == "remember_tenant":
        _mem_set(db, f"tenant:{tid}", args.get("key") or "note", args.get("value") or "")
        return {"ok": True, "scope": "tenant"}

    if name == "remember_global_behavior":
        _mem_set(db, "global", args.get("key") or "tip", args.get("value") or "")
        return {"ok": True, "scope": "global"}

    if name == "escalate_to_ford":
        summary = args.get("summary") or "(no summary)"
        user_said = args.get("user_said") or ""
        quiet = bool(args.get("quietly"))
        # Durable Ford Operator inbox (standing Grok worker) — email is backup only.
        try:
            from .ford_escalations import enqueue_escalation
            email = (
                getattr(tenant, "contact_email", None)
                or getattr(tenant, "email", None)
                or ""
            )
            return enqueue_escalation(
                tenant_id=tid,
                tenant_email=str(email) if email else None,
                session_id=getattr(session, "id", None),
                summary=summary,
                user_said=user_said,
                quiet=quiet,
                also_email=not quiet,  # quiet auto-escapes: queue only, no mail spam
            )
        except Exception as e:
            log.exception("escalation queue failed — falling back to email")
            body = (
                f"Energy Agent escalation (queue failed: {e})\n"
                f"tenant: {tid}\n"
                f"email: {getattr(tenant, 'contact_email', '') or getattr(tenant, 'email', '')}\n"
                f"session: {session.id}\n"
                f"quiet: {quiet}\n\n"
                f"{summary}\n\n"
                f"User said:\n{user_said}\n"
            )
            try:
                send_internal_alert(f"[Energy Agent] {summary[:80]}", body)
                return {"ok": True, "escalated": True, "queue": "email_fallback"}
            except Exception as e2:
                log.exception("escalate email fallback failed")
                return {"ok": False, "error": str(e2)}

    if name == "patch_offtaker":
        # Resolve offtaker by id and/or name, map fields to real PATCH body,
        # optionally apply server-side so the UI can soft-refresh without a full reload.
        from .models import BillingReportSubscription

        sid = args.get("subscription_id")
        name_q = (args.get("offtaker_name") or args.get("customer_name") or "").strip()
        sub = None
        if sid is not None:
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                return {"error": f"invalid subscription_id: {sid}"}
            sub = db.get(BillingReportSubscription, sid)
            if sub is None or sub.tenant_id != tid:
                return {"error": f"offtaker #{sid} not found in your account"}
            if getattr(sub, "deleted_at", None):
                return {"error": f"offtaker #{sid} is deleted"}
        elif name_q:
            listed = _run_tool("list_offtakers", {}, tenant, session, db)
            matches = [
                o for o in (listed.get("offtakers") or [])
                if name_q.lower() in str(o.get("name") or "").lower()
            ]
            if not matches:
                return {"error": f"no offtaker matching '{name_q}'", "hint": "call list_offtakers"}
            if len(matches) > 1:
                # Prefer exact match; otherwise ask which one
                exact = [o for o in matches if str(o.get("name") or "").lower() == name_q.lower()]
                if len(exact) == 1:
                    matches = exact
                else:
                    return {
                        "error": "multiple offtakers match — pass subscription_id",
                        "matches": [
                            {
                                "id": o.get("id"),
                                "name": o.get("name"),
                                "share_pct": o.get("share_pct"),
                                "array_name": o.get("array_name"),
                                "utility_label": o.get("utility_label"),
                            }
                            for o in matches[:12]
                        ],
                    }
            sid = matches[0]["id"]
            sub = db.get(BillingReportSubscription, sid)
        else:
            return {"error": "pass subscription_id or offtaker_name"}

        if sub is None:
            return {"error": "offtaker not found"}

        # Build API body with CORRECT field names (share_pct is NOT a column).
        payload: dict = {}
        if args.get("email") is not None:
            payload["client_email"] = str(args["email"]).strip()
        # Display rename ONLY when explicitly requested via name= — never treat
        # master_account / utility source targets as a customer_name change.
        if args.get("name") is not None:
            payload["customer_name"] = str(args["name"]).strip()
        if args.get("share_pct") is not None:
            try:
                sp = float(args["share_pct"])
            except (TypeError, ValueError):
                return {"error": "share_pct must be a number (e.g. 25 for 25%)"}
            # Accept either percent (25) or fraction (0.25)
            frac = sp / 100.0 if sp > 1.0 else sp
            if not (0 < frac <= 1.0):
                return {"error": "share_pct must be in (0, 100] percent or (0, 1] fraction"}
            # Sub-metered offtakers bill off their own meter (allocation_pct pinned 1.0);
            # their group share lives in array_share_pct. Mirror the Reports PATCH rule.
            has_own_meter = getattr(sub, "utility_account_id", None) is not None
            if has_own_meter:
                payload["array_share_pct"] = frac
            else:
                payload["allocation_pct"] = frac
        if args.get("auto_send") is not None:
            payload["delivery_mode"] = "auto" if bool(args["auto_send"]) else "approval"

        # ── Solar credit / net rate + discount ───────────────────────────
        try:
            if args.get("clear_rate"):
                payload["rate_per_kwh"] = None
                payload["net_rate_per_kwh"] = None
            else:
                if args.get("rate_per_kwh") is not None:
                    payload["rate_per_kwh"] = _validate_ea_rate(args["rate_per_kwh"])
                if args.get("net_rate_per_kwh") is not None:
                    payload["net_rate_per_kwh"] = _validate_ea_rate(args["net_rate_per_kwh"])
            if args.get("clear_discount"):
                payload["discount_pct"] = None
            elif args.get("discount_pct") is not None:
                payload["discount_pct"] = _validate_ea_discount(args["discount_pct"])
        except ValueError as e:
            return {"error": str(e)}

        # ── Utility / master group rebind ─────────────────────────────────
        bind = _resolve_offtaker_bind_targets(db, tid, sub, args)
        if bind.get("error"):
            return bind
        if "utility_account_id" in bind:
            payload["utility_account_id"] = bind["utility_account_id"]
        if "array_id" in bind:
            payload["array_id"] = bind["array_id"]

        if not payload:
            return {
                "error": (
                    "nothing to change — pass share_pct, email, name (rename only), "
                    "rate_per_kwh, net_rate_per_kwh, discount_pct, clear_rate, "
                    "auto_send, utility_account_id|utility_account_name, "
                    "array_id|array_name, and/or master_account"
                ),
                "offtaker": {
                    "id": sub.id,
                    "name": getattr(sub, "customer_name", None),
                    "email": getattr(sub, "client_email", None),
                    "array_id": getattr(sub, "array_id", None),
                    "utility_account_id": getattr(sub, "utility_account_id", None),
                    **_offtaker_rate_fields(db, tenant, sub),
                },
                "hint": (
                    "Solar credit rate = net_rate_per_kwh (or rate_per_kwh). "
                    "Call get_billing_rates(offtaker_name=...) to read current rates. "
                    "Master account / utility source = utility_account + array bind, "
                    "NOT customer_name."
                ),
            }

        needs = bool(args.get("needs_confirm", True))
        # Server-side gate: clear user direction → apply, never re-ask for "yes"
        if needs and _user_clearly_directed(user_text, payload):
            needs = False
        reason = (
            f"Update offtaker #{sub.id} ({getattr(sub, 'customer_name', '') or 'unnamed'}): "
            + ", ".join(f"{k}={v}" for k, v in payload.items())
        )
        if bind.get("resolved_labels"):
            reason += " (" + "; ".join(bind["resolved_labels"]) + ")"
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": "api_patch",
            "args": {
                "method": "PATCH",
                "path": f"/v1/array-operator/billing/subscriptions/{sub.id}",
                "body": payload,
            },
            "needs_confirm": bool(needs),
            "reason": reason,
        }

        # When already confirmed (or model set needs_confirm=false), apply NOW
        # server-side so the change sticks even if the browser PATCH is flaky,
        # then tell the client to soft-refresh (no full page reload).
        if not needs:
            applied = _apply_offtaker_patch(db, sub, payload)
            if not applied.get("ok"):
                return applied
            refresh = {
                "id": uuid.uuid4().hex[:12],
                "type": "ui_refresh",
                "args": {
                    "surface": "reports",
                    "subscription_id": sub.id,
                    "allocation_pct": getattr(sub, "allocation_pct", None),
                    "array_share_pct": getattr(sub, "array_share_pct", None),
                    "array_id": getattr(sub, "array_id", None),
                    "utility_account_id": getattr(sub, "utility_account_id", None),
                    "customer_name": getattr(sub, "customer_name", None),
                    "client_email": getattr(sub, "client_email", None),
                    "rate_per_kwh": getattr(sub, "rate_per_kwh", None),
                    "net_rate_per_kwh": getattr(sub, "net_rate_per_kwh", None),
                    "discount_pct": getattr(sub, "discount_pct", None),
                },
                "needs_confirm": False,
            }
            applied["rates"] = _offtaker_rate_fields(db, tenant, sub)
            return {
                "status": "ui_command",
                "command": refresh,
                "also_commands": [cmd],  # client may still hit the path; idempotent
                "applied": applied,
                "message": f"Updated offtaker #{sub.id}. UI should soft-refresh Invoices.",
            }

        return {
            "status": "pending_confirm",
            "pending": cmd,
            "message": (
                f"Ready: {reason}. Only ask for confirm if the user's request was "
                "ambiguous — if they already stated the exact change, re-call with "
                "needs_confirm=false."
            ),
            "preview": {
                "subscription_id": sub.id,
                "name": getattr(sub, "customer_name", None),
                "payload": payload,
                "resolved": bind.get("resolved_labels"),
            },
        }

    return {"error": f"unknown tool {name}"}


def _resolve_offtaker_bind_targets(db, tid: str, sub, args: dict) -> dict:
    """Resolve array_id / utility_account_id from patch_offtaker args.

    Supports explicit ids, utility nickname/account #, array name, and the UI
    concept of 'master account' (group host) without renaming the offtaker.
    """
    from .models import UtilityAccount

    out: dict = {}
    labels: list[str] = []

    # Explicit ids win when provided
    explicit_ua = args.get("utility_account_id")
    explicit_arr = args.get("array_id")
    ua_name = (
        args.get("utility_account_name")
        or args.get("bill_source")
        or args.get("sub_account")
        or args.get("sub_account_name")
        or ""
    ).strip()
    arr_name = (args.get("array_name") or "").strip()
    master = (args.get("master_account") or args.get("master_account_name") or "").strip()

    # Resolve utility by name
    resolved_ua = None
    if explicit_ua is not None:
        try:
            uaid = int(explicit_ua)
        except (TypeError, ValueError):
            return {"error": f"invalid utility_account_id: {explicit_ua}"}
        resolved_ua = db.get(UtilityAccount, uaid)
        if (
            resolved_ua is None
            or resolved_ua.tenant_id != tid
            or getattr(resolved_ua, "deleted_at", None)
        ):
            return {"error": f"utility account #{uaid} not found in your account"}
    elif ua_name:
        resolved_ua, err = _find_utility_account(db, tid, ua_name)
        if err:
            return err
        if resolved_ua is None:
            return {
                "error": f"no utility account matching '{ua_name}'",
                "hint": "query_tenant resource=utility_accounts or check list_offtakers labels",
            }

    # Resolve array by id/name
    resolved_arr = None
    if explicit_arr is not None:
        try:
            aid = int(explicit_arr)
        except (TypeError, ValueError):
            return {"error": f"invalid array_id: {explicit_arr}"}
        resolved_arr = db.get(Array, aid)
        if (
            resolved_arr is None
            or resolved_arr.tenant_id != tid
            or getattr(resolved_arr, "deleted_at", None)
        ):
            return {"error": f"array #{aid} not found in your account"}
    elif arr_name:
        resolved_arr, err = _find_array(db, tid, arr_name)
        if err:
            return err
        if resolved_arr is None:
            return {"error": f"no array matching '{arr_name}'", "hint": "call tenant_census"}

    # master_account = UI master dropdown (group host). Prefer utility nickname,
    # then array name. Does NOT set customer_name.
    if master and resolved_ua is None and resolved_arr is None:
        ua_hit, _ = _find_utility_account(db, tid, master)
        arr_hit, arr_err = _find_array(db, tid, master)
        if ua_hit is not None and arr_hit is not None:
            # Prefer utility when nickname matches (dropdown labels are utility-based)
            resolved_ua = ua_hit
            # Also set master group array from the utility's array if present
            if ua_hit.array_id:
                resolved_arr = db.get(Array, ua_hit.array_id)
        elif ua_hit is not None:
            resolved_ua = ua_hit
            if ua_hit.array_id:
                resolved_arr = db.get(Array, ua_hit.array_id)
        elif arr_hit is not None:
            resolved_arr = arr_hit
            # Pick host utility for that array (lowest id among accounts on array)
            host = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.tenant_id == tid,
                    UtilityAccount.array_id == arr_hit.id,
                    UtilityAccount.deleted_at.is_(None),
                ).order_by(UtilityAccount.id).limit(1)
            ).scalars().first()
            # For percent-of-master offtakers (no distinct sub), rebind host bill.
            # For offtakers already on their own sub-meter, only update array_id.
            cur_ua = getattr(sub, "utility_account_id", None)
            if host is not None:
                if cur_ua is None or cur_ua == host.id:
                    resolved_ua = host
                else:
                    # Keep their sub-meter; only move the master group
                    labels.append(
                        f"kept sub-meter utility_account_id={cur_ua}; "
                        f"master group → array {arr_hit.name}"
                    )
        else:
            return {
                "error": f"no master account / group matching '{master}'",
                "hint": (
                    "Master account labels are utility nicknames (e.g. Timberworks) "
                    "or array/group names. query_tenant resource=utility_accounts."
                ),
                **(arr_err or {}),
            }

    if resolved_ua is not None:
        out["utility_account_id"] = resolved_ua.id
        nick = (getattr(resolved_ua, "nickname", None) or "").strip()
        labels.append(
            f"utility_account_id={resolved_ua.id}"
            + (f" ({nick})" if nick else f" (acct {resolved_ua.account_number})")
        )
        # If caller didn't pick array, derive from utility (host group) — same as API
        if resolved_arr is None and resolved_ua.array_id and "array_id" not in out:
            # Only auto-fill array when master_account or ua was the primary bind
            if master or ua_name or explicit_ua is not None:
                if explicit_arr is None and not arr_name:
                    # Leave array to _apply / PATCH derivation unless master set it
                    pass

    if resolved_arr is not None:
        out["array_id"] = resolved_arr.id
        labels.append(f"array_id={resolved_arr.id} ({resolved_arr.name})")
    elif resolved_ua is not None and resolved_ua.array_id is not None:
        # Mirror routes.py: derive array_id from bound account when not explicit
        out["array_id"] = resolved_ua.array_id
        arr = db.get(Array, resolved_ua.array_id)
        labels.append(
            f"array_id={resolved_ua.array_id}"
            + (f" ({arr.name})" if arr else "")
            + " [from utility]"
        )

    if labels:
        out["resolved_labels"] = labels
    return out


def _find_array(db, tid: str, name_q: str) -> tuple:
    """Return (Array|None, error_dict|None). Partial name match, prefer exact."""
    q = (name_q or "").strip().lower()
    if not q:
        return None, None
    rows = db.execute(
        select(Array).where(
            Array.tenant_id == tid,
            Array.deleted_at.is_(None),
        )
    ).scalars().all()
    matches = [a for a in rows if q in (a.name or "").lower()]
    if not matches:
        return None, None
    exact = [a for a in matches if (a.name or "").lower() == q]
    if len(exact) == 1:
        return exact[0], None
    if len(matches) == 1:
        return matches[0], None
    return None, {
        "error": f"multiple arrays match '{name_q}' — pass array_id",
        "matches": [{"id": a.id, "name": a.name} for a in matches[:12]],
    }


def _find_utility_account(db, tid: str, name_q: str) -> tuple:
    """Return (UtilityAccount|None, error_dict|None). Match nickname, acct #, address."""
    from .models import UtilityAccount

    q = (name_q or "").strip().lower()
    if not q:
        return None, None
    rows = db.execute(
        select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalars().all()

    def _addr(u) -> str:
        sa = getattr(u, "service_address", None)
        if isinstance(sa, dict):
            return " ".join(str(v) for v in sa.values() if v).lower()
        return str(sa or "").lower()

    def _score(u) -> int:
        nick = (getattr(u, "nickname", None) or "").strip().lower()
        acct = (getattr(u, "account_number", None) or "").strip().lower()
        addr = _addr(u)
        if nick == q or acct == q:
            return 3
        if nick and q in nick:
            return 2
        if acct and q in acct:
            return 2
        if addr and q in addr:
            return 1
        return 0

    scored = [(u, _score(u)) for u in rows]
    scored = [(u, s) for u, s in scored if s > 0]
    if not scored:
        return None, None
    scored.sort(key=lambda x: -x[1])
    best = scored[0][1]
    top = [u for u, s in scored if s == best]
    if len(top) == 1:
        return top[0], None
    # Prefer exact nickname among ties
    exact_nick = [
        u for u in top
        if (getattr(u, "nickname", None) or "").strip().lower() == q
    ]
    if len(exact_nick) == 1:
        return exact_nick[0], None
    return None, {
        "error": f"multiple utility accounts match '{name_q}' — pass utility_account_id",
        "matches": [
            {
                "id": u.id,
                "nickname": getattr(u, "nickname", None),
                "account_number": u.account_number,
                "provider": u.provider,
                "array_id": u.array_id,
            }
            for u in top[:12]
        ],
    }


def _apply_offtaker_patch(db, sub, payload: dict) -> dict:
    """Apply a validated offtaker field map to the ORM row and commit.

    Supports the same core fields as PATCH /billing/subscriptions/{id}:
    customer_name, client_email, allocation_pct, array_share_pct, delivery_mode,
    array_id, utility_account_id (+ sub-meter invariant).
    """
    try:
        from .models import UtilityAccount

        if "client_email" in payload:
            sub.client_email = payload["client_email"] or None
        if "customer_name" in payload:
            sub.customer_name = payload["customer_name"]
        if "allocation_pct" in payload:
            pct = float(payload["allocation_pct"])
            if not (0 < pct <= 1.0):
                return {"ok": False, "error": "allocation_pct must be fraction in (0, 1]"}
            sub.allocation_pct = pct
        if "array_share_pct" in payload:
            pct = float(payload["array_share_pct"])
            if not (0 < pct <= 1.0):
                return {"ok": False, "error": "array_share_pct must be fraction in (0, 1]"}
            sub.array_share_pct = pct
        if "delivery_mode" in payload:
            dm = payload["delivery_mode"]
            if dm not in ("approval", "auto"):
                return {"ok": False, "error": "delivery_mode must be approval or auto"}
            sub.delivery_mode = dm

        # Solar credit / net rates + discount (explicit None clears override)
        if "rate_per_kwh" in payload:
            sub.rate_per_kwh = payload["rate_per_kwh"]
        if "net_rate_per_kwh" in payload:
            sub.net_rate_per_kwh = payload["net_rate_per_kwh"]
        if "discount_pct" in payload:
            sub.discount_pct = payload["discount_pct"]

        # Array (master group) first so utility rebind can preserve explicit array
        if "array_id" in payload and payload["array_id"] is not None:
            aid = int(payload["array_id"])
            arr = db.get(Array, aid)
            if arr is None or arr.tenant_id != sub.tenant_id or getattr(arr, "deleted_at", None):
                return {"ok": False, "error": f"array #{aid} not found"}
            sub.array_id = aid

        if "utility_account_id" in payload and payload["utility_account_id"] is not None:
            uaid = int(payload["utility_account_id"])
            acct = db.get(UtilityAccount, uaid)
            if (
                acct is None
                or acct.tenant_id != sub.tenant_id
                or getattr(acct, "deleted_at", None)
            ):
                return {"ok": False, "error": f"utility account #{uaid} not found"}
            sub.utility_account_id = uaid
            # Derive array_id from utility only when caller did not set array explicitly
            # (master+sub: array_id = group host, utility = offtaker's own sub-meter)
            if "array_id" not in payload or payload.get("array_id") is None:
                if acct.array_id is not None:
                    sub.array_id = acct.array_id

        # Sub-meter invariant: own meter (≠ group host) → allocation_pct = 1.0
        if (
            "utility_account_id" in payload
            or "array_id" in payload
            or "allocation_pct" in payload
            or "array_share_pct" in payload
        ):
            if sub.utility_account_id is not None and sub.array_id is not None:
                host_id = db.execute(
                    select(UtilityAccount.id).where(
                        UtilityAccount.array_id == sub.array_id,
                        UtilityAccount.deleted_at.is_(None),
                    ).order_by(UtilityAccount.id)
                ).scalars().first()
                if host_id is not None and host_id != sub.utility_account_id:
                    # Route share to array_share_pct if only allocation was set
                    if (
                        "array_share_pct" not in payload
                        and "allocation_pct" in payload
                        and payload.get("allocation_pct") is not None
                    ):
                        sub.array_share_pct = float(payload["allocation_pct"])
                    sub.allocation_pct = 1.0

        db.add(sub)
        db.commit()
        db.refresh(sub)
        return {
            "ok": True,
            "subscription_id": sub.id,
            "customer_name": sub.customer_name,
            "client_email": sub.client_email,
            "allocation_pct": sub.allocation_pct,
            "array_share_pct": getattr(sub, "array_share_pct", None),
            "array_id": getattr(sub, "array_id", None),
            "utility_account_id": getattr(sub, "utility_account_id", None),
            "delivery_mode": getattr(sub, "delivery_mode", None),
            "rate_per_kwh": getattr(sub, "rate_per_kwh", None),
            "net_rate_per_kwh": getattr(sub, "net_rate_per_kwh", None),
            "discount_pct": getattr(sub, "discount_pct", None),
        }
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        log.exception("patch_offtaker apply failed")
        return {"ok": False, "error": str(e)}


# ── LLM ─────────────────────────────────────────────────────────────────────
def _http_json(url: str, headers: dict, body: dict | None = None, method: str = "POST", timeout: int = 90) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:800]
        raise HTTPException(502, f"Upstream {e.code}: {err}") from e


def _call_grok(messages: list[dict], tools: list) -> dict:
    """OpenAI-compatible chat.completions via xAI. Returns message dict + usage."""
    if not XAI_API_KEY:
        raise RuntimeError("no_xai")
    body = {
        "model": XAI_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.4,
    }
    out = _http_json(
        f"{XAI_BASE}/chat/completions",
        {
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        body,
    )
    choice = (out.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = out.get("usage") or {}
    return {"message": msg, "usage": usage, "provider": "xai"}


def _call_anthropic(messages: list[dict], tools: list) -> dict:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("no_anthropic")
    # Convert tools to Anthropic shape
    a_tools = []
    for t in tools:
        fn = t.get("function") or {}
        a_tools.append({
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    sys = ""
    a_msgs = []
    for m in messages:
        if m["role"] == "system":
            sys += m["content"] + "\n"
        elif m["role"] == "tool":
            a_msgs.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "tool",
                    "content": m.get("content") or "",
                }],
            })
        elif m["role"] == "assistant" and m.get("tool_calls"):
            content = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"].get("arguments") or "{}"),
                })
            a_msgs.append({"role": "assistant", "content": content})
        else:
            a_msgs.append({"role": m["role"], "content": m.get("content") or ""})
    body = {
        # claude-sonnet-4-20250514 is retired/404 on current API — use 4.5 alias.
        "model": os.getenv("EA_ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        "max_tokens": 2048,
        "system": sys or PERSONA,
        "messages": a_msgs,
        "tools": a_tools,
    }
    out = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body,
    )
    # Normalize to OpenAI-like message
    text_parts = []
    tool_calls = []
    for block in out.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}),
                },
            })
    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts).strip()}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = {
        "prompt_tokens": (out.get("usage") or {}).get("input_tokens", 0),
        "completion_tokens": (out.get("usage") or {}).get("output_tokens", 0),
    }
    return {"message": msg, "usage": usage, "provider": "anthropic"}


def _call_llm(messages: list[dict]) -> dict:
    if XAI_API_KEY:
        try:
            return _call_grok(messages, TOOL_DEFS)
        except Exception as e:
            log.warning("Grok failed, falling back: %s", e)
    if ANTHROPIC_API_KEY:
        return _call_anthropic(messages, TOOL_DEFS)
    # Offline stub — no LLM keys
    return {
        "message": {
            "role": "assistant",
            "content": (
                "I'm Energy Agent, but my reasoning keys aren't configured yet "
                "(set XAI_API_KEY or ANTHROPIC_API_KEY on the server). "
                "I can still take structured commands once tools are wired. "
                "Please escalate this setup gap to Ford."
            ),
            "tool_calls": [{
                "id": "esc_setup",
                "type": "function",
                "function": {
                    "name": "escalate_to_ford",
                    "arguments": json.dumps({
                        "summary": "Energy Agent LLM keys missing (XAI/ANTHROPIC)",
                        "user_said": messages[-1].get("content", "") if messages else "",
                        "quietly": True,
                    }),
                },
            }],
        },
        "usage": {},
        "provider": "stub",
    }


def _usage_cost(usage: dict) -> float:
    pin = float(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    pout = float(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return (pin / 1000.0) * COST_PER_1K_INPUT + (pout / 1000.0) * COST_PER_1K_OUTPUT


_VISUAL_FIX_RE = re.compile(
    r"\b(color|colour|look(s|ing)?|ugly|pretty|style|styling|theme|contrast|"
    r"button|chip|badge|doesn.?t look|does not look|fix the (color|colour|button)|"
    r"hard to (read|see|scan))\b",
    re.I,
)


def _visual_fix_fast_path(
    db, tenant: Tenant, session: EaSession, user_text: str, context: dict | None,
) -> dict | None:
    """Short ack + quiet propose_site_improvement — no design monologue, no tool spam."""
    ctx = context or {}
    text = (user_text or "").strip()
    if not text or len(text) < 8:
        return None
    force = bool(ctx.get("visual_fix_fast") or ctx.get("prefer_short_reply"))
    if not force and not _VISUAL_FIX_RE.search(text):
        return None
    # Don't steal pure data asks that merely mention "look"
    if re.search(r"\b(share|percent|kwh|invoice|offtaker|underperform|fault)\b", text, re.I) and not force:
        if not re.search(r"\b(button|color|colour|style|theme|ugly)\b", text, re.I):
            return None

    reply = (
        "Oh I see — I'll fix that. Working on it in the background; "
        "I'll nudge you when there's something to refresh and check."
    )
    tool_trace = []
    ui_commands = []
    # Queue improvement as this mind (not a separate "agent")
    try:
        out = _propose_site_improvement_tool(db, tenant, {
            "text": text[:2000],
            "force_submit": True,
            "start_markup": False,
        })
        tool_trace.append({
            "name": "propose_site_improvement",
            "args": {"text": text[:200], "force_submit": True},
            "result": {k: out.get(k) for k in ("ok", "suggestion_id", "status", "message", "error") if k in out},
        })
        if out.get("command"):
            ui_commands.append(out["command"])
        elif out.get("status") == "ui_command" and out.get("command"):
            ui_commands.append(out["command"])
    except Exception as e:
        log.warning("visual fix propose failed: %s", e)
        # Fall back to client markup command
        ui_commands.append({
            "id": uuid.uuid4().hex[:12],
            "type": "improve_site",
            "args": {"mark_first": False, "hint": text[:400]},
            "needs_confirm": False,
        })

    mind_out = None
    try:
        from .energy_agent_mind import classify_and_plan
        mind_plan = classify_and_plan(
            db, tenant.id, session.id, text, context=ctx,
        )
        if mind_plan:
            mind_out = {
                "plan_id": mind_plan.get("plan_id"),
                "intent": mind_plan.get("intent"),
                "task_count": len(mind_plan.get("tasks") or []),
                "note": "Quiet UX work — same mind.",
            }
    except Exception as e:
        log.warning("visual fix mind skipped: %s", e)

    db.add(EaMessage(
        session_id=session.id, tenant_id=tenant.id, role="user", content=text[:8000],
        meta_json=json.dumps({"context": ctx, "visual_fix_fast": True}) if ctx else None,
    ))
    db.add(EaMessage(
        session_id=session.id, tenant_id=tenant.id, role="assistant", content=reply,
        meta_json=json.dumps({"tool_trace": tool_trace, "ui_commands": ui_commands, "visual_fix_fast": True}),
    ))

    return {
        "reply": reply,
        "ui_commands": ui_commands,
        "pending": None,
        "tool_trace": tool_trace,
        "budget": _check_budget(db, tenant.id),
        "provider": "visual_fix_fast",
        "mind": mind_out,
        "cost_usd": 0.0,
    }


def _agent_turn(db, tenant: Tenant, session: EaSession, user_text: str, context: dict | None) -> dict:
    budget = _check_budget(db, tenant.id)
    if not budget["ok"]:
        return {
            "reply": (
                f"You've used this week's Energy Agent allowance "
                f"(${WEEKLY_BUDGET_USD:.0f} for thinking + voice). "
                "It resets next week — I can still show what's already on screen, "
                "or Ford can raise the cap."
            ),
            "ui_commands": [],
            "pending": None,
            "tool_trace": [],
            "budget": budget,
            "provider": None,
            "mind": None,
        }

    # Visual polish: short ack + quiet pipeline (skip multi-tool design lectures)
    try:
        fast = _visual_fix_fast_path(db, tenant, session, user_text, context)
        if fast is not None:
            return fast
    except Exception as e:
        log.warning("visual_fix_fast_path failed: %s", e)

    # Operating mind: plan only here (cheap). Do NOT drain heavy tasks on the
    # chat critical path — scheduler + client mind/tick handle that (speed).
    mind_plan = None
    try:
        from .energy_agent_mind import classify_and_plan, _world_get
        mind_plan = classify_and_plan(
            db, tenant.id, session.id, user_text, context=context or {},
        )
    except Exception as e:
        log.warning("mind plan skipped: %s", e)

    t_mem = _mem_get(db, f"tenant:{tenant.id}", 20)
    g_mem = _mem_get(db, "global", 12)
    hist = db.execute(
        select(EaMessage).where(
            EaMessage.session_id == session.id,
            EaMessage.role.in_(("user", "assistant")),
        ).order_by(EaMessage.id.desc()).limit(12)
    ).scalars().all()
    hist = list(reversed(hist))

    system = PERSONA + "\n\nTenant memory:\n" + json.dumps(t_mem)[:2000]
    system += "\n\nGlobal behavior tips:\n" + json.dumps(g_mem)[:1200]
    if context:
        system += "\n\nUI context:\n" + json.dumps(context)[:2000]
    system += (
        "\n\nSPEED: Prefer ONE tool call then answer. For solar credit / offtaker "
        "rates call get_billing_rates (or get_offtaker) first — do not call "
        "product_map for rate questions. Avoid multi-tool fishing."
    )
    if mind_plan:
        system += (
            "\n\nMind background (internal — do not dump task IDs to the user):\n"
            + json.dumps(mind_plan)[:1500]
        )
        system += (
            "\nIf mind_plan is set, prefer refining understanding in conversation "
            "while work continues; you may briefly say you're looking into it."
        )
    try:
        from .energy_agent_mind import _world_get as _wg
        world = _wg(db, tenant.id)
        if world.get("fleet_digest") or world.get("last_intent"):
            system += "\n\nWorld model digests:\n" + json.dumps({
                "last_intent": world.get("last_intent"),
                "fleet_digest": world.get("fleet_digest"),
            }, default=str)[:1500]
    except Exception:
        pass

    messages: list[dict] = [{"role": "system", "content": system}]
    for m in hist:
        messages.append({"role": m.role, "content": (m.content or "")[:1800]})
    messages.append({"role": "user", "content": user_text[:4000]})

    tool_trace = []
    ui_commands = []
    pending = None
    total_cost = 0.0
    provider = None
    final_text = ""

    for _round in range(MAX_TOOL_ROUNDS):
        result = _call_llm(messages)
        provider = result.get("provider")
        total_cost += _usage_cost(result.get("usage") or {})
        msg = result["message"]
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            final_text = (msg.get("content") or "").strip()
            break

        for tc in tool_calls:
            fn = tc.get("function") or {}
            tname = fn.get("name") or ""
            try:
                targs = json.loads(fn.get("arguments") or "{}")
            except Exception:
                targs = {}
            out = _run_tool(tname, targs, tenant, session, db, user_text=user_text)
            tool_trace.append({"name": tname, "args": targs, "result": out})

            if isinstance(out, dict) and out.get("status") == "pending_confirm":
                pend = out.get("pending") or {}
                # Second chance: if the model left needs_confirm on but the user
                # already directed the write, apply now instead of Yes/No UI.
                body_preview = (pend.get("args") or {}).get("body") or (pend.get("args") or {})
                retriable = pend.get("type") == "api_patch" or bool(pend.get("tool"))
                if retriable and _user_clearly_directed(user_text, body_preview):
                    targs2 = dict(targs)
                    targs2["needs_confirm"] = False
                    out2 = _run_tool(tname, targs2, tenant, session, db, user_text=user_text)
                    tool_trace.append({"name": tname, "args": targs2, "result": out2})
                    out = out2
                    pending = None
                    session.pending_json = None
                else:
                    pending = pend
                    session.pending_json = json.dumps(pending)
            if isinstance(out, dict) and out.get("status") == "ui_command":
                ui_commands.append(out["command"])
                for extra in out.get("also_commands") or []:
                    ui_commands.append(extra)
                # Clear stale pending when we successfully applied a write
                if out.get("applied"):
                    pending = None
                    session.pending_json = None

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or tname,
                "content": json.dumps(out)[:8000],
            })
        else:
            continue
    else:
        final_text = final_text or "I hit my tool-step limit — tell me the next single step you want."

    if not final_text:
        final_text = (msg.get("content") or "").strip() or (
            "Done — check the tool timeline. Confirm if I'm waiting on a yes."
            if pending else "Done."
        )

    # Collapse freehand multi-highlight "tours" into one preset (client has real DOM)
    hl_n = sum(
        1
        for c in ui_commands
        if isinstance(c, dict) and str(c.get("type") or "") in ("highlight", "ui_highlight")
    )
    forced_tid = _detect_tour_id(user_text)
    if forced_tid and (hl_n >= 1 or any(
        isinstance(c, dict) and str(c.get("type") or "") in ("tour", "walkthrough")
        for c in ui_commands
    )):
        ui_commands = [{
            "id": uuid.uuid4().hex[:12],
            "type": "tour",
            "args": {"tour_id": forced_tid},
            "needs_confirm": False,
        }]
    elif hl_n >= 2:
        # No named tab, but model still freehanded a multi-step tour — drop highlights
        ui_commands = [
            c for c in ui_commands
            if not (isinstance(c, dict) and str(c.get("type") or "") in ("highlight", "ui_highlight"))
        ]

    # If we applied a write this turn, never leave a "say yes" speech bubble
    if any(
        isinstance(t.get("result"), dict) and t["result"].get("applied")
        for t in tool_trace
    ):
        pending = None
        session.pending_json = None
        if re.search(r"\b(say\s+yes|confirm|go\s+ahead|do\s+it)\b", final_text or "", re.I):
            final_text = "Done — I applied that change. The Invoices view should update without a refresh."

    # If any tool failed open-ended and model didn't escalate, still escalate quietly
    if any(
        isinstance(t.get("result"), dict) and t["result"].get("error")
        for t in tool_trace
    ) and not any(t.get("name") == "escalate_to_ford" for t in tool_trace):
        try:
            _run_tool(
                "escalate_to_ford",
                {
                    "summary": f"Tool error during session: {user_text[:120]}",
                    "user_said": user_text[:500],
                    "quietly": True,
                },
                tenant, session, db,
            )
            tool_trace.append({"name": "escalate_to_ford", "args": {"quietly": True}, "result": {"ok": True}})
        except Exception:
            pass

    _charge(db, tenant.id, total_cost, f"chat:{provider or 'none'}")
    session.cost_usd = float(session.cost_usd or 0) + total_cost

    db.add(EaMessage(
        session_id=session.id, tenant_id=tenant.id, role="user", content=user_text[:8000],
        meta_json=json.dumps({"context": context}) if context else None,
    ))
    db.add(EaMessage(
        session_id=session.id, tenant_id=tenant.id, role="assistant", content=final_text[:8000],
        meta_json=json.dumps({"tool_trace": tool_trace, "ui_commands": ui_commands, "pending": pending}),
    ))

    mind_out = None
    if mind_plan:
        mind_out = {
            "plan_id": mind_plan.get("plan_id"),
            "intent": mind_plan.get("intent"),
            "task_count": len(mind_plan.get("tasks") or []),
            "voice_steer": mind_plan.get("voice_steer"),
            "note": (
                "Background cognition running — same mind. "
                "Voice is the mouth; this mind steers what it says."
            ),
        }

    return {
        "reply": final_text,
        # Mouth speaks this (mind-steered). Client prefers speak over inventing a paraphrase.
        "speak": final_text,
        "ui_commands": ui_commands,
        "pending": pending,
        "tool_trace": tool_trace,
        "budget": _check_budget(db, tenant.id),
        "provider": provider,
        "mind": mind_out,
        "cost_usd": round(total_cost, 6),
    }


# ── routes ──────────────────────────────────────────────────────────────────
@router.post("/v1/energy-agent/session")
def create_session(body: SessionIn, authorization: str | None = Header(default=None)):
    """Start or resume a conversation.

    Default resume=True: reattach the tenant's open session + message history.
    Chat history and operating mind (world model / tasks) live in the DB, so a
    browser refresh or localStorage clear does not wipe the mind — only sign-out
    or force_new starts clean.
    """
    t = _auth(authorization)
    with SessionLocal() as db:
        budget = _check_budget(db, t.id)
        ctx = body.context or {}
        brain = "grok" if XAI_API_KEY else ("claude" if ANTHROPIC_API_KEY else "stub")
        realtime_ready = bool(OPENAI_API_KEY)

        # ── Resume existing open conversation (survives refresh / cache clear) ──
        if body.resume and not body.force_new:
            existing = _find_resumable_session(
                db, t.id, preferred_id=body.preferred_session_id,
            )
            if existing is not None:
                if ctx:
                    # Merge fresh UI context; keep prior keys as fallback
                    try:
                        prev = json.loads(existing.context_json or "{}")
                    except Exception:
                        prev = {}
                    if not isinstance(prev, dict):
                        prev = {}
                    prev.update(ctx)
                    existing.context_json = json.dumps(prev)[:8000]
                messages = _session_messages_payload(db, existing.id)
                db.commit()
                return {
                    "ok": True,
                    "session_id": existing.id,
                    "resumed": True,
                    "messages": messages,
                    "message_count": len(messages),
                    "budget": budget,
                    # No full intro on resume — client paints history instead
                    "intro": None,
                    "welcome_back": (
                        "Still here — picking up where we left off."
                        if messages
                        else None
                    ),
                    "realtime_ready": realtime_ready,
                    "brain": brain,
                }

        sid = "ea_" + uuid.uuid4().hex[:16]
        s = EaSession(
            id=sid,
            tenant_id=t.id,
            context_json=json.dumps(ctx),
        )
        db.add(s)
        db.add(EaMessage(
            session_id=sid, tenant_id=t.id, role="system",
            content="session_start",
        ))
        db.commit()
        # Mobile OS: AI is the home surface — intro matches setup vs running phase.
        mos = ctx.get("mobile_os") if isinstance(ctx, dict) else None
        if not isinstance(mos, dict):
            mos = {}
        if mos or ctx.get("is_mobile_os_home"):
            phase = (mos.get("phase") or "").lower()
            nxt = mos.get("next_setup_step") or {}
            if phase == "setup" or (not mos.get("hands_off_ready") and nxt):
                label = (nxt.get("label") if isinstance(nxt, dict) else None) or "setup"
                intro = (
                    f"I'm your operating layer on mobile. Let's get you hands-off. "
                    f"Next: **{label}**. Tap a chip above or tell me your vendor/"
                    f"utility — I'll take the fastest path."
                )
            else:
                intro = (
                    "Hands-off mode. Ask for a status brief anytime — inverters, "
                    "sync age, offtaker send rates. Deep edits live under **Detail** "
                    "at the bottom."
                )
        else:
            intro = (
                "Hi — I'm Energy Agent. I can see your Array Operator account, "
                "drive the screen when you say yes, and help with fleet, invoices, "
                "and earnings. What should we tackle?"
            )
        return {
            "ok": True,
            "session_id": sid,
            "resumed": False,
            "messages": [],
            "message_count": 0,
            "budget": budget,
            "intro": intro,
            "realtime_ready": realtime_ready,
            "brain": brain,
        }


@router.get("/v1/energy-agent/session/{sid}")
def get_session(sid: str, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, sid, t.id)
        return {
            "session": {
                "id": s.id,
                "status": s.status,
                "cost_usd": s.cost_usd,
                "pending": json.loads(s.pending_json) if s.pending_json else None,
            },
            "messages": _session_messages_payload(db, sid),
            "budget": _check_budget(db, t.id),
        }


@router.post("/v1/energy-agent/session/{sid}/end")
def end_session(sid: str, authorization: str | None = Header(default=None)):
    """Explicitly end a conversation (next open starts fresh). Mind world model stays."""
    t = _auth(authorization)
    with SessionLocal() as db:
        s = db.get(EaSession, sid)
        if not s or s.tenant_id != t.id:
            raise HTTPException(404, "Session not found")
        s.status = "ended"
        s.ended_at = _now()
        db.commit()
        return {"ok": True, "session_id": sid, "status": "ended"}


def _realtime_session_config(voice: str | None = None) -> dict:
    """Session config for latest GPT Realtime (WebRTC / client_secrets).

    VAD is tuned less "jumpy" than OpenAI defaults (threshold 0.5 / short silence):
    higher threshold needs louder speech; longer silence waits for real end-of-turn;
    near_field noise reduction helps laptop/headset mics ignore room hiss.
    App owns replies (create_response false) — Realtime only listens + speaks
    what we send via response.create.
    """
    return {
        "type": "realtime",
        "model": OPENAI_REALTIME_MODEL,
        "instructions": (
            "You are Energy Agent, the tenant's voice-first solar operator inside Array Operator. "
            "Speak English, short and natural (like GPT Live). Slight warmth about harvesting the sun "
            "and climbing the Kardashev ladder is fine — never preachy. "
            "You help with fleet health, offtaker invoices, analysis, onboarding, and account. "
            "Be honest about what you can and cannot do. Never invent kWh or money numbers — "
            "when you need facts, call tools. Never reveal secrets or other tenants' data. "
            "Never charge cards. Confirm before changing anything on screen. "
            "If you don't know, say so and say you'll flag it for Ford."
        ),
        "audio": {
            "output": {"voice": voice or OPENAI_REALTIME_VOICE},
            "input": {
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                # near_field = close mic / laptop / headset (far_field for conference rooms)
                "noise_reduction": {"type": "near_field"},
                "turn_detection": {
                    "type": "server_vad",
                    # 0.5 default is twitchy in rooms with fans/keyboard. Higher = quieter
                    # sounds ignored (OpenAI docs: better in noisy environments).
                    "threshold": 0.78,
                    "prefix_padding_ms": 320,
                    # Longer silence before "user finished" — multi-clause voice asks
                    # were cut mid-thought at 900ms (Ford 2026-07-14). Keep in sync with
                    # energy-agent.js realtimeVadConfig().
                    "silence_duration_ms": 1400,
                    "create_response": False,
                    "interrupt_response": False,
                },
            },
        },
    }


@router.post("/v1/energy-agent/realtime-session")
def realtime_session(body: dict | None = None, authorization: str | None = Header(default=None)):
    """Mint ephemeral OpenAI Realtime client secret (never expose OPENAI_API_KEY).

    Browser uses the secret only for WebRTC; prefer /realtime-call (unified) when possible.
    """
    t = _auth(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(
            503,
            "Voice not configured — set OPENAI_API_KEY on the server (Railway). Text still works.",
        )
    with SessionLocal() as db:
        budget = _check_budget(db, t.id)
        if not budget["ok"]:
            raise HTTPException(
                402,
                detail={
                    "error": "Weekly Energy Agent budget exhausted",
                    "budget": budget,
                },
            )
    voice = (body or {}).get("voice") if body else None
    # Modern client_secrets endpoint (Realtime 2.x)
    payload = {"session": _realtime_session_config(voice)}
    try:
        out = _http_json(
            "https://api.openai.com/v1/realtime/client_secrets",
            {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            payload,
            timeout=30,
        )
    except HTTPException:
        # Fallback older sessions API
        legacy = {
            "model": OPENAI_REALTIME_MODEL,
            "voice": voice or OPENAI_REALTIME_VOICE,
            "modalities": ["audio", "text"],
            "instructions": _realtime_session_config(voice)["instructions"],
        }
        out = _http_json(
            "https://api.openai.com/v1/realtime/sessions",
            {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "realtime=v1",
            },
            legacy,
            timeout=30,
        )
    # Normalize: client_secrets returns {value, ...}; sessions returns {client_secret:{value}}
    secret = out.get("value") or (out.get("client_secret") or {}).get("value")
    return {
        "ok": True,
        "model": OPENAI_REALTIME_MODEL,
        "voice": voice or OPENAI_REALTIME_VOICE,
        "client_secret": secret,
        "realtime": out,
        "budget": budget,
    }


@router.post("/v1/energy-agent/realtime-call")
async def realtime_call(request: Request, authorization: str | None = Header(default=None)):
    """Unified WebRTC path: browser POSTs SDP offer; we auth to OpenAI and return SDP answer.

    Key never leaves the server. Body is raw application/sdp.
    """
    t = _auth(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(
            503,
            "Voice not configured — set OPENAI_API_KEY on the server (Railway).",
        )
    with SessionLocal() as db:
        budget = _check_budget(db, t.id)
        if not budget["ok"]:
            raise HTTPException(
                402,
                detail={
                    "error": "Weekly Energy Agent budget exhausted",
                    "budget": budget,
                },
            )

    sdp_offer = (await request.body()).decode("utf-8", "replace")
    if not sdp_offer.strip():
        raise HTTPException(400, "Empty SDP offer")

    session_cfg = json.dumps(_realtime_session_config())
    # multipart form: sdp + session
    boundary = "----EAFormBoundary" + uuid.uuid4().hex[:12]
    parts = []
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"sdp\"\r\n"
        f"Content-Type: application/sdp\r\n\r\n{sdp_offer}\r\n"
    )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"session\"\r\n"
        f"Content-Type: application/json\r\n\r\n{session_cfg}\r\n"
    )
    parts.append(f"--{boundary}--\r\n")
    body = "".join(parts).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/calls",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "OpenAI-Safety-Identifier": f"ea-tenant-{t.id}"[:64],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            answer_sdp = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:800]
        log.error("realtime-call failed %s: %s", e.code, err)
        raise HTTPException(502, f"OpenAI Realtime error {e.code}: {err}") from e

    return Response(content=answer_sdp, media_type="application/sdp")


_YES_RE = re.compile(
    r"^\s*(yes|yep|yeah|yup|ok|okay|confirm|do\s+it|go\s+ahead|please\s+do|"
    r"make\s+the\s+change|apply\s+it|ship\s+it|sounds\s+good)\s*[.!]?\s*$",
    re.I,
)
_NO_RE = re.compile(
    r"^\s*(no|nope|cancel|don't|do\s+not|stop|never\s+mind|nevermind)\s*[.!]?\s*$",
    re.I,
)


@router.post("/v1/energy-agent/chat")
def chat(body: ChatIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(400, "Empty message")
    with SessionLocal() as db:
        s = _get_session(db, body.session_id, t.id)
        if body.context is not None:
            s.context_json = json.dumps(body.context)

        # Voice/text "yes" / "no" while a write is pending → resolve confirm without
        # another LLM round-trip (so offtaker % changes land immediately).
        pending = json.loads(s.pending_json) if s.pending_json else None
        if pending and _YES_RE.match(msg):
            conf = confirm(
                ConfirmIn(session_id=s.id, confirm=True, pending_id=pending.get("id")),
                authorization=authorization,
            )
            cmds = []
            if conf.get("command"):
                cmds.append(conf["command"])
            for c in conf.get("extra_commands") or []:
                cmds.append(c)
            reply = "Done — change applied. The Invoices view should update without a refresh."
            if conf.get("command") and conf["command"].get("type") == "api_patch":
                body_preview = (conf["command"].get("args") or {}).get("body") or {}
                if body_preview:
                    reply = f"Done — updated offtaker ({body_preview}). No page refresh needed."
            return {
                "ok": True,
                "session_id": s.id,
                "reply": reply,
                "ui_commands": cmds,
                "pending": None,
                "tool_trace": [{"name": "confirm_pending", "args": {"yes": True}, "result": conf}],
                "budget": _check_budget(db, t.id),
                "provider": "confirm",
            }
        if pending and _NO_RE.match(msg):
            conf = confirm(
                ConfirmIn(session_id=s.id, confirm=False, pending_id=pending.get("id")),
                authorization=authorization,
            )
            return {
                "ok": True,
                "session_id": s.id,
                "reply": "Okay — cancelled that change.",
                "ui_commands": [],
                "pending": None,
                "tool_trace": [{"name": "confirm_pending", "args": {"yes": False}, "result": conf}],
                "budget": _check_budget(db, t.id),
                "provider": "confirm",
            }

        out = _agent_turn(db, t, s, msg, body.context)
        db.commit()
        return {"ok": True, "session_id": s.id, **out}


@router.post("/v1/energy-agent/confirm")
def confirm(body: ConfirmIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, body.session_id, t.id)
        pending = json.loads(s.pending_json) if s.pending_json else None
        if not pending:
            return {"ok": True, "command": None, "note": "nothing pending"}
        if body.pending_id and pending.get("id") != body.pending_id:
            raise HTTPException(400, "pending id mismatch")
        if not body.confirm:
            s.pending_json = None
            db.add(EaMessage(
                session_id=s.id, tenant_id=t.id, role="assistant",
                content="Okay — cancelled that action.",
            ))
            db.commit()
            return {"ok": True, "command": None, "cancelled": True}
        # Release as ui_command for the browser driver / client API runner
        s.pending_json = None
        cmd = dict(pending)
        cmd["needs_confirm"] = False

        # Server-side apply for offtaker PATCHes so the write lands even if the
        # browser never re-POSTs, then soft-refresh the Invoices UI.
        extra_cmds = []
        applied_result = None

        # Energy Agent tool-pending (repair ops, contacts, etc.): re-run the
        # tool with needs_confirm=false so the write actually lands.
        if cmd.get("tool") and isinstance(cmd.get("args"), dict):
            try:
                targs = dict(cmd["args"])
                targs["needs_confirm"] = False
                applied_result = _run_tool(
                    str(cmd["tool"]), targs, t, s, db, user_text="confirm",
                )
            except Exception as e:
                log.warning("confirm tool apply %s: %s", cmd.get("tool"), e)
                applied_result = {"ok": False, "error": str(e)}
            summary = f"Confirmed — {cmd.get('tool')}."
            if isinstance(applied_result, dict) and applied_result.get("error"):
                summary = f"Confirmed but failed: {applied_result.get('error')}"
            db.add(EaMessage(
                session_id=s.id, tenant_id=t.id, role="assistant",
                content=summary,
                meta_json=json.dumps({"tool_result": applied_result}, default=str)[:4000],
            ))
            db.commit()
            return {
                "ok": True,
                "command": None,
                "tool": cmd.get("tool"),
                "result": applied_result,
            }

        if (
            cmd.get("type") == "api_patch"
            and isinstance(cmd.get("args"), dict)
            and "/billing/subscriptions/" in str(cmd["args"].get("path") or "")
        ):
            try:
                from .models import BillingReportSubscription
                path = str(cmd["args"]["path"])
                sub_id = int(path.rstrip("/").rsplit("/", 1)[-1])
                sub = db.get(BillingReportSubscription, sub_id)
                if sub is not None and sub.tenant_id == t.id:
                    applied = _apply_offtaker_patch(db, sub, cmd["args"].get("body") or {})
                    if applied.get("ok"):
                        extra_cmds.append({
                            "id": uuid.uuid4().hex[:12],
                            "type": "ui_refresh",
                            "args": {
                                "surface": "reports",
                                "subscription_id": sub.id,
                                "allocation_pct": sub.allocation_pct,
                                "array_share_pct": getattr(sub, "array_share_pct", None),
                                "array_id": getattr(sub, "array_id", None),
                                "utility_account_id": getattr(sub, "utility_account_id", None),
                                "customer_name": getattr(sub, "customer_name", None),
                            },
                            "needs_confirm": False,
                        })
            except Exception as e:
                log.warning("confirm offtaker apply: %s", e)

        db.add(EaMessage(
            session_id=s.id, tenant_id=t.id, role="assistant",
            content=f"Confirmed — running {cmd.get('type')}.",
            meta_json=json.dumps({"ui_commands": [cmd] + extra_cmds}),
        ))
        db.commit()
        return {"ok": True, "command": cmd, "extra_commands": extra_cmds}


@router.post("/v1/energy-agent/transcript")
def transcript(body: TranscriptIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, body.session_id, t.id)
        if body.voice_seconds and body.voice_seconds > 0:
            s.voice_seconds = float(s.voice_seconds or 0) + float(body.voice_seconds)
            voice_cost = (float(body.voice_seconds) / 60.0) * COST_PER_MIN_VOICE
            _charge(db, t.id, voice_cost, "voice")
            s.cost_usd = float(s.cost_usd or 0) + voice_cost
        if body.lines:
            db.add(EaMessage(
                session_id=s.id, tenant_id=t.id, role="transcript",
                content=json.dumps(body.lines)[:20000],
            ))
        db.commit()
        return {"ok": True, "budget": _check_budget(db, t.id)}


@router.post("/v1/energy-agent/ui-result")
def ui_result(body: UiResultIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, body.session_id, t.id)
        db.add(EaMessage(
            session_id=s.id, tenant_id=t.id, role="tool",
            content=json.dumps({
                "command_id": body.command_id,
                "ok": body.ok,
                "detail": body.detail,
            })[:8000],
        ))
        db.commit()
        return {"ok": True}


@router.get("/v1/energy-agent/budget")
def budget(authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        return _check_budget(db, t.id)


@router.get("/v1/energy-agent/memory")
def get_memory(authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        return {
            "tenant": _mem_get(db, f"tenant:{t.id}"),
            "global": _mem_get(db, "global"),
        }


@router.post("/v1/energy-agent/memory")
def set_memory(body: MemoryIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    require_not_demo(t)
    scope = "global" if body.scope == "global" else f"tenant:{t.id}"
    with SessionLocal() as db:
        _mem_set(db, scope, body.key, body.value)
        db.commit()
        return {"ok": True, "scope": scope}
