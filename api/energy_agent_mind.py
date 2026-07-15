"""Energy Agent — Operating Mind (continuous cognition).

North star: conversation is one window into a mind that thinks continuously.
Not "voice plus agents" — one mind, background tasks, seamless updates.

Phases:
  A foundations · B interrupt policy · C richer workers · D metrics

See docs/plans/2026-07-14-energy-agent-operating-mind.md
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .account import require_not_demo, tenant_from_session
from .db import SessionLocal
from .models import Base

log = logging.getLogger("energy_agent.mind")
router = APIRouter()


def _now() -> datetime:
    return datetime.utcnow()


# ── interrupt policy (Phase B) ──────────────────────────────────────────────
# Importance 0–100. Only surface interrupts at/above MIN when rate budget allows.
MIN_IMPORTANCE_TO_SPEAK = 55
INTERRUPT_COOLDOWN_SEC = 90
INTERRUPT_MAX_PER_HOUR = 3
INTERRUPT_MAX_PER_DAY = 12

# Baseline importance by task kind (adjusted by result richness)
KIND_IMPORTANCE: dict[str, int] = {
    "note_complaint": 15,
    "snapshot_context": 10,
    "search_similar": 50,
    "fleet_pulse": 45,
    "propose_ui_candidate": 40,
    "propose_ui": 82,
    "analyze_focus": 68,
    "mark_improvement": 90,
    # Phase E — long-term / proactive
    "proactive_insight": 48,  # quiet by default; only speak on spike / first notice
    "prepare_ux_approval": 78,
    "profile_sync": 12,
}

# Cheap cost attribution for workers (ledger reason worker:<kind>)
WORKER_COST_USD: dict[str, float] = {
    "note_complaint": 0.0,
    "snapshot_context": 0.0,
    "search_similar": 0.002,
    "fleet_pulse": 0.01,
    "propose_ui_candidate": 0.001,
    "propose_ui": 0.05,  # queues judge pipeline — real cost is downstream
    "analyze_focus": 0.015,
    "proactive_insight": 0.008,
    "prepare_ux_approval": 0.02,
    "profile_sync": 0.0,
}

# How often the long-term mind may email the owner (proactive insights)
PROACTIVE_EMAIL_COOLDOWN_HOURS = 20
# How often the same fleet insight may interrupt in-app (spam guard)
PROACTIVE_SPEAK_COOLDOWN_HOURS = 8
# Min hours between proactive_insight *runs* unless attention count changes
PROACTIVE_RUN_COOLDOWN_HOURS = 6
# Attention spike threshold to wake proactive insight
ATTENTION_SPIKE_DELTA = 1

# HARD GATE: proactive mind emails to the *owner* only for allowlisted addresses.
# Never blast random tenants — Ford dogfood only until productized.
# Internal Ford alerts (ops) are separate and may still fire for any tenant.
_MIND_OWNER_EMAIL_ALLOWLIST = frozenset({
    "ford.genereaux@gmail.com",
    "ford.genereaux@dysonswarmtechnologies.com",
    "ford@dysonswarmtechnologies.com",
})


# ── persistence ─────────────────────────────────────────────────────────────
class EaWorldState(Base):
    """Per-tenant world model (lightweight). Revisioned blob + digests."""
    __tablename__ = "ea_world_state"
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EaPlan(Base):
    """A user intent broken into objectives + child tasks."""
    __tablename__ = "ea_plans"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|done|cancelled
    intent: Mapped[str] = mapped_column(Text, default="")
    objectives_json: Mapped[str] = mapped_column(Text, default="[]")
    user_utterance: Mapped[str] = mapped_column(Text, default="")


class EaTask(Base):
    """Background unit of work. Mind speaks; tasks stay invisible as 'agents'."""
    __tablename__ = "ea_tasks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # queued | running | done | failed | cancelled
    priority: Mapped[int] = mapped_column(Integer, default=50)
    title: Mapped[str] = mapped_column(String(200), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Spoken interrupt candidate when done (mind decides whether to surface)
    speak_hint: Mapped[str | None] = mapped_column(Text, nullable=True)


class EaEvent(Base):
    """Append-only event stream for seamless updates + UI activity."""
    __tablename__ = "ea_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    # plan_created | task_queued | task_done | task_failed | mind_note |
    # interrupt_candidate | interrupt_suppressed | interrupt_outcome | improvement_win
    ref_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # If set, client may inject as same-mind spoken/text update
    speak_as_mind: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed: Mapped[int] = mapped_column(Integer, default=0)  # 0|1 for interrupt delivery


# ── auth ────────────────────────────────────────────────────────────────────
def _auth(authorization: str | None):
    t = tenant_from_session(authorization)
    require_not_demo(t)
    return t


def _emit(
    db,
    tenant_id: str,
    kind: str,
    summary: str,
    *,
    session_id: str | None = None,
    ref_id: str | None = None,
    payload: dict | None = None,
    speak_as_mind: str | None = None,
) -> EaEvent:
    ev = EaEvent(
        tenant_id=tenant_id,
        session_id=session_id,
        kind=kind,
        ref_id=ref_id,
        summary=summary[:2000],
        payload_json=json.dumps(payload or {}, default=str)[:8000],
        speak_as_mind=(speak_as_mind or "")[:2000] or None,
    )
    db.add(ev)
    db.flush()
    return ev


def _payload(ev: EaEvent) -> dict:
    try:
        return json.loads(ev.payload_json or "{}")
    except Exception:
        return {}


def _default_world() -> dict:
    """Canonical long-term mind profile + digests (one mind per tenant)."""
    return {
        "revision": 0,
        "open_intents": [],
        "notes": {},
        "fleet_digest": None,
        "profile": {
            # Preferences that survive sessions — the mind "knows you".
            # Owner-email defaults OFF for everyone; hard allowlist still required.
            "email_insights": False,      # proactive fleet/UX notes by email (Ford only)
            "email_ux_approvals": False,  # when mind prepares/ships a UI change (Ford only)
            "auto_prepare_ux": True,      # may stage UX proposals offline (silent + Ford ops mail)
            "auto_approve_ux": False,     # only auto-queue judge after clear pattern
            "voice_pref": "one_mind",     # never narrate multi-agent
        },
        "insights": [],                   # recent proactive insight objects
        "pending_approvals": [],          # UX changes prepared, awaiting/notified
        "last_proactive_at": None,
        "last_proactive_email_at": None,
        "last_attention_count": None,
        "last_wake_reason": None,
        "last_wake_at": None,
    }


def _tenant_contact_email(db, tenant_id: str) -> str | None:
    try:
        from .models import Tenant
        t = db.get(Tenant, tenant_id)
        return (getattr(t, "contact_email", None) or None) if t else None
    except Exception:
        return None


def _merge_profile(db, tenant_id: str, stored: dict | None) -> dict:
    """Defaults + stored prefs. Email flags only True if *explicitly* stored True.

    Never auto-enable email for anyone (including Ford). User must ask / opt in.
    Non-allowlisted contacts can never receive owner mail at send time either.
    """
    base = dict((_default_world().get("profile") or {}))
    stored = dict(stored or {})
    base.update(stored)
    # Coerce: only explicit True counts as opted in
    base["email_insights"] = stored.get("email_insights") is True
    base["email_ux_approvals"] = stored.get("email_ux_approvals") is True
    email = _tenant_contact_email(db, tenant_id)
    base["_ford_dogfood"] = _owner_email_allowed(email)
    base["_email_eligible"] = bool(base["_ford_dogfood"])  # allowlist only for now
    return base


def _wants_owner_email(world: dict, flag: str) -> bool:
    """True only when profile flag is explicitly True (opt-in)."""
    prof = (world or {}).get("profile") or {}
    return prof.get(flag) is True


def _world_get(db, tenant_id: str) -> dict:
    base = _default_world()
    row = db.get(EaWorldState, tenant_id)
    if not row:
        base["profile"] = _merge_profile(db, tenant_id, None)
        return base
    try:
        data = json.loads(row.state_json or "{}")
    except Exception:
        data = {}
    data = {**base, **data, "profile": _merge_profile(db, tenant_id, data.get("profile"))}
    data["revision"] = row.revision
    data["updated_at"] = row.updated_at.isoformat() + "Z" if row.updated_at else None
    data["last_tick_at"] = row.last_tick_at.isoformat() + "Z" if row.last_tick_at else None
    return data


def _world_patch(db, tenant_id: str, patch: dict) -> dict:
    row = db.get(EaWorldState, tenant_id)
    if not row:
        row = EaWorldState(tenant_id=tenant_id, state_json="{}", revision=0)
        db.add(row)
        db.flush()
    try:
        cur = json.loads(row.state_json or "{}")
    except Exception:
        cur = {}
    cur.update(patch or {})
    row.state_json = json.dumps(cur, default=str)[:50000]
    row.revision = int(row.revision or 0) + 1
    row.updated_at = _now()
    db.flush()
    return _world_get(db, tenant_id)


def _charge_worker(db, tenant_id: str, kind: str, amount: float | None = None) -> float:
    """Attribute small worker cost to the EA weekly ledger (soft).

    Uses a savepoint so a missing ledger table never aborts the task.
    """
    cost = float(amount if amount is not None else WORKER_COST_USD.get(kind, 0.0) or 0.0)
    if cost <= 0:
        return 0.0
    try:
        from .energy_agent import _charge
        try:
            nested = db.begin_nested()  # SAVEPOINT
        except Exception:
            nested = None
        try:
            _charge(db, tenant_id, cost, f"worker:{kind}"[:64])
            if nested is not None:
                nested.commit()
            else:
                db.flush()
        except Exception as e:
            log.debug("worker charge skipped: %s", e)
            if nested is not None:
                try:
                    nested.rollback()
                except Exception:
                    pass
    except Exception as e:
        log.debug("worker charge outer skip: %s", e)
    return cost


# ── Phase B: importance + rate limit ────────────────────────────────────────
def score_importance(kind: str, result: dict | None, speak: str | None) -> int:
    """0–100 importance for whether the mind should interrupt the user."""
    base = int(KIND_IMPORTANCE.get(kind, 30))
    result = result or {}
    boost = 0

    if kind == "search_similar":
        hits = result.get("hits") or []
        boost += min(35, 8 * len(hits))
        if result.get("suggestion_hits"):
            boost += 10
    elif kind == "fleet_pulse":
        dig = result.get("fleet_digest") or {}
        n = int(dig.get("attention_count") or 0)
        if n <= 0:
            boost -= 25  # quiet fleet → rarely interrupt
        else:
            boost += min(40, 8 * n)
    elif kind == "propose_ui_candidate":
        if result.get("deferred"):
            boost += 5
        if result.get("expected_value_high"):
            boost += 25
    elif kind == "propose_ui":
        if result.get("suggestion_id"):
            boost += 15
        if result.get("ok") is False:
            boost -= 40
    elif kind == "analyze_focus":
        if result.get("problems"):
            boost += 15
    elif kind == "proactive_insight":
        ins = result.get("insight") or {}
        if result.get("silent") or result.get("duplicate"):
            boost -= 80  # never interrupt repeats
        elif result.get("attention_spike"):
            boost += 30
        else:
            boost += max(0, int(ins.get("importance") or 0) - 70)
        if result.get("emailed", {}).get("owner"):
            boost -= 20
    elif kind == "prepare_ux_approval":
        if result.get("suggestion_id"):
            boost += 10

    if speak and len(speak) > 20:
        boost += 5

    return max(0, min(100, base + boost))


def interrupt_budget(db, tenant_id: str) -> dict:
    """How many interrupts remain under rate policy."""
    world = _world_get(db, tenant_id)
    now = _now()
    last_iso = world.get("last_interrupt_at")
    last_dt = None
    if last_iso:
        try:
            last_dt = datetime.fromisoformat(str(last_iso).replace("Z", ""))
        except Exception:
            last_dt = None

    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    hour_n = db.execute(
        select(func.count()).select_from(EaEvent).where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "interrupt_candidate",
            EaEvent.created_at >= hour_ago,
        )
    ).scalar() or 0
    day_n = db.execute(
        select(func.count()).select_from(EaEvent).where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "interrupt_candidate",
            EaEvent.created_at >= day_ago,
        )
    ).scalar() or 0

    cooldown_ok = True
    cooldown_remaining = 0
    if last_dt:
        elapsed = (now - last_dt).total_seconds()
        if elapsed < INTERRUPT_COOLDOWN_SEC:
            cooldown_ok = False
            cooldown_remaining = int(INTERRUPT_COOLDOWN_SEC - elapsed)

    allow = (
        cooldown_ok
        and int(hour_n) < INTERRUPT_MAX_PER_HOUR
        and int(day_n) < INTERRUPT_MAX_PER_DAY
    )
    return {
        "allow": allow,
        "hour_count": int(hour_n),
        "day_count": int(day_n),
        "max_per_hour": INTERRUPT_MAX_PER_HOUR,
        "max_per_day": INTERRUPT_MAX_PER_DAY,
        "cooldown_ok": cooldown_ok,
        "cooldown_remaining_sec": cooldown_remaining,
        "min_importance": MIN_IMPORTANCE_TO_SPEAK,
    }


def maybe_queue_interrupt(
    db,
    tenant_id: str,
    *,
    session_id: str | None,
    ref_id: str | None,
    title: str,
    speak: str | None,
    kind: str,
    result: dict | None,
) -> dict:
    """Apply policy: emit interrupt_candidate or interrupt_suppressed."""
    importance = score_importance(kind, result, speak)
    budget = interrupt_budget(db, tenant_id)
    payload = {
        "task_kind": kind,
        "importance": importance,
        "budget": {
            "hour_count": budget["hour_count"],
            "day_count": budget["day_count"],
            "cooldown_ok": budget["cooldown_ok"],
        },
    }

    if not speak:
        return {"emitted": False, "importance": importance, "reason": "no_speak"}

    if importance < MIN_IMPORTANCE_TO_SPEAK:
        _emit(
            db, tenant_id, "interrupt_suppressed",
            f"low importance ({importance}): {title}",
            session_id=session_id, ref_id=ref_id,
            payload={**payload, "reason": "low_importance"},
        )
        return {"emitted": False, "importance": importance, "reason": "low_importance"}

    if not budget["allow"]:
        reason = "cooldown" if not budget["cooldown_ok"] else "rate_limit"
        _emit(
            db, tenant_id, "interrupt_suppressed",
            f"{reason} ({importance}): {title}",
            session_id=session_id, ref_id=ref_id,
            payload={**payload, "reason": reason},
        )
        return {"emitted": False, "importance": importance, "reason": reason}

    _emit(
        db, tenant_id, "interrupt_candidate",
        title,
        session_id=session_id, ref_id=ref_id,
        speak_as_mind=speak,
        payload=payload,
    )
    _world_patch(db, tenant_id, {
        "last_interrupt_at": _now().isoformat() + "Z",
        "last_interrupt_importance": importance,
        "last_interrupt_kind": kind,
    })
    return {"emitted": True, "importance": importance, "reason": "ok"}


# ── intent → plan (lightweight, no extra LLM required) ──────────────────────
_UX_FRICTION = re.compile(
    r"\b(hard to use|confusing|cluttered|can.?t find|difficult|ux|ui|layout|"
    r"dashboard is (bad|hard|messy)|wish .{0,40}(easier|better|clearer))\b",
    re.I,
)
_FLEET_WORRY = re.compile(
    r"\b(underperform|not producing|what.?s wrong|attention|down|fault|offline|"
    r"why is .{0,30}(low|bad|dark))\b",
    re.I,
)
_CAPTURE_Q = re.compile(
    r"\b(how did you get|cloud capture|auto-?refresh|extension|where.*(login|password)|"
    r"never entered)\b",
    re.I,
)
# User green-lights a UI proposal (Phase C refresh-and-ask loop)
_YES_PROPOSAL = re.compile(
    r"\b("
    r"open (a |the )?proposal|yes[,.]? (open|propose|do|ship|build)|"
    r"go ahead|ship it|build (it|that|this)|please (fix|improve|change)|"
    r"do the (ui|layout|change)|want (you to )?(open|propose)"
    r")\b",
    re.I,
)
_FINDING = re.compile(
    r"\b(find(ing)?|search(ing)?|locate|can.?t find|where is|buried|hidden)\b",
    re.I,
)
_UNDERSTANDING = re.compile(
    r"\b(understand(ing)?|make sense|confus|clutter|scan|overwhelm|what (does|do) "
    r".{0,20} mean|too much)\b",
    re.I,
)


def _count_recent_ux_friction(db, tenant_id: str, days: int = 14) -> int:
    since = _now() - timedelta(days=days)
    n = db.execute(
        select(func.count()).select_from(EaPlan).where(
            EaPlan.tenant_id == tenant_id,
            EaPlan.intent == "ux_friction",
            EaPlan.created_at >= since,
        )
    ).scalar() or 0
    return int(n)


def classify_and_plan(
    db,
    tenant_id: str,
    session_id: str | None,
    user_text: str,
    context: dict | None = None,
) -> dict | None:
    """If utterance warrants continuous work, create plan + cheap tasks.

    Returns plan summary for the mind to optionally acknowledge, or None.
    Heavy workers (propose_ui) run only on clear user yes or high expected value.
    """
    text = (user_text or "").strip()
    if len(text) < 4:
        return None
    ctx = context or {}
    world = _world_get(db, tenant_id)

    # Explicit opt-in / opt-out for email updates (never email unless asked).
    # Opt-out first so "stop email updates" does not match opt-in "email updates".
    low = text.lower()
    if re.search(
        r"\b(stop email|no more emails?|don'?t email|do not email|unsubscribe|"
        r"fewer emails?|turn off email|no email updates|stop (the )?updates)\b",
        low,
    ):
        prof = dict(world.get("profile") or {})
        prof["email_insights"] = False
        prof["email_ux_approvals"] = False
        _world_patch(db, tenant_id, {"profile": prof})
        _emit(
            db, tenant_id, "mind_note",
            "User opted out of email updates",
            session_id=session_id,
            payload={"email_insights": False, "email_ux_approvals": False},
        )
    elif re.search(
        r"\b(email me|send me (emails?|updates)|"
        r"keep me (posted|updated) by email|proactive emails?|"
        r"more updates by email|notify me by email|"
        r"please email (me )?updates|email updates please)\b",
        low,
    ):
        prof = dict(world.get("profile") or {})
        prof["email_insights"] = True
        prof["email_ux_approvals"] = True
        _world_patch(db, tenant_id, {"profile": prof})
        _emit(
            db, tenant_id, "mind_note",
            "User opted in to email updates",
            session_id=session_id,
            payload={"email_insights": True, "email_ux_approvals": True},
        )

    plan_kind = None
    objectives: list[str] = []
    tasks: list[dict] = []

    # ── Phase C: user accepted a proposal after friction dialogue ──
    if _YES_PROPOSAL.search(text) and (
        world.get("last_intent") == "ux_friction"
        or world.get("pending_ui_proposal")
        or world.get("ux_clarification")
    ):
        plan_kind = "ux_proposal_execute"
        source_text = (
            (world.get("pending_ui_proposal") or {}).get("text")
            or world.get("last_user_utterance")
            or text
        )
        clarification = world.get("ux_clarification") or "unspecified"
        objectives = [
            "Ship a grounded UI proposal via the existing judge pipeline",
            "Ask user to refresh and validate scanability",
            "Stay one mind — no agent-swarm narration",
        ]
        tasks = [
            {
                "kind": "propose_ui",
                "title": "Queue UI improvement for judge",
                "priority": 20,
                "payload": {
                    "text": source_text,
                    "clarification": clarification,
                    "context": world.get("last_context") or ctx,
                    "user_confirm": text,
                },
                "speak_hint": (
                    "Quick update: I queued a layout improvement based on what you described. "
                    "When it lands, refresh and tell me if it feels easier to scan."
                ),
            },
        ]
    elif _UX_FRICTION.search(text):
        plan_kind = "ux_friction"
        friction_n = _count_recent_ux_friction(db, tenant_id)
        high_ev = friction_n >= 2  # repeated friction → higher expected value
        objectives = [
            "Keep conversation refining what 'hard' means",
            "Improve UX with minimal churn",
            "Ground work in current UI context",
        ]
        tasks = [
            {
                "kind": "snapshot_context",
                "title": "Remember where you were in the product",
                "priority": 40,
                "payload": {"context": ctx},
            },
            {
                "kind": "note_complaint",
                "title": "Save the friction note",
                "priority": 30,
                "payload": {"text": text, "hash": ctx.get("hash"), "tab": ctx.get("tab_label")},
            },
            {
                "kind": "search_similar",
                "title": "Look for similar past notes",
                "priority": 50,
                "payload": {"text": text},
            },
            {
                "kind": "propose_ui_candidate",
                "title": "Consider a UI improvement proposal",
                "priority": 70 if high_ev else 80,
                "payload": {
                    "text": text,
                    "context": ctx,
                    "friction_count_14d": friction_n,
                    "expected_value_high": high_ev,
                },
                "speak_hint": (
                    (
                        "You've hit this kind of friction more than once. "
                        "I can open a real layout proposal now — want me to?"
                    )
                    if high_ev
                    else (
                        "I held a UI direction until we pin down what feels hard. "
                        "Is it finding the information, or making sense of what you see?"
                    )
                ),
            },
        ]
        if high_ev:
            # Optionally pre-stage propose_ui as low priority (still needs user or auto-gate)
            pass
    elif world.get("last_intent") == "ux_friction" and (
        _FINDING.search(text) or _UNDERSTANDING.search(text)
    ):
        # Refinement of prior UX complaint — update world model, maybe strengthen proposal
        clarification = "finding" if _FINDING.search(text) else "understanding"
        if _FINDING.search(text) and _UNDERSTANDING.search(text):
            clarification = "both"
        plan_kind = "ux_refine"
        objectives = [
            f"Refine UX friction as primarily: {clarification}",
            "Prepare a tighter proposal if user wants one",
        ]
        tasks = [
            {
                "kind": "snapshot_context",
                "title": "Update UX clarification in world model",
                "priority": 30,
                "payload": {
                    "context": ctx,
                    "ux_clarification": clarification,
                    "pending_ui_proposal": {
                        "text": world.get("last_user_utterance") or text,
                        "clarification": clarification,
                    },
                },
            },
            {
                "kind": "search_similar",
                "title": "Search notes matching this friction type",
                "priority": 45,
                "payload": {
                    "text": f"{clarification} {world.get('last_user_utterance') or text}",
                    "topic": clarification,
                },
            },
            {
                "kind": "propose_ui_candidate",
                "title": "Tighten UI proposal from clarification",
                "priority": 55,
                "payload": {
                    "text": world.get("last_user_utterance") or text,
                    "clarification": clarification,
                    "context": ctx,
                    "expected_value_high": True,
                },
                "speak_hint": (
                    f"Got it — more about {clarification}. "
                    "I can open a proposal that leads with status and scannability. Want that?"
                ),
            },
        ]
    elif _FLEET_WORRY.search(text):
        plan_kind = "fleet_concern"
        objectives = [
            "Stay truthful to live fleet data",
            "Explain attention without inventing causes",
        ]
        tasks = [
            {
                "kind": "fleet_pulse",
                "title": "Refresh fleet attention into world model",
                "priority": 35,
                "payload": {},
                "speak_hint": (
                    "I refreshed the fleet picture in the background. "
                    "I can walk the worst sites if you want."
                ),
            },
            {
                "kind": "note_complaint",
                "title": "Note the fleet concern",
                "priority": 45,
                "payload": {"text": text, "topic": "fleet"},
            },
            {
                "kind": "analyze_focus",
                "title": "Summarize attention for conversation",
                "priority": 50,
                "payload": {"limit": 6},
            },
        ]
    elif _CAPTURE_Q.search(text):
        plan_kind = "capture_provenance"
        objectives = [
            "Explain capture paths honestly (cloud vs extension one-click)",
            "Never invent vault credentials",
        ]
        tasks = [
            {
                "kind": "note_complaint",
                "title": "Note capture-provenance question",
                "priority": 40,
                "payload": {"text": text, "topic": "capture"},
            },
            {
                "kind": "snapshot_context",
                "title": "Store extension/context flags",
                "priority": 50,
                "payload": {
                    "extension_present": ctx.get("extension_present"),
                    "capture_mode_client": ctx.get("capture_mode_client"),
                    "fleet_vendors_client": ctx.get("fleet_vendors_client"),
                },
            },
        ]
    else:
        return None

    plan_id = "pl_" + uuid.uuid4().hex[:16]
    plan = EaPlan(
        id=plan_id,
        tenant_id=tenant_id,
        session_id=session_id,
        status="open",
        intent=plan_kind,
        objectives_json=json.dumps(objectives),
        user_utterance=text[:4000],
    )
    db.add(plan)
    _emit(
        db, tenant_id, "plan_created",
        f"Plan {plan_kind}: {text[:120]}",
        session_id=session_id, ref_id=plan_id,
        payload={"objectives": objectives},
    )

    created_tasks = []
    for tdef in tasks:
        tid = "tk_" + uuid.uuid4().hex[:16]
        task = EaTask(
            id=tid,
            tenant_id=tenant_id,
            plan_id=plan_id,
            session_id=session_id,
            kind=tdef["kind"],
            status="queued",
            priority=int(tdef.get("priority") or 50),
            title=tdef.get("title") or tdef["kind"],
            payload_json=json.dumps(tdef.get("payload") or {}, default=str)[:8000],
            speak_hint=tdef.get("speak_hint"),
        )
        db.add(task)
        _emit(
            db, tenant_id, "task_queued",
            task.title,
            session_id=session_id, ref_id=tid,
            payload={"kind": task.kind},
        )
        created_tasks.append({"id": tid, "kind": task.kind, "title": task.title})

    patch: dict[str, Any] = {
        "last_intent": plan_kind,
        "last_user_utterance": text[:500],
        "open_plan_id": plan_id,
    }
    if plan_kind in ("ux_friction", "ux_refine"):
        patch["pending_ui_proposal"] = {
            "text": text[:1000] if plan_kind == "ux_friction" else (world.get("last_user_utterance") or text)[:1000],
            "plan_id": plan_id,
        }
    if plan_kind == "ux_refine":
        clar = "finding" if _FINDING.search(text) else "understanding"
        if _FINDING.search(text) and _UNDERSTANDING.search(text):
            clar = "both"
        patch["ux_clarification"] = clar
    if plan_kind == "ux_proposal_execute":
        patch["pending_ui_proposal"] = None

    _world_patch(db, tenant_id, patch)

    return {
        "plan_id": plan_id,
        "intent": plan_kind,
        "objectives": objectives,
        "tasks": created_tasks,
        "mind_note": (
            "Background work started silently — keep refining understanding in conversation. "
            "Do not list internal task IDs to the user."
        ),
    }


# ── Phase C: richer workers ─────────────────────────────────────────────────
def _search_similar_worker(db, tenant_id: str, payload: dict) -> dict:
    """Scan memory + past plans + feature suggestions for overlapping language."""
    from .energy_agent import _mem_get

    text = payload.get("text") or ""
    needle = set(re.findall(r"[a-z]{4,}", text.lower()))
    # drop ultra-common tokens
    stop = {
        "this", "that", "with", "from", "have", "what", "when", "your", "about",
        "hard", "just", "like", "want", "need", "make", "more", "than", "them",
    }
    needle -= stop
    hits: list[dict] = []

    notes = _mem_get(db, f"tenant:{tenant_id}", limit=100)
    for n in notes:
        blob = (n.get("value") or "").lower()
        words = set(re.findall(r"[a-z]{4,}", blob)) - stop
        score = len(needle.intersection(words))
        if score >= 2:
            hits.append({
                "source": "memory",
                "key": n.get("key"),
                "score": score,
                "value": (n.get("value") or "")[:220],
            })

    # Past plan utterances (complaint digests)
    since = _now() - timedelta(days=90)
    plans = db.execute(
        select(EaPlan)
        .where(EaPlan.tenant_id == tenant_id, EaPlan.created_at >= since)
        .order_by(EaPlan.created_at.desc())
        .limit(40)
    ).scalars().all()
    for p in plans:
        blob = (p.user_utterance or "").lower()
        words = set(re.findall(r"[a-z]{4,}", blob)) - stop
        score = len(needle.intersection(words))
        if score >= 2:
            hits.append({
                "source": "plan",
                "key": p.id,
                "intent": p.intent,
                "score": score,
                "value": (p.user_utterance or "")[:220],
            })

    # Feature suggestions / shipped digests for this tenant
    suggestion_hits = []
    try:
        from .feature_suggestions import FeatureSuggestion
        rows = db.execute(
            select(FeatureSuggestion)
            .where(FeatureSuggestion.tenant_id == tenant_id)
            .order_by(FeatureSuggestion.created_at.desc())
            .limit(30)
        ).scalars().all()
        for s in rows:
            blob = (s.text or "").lower()
            words = set(re.findall(r"[a-z]{4,}", blob)) - stop
            score = len(needle.intersection(words))
            if score >= 2:
                suggestion_hits.append({
                    "id": s.id,
                    "status": s.status,
                    "score": score,
                    "text": (s.text or "")[:220],
                })
                hits.append({
                    "source": "suggestion",
                    "key": f"fs_{s.id}",
                    "score": score + (5 if s.status == "shipped" else 0),
                    "value": (s.text or "")[:220],
                    "status": s.status,
                })
    except Exception as e:
        log.debug("suggestion search skipped: %s", e)

    hits.sort(key=lambda x: -int(x.get("score") or 0))
    hits = hits[:8]
    result = {
        "ok": True,
        "hits": hits,
        "suggestion_hits": suggestion_hits[:5],
        "needle_size": len(needle),
    }
    return result


def _owner_email_allowed(email: str | None) -> bool:
    """True only for Ford dogfood addresses — never random tenants."""
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return False
    if e in _MIND_OWNER_EMAIL_ALLOWLIST:
        return True
    # Plus-addressing / subdomain variants on allowlisted local+domain
    local, _, domain = e.partition("@")
    base_local = local.split("+", 1)[0]
    for allowed in _MIND_OWNER_EMAIL_ALLOWLIST:
        a_local, _, a_domain = allowed.partition("@")
        if domain == a_domain and base_local == a_local:
            return True
    return False


def _email_owner_and_ford(
    tenant,
    *,
    subject: str,
    body: str,
    html: str | None = None,
    owner: bool = False,
    ford: bool = False,
) -> dict:
    """Outbound mind email — opt-in only. Never spam random tenants.

    - owner: only if caller set owner=True AND contact is allowlisted
      (caller must also check profile.email_* opt-in)
    - ford: internal ops — caller should only set for user-directed actions
      or when owner opted into the related email category
    """
    from .notify import send_internal_alert, _send_via_resend

    out = {"owner": False, "ford": False, "owner_blocked": False, "skipped": False}
    if not owner and not ford:
        out["skipped"] = True
        return out

    owner_email = (getattr(tenant, "contact_email", None) or "").strip()
    if ford:
        try:
            send_internal_alert(subject, body, to="ford.genereaux@gmail.com")
            out["ford"] = True
        except Exception:
            try:
                send_internal_alert(subject, body)
                out["ford"] = True
            except Exception as e:
                log.warning("mind email ford failed: %s", e)
    if owner and owner_email and "@" in owner_email:
        if not _owner_email_allowed(owner_email):
            out["owner_blocked"] = True
            log.info(
                "mind owner email blocked (not allowlisted): tenant=%s",
                getattr(tenant, "id", None),
            )
        else:
            try:
                out["owner"] = bool(
                    _send_via_resend(
                        to=owner_email,
                        subject=f"EnergyAgent · {subject}",
                        html=html
                        or (
                            "<div style='font-family:system-ui,sans-serif;line-height:1.5;"
                            f"color:#0f172a'><p>{body.replace(chr(10), '<br>')}</p>"
                            "<p style='color:#64748b;font-size:13px'>— Your Energy Agent "
                            "(one mind, working for this account)</p></div>"
                        ),
                        text=body,
                    )
                )
            except Exception as e:
                log.warning("mind email owner failed: %s", e)
    elif owner:
        out["owner_blocked"] = True
    return out


def _propose_ui_worker(db, tenant_id: str, payload: dict) -> dict:
    """Queue a real feature suggestion (judge pipeline) — heavy path on clear win.

    Always emails Ford (and owner when profile.email_ux_approvals) so prepared
    / auto-approved changes are never silent.
    """
    from .models import Tenant
    from .energy_agent import _check_budget

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return {"ok": False, "error": "tenant not found"}

    budget = _check_budget(db, tenant_id)
    if not budget.get("ok"):
        return {
            "ok": False,
            "deferred": True,
            "reason": "weekly budget exhausted — proposal held",
        }

    text = (payload.get("text") or "").strip()
    clarification = payload.get("clarification") or ""
    ctx = payload.get("context") or {}
    auto = bool(payload.get("auto_approved") or payload.get("proactive"))
    if not text:
        text = "Improve layout scannability on the current surface."

    composed = text[:2000]
    if clarification:
        composed = (
            f"[UX friction — primarily about {clarification}]\n"
            f"{text}\n"
            f"Context tab: {ctx.get('tab_label') or ctx.get('hash') or 'unknown'}. "
            "Prefer status-first layout, less visual noise, easier scan."
        )[:5000]
    if auto:
        composed = (
            f"[Proactive mind — prepared for your fleet's UX]\n{composed}"
        )[:5000]

    try:
        from .feature_suggestions import FeatureSuggestion

        fs = FeatureSuggestion(
            text=composed,
            email=getattr(tenant, "contact_email", None),
            tenant_id=tenant.id,
            product=getattr(tenant, "product", None) or "array_operator",
            screenshot_b64=None,
            status="new",
        )
        db.add(fs)
        db.flush()
        sid = fs.id

        world = _world_get(db, tenant_id)
        approval = {
            "id": sid,
            "kind": "ux_change",
            "status": "queued_judge",
            "auto_approved": auto,
            "text": composed[:400],
            "at": _now().isoformat() + "Z",
        }
        pending = list(world.get("pending_approvals") or [])
        pending = [approval] + pending[:19]

        # Email ONLY if opted in (or user-directed confirm — they asked).
        # Proactive offline prep stays silent unless email_ux_approvals is True.
        user_directed = not auto and bool(
            payload.get("user_confirm") or payload.get("user_directed")
        )
        wants_mail = _wants_owner_email(world, "email_ux_approvals") or user_directed
        email_bits = {"owner": False, "ford": False, "skipped": True}
        if wants_mail:
            email_bits = _email_owner_and_ford(
                tenant,
                subject=(
                    f"Mind auto-prepared UX change #{sid}"
                    if auto
                    else f"Mind UI proposal #{sid}"
                ),
                body=(
                    f"Your Energy Agent prepared a UI improvement for this account.\n\n"
                    f"Suggestion id: {sid}\n"
                    f"Tenant: {tenant.id}\n"
                    f"Mode: {'proactive/auto-prepared' if auto else 'user-directed'}\n"
                    f"Clarification: {clarification or 'n/a'}\n\n"
                    f"{composed}\n\n"
                    f"Pipeline: feature_suggestion judge (same as in-app improve).\n"
                    f"Status: /v1/feature-suggestion/{sid}/status\n"
                    f"\n(You get this because you asked for updates, or confirmed a change.)\n"
                ),
                owner=_wants_owner_email(world, "email_ux_approvals")
                or (user_directed and _owner_email_allowed(
                    getattr(tenant, "contact_email", None)
                )),
                # Ford ops copy only when owner opted into UX emails or directed the change
                ford=True,
            )

        _world_patch(db, tenant_id, {
            "last_proposal_id": sid,
            "last_proposal_at": _now().isoformat() + "Z",
            "pending_ui_proposal": None,
            "pending_approvals": pending,
        })
        _emit(
            db, tenant_id, "ux_prepared",
            f"UI change #{sid} prepared" + (" (proactive)" if auto else ""),
            payload={"suggestion_id": sid, "auto": auto, "email": email_bits},
            speak_as_mind=None,
        )
        return {
            "ok": True,
            "suggestion_id": sid,
            "status": "new",
            "pipeline": "feature_suggestion_judge",
            "refresh_and_ask": True,
            "auto_approved": auto,
            "emailed": email_bits,
            "status_url": f"/v1/feature-suggestion/{sid}/status",
            "message": (
                f"Queued improvement #{sid}. "
                + (
                    "I emailed you about it. "
                    if email_bits.get("owner") or email_bits.get("ford")
                    else ""
                )
                + "When live: refresh and tell me if it feels better."
            ),
        }
    except Exception as e:
        log.exception("propose_ui worker failed")
        return {"ok": False, "error": str(e)[:500]}


def _proactive_insight_worker(db, tenant_id: str, payload: dict) -> dict:
    """Synthesize a long-term insight from world model + fleet (one mind, offline)."""
    from .models import Tenant
    from .energy_agent import _tenant_census_tool, _investigate_attention_tool

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return {"ok": False, "skipped": True}

    world = _world_get(db, tenant_id)
    dig = world.get("fleet_digest")
    # Refresh pulse if empty
    if not dig or payload.get("force_pulse"):
        census = _tenant_census_tool(db, tenant, {"include_names": False})
        att = _investigate_attention_tool(db, tenant, {"limit": 8})
        problems = att.get("problems") or []
        dig = {
            "arrays": (census.get("totals") or {}).get("arrays"),
            "inverters": (census.get("totals") or {}).get("inverters"),
            "attention_count": int(att.get("count") or len(problems)),
            "top": [
                {
                    "name": p.get("name"),
                    "why": p.get("why"),
                    "next_step": p.get("next_step"),
                }
                for p in problems[:5]
            ],
            "brief": att.get("brief"),
        }
        _world_patch(db, tenant_id, {"fleet_digest": dig})

    attn = int((dig or {}).get("attention_count") or 0)
    top = (dig or {}).get("top") or []
    reason = payload.get("reason") or "scheduled"
    prev = world.get("last_attention_count")
    spiked = prev is not None and attn > int(prev) + (ATTENTION_SPIKE_DELTA - 1)
    # First time we ever record attention (prev is None) is a soft "notice", not a nag
    first_notice = prev is None and attn > 0

    # Build plain-language insight (no invented kWh)
    if attn <= 0:
        headline = "Fleet looks clear"
        detail = (
            f"{(dig or {}).get('arrays') or '—'} arrays · "
            f"{(dig or {}).get('inverters') or '—'} inverters. "
            "Nothing needs attention right now."
        )
        importance = 25
    else:
        first = top[0] if top else {}
        name = first.get("name") or "a site"
        why = first.get("why") or "needs a look"
        headline = f"{attn} need attention" + (f" · {name}" if name else "")
        detail = (
            f"{why}. "
            + (f"Next: {first.get('next_step')}. " if first.get("next_step") else "")
            + ("Attention increased since last check. " if spiked else "")
            + "Open Fleet Triage for the full queue."
        )
        # Only "loud" when things got worse or first time we notice
        importance = 72 if spiked else (58 if first_notice else 40)

    # UX keep-good: if repeated friction notes, nudge prepare (quiet unless new)
    friction_n = _count_recent_ux_friction(db, tenant_id)
    ux_note = None
    if friction_n >= 2 and (world.get("profile") or {}).get("auto_prepare_ux", True):
        ux_note = (
            f"You've hit UX friction {friction_n}× recently — "
            "I can prepare a layout fix offline in the app"
            + (
                " (and email you, since you asked for updates)."
                if _wants_owner_email(world, "email_ux_approvals")
                else " (say if you want email updates)."
            )
        )
        if spiked or first_notice:
            importance = max(importance, 65)

    # Fingerprint: same fleet story should not re-speak every wake/tick
    top_key = ",".join(
        f"{t.get('name') or ''}:{(t.get('why') or '')[:40]}" for t in (top or [])[:3]
    )
    fingerprint = f"{attn}|{top_key}|{headline}"
    last_fp = world.get("last_insight_fingerprint")
    last_speak_at = world.get("last_insight_speak_at")
    same_story = last_fp == fingerprint
    within_speak_cd = False
    if last_speak_at:
        try:
            ls = datetime.fromisoformat(str(last_speak_at).replace("Z", ""))
            within_speak_cd = (_now() - ls).total_seconds() < PROACTIVE_SPEAK_COOLDOWN_HOURS * 3600
        except Exception:
            within_speak_cd = False

    # Silent refresh of world model — no chat spam
    silent = bool(payload.get("silent"))
    duplicate = same_story and within_speak_cd and not spiked
    if attn <= 0 and not first_notice and same_story:
        silent = True  # don't re-announce "all clear" either

    insight = {
        "id": "ins_" + uuid.uuid4().hex[:12],
        "at": _now().isoformat() + "Z",
        "reason": reason,
        "headline": headline,
        "detail": detail,
        "ux_note": ux_note,
        "attention_count": attn,
        "importance": importance,
        "fingerprint": fingerprint,
    }
    insights = [insight] + list(world.get("insights") or [])[:14]

    speak = None
    if (
        not silent
        and not duplicate
        and importance >= MIN_IMPORTANCE_TO_SPEAK
        and (spiked or first_notice)
    ):
        speak = f"{headline}. {detail}"
        if ux_note:
            speak = f"{speak} {ux_note}"

    emailed = {"owner": False, "ford": False}
    # Proactive email ONLY if user explicitly opted in (email_insights=True).
    can_email = (
        _wants_owner_email(world, "email_insights")
        and importance >= 60
        and (spiked or (first_notice and attn >= 2))
        and not duplicate
    )
    last_em = world.get("last_proactive_email_at")
    if can_email and last_em:
        try:
            le = datetime.fromisoformat(str(last_em).replace("Z", ""))
            if (_now() - le).total_seconds() < PROACTIVE_EMAIL_COOLDOWN_HOURS * 3600:
                can_email = False
        except Exception:
            pass

    if can_email:
        body = (
            f"Proactive note from your Energy Agent\n\n"
            f"{headline}\n{detail}\n"
            + (f"\n{ux_note}\n" if ux_note else "\n")
            + f"Reason: {reason}\nTenant: {tenant_id}\n"
            "Open arrayoperator.com → Fleet Triage (or ask me in the app).\n"
            "\nYou asked for email updates. Reply or say in-app if you want fewer.\n"
        )
        emailed = _email_owner_and_ford(
            tenant,
            subject=headline[:80],
            body=body,
            owner=True,
            ford=False,  # no ops spam on routine fleet notes
        )

    patch = {
        "insights": insights,
        "last_proactive_at": _now().isoformat() + "Z",
        "last_attention_count": attn,
        "last_wake_reason": reason,
        "last_insight_fingerprint": fingerprint,
    }
    if speak:
        patch["last_insight_speak_at"] = _now().isoformat() + "Z"
    if emailed.get("owner") or emailed.get("ford"):
        patch["last_proactive_email_at"] = _now().isoformat() + "Z"
    _world_patch(db, tenant_id, patch)

    # Stage UX prepare if friction pattern + auto_prepare
    staged = False
    if ux_note and (world.get("profile") or {}).get("auto_prepare_ux", True):
        already = db.execute(
            select(func.count()).select_from(EaTask).where(
                EaTask.tenant_id == tenant_id,
                EaTask.kind.in_(("prepare_ux_approval", "propose_ui")),
                EaTask.status == "queued",
            )
        ).scalar() or 0
        if not int(already):
            db.add(EaTask(
                id="tk_" + uuid.uuid4().hex[:16],
                tenant_id=tenant_id,
                kind="prepare_ux_approval",
                status="queued",
                priority=55,
                title="Prepare UX improvement from friction pattern",
                payload_json=json.dumps({
                    "text": (
                        "Repeated UX friction notes — improve scannability and "
                        "status-first layout on the surfaces they use most."
                    ),
                    "clarification": "understanding",
                    "proactive": True,
                    "auto_approved": bool(
                        (world.get("profile") or {}).get("auto_approve_ux")
                    ),
                }),
                speak_hint=(
                    "I prepared a UX improvement offline based on repeated friction. "
                    "Check your email — I can open it in the app when you're ready."
                ),
            ))
            staged = True

    return {
        "ok": True,
        "insight": insight,
        "emailed": emailed,
        "staged_ux": staged,
        "speak": speak,
        "silent": silent or not speak,
        "duplicate": duplicate,
        "attention_spike": spiked,
    }


def _prepare_ux_approval_worker(db, tenant_id: str, payload: dict) -> dict:
    """Think → prepare UI change → email humans. Auto-queue judge only if allowed."""
    world = _world_get(db, tenant_id)
    prof = world.get("profile") or {}
    auto = bool(
        payload.get("auto_approved")
        if "auto_approved" in payload
        else prof.get("auto_approve_ux")
    )
    # Always prepare via propose_ui path (creates FeatureSuggestion + emails)
    result = _propose_ui_worker(
        db,
        tenant_id,
        {
            "text": payload.get("text"),
            "clarification": payload.get("clarification") or "proactive",
            "context": payload.get("context") or world.get("last_context") or {},
            "proactive": True,
            "auto_approved": auto,
        },
    )
    result["prepared"] = True
    result["awaiting_human"] = not auto
    return result


def wake_mind(
    db,
    tenant_id: str,
    reason: str,
    *,
    payload: dict | None = None,
    session_id: str | None = None,
    enqueue_insight: bool = True,
) -> dict:
    """Event-driven wake: something happened → long-term mind notices.

    Call from capture success, alert sweeps, bill settlement, chat, scheduler.
    Never narrates multi-agent — one mind, internal tasks only.
    session_open is quiet (pulse only) so opening chat does not re-spam insights.
    """
    reason = (reason or "event")[:80]
    pl = payload or {}
    _world_patch(db, tenant_id, {
        "last_wake_reason": reason,
        "last_wake_at": _now().isoformat() + "Z",
    })
    _emit(
        db, tenant_id, "mind_wake",
        f"Woke: {reason}",
        session_id=session_id,
        payload={"reason": reason, **{k: pl[k] for k in list(pl)[:8]}},
    )
    actions: list[str] = ["woke"]

    # Quiet open: refresh digest only — no chatty insight every panel open
    if reason == "session_open":
        already = db.execute(
            select(func.count()).select_from(EaTask).where(
                EaTask.tenant_id == tenant_id,
                EaTask.kind == "fleet_pulse",
                EaTask.status == "queued",
            )
        ).scalar() or 0
        if not int(already):
            db.add(EaTask(
                id="tk_" + uuid.uuid4().hex[:16],
                tenant_id=tenant_id,
                kind="fleet_pulse",
                status="queued",
                priority=55,
                title="Quiet fleet pulse on open",
                payload_json="{}",
                session_id=session_id,
            ))
            actions.append("queued_fleet_pulse_quiet")
        ran = drain_tasks(db, tenant_id, limit=3)
        actions.append(f"drained_{ran}")
        return {"ok": True, "reason": reason, "actions": actions, "tasks_ran": ran}

    if reason in (
        "fleet_attention", "capture", "alert", "bill", "scheduled_proactive",
        "inverter_flag",
    ):
        already = db.execute(
            select(func.count()).select_from(EaTask).where(
                EaTask.tenant_id == tenant_id,
                EaTask.kind == "fleet_pulse",
                EaTask.status == "queued",
            )
        ).scalar() or 0
        if not int(already):
            db.add(EaTask(
                id="tk_" + uuid.uuid4().hex[:16],
                tenant_id=tenant_id,
                kind="fleet_pulse",
                status="queued",
                priority=40,
                title=f"Fleet pulse after {reason}",
                payload_json="{}",
                session_id=session_id,
            ))
            actions.append("queued_fleet_pulse")

    # Insight only when requested and not in run-cooldown with unchanged story
    if enqueue_insight:
        world = _world_get(db, tenant_id)
        skip_insight = False
        last_pro = world.get("last_proactive_at")
        if last_pro and reason == "scheduled_proactive":
            try:
                lp = datetime.fromisoformat(str(last_pro).replace("Z", ""))
                if (_now() - lp).total_seconds() < PROACTIVE_RUN_COOLDOWN_HOURS * 3600:
                    skip_insight = True
                    actions.append("insight_cooldown")
            except Exception:
                pass
        already_i = db.execute(
            select(func.count()).select_from(EaTask).where(
                EaTask.tenant_id == tenant_id,
                EaTask.kind == "proactive_insight",
                EaTask.status == "queued",
            )
        ).scalar() or 0
        if not skip_insight and not int(already_i):
            db.add(EaTask(
                id="tk_" + uuid.uuid4().hex[:16],
                tenant_id=tenant_id,
                kind="proactive_insight",
                status="queued",
                priority=50,
                title=f"Proactive insight ({reason})",
                payload_json=json.dumps({"reason": reason, **pl}, default=str)[:4000],
                session_id=session_id,
                speak_hint=None,
            ))
            actions.append("queued_proactive_insight")

    ran = drain_tasks(db, tenant_id, limit=6)
    actions.append(f"drained_{ran}")
    return {"ok": True, "reason": reason, "actions": actions, "tasks_ran": ran}


def _analyze_focus_worker(db, tenant_id: str, payload: dict) -> dict:
    from .models import Tenant
    from .energy_agent import _investigate_attention_tool

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return {"ok": False, "skipped": True, "reason": "tenant not found"}
    limit = int(payload.get("limit") or 6)
    att = _investigate_attention_tool(db, tenant, {"limit": limit})
    problems = att.get("problems") or []
    _world_patch(db, tenant_id, {
        "attention_focus": {
            "count": att.get("count"),
            "brief": att.get("brief"),
            "top": [
                {"name": p.get("name"), "why": p.get("why"), "next_step": p.get("next_step")}
                for p in problems[:5]
            ],
            "at": _now().isoformat() + "Z",
        }
    })
    return {
        "ok": True,
        "problems": problems[:limit],
        "count": att.get("count"),
        "brief": att.get("brief"),
    }


# ── task executor ───────────────────────────────────────────────────────────
def run_task(db, task: EaTask) -> None:
    from .energy_agent import _mem_set, _tenant_census_tool, _investigate_attention_tool

    if task.status not in ("queued", "running"):
        return
    task.status = "running"
    task.started_at = _now()
    db.flush()

    try:
        payload = json.loads(task.payload_json or "{}")
    except Exception:
        payload = {}

    result: dict[str, Any] = {"ok": True}
    speak = task.speak_hint

    try:
        if task.kind == "note_complaint":
            key = f"mind.note.{_now().strftime('%Y%m%d%H%M%S')}"
            val = json.dumps({
                "text": payload.get("text"),
                "topic": payload.get("topic") or "general",
                "hash": payload.get("hash"),
                "tab": payload.get("tab"),
            }, default=str)[:4000]
            _mem_set(db, f"tenant:{task.tenant_id}", key, val)
            result["memory_key"] = key

        elif task.kind == "snapshot_context":
            patch = {"last_context": payload.get("context") or payload}
            if payload.get("ux_clarification"):
                patch["ux_clarification"] = payload["ux_clarification"]
            if payload.get("pending_ui_proposal"):
                patch["pending_ui_proposal"] = payload["pending_ui_proposal"]
            _world_patch(db, task.tenant_id, patch)
            result["snapshotted"] = True

        elif task.kind == "search_similar":
            result = _search_similar_worker(db, task.tenant_id, payload)
            hits = result.get("hits") or []
            if hits:
                top = hits[0]
                speak = speak or (
                    "I found earlier notes that sound related to what you described. "
                    "We can build on those if you want."
                )
                if any(h.get("status") == "shipped" for h in hits if isinstance(h, dict)):
                    speak = (
                        "Related improvements already shipped before for similar friction. "
                        "Want me to lean on that pattern, or open a fresh proposal?"
                    )
                result["top_hit"] = {"source": top.get("source"), "score": top.get("score")}

        elif task.kind == "fleet_pulse":
            from .models import Tenant
            tenant = db.get(Tenant, task.tenant_id)
            if tenant is None:
                result = {"ok": False, "skipped": True, "reason": "tenant not found"}
                speak = None
            else:
                census = _tenant_census_tool(db, tenant, {"include_names": False})
                att = _investigate_attention_tool(db, tenant, {"limit": 8})
                problems = att.get("problems") or []
                digest = {
                    "arrays": (census.get("totals") or {}).get("arrays"),
                    "inverters": (census.get("totals") or {}).get("inverters"),
                    "attention_count": int(att.get("count") or len(problems)),
                    "top": [
                        {
                            "name": p.get("name"),
                            "why": p.get("why"),
                            "next_step": p.get("next_step"),
                        }
                        for p in problems[:5]
                    ],
                    "brief": att.get("brief"),
                }
                _world_patch(db, task.tenant_id, {"fleet_digest": digest})
                result["fleet_digest"] = digest
                if int(digest.get("attention_count") or 0) <= 0:
                    speak = None  # don't nag on a quiet fleet

        elif task.kind == "analyze_focus":
            result = _analyze_focus_worker(db, task.tenant_id, payload)
            if int(result.get("count") or 0) > 0:
                speak = speak or (
                    "I lined up the sites that need attention. "
                    "Say the word and I'll walk the worst first."
                )
            else:
                speak = None

        elif task.kind == "propose_ui_candidate":
            high = bool(payload.get("expected_value_high"))
            friction_n = int(payload.get("friction_count_14d") or 0)
            result = {
                "ok": True,
                "deferred": True,
                "expected_value_high": high,
                "friction_count_14d": friction_n,
                "reason": (
                    "High expected value — ready to open proposal on user yes"
                    if high
                    else "Heavy UI proposals wait for clear user yes or high expected value"
                ),
                "suggestion": payload.get("text"),
                "clarification": payload.get("clarification"),
            }
            _world_patch(db, task.tenant_id, {
                "pending_ui_proposal": {
                    "text": payload.get("text"),
                    "clarification": payload.get("clarification"),
                    "expected_value_high": high,
                }
            })
            if not speak:
                speak = (
                    "I held off spinning up a full UI redesign until we pin down what feels hard. "
                    "Finding vs understanding — which is closer?"
                )

        elif task.kind == "propose_ui":
            result = _propose_ui_worker(db, task.tenant_id, payload)
            if result.get("ok") and result.get("suggestion_id"):
                mailed = (result.get("emailed") or {}).get("owner") or (
                    result.get("emailed") or {}
                ).get("ford")
                speak = speak or (
                    "Quick update: the improvement is queued for review"
                    + (" — I emailed you too" if mailed else "")
                    + ". When it ships, refresh and tell me if it feels easier to scan."
                )
            else:
                speak = None

        elif task.kind == "proactive_insight":
            result = _proactive_insight_worker(db, task.tenant_id, payload)
            speak = result.get("speak") or speak
            if result.get("emailed", {}).get("owner"):
                speak = speak or (
                    "I left a proactive note for this account (also emailed, since you asked for updates). "
                    "Ask me about fleet attention anytime."
                )

        elif task.kind == "prepare_ux_approval":
            result = _prepare_ux_approval_worker(db, task.tenant_id, payload)
            if result.get("ok") and result.get("suggestion_id"):
                mailed = (result.get("emailed") or {}).get("owner") or (
                    result.get("emailed") or {}
                ).get("ford")
                speak = speak or (
                    "I prepared a UX change offline"
                    + (" and emailed you" if mailed else " in the app")
                    + ". Say yes if you want me to walk it when it lands."
                )
            else:
                speak = None

        elif task.kind == "profile_sync":
            # Merge payload prefs into world.profile
            world = _world_get(db, task.tenant_id)
            prof = dict(world.get("profile") or {})
            prefs = payload.get("prefs") or payload
            for k in (
                "email_insights", "email_ux_approvals",
                "auto_prepare_ux", "auto_approve_ux", "voice_pref",
            ):
                if k in prefs:
                    prof[k] = prefs[k]
            _world_patch(db, task.tenant_id, {"profile": prof})
            result = {"ok": True, "profile": prof}
            speak = None

        else:
            result = {"ok": True, "skipped": True, "kind": task.kind}

        cost = _charge_worker(db, task.tenant_id, task.kind)
        task.cost_usd = float(cost or 0)
        task.status = "done"
        task.result_json = json.dumps(result, default=str)[:12000]
        task.finished_at = _now()
        _emit(
            db, task.tenant_id, "task_done",
            task.title,
            session_id=task.session_id, ref_id=task.id,
            payload={**result, "importance": score_importance(task.kind, result, speak)},
            speak_as_mind=None,  # only interrupt_candidate carries speak (policy-gated)
        )
        if speak:
            maybe_queue_interrupt(
                db, task.tenant_id,
                session_id=task.session_id,
                ref_id=task.id,
                title=task.title,
                speak=speak,
                kind=task.kind,
                result=result,
            )

        # Close parent plan if no open child tasks remain
        if task.plan_id:
            open_left = db.execute(
                select(func.count()).select_from(EaTask).where(
                    EaTask.plan_id == task.plan_id,
                    EaTask.status.in_(("queued", "running")),
                    EaTask.id != task.id,
                )
            ).scalar() or 0
            if int(open_left) == 0:
                plan = db.get(EaPlan, task.plan_id)
                if plan and plan.status == "open":
                    plan.status = "done"
    except Exception as e:
        log.exception("mind task failed %s", task.id)
        task.status = "failed"
        task.error = str(e)[:1000]
        task.finished_at = _now()
        _emit(
            db, task.tenant_id, "task_failed",
            f"{task.title}: {e}",
            session_id=task.session_id, ref_id=task.id,
        )
    db.flush()


def drain_tasks(db, tenant_id: str, *, limit: int = 5) -> int:
    """Run up to `limit` queued tasks for this tenant (cheap workers)."""
    rows = db.execute(
        select(EaTask)
        .where(EaTask.tenant_id == tenant_id, EaTask.status == "queued")
        .order_by(EaTask.priority.asc(), EaTask.created_at.asc())
        .limit(limit)
    ).scalars().all()
    n = 0
    for t in rows:
        run_task(db, t)
        n += 1
    return n


def observe_and_reprioritize(db, tenant_id: str) -> dict:
    """Cognitive observe: stale fleet pulse, proactive insights, open-plan hygiene."""
    world = _world_get(db, tenant_id)
    actions: list[str] = []

    dig = world.get("fleet_digest")
    stale = True
    if dig and world.get("last_tick_at"):
        try:
            lt = datetime.fromisoformat(str(world.get("last_tick_at")).replace("Z", ""))
            if (_now() - lt).total_seconds() < 6 * 3600 and dig:
                stale = False
        except Exception:
            pass

    open_fleet = db.execute(
        select(func.count()).select_from(EaPlan).where(
            EaPlan.tenant_id == tenant_id,
            EaPlan.status == "open",
            EaPlan.intent.in_(("fleet_concern",)),
        )
    ).scalar() or 0

    already_queued = db.execute(
        select(func.count()).select_from(EaTask).where(
            EaTask.tenant_id == tenant_id,
            EaTask.kind == "fleet_pulse",
            EaTask.status == "queued",
        )
    ).scalar() or 0

    if (stale or int(open_fleet) > 0) and not dig and not int(already_queued):
        db.add(EaTask(
            id="tk_" + uuid.uuid4().hex[:16],
            tenant_id=tenant_id,
            kind="fleet_pulse",
            status="queued",
            priority=60,
            title="Scheduled fleet awareness pulse",
            payload_json="{}",
        ))
        actions.append("queued_fleet_pulse")

    # Proactive insight at most every PROACTIVE_RUN_COOLDOWN_HOURS (silent refresh OK)
    last_pro = world.get("last_proactive_at")
    need_insight = not last_pro
    if last_pro:
        try:
            lp = datetime.fromisoformat(str(last_pro).replace("Z", ""))
            if (_now() - lp).total_seconds() > PROACTIVE_RUN_COOLDOWN_HOURS * 3600:
                need_insight = True
        except Exception:
            need_insight = True

    already_insight = db.execute(
        select(func.count()).select_from(EaTask).where(
            EaTask.tenant_id == tenant_id,
            EaTask.kind == "proactive_insight",
            EaTask.status == "queued",
        )
    ).scalar() or 0

    if need_insight and not int(already_insight):
        db.add(EaTask(
            id="tk_" + uuid.uuid4().hex[:16],
            tenant_id=tenant_id,
            kind="proactive_insight",
            status="queued",
            priority=70,  # lower urgency than user-directed work
            title="Long-term proactive insight",
            payload_json=json.dumps({"reason": "observe_tick"}),
        ))
        actions.append("queued_proactive_insight")

    # Bump priority of propose_ui if pending and user clarified
    if world.get("ux_clarification") and world.get("pending_ui_proposal"):
        cand = db.execute(
            select(EaTask).where(
                EaTask.tenant_id == tenant_id,
                EaTask.kind == "propose_ui_candidate",
                EaTask.status == "queued",
            ).order_by(EaTask.created_at.desc()).limit(1)
        ).scalars().first()
        if cand and int(cand.priority or 50) > 40:
            cand.priority = 40
            actions.append("reprioritized_ui_candidate")

    return {"actions": actions}


def mind_tick(db, tenant_id: str, session_id: str | None = None) -> dict:
    """One cognitive cycle: observe → drain → reprioritize → interrupt candidates."""
    obs = observe_and_reprioritize(db, tenant_id)
    world = _world_get(db, tenant_id)
    ran = drain_tasks(db, tenant_id, limit=5)
    row = db.get(EaWorldState, tenant_id)
    if row:
        row.last_tick_at = _now()
    elif ran:
        _world_patch(db, tenant_id, {})
        row = db.get(EaWorldState, tenant_id)
        if row:
            row.last_tick_at = _now()

    events = db.execute(
        select(EaEvent)
        .where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "interrupt_candidate",
            EaEvent.consumed == 0,
        )
        .order_by(EaEvent.id.asc())
        .limit(5)
    ).scalars().all()
    interrupts = []
    for ev in events:
        pl = _payload(ev)
        interrupts.append({
            "event_id": ev.id,
            "summary": ev.summary,
            "speak": ev.speak_as_mind,
            "ref_id": ev.ref_id,
            "importance": pl.get("importance"),
            "created_at": ev.created_at.isoformat() + "Z" if ev.created_at else None,
        })

    open_tasks = db.execute(
        select(EaTask)
        .where(
            EaTask.tenant_id == tenant_id,
            EaTask.status.in_(("queued", "running")),
        )
        .order_by(EaTask.priority.asc())
        .limit(20)
    ).scalars().all()

    return {
        "ok": True,
        "tasks_ran": ran,
        "observe": obs,
        "interrupt_budget": interrupt_budget(db, tenant_id),
        "world_revision": world.get("revision"),
        "open_tasks": [
            {"id": t.id, "kind": t.kind, "title": t.title, "status": t.status}
            for t in open_tasks
        ],
        "interrupt_candidates": interrupts,
        "principles": {
            "north_star": "The conversation is one window into a mind that's thinking continuously.",
            "one_mind": True,
            "continuous_awareness": True,
            "initiative": True,
            "truthfulness": True,
        },
    }


# ── Phase D: metrics ────────────────────────────────────────────────────────
def compute_metrics(db, tenant_id: str, *, days: int = 30) -> dict:
    """North-star KPI: cost per successful improvement (+ task/interrupt rates)."""
    since = _now() - timedelta(days=max(1, min(int(days), 365)))

    tasks = db.execute(
        select(EaTask).where(
            EaTask.tenant_id == tenant_id,
            EaTask.created_at >= since,
        )
    ).scalars().all()
    done = [t for t in tasks if t.status == "done"]
    failed = [t for t in tasks if t.status == "failed"]
    queued = [t for t in tasks if t.status in ("queued", "running")]
    task_cost = float(sum(float(t.cost_usd or 0) for t in tasks))

    # Ledger worker:* spend (more complete if chat also charged workers)
    ledger_worker = 0.0
    try:
        from .energy_agent import EaCostLedger
        rows = db.execute(
            select(EaCostLedger).where(
                EaCostLedger.tenant_id == tenant_id,
                EaCostLedger.created_at >= since,
            )
        ).scalars().all()
        for r in rows:
            if (r.reason or "").startswith("worker:"):
                ledger_worker += float(r.amount_usd or 0)
    except Exception:
        pass

    total_worker_cost = max(task_cost, ledger_worker)

    # Interrupts
    interrupts = db.execute(
        select(EaEvent).where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "interrupt_candidate",
            EaEvent.created_at >= since,
        )
    ).scalars().all()
    suppressed = db.execute(
        select(func.count()).select_from(EaEvent).where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "interrupt_suppressed",
            EaEvent.created_at >= since,
        )
    ).scalar() or 0
    outcomes = db.execute(
        select(EaEvent).where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "interrupt_outcome",
            EaEvent.created_at >= since,
        )
    ).scalars().all()
    accepted = 0
    dismissed = 0
    shown = 0
    for o in outcomes:
        pl = _payload(o)
        out = (pl.get("outcome") or o.summary or "").lower()
        if "accept" in out:
            accepted += 1
        elif "dismiss" in out or "ignore" in out:
            dismissed += 1
        elif "shown" in out:
            shown += 1
    # "shown" also counts consumed interrupts without explicit outcome
    consumed_interrupts = sum(1 for e in interrupts if e.consumed)
    shown = max(shown, consumed_interrupts)

    # Successful improvements: shipped feature suggestions + explicit improvement_win events
    wins = 0
    shipped_ids: list[int] = []
    try:
        from .feature_suggestions import FeatureSuggestion
        shipped = db.execute(
            select(FeatureSuggestion).where(
                FeatureSuggestion.tenant_id == tenant_id,
                FeatureSuggestion.status == "shipped",
                FeatureSuggestion.created_at >= since,
            )
        ).scalars().all()
        wins = len(shipped)
        shipped_ids = [s.id for s in shipped]
    except Exception:
        pass
    win_events = db.execute(
        select(func.count()).select_from(EaEvent).where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "improvement_win",
            EaEvent.created_at >= since,
        )
    ).scalar() or 0
    wins = max(wins, int(win_events))

    # propose_ui tasks that completed successfully count as "improvements started"
    proposals = [t for t in done if t.kind == "propose_ui"]
    proposals_ok = 0
    for t in proposals:
        try:
            r = json.loads(t.result_json or "{}")
            if r.get("ok") and r.get("suggestion_id"):
                proposals_ok += 1
        except Exception:
            pass

    success_rate = (len(done) / len(tasks)) if tasks else None
    accept_rate = (accepted / shown) if shown else None
    cost_per_win = (total_worker_cost / wins) if wins else None
    # Fallback: cost per successful proposal queued (proxy when nothing shipped yet)
    cost_per_proposal = (total_worker_cost / proposals_ok) if proposals_ok else None

    return {
        "ok": True,
        "window_days": days,
        "since": since.isoformat() + "Z",
        "north_star_kpi": "cost_per_successful_improvement",
        "tasks": {
            "total": len(tasks),
            "done": len(done),
            "failed": len(failed),
            "open": len(queued),
            "success_rate": round(success_rate, 4) if success_rate is not None else None,
        },
        "interrupts": {
            "candidates": len(interrupts),
            "suppressed": int(suppressed),
            "shown": shown,
            "accepted": accepted,
            "dismissed": dismissed,
            "accept_rate": round(accept_rate, 4) if accept_rate is not None else None,
        },
        "improvements": {
            "proposals_queued": proposals_ok,
            "shipped": wins,
            "shipped_ids": shipped_ids[:20],
        },
        "cost": {
            "worker_usd": round(total_worker_cost, 6),
            "cost_per_successful_improvement_usd": (
                round(cost_per_win, 4) if cost_per_win is not None else None
            ),
            "cost_per_proposal_usd": (
                round(cost_per_proposal, 4) if cost_per_proposal is not None else None
            ),
            "note": (
                "Optimize $ per shipped win, not $ per conversation minute. "
                "When shipped=0, cost_per_proposal is the leading indicator."
            ),
        },
        "interrupt_policy": {
            "min_importance": MIN_IMPORTANCE_TO_SPEAK,
            "cooldown_sec": INTERRUPT_COOLDOWN_SEC,
            "max_per_hour": INTERRUPT_MAX_PER_HOUR,
            "max_per_day": INTERRUPT_MAX_PER_DAY,
        },
    }


def sync_improvement_wins(db, tenant_id: str) -> int:
    """Mark shipped suggestions as wins (for KPI). Idempotent via ref_id."""
    try:
        from .feature_suggestions import FeatureSuggestion
    except Exception:
        return 0
    shipped = db.execute(
        select(FeatureSuggestion).where(
            FeatureSuggestion.tenant_id == tenant_id,
            FeatureSuggestion.status == "shipped",
        )
    ).scalars().all()
    n = 0
    for s in shipped:
        ref = f"fs_win_{s.id}"
        exists = db.execute(
            select(EaEvent).where(
                EaEvent.tenant_id == tenant_id,
                EaEvent.kind == "improvement_win",
                EaEvent.ref_id == ref,
            ).limit(1)
        ).scalars().first()
        if exists:
            continue
        _emit(
            db, tenant_id, "improvement_win",
            f"Shipped improvement #{s.id}",
            ref_id=ref,
            payload={"suggestion_id": s.id, "text": (s.text or "")[:300]},
        )
        n += 1
    return n


# ── HTTP ────────────────────────────────────────────────────────────────────
class TickIn(BaseModel):
    session_id: str | None = None


class ConsumeIn(BaseModel):
    event_ids: list[int] = Field(default_factory=list)
    # Phase D: shown | accepted | dismissed | ignored
    outcome: str | None = None
    feedback: str | None = None


@router.get("/v1/energy-agent/mind")
def mind_snapshot(authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        world = _world_get(db, t.id)
        open_tasks = db.execute(
            select(EaTask)
            .where(EaTask.tenant_id == t.id, EaTask.status.in_(("queued", "running")))
            .order_by(EaTask.priority.asc())
            .limit(30)
        ).scalars().all()
        recent = db.execute(
            select(EaEvent)
            .where(EaEvent.tenant_id == t.id)
            .order_by(EaEvent.id.desc())
            .limit(30)
        ).scalars().all()
        return {
            "ok": True,
            "north_star": "The conversation is one window into a mind that's thinking continuously.",
            "one_mind": True,
            "world": world,
            "profile": world.get("profile") or {},
            "insights": (world.get("insights") or [])[:5],
            "pending_approvals": (world.get("pending_approvals") or [])[:5],
            "interrupt_budget": interrupt_budget(db, t.id),
            "open_tasks": [
                {
                    "id": x.id, "kind": x.kind, "title": x.title,
                    "status": x.status, "priority": x.priority,
                }
                for x in open_tasks
            ],
            "recent_events": [
                {
                    "id": e.id, "kind": e.kind, "summary": e.summary,
                    "speak_as_mind": e.speak_as_mind,
                    "importance": _payload(e).get("importance"),
                    "created_at": e.created_at.isoformat() + "Z" if e.created_at else None,
                    "consumed": bool(e.consumed),
                }
                for e in recent
            ],
        }


class ProfileIn(BaseModel):
    """Update long-term mind preferences (persists with this account)."""
    email_insights: bool | None = None
    email_ux_approvals: bool | None = None
    auto_prepare_ux: bool | None = None
    auto_approve_ux: bool | None = None


class WakeIn(BaseModel):
    reason: str = "client"
    payload: dict | None = None
    session_id: str | None = None


@router.patch("/v1/energy-agent/mind/profile")
def mind_profile(body: ProfileIn, authorization: str | None = Header(default=None)):
    """Owner prefs for the long-term mind — part of the account profile."""
    t = _auth(authorization)
    with SessionLocal() as db:
        world = _world_get(db, t.id)
        prof = dict(world.get("profile") or {})
        data = body.model_dump(exclude_none=True)
        prof.update(data)
        _world_patch(db, t.id, {"profile": prof})
        db.commit()
        return {"ok": True, "profile": prof}


@router.post("/v1/energy-agent/mind/wake")
def mind_wake_ep(body: WakeIn, authorization: str | None = Header(default=None)):
    """Explicit wake (client open, or internal systems with session auth)."""
    t = _auth(authorization)
    with SessionLocal() as db:
        out = wake_mind(
            db, t.id, body.reason or "client",
            payload=body.payload or {},
            session_id=body.session_id,
        )
        db.commit()
        return out


@router.get("/v1/energy-agent/mind/metrics")
def mind_metrics(
    days: int = 30,
    authorization: str | None = Header(default=None),
):
    t = _auth(authorization)
    with SessionLocal() as db:
        sync_improvement_wins(db, t.id)
        out = compute_metrics(db, t.id, days=days)
        db.commit()
        return out


@router.post("/v1/energy-agent/mind/tick")
def mind_tick_ep(body: TickIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        out = mind_tick(db, t.id, session_id=body.session_id)
        db.commit()
        return out


@router.get("/v1/energy-agent/mind/events")
def mind_events(
    since_id: int = 0,
    session_id: str | None = None,
    authorization: str | None = Header(default=None),
):
    t = _auth(authorization)
    with SessionLocal() as db:
        q = (
            select(EaEvent)
            .where(EaEvent.tenant_id == t.id, EaEvent.id > int(since_id or 0))
            .order_by(EaEvent.id.asc())
            .limit(50)
        )
        rows = db.execute(q).scalars().all()
        return {
            "ok": True,
            "interrupt_budget": interrupt_budget(db, t.id),
            "events": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "summary": e.summary,
                    "speak_as_mind": e.speak_as_mind,
                    "ref_id": e.ref_id,
                    "importance": _payload(e).get("importance"),
                    "created_at": e.created_at.isoformat() + "Z" if e.created_at else None,
                    "consumed": bool(e.consumed),
                }
                for e in rows
            ],
        }


@router.post("/v1/energy-agent/mind/events/consume")
def mind_consume(body: ConsumeIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        n = 0
        outcome = (body.outcome or "shown").strip().lower()[:32]
        for eid in body.event_ids or []:
            ev = db.get(EaEvent, int(eid))
            if not ev or ev.tenant_id != t.id:
                continue
            was_open = not ev.consumed
            ev.consumed = 1
            if was_open:
                n += 1
            # Always record outcome (shown → accepted upgrade after chips)
            _emit(
                db, t.id, "interrupt_outcome",
                f"outcome={outcome}",
                session_id=ev.session_id,
                ref_id=str(ev.id),
                payload={
                    "outcome": outcome,
                    "event_id": ev.id,
                    "feedback": (body.feedback or "")[:500] or None,
                    "importance": _payload(ev).get("importance"),
                    "upgraded": not was_open,
                },
            )
            # Accepted proposal interrupt → ensure propose_ui is queued
            # (frontend may also send chat "yes open proposal" — classify is idempotent-ish)
            if outcome in ("accepted", "accept", "yes") and (
                "proposal" in (ev.speak_as_mind or "").lower()
                or "layout" in (ev.speak_as_mind or "").lower()
            ):
                world = _world_get(db, t.id)
                pend = world.get("pending_ui_proposal") or {}
                if pend.get("text") or world.get("last_user_utterance"):
                    classify_and_plan(
                        db, t.id, ev.session_id,
                        "yes open proposal",
                        context=world.get("last_context") or {},
                    )
                    drain_tasks(db, t.id, limit=3)
        db.commit()
        return {"ok": True, "consumed": n, "outcome": outcome}
