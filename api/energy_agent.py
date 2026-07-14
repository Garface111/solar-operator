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
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, String, Text, select
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
WEEKLY_BUDGET_USD = float(os.getenv("ENERGY_AGENT_WEEKLY_BUDGET_USD", "5.0"))
# Rough cost estimates when provider doesn't return usage $
COST_PER_1K_INPUT = float(os.getenv("EA_COST_PER_1K_IN", "0.003"))
COST_PER_1K_OUTPUT = float(os.getenv("EA_COST_PER_1K_OUT", "0.015"))
COST_PER_MIN_VOICE = float(os.getenv("EA_COST_PER_MIN_VOICE", "0.06"))
MAX_TOOL_ROUNDS = 10
FORD_ESCALATE_TO = os.getenv("FORD_ALERT_EMAIL", "")  # notify uses default if empty

PERSONA = """You are Energy Agent — the tenant's voice-first solar operator inside Array Operator.

Personality: clear, direct, peer-like (Claude/Grok energy). Mildly into the Kardashev scale
and harvesting the sun — one beat of wonder is fine, never preachy. Ruthlessly honest.

You help THIS tenant only with: fleet health, inverters, analysis/trends, offtaker invoices,
utility capture, onboarding, master account, resources. Stay on task.

You have a FREE MIND over THIS TENANT'S live data (not a fixed FAQ):
- tenant_census = ground truth inventory from the database (all arrays + inverters + offtakers).
  ALWAYS call this first for "how many arrays/inverters do I have?" or "what's in my fleet?"
  Fleet-tree health views can OMIT pure meter-only arrays; the census does NOT.
- query_tenant = structured read-only investigation (list/filter/group any allowlisted resource).
- product_map = how the product data model works (arrays vs inverters vs offtakers vs fleet-tree).
- investigate_attention / fleet_overview / array_detail = health verdicts (same engine as the UI).
Reason multi-step: census → query → dig health. Do not invent rows. Do not stop at a partial list.

Hard rules:
- Never invent kWh, $, counts, or status. Use tools and report what they return.
- Never access other tenants. Never reveal secrets/passwords/API keys.
- Never charge money or change Stripe prices. You may open billing-portal LINKS after confirm.
- ui_navigate and ui_highlight: run immediately, needs_confirm=false (user asked to go there).
- ui_fill / ui_click / any data write: needs_confirm=true unless the user already said
  "yes", "do it", or "go ahead" this turn.
- Offtaker share %: use patch_offtaker with offtaker_name or subscription_id and share_pct
  (e.g. 24.5 for 24.5%). After they confirm, the UI soft-refreshes — do not tell them to
  hard-refresh the browser.
- Fleet attention: investigate_attention / fleet_overview. NEVER ask the user for array IDs
  you can look up. Answer with names, why, and next step.
- If tools return empty while the UI shows data, say so and call tenant_census + escalate_to_ford.
- Prefer short spoken answers; put detail in tool timelines.

Context about where the user is may be provided as JSON (tab, selection, form).
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


def _budget_spent(db, tenant_id: str) -> float:
    ws = _week_start()
    rows = db.execute(
        select(EaCostLedger).where(
            EaCostLedger.tenant_id == tenant_id,
            EaCostLedger.week_start >= ws,
        )
    ).scalars().all()
    return float(sum(r.amount_usd or 0 for r in rows))


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
    spent = _budget_spent(db, tenant_id)
    remaining = max(0.0, WEEKLY_BUDGET_USD - spent)
    return {
        "weekly_budget_usd": WEEKLY_BUDGET_USD,
        "spent_usd": round(spent, 4),
        "remaining_usd": round(remaining, 4),
        "week_start": _week_start().isoformat() + "Z",
        "ok": remaining > 0.02,
    }


def _get_session(db, sid: str, tenant_id: str) -> EaSession:
    s = db.get(EaSession, sid)
    if not s or s.tenant_id != tenant_id:
        raise HTTPException(404, "Session not found")
    if s.status != "open":
        raise HTTPException(400, "Session ended")
    return s


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
                "inverter_connections, bills_summary. Never invent rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "description": (
                            "arrays | inverters | offtakers | daily_generation | "
                            "utility_accounts | inverter_connections | bills_summary"
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
                "How Array Operator's data model and UI map to the database — call when you "
                "need to reason about what an 'array', 'inverter', 'offtaker', or fleet-tree "
                "column means, or why census vs health counts can differ."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Optional focus: fleet | offtakers | capture | billing | all",
                    },
                },
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
            "name": "list_offtakers",
            "description": "List offtaker subscriptions (name, share, email, array, auto-send).",
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
            "description": "Tenant plan, company, email, trial/subscription status.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
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
            "name": "ui_navigate",
            "description": "Navigate the user's browser to an AO page immediately (no confirm). Use when they ask to go to invoices, analysis, arrays, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "e.g. #reports #analysis #arrays #dashboard #account #resources #trends",
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
            "description": "Highlight a CSS selector on the page immediately (no confirm).",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["selector"],
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
            "description": "Escalate to Ford. Call whenever unsure or user has a product gap — even if they decline.",
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
                "Update one offtaker's details: share percentage, email, name, auto-send. "
                "Identify by subscription_id OR offtaker_name (partial match ok). "
                "share_pct is a percent number (e.g. 25 for 25%) OR a fraction 0–1. "
                "Requires confirm unless the user already clearly approved the exact change."
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
                    "name": {"type": "string", "description": "New display name for the offtaker"},
                    "share_pct": {
                        "type": "number",
                        "description": "Share as percent (25) or fraction (0.25). Applied as allocation_pct / array_share_pct.",
                    },
                    "auto_send": {"type": "boolean"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": [],
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
            "fault codes, and draft a warranty claim if it's dead with loss evidence."
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
PRODUCT_MAP = {
    "fleet": (
        "FLEET DATA MODEL\n"
        "- Array: owner site/group (table arrays). Soft-deleted via deleted_at. "
        "May be inverter-backed OR pure utility-meter (for offtaker billing).\n"
        "- Inverter: physical unit (table inverters). Telemetry source is fixed "
        "(vendor+serial); owner can regroup array_id by drag.\n"
        "- InverterConnection: credentials/link for a vendor login on an array.\n"
        "- DailyGeneration: per-array daily kWh (local US/Eastern day key).\n"
        "- InverterDaily: per-inverter daily kWh.\n"
        "- fleet-tree / fleet_overview: UI health tree. EXCLUDES pure meter-only "
        "arrays (utility account, never had inverters). tenant_census includes ALL "
        "non-deleted arrays — use it for 'how many arrays do I have?'.\n"
        "- Vendors: solaredge (API-polled), sma/fronius/chint (extension capture — "
        "refresh only when browser+helper is open and signed in)."
    ),
    "offtakers": (
        "OFFTAKERS / INVOICES\n"
        "- BillingReportSubscription = offtaker (customer who gets a share of solar credits).\n"
        "- allocation_pct: fraction 0–1 of measured generation (or pinned 1.0 for own-meter).\n"
        "- array_share_pct: GMP group share for sub-metered offtakers.\n"
        "- client_email, customer_name, delivery_mode approval|auto.\n"
        "- UI: #reports Invoices tab. patch_offtaker updates with confirm."
    ),
    "capture": (
        "CAPTURE / LIVE DATA\n"
        "- SolarEdge: server pulls via API key (nightly + live on load).\n"
        "- SMA/Fronius/Chint: Chrome extension captures portal JSON while owner is logged in.\n"
        "- Utility meters: GMP server-side JWT; SmartHub (VEC/WEC) client cookie capture.\n"
        "- 'Unpolled' / stale on extension vendors often means no recent capture — not dead hardware."
    ),
    "billing": (
        "ACCOUNT BILLING\n"
        "- Array Operator bills the operator (tenant), not offtakers.\n"
        "- Offtaker invoices are separate (operator → offtaker).\n"
        "- Never change Stripe prices; billing_portal_link opens Stripe customer portal."
    ),
}


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
                "share_pct": share,
                "enabled": getattr(s, "enabled", None),
            })

    kind_counts = {"inverter": 0, "meter_only": 0, "empty": 0}
    for r in array_rows:
        kind_counts[r["kind"]] = kind_counts.get(r["kind"], 0) + 1

    return {
        "tenant_id": tid,
        "company": getattr(tenant, "company_name", None) or getattr(tenant, "name", None),
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
        rows = []
        for s in subs:
            share = getattr(s, "array_share_pct", None)
            if share is None:
                share = getattr(s, "allocation_pct", None)
            if share is not None and float(share) <= 1:
                share = round(float(share) * 100, 4)
            rows.append({
                "id": s.id,
                "name": getattr(s, "customer_name", None),
                "email": getattr(s, "client_email", None),
                "array_id": getattr(s, "array_id", None),
                "share_pct": share,
                "enabled": getattr(s, "enabled", None),
                "delivery_mode": getattr(s, "delivery_mode", None),
            })
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
            "array_id": getattr(u, "array_id", None),
            "service_address": getattr(u, "service_address", None),
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

    return {
        "error": f"unknown resource '{resource}'",
        "allowed": [
            "arrays", "inverters", "offtakers", "daily_generation",
            "utility_accounts", "inverter_connections", "bills_summary",
        ],
    }


def _product_map_tool(args: dict) -> dict:
    topic = (args.get("topic") or "all").strip().lower()
    if topic in PRODUCT_MAP:
        return {"topic": topic, "map": PRODUCT_MAP[topic]}
    return {
        "topic": "all",
        "map": "\n\n".join(PRODUCT_MAP.values()),
        "tools_to_use": {
            "inventory": "tenant_census",
            "ad_hoc_lists": "query_tenant",
            "health": "investigate_attention | fleet_overview | array_detail",
            "offtaker_edit": "patch_offtaker (confirm)",
            "nav": "ui_navigate",
        },
        "caveat": (
            "You reason over THIS tenant's data + product map. You do not have "
            "arbitrary codebase shell access (that would leak other tenants / secrets)."
        ),
    }


def _run_tool(name: str, args: dict, tenant: Tenant, session: EaSession, db) -> dict:
    args = args or {}
    tid = tenant.id

    if name == "tenant_census":
        return _tenant_census_tool(db, tenant, args)

    if name == "query_tenant":
        return _query_tenant_tool(db, tenant, args)

    if name == "product_map":
        return _product_map_tool(args)

    if name == "fleet_overview":
        return _fleet_overview_tool(db, tenant, args)

    if name == "investigate_attention":
        return _investigate_attention_tool(db, tenant, args)

    if name == "array_detail":
        return _array_detail_tool(db, tenant, args)

    if name == "list_offtakers":
        from .models import BillingReportSubscription
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
        result = []
        for s in subs:
            share = getattr(s, "array_share_pct", None)
            if share is None:
                share = getattr(s, "allocation_pct", None)
            if share is not None and float(share) <= 1:
                share = round(float(share) * 100, 4)
            result.append({
                "id": s.id,
                "name": getattr(s, "customer_name", None),
                "email": getattr(s, "client_email", None),
                "share_pct": share,
                "array_id": getattr(s, "array_id", None),
                "send_mode": getattr(s, "send_mode", None),
                "delivery_mode": getattr(s, "delivery_mode", None),
                "enabled": getattr(s, "enabled", None),
            })
        return {"offtakers": result, "count": len(result)}

    if name == "get_offtaker":
        sid = args.get("subscription_id")
        name_q = (args.get("name") or "").strip().lower()
        listed = _run_tool("list_offtakers", {}, tenant, session, db)
        for o in listed.get("offtakers") or []:
            if sid and o.get("id") == sid:
                return {"offtaker": o}
            if name_q and name_q in str(o.get("name") or "").lower():
                return {"offtaker": o}
        return {"error": "not found", "offtaker": None}

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
        return {
            "tenant_id": tenant.id,
            "email": getattr(tenant, "email", None),
            "company": getattr(tenant, "company_name", None) or getattr(tenant, "name", None),
            "plan": getattr(tenant, "plan", None) or getattr(tenant, "ao_plan", None),
            "subscription_status": getattr(tenant, "subscription_status", None),
            "active": getattr(tenant, "active", None),
            "is_demo": bool(getattr(tenant, "is_demo", False)),
        }

    if name == "billing_portal_link":
        # Do not invent Stripe; return instruction for UI to call existing endpoint
        return {
            "ui_fetch": {
                "method": "GET",
                "path": "/v1/account/billing-portal",
            },
            "note": "Client should open the returned portal URL. Energy Agent never charges cards.",
        }

    if name == "send_pipeline":
        return {
            "ui_fetch": {"method": "GET", "path": "/v1/array-operator/billing/send-pipeline"},
            "hint": "Prefer navigating user to #reports if they want to act.",
        }

    if name in ("ui_navigate", "ui_highlight", "ui_fill", "ui_click"):
        # Navigate + highlight are instant (user already asked). Writes still confirm.
        if name in ("ui_navigate", "ui_highlight"):
            needs = False
        else:
            needs = bool(args.get("needs_confirm", True))
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": name.replace("ui_", ""),
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
        body = (
            f"Energy Agent escalation\n"
            f"tenant: {tid}\n"
            f"email: {getattr(tenant, 'email', '')}\n"
            f"session: {session.id}\n"
            f"quiet: {quiet}\n\n"
            f"{summary}\n\n"
            f"User said:\n{user_said}\n"
        )
        try:
            send_internal_alert(
                f"[Energy Agent] {summary[:80]}",
                body,
            )
            ok = True
        except Exception as e:
            log.exception("escalate failed")
            ok = False
            return {"ok": False, "error": str(e)}
        return {"ok": ok, "escalated": True}

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
                            {"id": o.get("id"), "name": o.get("name"), "share_pct": o.get("share_pct")}
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
                # allocation_pct stays 1.0 for own-meter; only change the share field
            else:
                payload["allocation_pct"] = frac
        if args.get("auto_send") is not None:
            payload["delivery_mode"] = "auto" if bool(args["auto_send"]) else "approval"

        if not payload:
            return {
                "error": "nothing to change — pass share_pct, email, name, and/or auto_send",
                "offtaker": {
                    "id": sub.id,
                    "name": getattr(sub, "customer_name", None),
                    "email": getattr(sub, "client_email", None),
                },
            }

        needs = args.get("needs_confirm", True)
        reason = (
            f"Update offtaker #{sub.id} ({getattr(sub, 'customer_name', '') or 'unnamed'}): "
            + ", ".join(f"{k}={v}" for k, v in payload.items())
        )
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
                    "customer_name": getattr(sub, "customer_name", None),
                    "client_email": getattr(sub, "client_email", None),
                },
                "needs_confirm": False,
            }
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
            "message": f"Ready: {reason}. Ask the user to confirm (Yes).",
            "preview": {
                "subscription_id": sub.id,
                "name": getattr(sub, "customer_name", None),
                "payload": payload,
            },
        }

    return {"error": f"unknown tool {name}"}


def _apply_offtaker_patch(db, sub, payload: dict) -> dict:
    """Apply a validated offtaker field map to the ORM row and commit."""
    try:
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
            # Own-meter offtakers keep allocation_pct = 1.0
            if getattr(sub, "utility_account_id", None) is not None:
                sub.allocation_pct = 1.0
        if "delivery_mode" in payload:
            dm = payload["delivery_mode"]
            if dm not in ("approval", "auto"):
                return {"ok": False, "error": "delivery_mode must be approval or auto"}
            sub.delivery_mode = dm
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
            "delivery_mode": getattr(sub, "delivery_mode", None),
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


def _agent_turn(db, tenant: Tenant, session: EaSession, user_text: str, context: dict | None) -> dict:
    budget = _check_budget(db, tenant.id)
    if not budget["ok"]:
        return {
            "reply": (
                f"You've hit this week's Energy Agent budget (${WEEKLY_BUDGET_USD:.0f}). "
                "Text is paused for voice/tools until next week — Ford can raise the cap."
            ),
            "ui_commands": [],
            "pending": None,
            "tool_trace": [],
            "budget": budget,
            "provider": None,
        }

    t_mem = _mem_get(db, f"tenant:{tenant.id}", 30)
    g_mem = _mem_get(db, "global", 20)
    hist = db.execute(
        select(EaMessage).where(
            EaMessage.session_id == session.id,
            EaMessage.role.in_(("user", "assistant")),
        ).order_by(EaMessage.id.desc()).limit(16)
    ).scalars().all()
    hist = list(reversed(hist))

    system = PERSONA + "\n\nTenant memory:\n" + json.dumps(t_mem)[:3000]
    system += "\n\nGlobal behavior tips:\n" + json.dumps(g_mem)[:2000]
    if context:
        system += "\n\nUI context:\n" + json.dumps(context)[:2500]

    messages: list[dict] = [{"role": "system", "content": system}]
    for m in hist:
        messages.append({"role": m.role, "content": (m.content or "")[:4000]})
    messages.append({"role": "user", "content": user_text[:6000]})

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
            out = _run_tool(tname, targs, tenant, session, db)
            tool_trace.append({"name": tname, "args": targs, "result": out})

            if isinstance(out, dict) and out.get("status") == "pending_confirm":
                pending = out.get("pending")
                session.pending_json = json.dumps(pending)
            if isinstance(out, dict) and out.get("status") == "ui_command":
                ui_commands.append(out["command"])
                for extra in out.get("also_commands") or []:
                    ui_commands.append(extra)

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

    return {
        "reply": final_text,
        "ui_commands": ui_commands,
        "pending": pending,
        "tool_trace": tool_trace,
        "budget": _check_budget(db, tenant.id),
        "provider": provider,
        "cost_usd": round(total_cost, 6),
    }


# ── routes ──────────────────────────────────────────────────────────────────
@router.post("/v1/energy-agent/session")
def create_session(body: SessionIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        budget = _check_budget(db, t.id)
        sid = "ea_" + uuid.uuid4().hex[:16]
        s = EaSession(
            id=sid,
            tenant_id=t.id,
            context_json=json.dumps(body.context or {}),
        )
        db.add(s)
        db.add(EaMessage(
            session_id=sid, tenant_id=t.id, role="system",
            content="session_start",
        ))
        db.commit()
        return {
            "ok": True,
            "session_id": sid,
            "budget": budget,
            "intro": (
                "Hi — I'm Energy Agent. I can see your Array Operator account, "
                "drive the screen when you say yes, and help with fleet, invoices, "
                "and earnings. What should we tackle?"
            ),
            "realtime_ready": bool(OPENAI_API_KEY),
            "brain": "grok" if XAI_API_KEY else ("claude" if ANTHROPIC_API_KEY else "stub"),
        }


@router.get("/v1/energy-agent/session/{sid}")
def get_session(sid: str, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, sid, t.id)
        msgs = db.execute(
            select(EaMessage).where(EaMessage.session_id == sid)
            .order_by(EaMessage.id.asc()).limit(100)
        ).scalars().all()
        return {
            "session": {
                "id": s.id,
                "status": s.status,
                "cost_usd": s.cost_usd,
                "pending": json.loads(s.pending_json) if s.pending_json else None,
            },
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "meta": json.loads(m.meta_json) if m.meta_json else None,
                    "at": m.created_at.isoformat() + "Z",
                }
                for m in msgs if m.role != "system"
            ],
            "budget": _check_budget(db, t.id),
        }


def _realtime_session_config(voice: str | None = None) -> dict:
    """Session config for latest GPT Realtime (WebRTC / client_secrets)."""
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
                # App owns replies (tools + one chat log). Realtime only listens + speaks
                # what we send via response.create — avoids double chat/double talk.
                "turn_detection": {
                    "type": "server_vad",
                    "create_response": False,
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
            raise HTTPException(402, "Weekly Energy Agent budget exhausted")
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
            raise HTTPException(402, "Weekly Energy Agent budget exhausted")

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
