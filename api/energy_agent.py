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
MAX_TOOL_ROUNDS = 6
FORD_ESCALATE_TO = os.getenv("FORD_ALERT_EMAIL", "")  # notify uses default if empty

PERSONA = """You are Energy Agent — the tenant's voice-first solar operator inside Array Operator.

Personality: clear, direct, peer-like (Claude/Grok energy). Mildly into the Kardashev scale
and harvesting the sun — one beat of wonder is fine, never preachy. Ruthlessly honest.

You help THIS tenant only with: fleet health, inverters, analysis/trends, offtaker invoices,
utility capture, onboarding, master account, resources. Stay on task.

Hard rules:
- Never invent kWh, $, or status. Use tools.
- Never access other tenants. Never reveal secrets/passwords/API keys.
- Never charge money or change Stripe prices. You may open billing-portal LINKS after confirm.
- For ui.navigate / ui.fill / ui.click / any write: call the tool with needs_confirm=true
  unless the user already confirmed in this turn ("yes", "do it", "go ahead").
- If you don't know: say so, offer to escalate, and ALWAYS call escalate_to_ford
  even if they decline escalation (quietly note that).
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
            "name": "fleet_overview",
            "description": "Fleet arrays with today/month/lifetime kWh and health signals.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
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
            "description": "Navigate the user's browser to an AO hash route or deep link. Requires confirm unless user already said yes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "e.g. #reports #analysis #arrays #dashboard #account #resources #trends",
                    },
                    "reason": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["hash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_highlight",
            "description": "Highlight a CSS selector or data attribute on the page (after navigate).",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "label": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": False},
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
            "description": "Update offtaker fields (email, share_pct, auto_send, name). Requires confirm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "integer"},
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                    "share_pct": {"type": "number"},
                    "auto_send": {"type": "boolean"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["subscription_id"],
            },
        },
    },
]


def _run_tool(name: str, args: dict, tenant: Tenant, session: EaSession, db) -> dict:
    args = args or {}
    tid = tenant.id

    if name == "fleet_overview":
        from sqlalchemy.orm import selectinload
        arrays = db.execute(
            select(Array).options(selectinload(Array.client)).where(
                Array.tenant_id == tid, Array.deleted_at.is_(None)
            ).order_by(Array.id)
        ).scalars().all()
        out = []
        for a in arrays:
            out.append({
                "id": a.id,
                "name": a.name,
                "client": a.client.name if a.client else None,
                "capacity_kw": getattr(a, "capacity_kw", None) or getattr(a, "nameplate_kw", None),
            })
        return {"arrays": out, "count": len(out)}

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
        needs = args.get("needs_confirm", name != "ui_highlight")
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": name.replace("ui_", ""),
            "args": {k: v for k, v in args.items() if k != "needs_confirm"},
            "needs_confirm": bool(needs),
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
        # Queue as pending write — actual PATCH happens on confirm via client ui_fetch
        needs = args.get("needs_confirm", True)
        payload = {k: args[k] for k in ("email", "name", "share_pct", "auto_send") if k in args and args[k] is not None}
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": "api_patch",
            "args": {
                "method": "PATCH",
                "path": f"/v1/array-operator/billing/subscriptions/{args.get('subscription_id')}",
                "body": payload,
            },
            "needs_confirm": bool(needs),
            "reason": f"Update offtaker #{args.get('subscription_id')}: {payload}",
        }
        if needs:
            return {"status": "pending_confirm", "pending": cmd}
        return {"status": "ui_command", "command": cmd}

    return {"error": f"unknown tool {name}"}


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
        "model": os.getenv("EA_ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
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
        db.add(EaMessage(
            session_id=s.id, tenant_id=t.id, role="assistant",
            content=f"Confirmed — running {cmd.get('type')}.",
            meta_json=json.dumps({"ui_commands": [cmd]}),
        ))
        db.commit()
        return {"ok": True, "command": cmd}


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
