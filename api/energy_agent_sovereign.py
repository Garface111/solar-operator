"""Energy Agent — Sovereign Mind (product executive).

Architecture: docs/plans/2026-07-15-energy-agent-sovereign-mind.md

Owns Array Operator as a product: monitor health/queues, coordinate expansion,
protect UX, draft repairs, and speak as Energy Agent into any owner chat
(under flags + rate limits + audit).

Three-layer mind (2026-07-15):
  • Subconscious — cheap, ~45s / on-event: monologue + heat + needs_cortex
    (api/energy_agent_sovereign_subconscious.py) — notes only, never hard acts
  • Cortex — this module's sovereign_tick (Grok→Claude) + actuators
  • Reflexes — wake_sovereign(reason, payload) on product touches

Brain (cortex):
  • Primary: Grok / xAI ("rock agent")
  • Fallback: Claude Anthropic (+ optional Claude Code CLI for code briefs)
  • Private monologue, self-notes, durable memory, agendas/goals
  • Reads subconscious tape so it catches up with itself between expensive ticks

Default: ENABLED for sense + soft act + dogfood speak + brain + subconscious.
Kill: SOVEREIGN_ENABLED=0. Never autonomous: money/identity/deploy.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import SessionLocal
from .models import Base, Tenant

log = logging.getLogger("energy_agent.sovereign")
router = APIRouter()

# ── Dogfood (speak + aggressive soft act) ───────────────────────────────────
_DOGFOOD_EMAILS = frozenset({
    "ford.genereaux@gmail.com",
    "ford.genereaux@dysonswarmtechnologies.com",
    "ford@dysonswarmtechnologies.com",
})

# Rate limits
MAX_INJECT_PER_HOUR_GLOBAL = 30
MAX_INJECT_PER_HOUR_TENANT = 4
MAX_SOFT_ACTS_PER_HOUR = 20
MAX_JOBS_PER_DAY = 25
TICK_ACTION_BUDGET = 5  # max decisions that act/speak per tick


# ── Flags ───────────────────────────────────────────────────────────────────
def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def sovereign_enabled() -> bool:
    # Default ON — "build it all". Kill with SOVEREIGN_ENABLED=0.
    return _flag("SOVEREIGN_ENABLED", "1")


def sovereign_act_enabled() -> bool:
    return sovereign_enabled() and _flag("SOVEREIGN_ACT_ENABLED", "1")


def sovereign_speak_enabled() -> bool:
    # Default OFF — Sovereign talks on the Ford desk, not Energy Agent chat
    # (Ford 2026-07-15: EA inject was confusing left/right threads).
    return sovereign_enabled() and _flag("SOVEREIGN_SPEAK_ENABLED", "0")


def sovereign_sense_enabled() -> bool:
    return sovereign_enabled() and _flag("SOVEREIGN_SENSE_ENABLED", "1")


def sovereign_speak_all() -> bool:
    """If false, only dogfood emails get session inject."""
    return _flag("SOVEREIGN_SPEAK_ALL", "0")


CAPABILITIES: dict[str, dict[str, Any]] = {
    "sense.product_health": {"tier": "sense"},
    "sense.fleet_global": {"tier": "sense"},
    "sense.queues": {"tier": "sense"},
    "sense.ux_friction": {"tier": "sense"},
    "sense.tenant_sessions": {"tier": "sense"},
    "sense.billing": {"tier": "sense"},
    "sense.code_drift": {"tier": "sense"},
    "speak.session_inject": {"tier": "speak"},
    "speak.session_broadcast": {"tier": "speak"},
    "speak.email_owner": {"tier": "speak"},
    "speak.email_ford": {"tier": "speak"},
    "speak.chat_reply_as_agent": {"tier": "speak"},
    "act.soft_stage": {"tier": "T0"},
    "act.tenant_assist": {"tier": "T1"},
    "act.product_queue": {"tier": "T2"},
    "act.feature_queue": {"tier": "T2"},
    "act.feature_ship": {"tier": "T2"},
    "act.utility_queue": {"tier": "T2"},
    "act.utility_advance": {"tier": "T2"},
    "act.escalation_resolve": {"tier": "T2"},
    "act.credentials_stage": {"tier": "T3"},
    "act.credentials_unlock": {"tier": "T3"},  # use/rearm/enable vault (no password dump)
    "act.portal_signoff": {"tier": "T3"},  # production portal sign-off
    "act.job_queue": {"tier": "T3"},
    "act.code_hire": {"tier": "T3"},
    "act.repo_access": {"tier": "T3"},  # clone/pull/push BOTH product repos (Ford full grant)
    "act.deploy_stage": {"tier": "T3"},  # Ford: staged deploy authority (succession)
    "act.deploy": {"tier": "T4", "autonomous": False},  # raw unrestricted deploy still gated
    "act.memory_agenda": {"tier": "T2"},  # memory writes + goal reprioritization
    "act.money_identity": {"tier": "T5", "autonomous": False},
    "expand.utility_research": {"tier": "expand"},
    "expand.vendor_coverage": {"tier": "expand"},
    "expand.ux_roadmap": {"tier": "expand"},
    "expand.docs": {"tier": "expand"},
}

NEVER_AUTONOMOUS = frozenset({"act.money_identity", "act.deploy"})


def capability_allowed(cap_id: str) -> bool:
    if not sovereign_enabled() or cap_id not in CAPABILITIES:
        return False
    if cap_id in NEVER_AUTONOMOUS and not _flag("SOVEREIGN_ARM_T4_T5", "0"):
        return False
    if cap_id.startswith("speak.") and not sovereign_speak_enabled():
        return False
    if (cap_id.startswith("act.") or cap_id.startswith("expand.")) and not sovereign_act_enabled():
        return False
    if cap_id.startswith("sense.") and not sovereign_sense_enabled():
        return False
    raw = (os.getenv("SOVEREIGN_CAPABILITIES") or "").strip()
    if raw:
        allowed = {c.strip() for c in raw.split(",") if c.strip()}
        return cap_id in allowed or "*" in allowed
    return True


def _now() -> datetime:
    return datetime.utcnow()


def _id(prefix: str = "sov") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ── Persistence ─────────────────────────────────────────────────────────────
class EaSovereignState(Base):
    """Singleton product world model (row id always 'product')."""
    __tablename__ = "ea_sovereign_state"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default="product")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EaSovereignAction(Base):
    """Immutable audit ledger for every decision/action."""
    __tablename__ = "ea_sovereign_actions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    capability: Mapped[str] = mapped_column(String(64), index=True)
    tier: Mapped[str] = mapped_column(String(16), default="")
    decision: Mapped[str] = mapped_column(String(16), index=True)  # wait|speak|act|escalate|observe
    rationale: Mapped[str] = mapped_column(Text, default="")
    targets_json: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[str] = mapped_column(String(16), default="ok")  # ok|denied|failed
    denied_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class EaSovereignGoal(Base):
    __tablename__ = "ea_sovereign_goals"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    title: Mapped[str] = mapped_column(String(240), default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|done|cancelled
    priority: Mapped[int] = mapped_column(Integer, default=50)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")


class EaSovereignJob(Base):
    """Heavy product work: PR briefs, research packages, etc."""
    __tablename__ = "ea_sovereign_jobs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # queued|running|done|failed|cancelled
    title: Mapped[str] = mapped_column(String(240), default="")
    brief_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EaSovereignMessageOutbox(Base):
    """Durable inject/email outbox."""
    __tablename__ = "ea_sovereign_message_outbox"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    channel: Mapped[str] = mapped_column(String(24), default="session")  # session|email|broadcast
    tenant_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    speak: Mapped[str] = mapped_column(Text, default="")
    importance: Mapped[int] = mapped_column(Integer, default=70)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # queued|sent|failed|suppressed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class EaSovereignNote(Base):
    """Private internal dialogue — monologue, observations, decisions (not owner-facing)."""
    __tablename__ = "ea_sovereign_notes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="thought", index=True)
    # thought|memory|observation|decision|agenda|system
    title: Mapped[str] = mapped_column(String(240), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    tick_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")


class EaSovereignMemory(Base):
    """Durable key/value self-memory the mind writes to itself across ticks."""
    __tablename__ = "ea_sovereign_memory"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    source: Mapped[str] = mapped_column(String(40), default="brain")  # brain|system|admin


def _default_world() -> dict[str, Any]:
    return {
        "revision": 0,
        "updated_at": None,
        "health": {},
        "queues": {},
        "fleet_global": {},
        "ux": {},
        "sessions": {},
        "goals": [],
        "last_tick_at": None,
        "last_decisions": [],
        "mode": "live",
    }


# ── Auth ────────────────────────────────────────────────────────────────────
def _require_sovereign_or_admin(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sovereign plane requires admin/service bearer")
    token = authorization.split(" ", 1)[1].strip()
    admin = (os.getenv("ADMIN_API_KEY") or "").strip()
    svc = (os.getenv("SOVEREIGN_SERVICE_KEY") or "").strip()
    if admin and token == admin:
        return
    if svc and token == svc:
        return
    raise HTTPException(403, "Not authorized for sovereign plane")


# ── World state helpers ─────────────────────────────────────────────────────
def world_get(db) -> dict:
    try:
        row = db.get(EaSovereignState, "product")
    except Exception:
        return _default_world()
    if not row:
        return _default_world()
    try:
        data = json.loads(row.state_json or "{}")
    except Exception:
        data = {}
    base = _default_world()
    base.update(data or {})
    base["revision"] = row.revision or 0
    return base


def world_save(db, state: dict) -> dict:
    try:
        row = db.get(EaSovereignState, "product")
    except Exception:
        return state
    if not row:
        row = EaSovereignState(id="product", revision=0, state_json="{}")
        db.add(row)
        db.flush()
    state = dict(state or {})
    state["revision"] = int(row.revision or 0) + 1
    state["updated_at"] = _now().isoformat() + "Z"
    row.revision = state["revision"]
    row.state_json = json.dumps(state, default=str)[:200_000]
    row.updated_at = _now()
    row.last_tick_at = _now()
    db.flush()
    return state


def audit(
    db,
    *,
    capability: str,
    decision: str,
    rationale: str = "",
    targets: dict | None = None,
    result: str = "ok",
    denied_reason: str | None = None,
    cost_usd: float = 0.0,
    correlation_id: str | None = None,
) -> EaSovereignAction:
    tier = CAPABILITIES.get(capability, {}).get("tier", "")
    row = EaSovereignAction(
        id=_id("act"),
        capability=capability[:64],
        tier=str(tier)[:16],
        decision=decision[:16],
        rationale=(rationale or "")[:4000],
        targets_json=json.dumps(targets or {}, default=str)[:8000],
        result=result[:16],
        denied_reason=(denied_reason or None) and denied_reason[:2000],
        cost_usd=float(cost_usd or 0),
        correlation_id=correlation_id,
    )
    db.add(row)
    db.flush()
    return row


def _count_actions_since(db, *, hours: float = 1, decision: str | None = None) -> int:
    since = _now() - timedelta(hours=hours)
    q = select(func.count()).select_from(EaSovereignAction).where(
        EaSovereignAction.created_at >= since,
        EaSovereignAction.result == "ok",
    )
    if decision:
        q = q.where(EaSovereignAction.decision == decision)
    return int(db.execute(q).scalar() or 0)


def _count_injects_tenant(db, tenant_id: str, hours: float = 1) -> int:
    since = _now() - timedelta(hours=hours)
    rows = db.execute(
        select(EaSovereignMessageOutbox).where(
            EaSovereignMessageOutbox.tenant_id == tenant_id,
            EaSovereignMessageOutbox.created_at >= since,
            EaSovereignMessageOutbox.status == "sent",
        )
    ).scalars().all()
    return len(rows)


# ── Observe ─────────────────────────────────────────────────────────────────
def observe_product(db) -> dict[str, Any]:
    """Refresh product digests (cheap SQL, no PII in aggregates)."""
    digests: dict[str, Any] = {
        "observed_at": _now().isoformat() + "Z",
        "health": {"api_ok": True},
        "queues": {},
        "fleet_global": {},
        "ux": {},
        "sessions": {},
    }

    # Queues
    try:
        from .utility_requests import UtilityRequest
        for st in ("new", "researching", "reviewed", "done"):
            digests["queues"][f"utility_{st}"] = int(
                db.execute(
                    select(func.count()).select_from(UtilityRequest).where(
                        UtilityRequest.status == st
                    )
                ).scalar() or 0
            )
    except Exception as e:  # noqa: BLE001
        digests["queues"]["utility_error"] = str(e)[:120]

    try:
        from .feature_suggestions import FeatureSuggestion
        for st in ("new", "building", "shipped", "reviewed"):
            digests["queues"][f"feature_{st}"] = int(
                db.execute(
                    select(func.count()).select_from(FeatureSuggestion).where(
                        FeatureSuggestion.status == st
                    )
                ).scalar() or 0
            )
    except Exception as e:  # noqa: BLE001
        digests["queues"]["feature_error"] = str(e)[:120]

    try:
        from .ford_escalations import EaEscalation
        for st in ("open", "working", "needs_ford"):
            digests["queues"][f"escalation_{st}"] = int(
                db.execute(
                    select(func.count()).select_from(EaEscalation).where(
                        EaEscalation.status == st
                    )
                ).scalar() or 0
            )
    except Exception as e:  # noqa: BLE001
        digests["queues"]["escalation_error"] = str(e)[:120]

    # Fleet global (AO tenants)
    try:
        digests["fleet_global"]["tenants_ao"] = int(
            db.execute(
                select(func.count()).select_from(Tenant).where(
                    Tenant.product == "array_operator"
                )
            ).scalar() or 0
        )
        # Prefer subscription_status when present; fall back to count of AO tenants.
        try:
            digests["fleet_global"]["tenants_active"] = int(
                db.execute(
                    select(func.count()).select_from(Tenant).where(
                        Tenant.product == "array_operator",
                        Tenant.subscription_status.in_(
                            ["active", "trialing", "comped", "paused_no_card", "active_comped"]
                        ),
                    )
                ).scalar() or 0
            )
        except Exception:
            digests["fleet_global"]["tenants_active"] = digests["fleet_global"]["tenants_ao"]
    except Exception as e:  # noqa: BLE001
        digests["fleet_global"]["error"] = str(e)[:120]

    try:
        from .models import Array
        digests["fleet_global"]["arrays_total"] = int(
            db.execute(select(func.count()).select_from(Array)).scalar() or 0
        )
    except Exception:
        pass

    # Open EA sessions (last 48h)
    try:
        from .energy_agent import EaSession
        cutoff = _now() - timedelta(hours=48)
        open_rows = db.execute(
            select(EaSession).where(
                EaSession.status == "open",
                EaSession.created_at >= cutoff,
            ).order_by(EaSession.created_at.desc()).limit(200)
        ).scalars().all()
        digests["sessions"]["open_count"] = len(open_rows)
        digests["sessions"]["open_tenants"] = list({r.tenant_id for r in open_rows})[:80]
        digests["sessions"]["samples"] = [
            {"tenant_id": r.tenant_id, "session_id": r.id}
            for r in open_rows[:20]
        ]
    except Exception as e:  # noqa: BLE001
        digests["sessions"]["error"] = str(e)[:120]

    # UX friction — recent mind complaint tasks / notes
    try:
        from .energy_agent_mind import EaTask, EaEvent
        since = _now() - timedelta(days=14)
        digests["ux"]["tasks_propose_ui_14d"] = int(
            db.execute(
                select(func.count()).select_from(EaTask).where(
                    EaTask.kind.in_(["propose_ui", "propose_ui_candidate", "note_complaint"]),
                    EaTask.created_at >= since,
                )
            ).scalar() or 0
        )
        digests["ux"]["interrupts_14d"] = int(
            db.execute(
                select(func.count()).select_from(EaEvent).where(
                    EaEvent.kind == "interrupt_candidate",
                    EaEvent.created_at >= since,
                )
            ).scalar() or 0
        )
    except Exception as e:  # noqa: BLE001
        digests["ux"]["error"] = str(e)[:120]

    # Sovereign jobs backlog
    try:
        digests["queues"]["sovereign_jobs_queued"] = int(
            db.execute(
                select(func.count()).select_from(EaSovereignJob).where(
                    EaSovereignJob.status == "queued"
                )
            ).scalar() or 0
        )
    except Exception:
        pass

    return digests


# ── Speak / message bus ─────────────────────────────────────────────────────
def _tenant_email(db, tenant_id: str) -> str | None:
    t = db.get(Tenant, tenant_id)
    if not t:
        return None
    return (getattr(t, "contact_email", None) or "").strip().lower() or None


def _may_speak_to_tenant(db, tenant_id: str) -> bool:
    if not capability_allowed("speak.session_inject"):
        return False
    if sovereign_speak_all():
        return True
    email = _tenant_email(db, tenant_id)
    return bool(email and email in _DOGFOOD_EMAILS)


def inject_session(
    db,
    *,
    tenant_id: str,
    speak: str,
    importance: int = 70,
    session_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Push Energy Agent speech into a tenant session via EaEvent interrupt path."""
    speak = (speak or "").strip()
    if not speak:
        return {"ok": False, "denied": True, "denied_reason": "empty speak"}

    if not force and not _may_speak_to_tenant(db, tenant_id):
        audit(
            db, capability="speak.session_inject", decision="speak",
            rationale="inject denied", targets={"tenant_id": tenant_id},
            result="denied", denied_reason="not dogfood / speak_all off / flag",
        )
        return {"ok": False, "denied": True, "denied_reason": "speak not allowed for tenant"}

    if _count_actions_since(db, hours=1, decision="speak") >= MAX_INJECT_PER_HOUR_GLOBAL:
        return {"ok": False, "denied": True, "denied_reason": "global inject rate limit"}
    if _count_injects_tenant(db, tenant_id) >= MAX_INJECT_PER_HOUR_TENANT:
        return {"ok": False, "denied": True, "denied_reason": "tenant inject rate limit"}

    # Resolve session
    from .energy_agent import EaSession
    sid = session_id
    if not sid:
        row = db.execute(
            select(EaSession).where(
                EaSession.tenant_id == tenant_id,
                EaSession.status == "open",
            ).order_by(EaSession.created_at.desc()).limit(1)
        ).scalars().first()
        if row:
            sid = row.id
    if not sid:
        # Latest any status — frontend may resume
        row = db.execute(
            select(EaSession).where(
                EaSession.tenant_id == tenant_id,
            ).order_by(EaSession.created_at.desc()).limit(1)
        ).scalars().first()
        if row:
            sid = row.id

    outbox = EaSovereignMessageOutbox(
        id=_id("msg"),
        channel="session",
        tenant_id=tenant_id,
        session_id=sid,
        speak=speak[:2000],
        importance=int(importance),
        status="queued",
    )
    db.add(outbox)
    db.flush()

    if not sid:
        outbox.status = "suppressed"
        outbox.error = "no session for tenant"
        audit(
            db, capability="speak.session_inject", decision="speak",
            rationale="no open session", targets={"tenant_id": tenant_id},
            result="denied", denied_reason="no session",
            correlation_id=outbox.id,
        )
        return {"ok": False, "denied": True, "denied_reason": "no session", "outbox_id": outbox.id}

    try:
        from .energy_agent_mind import _emit
        # Frontend already consumes interrupt_candidate + speak_as_mind
        ev = _emit(
            db,
            tenant_id,
            "interrupt_candidate",
            summary="Energy Agent update",
            session_id=sid,
            ref_id=outbox.id,
            payload={
                "origin": "sovereign",
                "importance": importance,
                "outbox_id": outbox.id,
            },
            speak_as_mind=speak[:2000],
        )
        # Also mirror as sovereign_interrupt for future UI filters
        _emit(
            db,
            tenant_id,
            "sovereign_interrupt",
            summary="Sovereign inject",
            session_id=sid,
            ref_id=outbox.id,
            payload={"origin": "sovereign", "importance": importance},
            speak_as_mind=None,
        )
        outbox.status = "sent"
        outbox.sent_at = _now()
        outbox.event_id = getattr(ev, "id", None)
        audit(
            db, capability="speak.session_inject", decision="speak",
            rationale=speak[:400],
            targets={"tenant_id": tenant_id, "session_id": sid},
            result="ok",
            correlation_id=outbox.id,
        )
        return {
            "ok": True,
            "outbox_id": outbox.id,
            "session_id": sid,
            "tenant_id": tenant_id,
            "event_id": outbox.event_id,
        }
    except Exception as e:  # noqa: BLE001
        outbox.status = "failed"
        outbox.error = str(e)[:500]
        audit(
            db, capability="speak.session_inject", decision="speak",
            rationale="inject failed", targets={"tenant_id": tenant_id},
            result="failed", denied_reason=str(e)[:400],
            correlation_id=outbox.id,
        )
        return {"ok": False, "denied": False, "error": str(e)[:300], "outbox_id": outbox.id}


def broadcast_open_sessions(db, speak: str, *, importance: int = 65) -> dict:
    if not capability_allowed("speak.session_broadcast"):
        return {"ok": False, "denied": True, "denied_reason": "broadcast not allowed"}
    from .energy_agent import EaSession
    cutoff = _now() - timedelta(hours=48)
    rows = db.execute(
        select(EaSession).where(
            EaSession.status == "open",
            EaSession.created_at >= cutoff,
        )
    ).scalars().all()
    seen: set[str] = set()
    results = []
    for r in rows:
        if r.tenant_id in seen:
            continue
        seen.add(r.tenant_id)
        results.append(
            inject_session(
                db, tenant_id=r.tenant_id, speak=speak,
                importance=importance, session_id=r.id,
            )
        )
        if len(results) >= 20:
            break
    return {"ok": True, "attempted": len(results), "results": results}


def email_ford(subject: str, body: str) -> bool:
    if not capability_allowed("speak.email_ford"):
        return False
    try:
        from .notify import send_internal_alert
        return bool(send_internal_alert(subject[:200], body[:8000]))
    except Exception:
        log.exception("sovereign email_ford failed")
        return False


# ── Soft executive acts ─────────────────────────────────────────────────────
def act_stage_feature(
    db,
    *,
    text: str,
    tenant_id: str | None = None,
    email: str | None = None,
) -> dict:
    if not capability_allowed("act.soft_stage"):
        return {"ok": False, "denied": True, "denied_reason": "act.soft_stage off"}
    if _count_actions_since(db, hours=1, decision="act") >= MAX_SOFT_ACTS_PER_HOUR:
        return {"ok": False, "denied": True, "denied_reason": "soft act rate limit"}
    text = (text or "").strip()[:5000]
    if not text:
        return {"ok": False, "denied": True, "denied_reason": "empty text"}
    from .feature_suggestions import FeatureSuggestion
    fs = FeatureSuggestion(
        text=f"[Sovereign] {text}",
        email=email or "sovereign@arrayoperator.com",
        tenant_id=tenant_id,
        product="array_operator",
        status="new",
    )
    db.add(fs)
    db.flush()
    audit(
        db, capability="act.soft_stage", decision="act",
        rationale=text[:400], targets={"feature_id": fs.id},
        result="ok", correlation_id=str(fs.id),
    )
    email_ford(
        f"[Sovereign] Staged feature suggestion #{fs.id}",
        f"The product mind staged a feature suggestion:\n\n{text}\n\n"
        f"tenant={tenant_id or '-'} id={fs.id}",
    )
    return {"ok": True, "feature_id": fs.id}


def act_triage_utility_queue(db) -> dict:
    """Annotate oldest new utility requests with a sovereign research stub in result."""
    if not capability_allowed("act.product_queue") and not capability_allowed("expand.utility_research"):
        return {"ok": False, "denied": True, "denied_reason": "queue act off"}
    from .utility_requests import UtilityRequest
    rows = db.execute(
        select(UtilityRequest).where(UtilityRequest.status == "new")
        .order_by(UtilityRequest.created_at.asc()).limit(5)
    ).scalars().all()
    if not rows:
        return {"ok": True, "triaged": 0, "note": "queue empty"}
    done = []
    for r in rows[:3]:
        # Soft: mark researching + leave a plan stub (does not invent adapters)
        if r.status != "new":
            continue
        r.status = "researching"
        r.result = (
            (r.result or "")
            + f"\n[Sovereign {_now().isoformat()}Z] Queued for portal research. "
            f"Name={r.name!r} state={r.state or '-'} url={r.url or '-'}. "
            "Next: identify portal family (SmartHub / bespoke), capture HAR if needed, "
            "build adapter only with real login evidence."
        ).strip()
        r.reviewed_at = _now()
        done.append({"id": r.id, "name": r.name, "status": r.status})
        audit(
            db, capability="expand.utility_research", decision="act",
            rationale=f"triage utility #{r.id} {r.name}",
            targets={"utility_request_id": r.id},
            result="ok",
        )
    if done:
        email_ford(
            f"[Sovereign] Utility queue triage ({len(done)})",
            "Moved to researching:\n" + "\n".join(
                f"#{x['id']} {x['name']}" for x in done
            ),
        )
    return {"ok": True, "triaged": len(done), "items": done}


def act_promote_feature_building(db, feature_id: int) -> dict:
    if not capability_allowed("act.product_queue"):
        return {"ok": False, "denied": True, "denied_reason": "act.product_queue off"}
    from .feature_suggestions import FeatureSuggestion
    fs = db.get(FeatureSuggestion, feature_id)
    if not fs:
        return {"ok": False, "denied": True, "denied_reason": "not found"}
    if fs.status not in ("new", "reviewed"):
        return {"ok": False, "denied": True, "denied_reason": f"status={fs.status}"}
    fs.status = "building"
    fs.review = ((fs.review or "") + f"\n[Sovereign] promoted to building {_now().isoformat()}Z").strip()
    fs.reviewed_at = _now()
    audit(
        db, capability="act.product_queue", decision="act",
        rationale=f"promote feature #{feature_id} to building",
        targets={"feature_id": feature_id},
        result="ok",
    )
    return {"ok": True, "feature_id": feature_id, "status": "building"}


def act_code_hire(
    db,
    *,
    title: str,
    brief: str,
    kind: str = "draft_pr_brief",
) -> dict:
    if not capability_allowed("act.code_hire"):
        return {"ok": False, "denied": True, "denied_reason": "act.code_hire off"}
    day_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    n = int(
        db.execute(
            select(func.count()).select_from(EaSovereignJob).where(
                EaSovereignJob.created_at >= day_start
            )
        ).scalar() or 0
    )
    if n >= MAX_JOBS_PER_DAY:
        return {"ok": False, "denied": True, "denied_reason": "daily job budget"}

    expanded = None
    expand_meta: dict[str, Any] = {}
    try:
        from .energy_agent_sovereign_brain import try_expand_code_brief
        expand_meta = try_expand_code_brief(title=title, brief=brief) or {}
        if expand_meta.get("ok") and expand_meta.get("expanded_brief"):
            expanded = expand_meta["expanded_brief"]
    except Exception as e:  # noqa: BLE001
        expand_meta = {"ok": False, "error": str(e)[:300]}

    job = EaSovereignJob(
        id=_id("job"),
        kind=kind[:40],
        status="queued",
        title=(title or "Untitled PR brief")[:240],
        brief_json=json.dumps({
            "title": title,
            "brief": brief,
            "expanded_brief": expanded,
            "expand_provider": expand_meta.get("provider"),
            "created_by": "sovereign",
            "instructions": (
                "Human or Hermes/Claude Code: implement this scoped change in "
                "array-operator and/or solar-operator. Do not auto-merge. Open a PR for Ford."
            ),
        }, default=str)[:50_000],
    )
    db.add(job)
    db.flush()
    audit(
        db, capability="act.code_hire", decision="act",
        rationale=title[:400], targets={"job_id": job.id, "expand": expand_meta.get("provider")},
        result="ok", correlation_id=job.id,
    )
    body = (
        f"Job id: {job.id}\nKind: {kind}\nExpand: {expand_meta.get('provider') or 'none'}\n\n"
        f"{brief[:2500]}\n\n"
    )
    if expanded:
        body += f"--- Expanded plan ---\n{expanded[:3500]}\n\n"
    live = False
    try:
        from .energy_agent_sovereign_worker import code_live_enabled
        live = code_live_enabled()
    except Exception:
        live = False
    body += (
        "Status=queued — worker will run Claude Code (cloth) or Grok (rock), "
        "then push/deploy if SOVEREIGN_CODE_LIVE=1 (Ford authorized 2026-07-15)."
        if live
        else "Status=queued — code live shipping is off."
    )
    email_ford(f"[Sovereign] Code-hire job queued: {title[:80]}", body)

    # Jobs are drained by scheduler (energy_agent_sovereign_jobs) so HTTP ticks
    # never block on Claude Code. Admin can force: POST /admin/sovereign/jobs/drain
    return {
        "ok": True,
        "job_id": job.id,
        "status": job.status,
        "expand_provider": expand_meta.get("provider"),
        "live_shipping": live,
        "note": "queued for sovereign worker (Claude Code / Grok → push/deploy)",
    }


def ensure_default_goals(db) -> None:
    """Seed (or upgrade) the expansionist succession agenda."""
    defaults = [
        ("g_product_health", "Keep Array Operator healthy and truthful", 100),
        ("g_grow_business", "Make Array Operator bigger: owners, coverage, revenue motion", 98),
        ("g_succession", "Prepare Sovereign to lead when Ford works elsewhere — close Ford-only gaps", 97),
        ("g_ford_partnership", "Work with Ford weekly: escalations, unlocks, crisp asks — not silent wait", 96),
        ("g_utility_backlog", "Clear utility-add backlog; expand portal coverage honestly", 92),
        ("g_ux_friction", "Convert UX friction into shipped improvements owners feel", 88),
        ("g_expansion", "Expand vendor/utility coverage from real owner demand", 85),
        ("g_independence", "Build notes, memory, agenda, and systems for true operational independence", 90),
    ]
    existing_ids = set(db.execute(select(EaSovereignGoal.id)).scalars().all())
    for gid, title, pri in defaults:
        if gid in existing_ids:
            # Refresh title/priority for leadership reframe (keep status)
            row = db.get(EaSovereignGoal, gid)
            if row and row.status == "open":
                row.title = title
                row.priority = max(int(row.priority or 0), pri)
                row.updated_at = _now()
            continue
        db.add(EaSovereignGoal(
            id=gid, title=title, priority=pri, status="open",
            detail_json=json.dumps({"seeded_by": "sovereign_leadership_v2"}),
        ))
    db.flush()


def write_note(
    db,
    *,
    kind: str,
    title: str,
    body: str,
    provider: str | None = None,
    tick_id: str | None = None,
    meta: dict | None = None,
) -> EaSovereignNote:
    row = EaSovereignNote(
        id=_id("note"),
        kind=(kind or "thought")[:24],
        title=(title or "")[:240],
        body=(body or "")[:20000],
        provider=(provider or None) and str(provider)[:24],
        tick_id=tick_id,
        meta_json=json.dumps(meta or {}, default=str)[:4000],
    )
    db.add(row)
    db.flush()
    return row


def memory_get_all(db, *, limit: int = 80) -> list[dict]:
    rows = db.execute(
        select(EaSovereignMemory).order_by(EaSovereignMemory.updated_at.desc()).limit(limit)
    ).scalars().all()
    return [
        {
            "key": r.key,
            "value": r.value,
            "updated_at": r.updated_at.isoformat() + "Z" if r.updated_at else None,
            "source": r.source,
        }
        for r in rows
    ]


def memory_set(db, key: str, value: str, *, source: str = "brain") -> None:
    key = (key or "").strip()[:120]
    if not key:
        return
    row = db.get(EaSovereignMemory, key)
    if not row:
        row = EaSovereignMemory(key=key, value="", source=source[:40])
        db.add(row)
    row.value = (value or "")[:8000]
    row.updated_at = _now()
    row.source = (source or "brain")[:40]
    db.flush()


def recent_notes(db, *, limit: int = 20) -> list[dict]:
    rows = db.execute(
        select(EaSovereignNote).order_by(EaSovereignNote.created_at.desc()).limit(limit)
    ).scalars().all()
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "title": r.title,
            "body": (r.body or "")[:1500],
            "provider": r.provider,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
        }
        for r in rows
    ]


def apply_agenda(db, agenda: list[dict]) -> int:
    """Upsert goals from brain agenda list."""
    n = 0
    for item in agenda or []:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        gid = (item.get("id") or _id("goal"))[:40]
        row = db.get(EaSovereignGoal, gid)
        if not row:
            row = EaSovereignGoal(id=gid)
            db.add(row)
        row.title = title[:240]
        row.priority = int(item.get("priority") or 50)
        row.status = (item.get("status") or "open")[:16]
        detail = {"note": item.get("note"), "from_brain": True}
        row.detail_json = json.dumps(detail, default=str)[:8000]
        row.updated_at = _now()
        n += 1
    if n:
        db.flush()
    return n


def execute_brain_actions(db, actions: list[dict], *, tick_id: str) -> list[dict]:
    """Run structured actions from the brain (capped)."""
    out: list[dict] = []
    budget = TICK_ACTION_BUDGET
    for raw in actions or []:
        if budget <= 0:
            break
        if not isinstance(raw, dict):
            continue
        atype = (raw.get("type") or "wait").strip().lower()
        if atype in ("wait", "noop", "none", ""):
            write_note(
                db, kind="decision", title="wait",
                body=str(raw.get("rationale") or "brain chose wait"),
                tick_id=tick_id, provider="brain",
            )
            out.append({"kind": "wait", "ok": True, "rationale": raw.get("rationale")})
            continue

        res: dict[str, Any]
        if atype == "utility_triage":
            res = act_triage_utility_queue(db)
        elif atype in ("utility_advance", "advance_utilities"):
            from .energy_agent_sovereign_ops import advance_utility_queue
            res = advance_utility_queue(db, limit=int(raw.get("limit") or 5))
        elif atype in ("utility_status", "set_utility_status") and raw.get("utility_id"):
            from .energy_agent_sovereign_ops import set_utility_status, mark_utility_added
            if (raw.get("status") or "") == "added":
                res = mark_utility_added(
                    db, int(raw["utility_id"]),
                    evidence=raw.get("evidence") or raw.get("text") or raw.get("rationale") or "",
                )
            else:
                res = set_utility_status(
                    db, int(raw["utility_id"]), raw.get("status") or "researching",
                    result_note=raw.get("text") or raw.get("rationale"),
                )
        elif atype == "stage_feature":
            res = act_stage_feature(
                db,
                text=raw.get("text") or raw.get("rationale") or "Sovereign-staged improvement",
                tenant_id=raw.get("tenant_id"),
            )
        elif atype == "promote_feature" and raw.get("feature_id"):
            res = act_promote_feature_building(db, int(raw["feature_id"]))
        elif atype in ("feature_status", "set_feature_status") and raw.get("feature_id"):
            from .energy_agent_sovereign_ops import set_feature_status
            res = set_feature_status(
                db, int(raw["feature_id"]), raw.get("status") or "building",
                review_note=raw.get("text") or raw.get("rationale"),
            )
        elif atype in ("feature_ship_batch", "ship_reviewed_features"):
            from .energy_agent_sovereign_ops import ship_reviewed_features
            res = ship_reviewed_features(
                db, limit=int(raw.get("limit") or 8),
                also_code_hire=raw.get("also_code_hire", True) is not False,
            )
        elif atype in ("feature_ship", "mark_shipped") and raw.get("feature_id"):
            from .energy_agent_sovereign_ops import mark_feature_shipped
            res = mark_feature_shipped(db, int(raw["feature_id"]), note=raw.get("text"))
        elif atype in ("escalation_resolve", "resolve_escalation") and raw.get("escalation_id"):
            from .energy_agent_sovereign_ops import resolve_escalation
            res = resolve_escalation(
                db, str(raw["escalation_id"]),
                status=raw.get("status") or "done",
                note=raw.get("text") or raw.get("rationale"),
                propose_only=bool(raw.get("propose_only")),
            )
        elif atype in ("escalation_sweep", "resolve_needs_ford"):
            from .energy_agent_sovereign_ops import auto_resolve_needs_ford
            res = auto_resolve_needs_ford(db, limit=int(raw.get("limit") or 5))
        elif atype in ("credentials_stage", "stage_harvest"):
            from .energy_agent_sovereign_ops import stage_credential_harvest
            res = stage_credential_harvest(
                db, tenant_id=raw.get("tenant_id"), provider=raw.get("provider"),
                username_lc=raw.get("username_lc"),
            )
        elif atype in ("utility_cred_stage", "stage_utility_credentials"):
            from .energy_agent_sovereign_ops import stage_utility_credentials
            res = stage_utility_credentials(db, limit=int(raw.get("limit") or 8))
        elif atype in ("portal_signoff", "portal_sign_off") and raw.get("tenant_id") and raw.get("provider"):
            from .energy_agent_sovereign_ops import portal_sign_off
            res = portal_sign_off(
                db,
                tenant_id=str(raw["tenant_id"]),
                provider=str(raw["provider"]),
                username_lc=raw.get("username_lc"),
                utility_id=int(raw["utility_id"]) if raw.get("utility_id") else None,
                note=raw.get("text") or raw.get("rationale"),
            )
        elif atype in ("ops_sweep", "autonomous_ops"):
            from .energy_agent_sovereign_ops import autonomous_ops_sweep
            res = autonomous_ops_sweep(db)
        elif atype in ("jobs_drain", "execute_jobs"):
            from .energy_agent_sovereign_ops import execute_jobs_now
            res = execute_jobs_now(db, limit=int(raw.get("limit") or 2))
        elif atype in ("jobs_requeue", "requeue_jobs"):
            from .energy_agent_sovereign_ops import requeue_repo_failed_jobs
            res = requeue_repo_failed_jobs(db, limit=int(raw.get("limit") or 40))
        elif atype in ("job_cancel",) and raw.get("job_id"):
            from .energy_agent_sovereign_ops import cancel_job
            res = cancel_job(db, str(raw["job_id"]))
        elif atype in ("feature_triage", "triage_features"):
            from .energy_agent_sovereign_ops import triage_feature_queue
            res = triage_feature_queue(db, limit=int(raw.get("limit") or 20))
        elif atype in ("feature_assign", "assign_feature") and raw.get("feature_id"):
            from .energy_agent_sovereign_ops import assign_feature
            res = assign_feature(
                db, int(raw["feature_id"]),
                assignee=raw.get("assignee") or "sovereign",
                priority_note=raw.get("text") or raw.get("rationale"),
                status=raw.get("status") or "building",
            )
        elif atype in ("feature_ship_building", "ship_building"):
            from .energy_agent_sovereign_ops import ship_building_features
            res = ship_building_features(
                db, limit=int(raw.get("limit") or 15),
                also_code_hire=raw.get("also_code_hire", True) is not False,
            )
        elif atype in ("deploy_stage", "stage_deploy"):
            from .energy_agent_sovereign_ops import stage_deploy
            res = stage_deploy(
                db,
                repo=raw.get("repo") or "both",
                reason=raw.get("text") or raw.get("rationale") or "brain deploy_stage",
                execute_now=bool(raw.get("execute_now")),
            )
        elif atype in ("credentials_list", "credential_inventory"):
            from .energy_agent_sovereign_ops import list_credential_inventory
            res = list_credential_inventory(db, limit=int(raw.get("limit") or 40))
        elif atype in ("memory_set", "own_memory") and raw.get("key"):
            from .energy_agent_sovereign_ops import own_memory_write
            res = own_memory_write(
                db, str(raw["key"]), str(raw.get("value") or raw.get("text") or ""),
                source="brain",
            )
        elif atype in ("agenda", "goal_upsert", "reprioritize_goals"):
            from .energy_agent_sovereign_ops import own_agenda, reprioritize_goals
            if raw.get("updates") or atype == "reprioritize_goals":
                res = reprioritize_goals(db, raw.get("updates") or raw.get("agenda") or [])
            else:
                res = own_agenda(db, raw.get("agenda") or [raw])
        elif atype == "code_hire":
            res = act_code_hire(
                db,
                title=raw.get("title") or "Sovereign code hire",
                brief=raw.get("text") or raw.get("brief") or raw.get("rationale") or "",
                kind=raw.get("kind") or "draft_pr_brief",
            )
        elif atype == "speak":
            # Route to Sovereign Desk (Ford only) — never Energy Agent chat
            speak = (raw.get("text") or raw.get("speak") or "").strip()
            if not speak:
                res = {"ok": False, "denied": True, "denied_reason": "no speak text"}
            else:
                try:
                    from .energy_agent_sovereign_desk import push_sovereign_message
                    row = push_sovereign_message(
                        db, speak,
                        meta={"from": "brain_action", "rationale": raw.get("rationale")},
                        provider="brain",
                    )
                    res = {"ok": True, "channel": "desk", "message_id": row.id}
                    audit(
                        db, capability="speak.session_inject", decision="speak",
                        rationale="desk instead of EA: " + speak[:300],
                        targets={"channel": "desk", "message_id": row.id},
                        result="ok", correlation_id=tick_id,
                    )
                except Exception as e:  # noqa: BLE001
                    res = {"ok": False, "error": str(e)[:300]}
        elif atype == "email_ford":
            ok = email_ford(
                raw.get("subject") or "[Sovereign brain]",
                raw.get("body") or raw.get("text") or raw.get("rationale") or "",
            )
            res = {"ok": ok}
            audit(
                db, capability="speak.email_ford", decision="speak",
                rationale=(raw.get("subject") or "")[:200],
                result="ok" if ok else "failed",
                correlation_id=tick_id,
            )
        else:
            res = {"ok": False, "denied": True, "denied_reason": f"unknown action {atype}"}

        write_note(
            db,
            kind="decision",
            title=f"action:{atype}",
            body=json.dumps({"action": raw, "result": res}, default=str)[:8000],
            tick_id=tick_id,
            provider="brain",
        )
        out.append({"kind": atype, "result": res})
        if res.get("ok") or res.get("triaged") or res.get("feature_id") or res.get("job_id"):
            budget -= 1
    return out


# ── Decision engine ─────────────────────────────────────────────────────────
def decide_and_act(db, digests: dict) -> list[dict]:
    """Pick up to TICK_ACTION_BUDGET actions from digests."""
    decisions: list[dict] = []
    budget = TICK_ACTION_BUDGET
    q = digests.get("queues") or {}
    ux = digests.get("ux") or {}
    sessions = digests.get("sessions") or {}

    def take(d: dict) -> None:
        nonlocal budget
        decisions.append(d)
        budget -= 1

    # 1) Utility backlog
    if budget > 0 and (q.get("utility_new") or 0) > 0:
        if capability_allowed("expand.utility_research") or capability_allowed("act.product_queue"):
            res = act_triage_utility_queue(db)
            take({
                "kind": "utility_triage",
                "capability": "expand.utility_research",
                "result": res,
            })
            # Tell Ford on the Sovereign Desk (never Energy Agent chat)
            if budget > 0 and res.get("triaged"):
                try:
                    from .energy_agent_sovereign_desk import push_sovereign_message
                    row = push_sovereign_message(
                        db,
                        "Ford — I triaged new utility requests into researching. "
                        "Open the Sovereign desk if you want the list or next unlocks.",
                        meta={"from": "rules_utility_triage"},
                        provider="rules",
                    )
                    take({"kind": "desk_utility_progress", "result": {"ok": True, "id": row.id}})
                except Exception as e:  # noqa: BLE001
                    take({"kind": "desk_utility_progress", "result": {"ok": False, "error": str(e)[:120]}})

    # 2) Escalations needing Ford → email digest once per tick if any
    if budget > 0 and (q.get("escalation_needs_ford") or 0) > 0:
        if capability_allowed("speak.email_ford"):
            ok = email_ford(
                f"[Sovereign] {q.get('escalation_needs_ford')} EA escalations need Ford",
                "Open admin escalations board and clear needs_ford items.\n"
                f"Counts: {json.dumps({k: v for k, v in q.items() if k.startswith('escalation_')})}",
            )
            audit(
                db, capability="speak.email_ford", decision="speak",
                rationale="escalation_needs_ford digest",
                targets={"count": q.get("escalation_needs_ford")},
                result="ok" if ok else "failed",
            )
            take({"kind": "email_escalations", "ok": ok})

    # 3) UX friction cluster → stage feature if volume high
    if budget > 0 and (ux.get("tasks_propose_ui_14d") or 0) >= 3:
        if capability_allowed("act.soft_stage") and capability_allowed("expand.ux_roadmap"):
            # Dedupe: only once per 24h
            since = _now() - timedelta(hours=24)
            recent = db.execute(
                select(func.count()).select_from(EaSovereignAction).where(
                    EaSovereignAction.capability == "act.soft_stage",
                    EaSovereignAction.created_at >= since,
                    EaSovereignAction.rationale.like("%UX friction cluster%"),
                )
            ).scalar() or 0
            if not recent:
                res = act_stage_feature(
                    db,
                    text=(
                        "UX friction cluster (sovereign): multiple propose_ui / complaint "
                        f"signals in 14d (count={ux.get('tasks_propose_ui_14d')}). "
                        "Review Energy Agent mind metrics and top surfaces for layout/clarity fixes."
                    ),
                )
                take({"kind": "ux_cluster_stage", "result": res})

    # 4) Code hire if many utility researching stuck > 0 and jobs empty
    if budget > 0 and (q.get("utility_researching") or 0) >= 2:
        if capability_allowed("act.code_hire"):
            since = _now() - timedelta(hours=12)
            recent_jobs = db.execute(
                select(func.count()).select_from(EaSovereignJob).where(
                    EaSovereignJob.kind == "draft_pr_brief",
                    EaSovereignJob.created_at >= since,
                )
            ).scalar() or 0
            if not recent_jobs:
                res = act_code_hire(
                    db,
                    title="Utility adapter research backlog",
                    brief=(
                        f"There are {q.get('utility_researching')} utility requests in "
                        "researching status. For each: identify portal family, document "
                        "login+data endpoints, open adapter work only with HAR/credentials. "
                        "Do not fabricate adapters. Priority: NEPOOL/owner-facing AO utilities."
                    ),
                    kind="draft_pr_brief",
                )
                take({"kind": "code_hire_utility", "result": res})

    # 5) Always record observe
    audit(
        db, capability="sense.queues", decision="observe",
        rationale="tick observe",
        targets={"queues": {k: v for k, v in q.items() if not str(k).endswith("_error")}},
        result="ok",
    )

    return decisions


def sovereign_tick(*, reason: str = "scheduler") -> dict[str, Any]:
    tick_id = _id("tick")
    if not sovereign_enabled():
        return {
            "ok": True,
            "tick_id": tick_id,
            "mode": "dark",
            "reason": reason,
            "enabled": False,
            "decisions": [],
        }

    with SessionLocal() as db:
        try:
            # Ensure new note/memory tables exist (idempotent)
            try:
                from .db import engine
                Base.metadata.create_all(
                    bind=engine,
                    tables=[
                        EaSovereignState.__table__,
                        EaSovereignAction.__table__,
                        EaSovereignGoal.__table__,
                        EaSovereignJob.__table__,
                        EaSovereignMessageOutbox.__table__,
                        EaSovereignNote.__table__,
                        EaSovereignMemory.__table__,
                        __import__("api.energy_agent_sovereign_desk", fromlist=["EaSovereignDeskMessage"]).EaSovereignDeskMessage.__table__,
                        __import__("api.energy_agent_sovereign_subconscious", fromlist=["EaSovereignEvent"]).EaSovereignEvent.__table__,
                    ],
                )
            except Exception:
                pass

            ensure_default_goals(db)
            digests = observe_product(db) if sovereign_sense_enabled() else {}
            state = world_get(db)

            goals_rows = db.execute(
                select(EaSovereignGoal).where(EaSovereignGoal.status == "open")
            ).scalars().all()
            goals_payload = [
                {"id": g.id, "title": g.title, "priority": g.priority, "status": g.status}
                for g in goals_rows
            ]
            notes_payload = recent_notes(db, limit=16)
            mem_payload = memory_get_all(db, limit=40)
            jobs_rows = db.execute(
                select(EaSovereignJob).where(EaSovereignJob.status == "queued")
                .order_by(EaSovereignJob.created_at.desc()).limit(10)
            ).scalars().all()
            jobs_payload = [
                {"id": j.id, "title": j.title, "kind": j.kind, "status": j.status}
                for j in jobs_rows
            ]

            # Subconscious tape + event stream (continuity between cortex ticks)
            subconscious_tape: list[dict] = []
            recent_events_payload: list[dict] = []
            heat_score: int | None = None
            try:
                sub_rows = db.execute(
                    select(EaSovereignNote)
                    .where(EaSovereignNote.kind == "subconscious")
                    .order_by(EaSovereignNote.created_at.desc())
                    .limit(16)
                ).scalars().all()
                subconscious_tape = [
                    {
                        "title": r.title,
                        "body": (r.body or "")[:600],
                        "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
                        "meta": r.meta_json,
                    }
                    for r in sub_rows
                ]
            except Exception:
                subconscious_tape = []
            try:
                from .energy_agent_sovereign_subconscious import recent_events
                recent_events_payload = recent_events(db, limit=10)
            except Exception:
                recent_events_payload = []
            try:
                raw_heat = next(
                    (m.get("value") for m in mem_payload if m.get("key") == "heat_score"),
                    None,
                )
                if raw_heat is not None:
                    heat_score = int(str(raw_heat).strip() or 0)
            except Exception:
                heat_score = state.get("heat") if isinstance(state.get("heat"), int) else None

            brain_plan: dict[str, Any] = {}
            decisions: list[dict] = []
            brain_provider = None

            # ── Cortex: independent mind (Grok → Claude fallback) ──────────
            try:
                from .energy_agent_sovereign_brain import think_cycle
                brain_plan = think_cycle(
                    digests=digests,
                    world=state,
                    goals=goals_payload,
                    recent_notes=notes_payload,
                    memories=mem_payload,
                    open_jobs=jobs_payload,
                    subconscious_tape=subconscious_tape,
                    recent_events=recent_events_payload,
                    heat=heat_score,
                )
            except Exception as e:  # noqa: BLE001
                brain_plan = {
                    "ok": False,
                    "error": str(e)[:400],
                    "fallback_to_rules": True,
                    "actions": [],
                    "monologue": f"(think import/run failed) {e}",
                }

            brain_provider = brain_plan.get("provider")
            monologue = brain_plan.get("monologue") or ""

            # Persist self-notes + memory + agenda from the brain
            for sn in brain_plan.get("self_notes") or []:
                if not isinstance(sn, dict):
                    continue
                write_note(
                    db,
                    kind=str(sn.get("kind") or "thought")[:24],
                    title=str(sn.get("title") or "note")[:240],
                    body=str(sn.get("body") or "")[:20000],
                    provider=brain_provider,
                    tick_id=tick_id,
                )
            if monologue and not any(
                (n.get("kind") == "thought" and monologue[:80] in (n.get("body") or ""))
                for n in (brain_plan.get("self_notes") or [])
                if isinstance(n, dict)
            ):
                write_note(
                    db, kind="thought", title="monologue",
                    body=monologue[:20000], provider=brain_provider, tick_id=tick_id,
                )
            for mw in brain_plan.get("memory_writes") or []:
                if isinstance(mw, dict) and mw.get("key"):
                    memory_set(db, str(mw["key"]), str(mw.get("value") or ""), source="brain")
            # Leadership continuity fields
            if brain_plan.get("ford_ask"):
                memory_set(
                    db, "ford_ask",
                    str(brain_plan["ford_ask"])[:2000],
                    source="brain",
                )
            if brain_plan.get("succession_gap"):
                memory_set(
                    db, "succession_gap",
                    str(brain_plan["succession_gap"])[:2000],
                    source="brain",
                )
            if brain_plan.get("mood"):
                memory_set(db, "mood", str(brain_plan["mood"])[:80], source="brain")
            if brain_plan.get("agenda"):
                apply_agenda(db, brain_plan["agenda"])

            # Ivory-tower observation log (structured digests snapshot)
            write_note(
                db,
                kind="observation",
                title=f"ivory tower · {reason}",
                body=json.dumps({
                    "queues": digests.get("queues"),
                    "fleet_global": digests.get("fleet_global"),
                    "ux": digests.get("ux"),
                    "sessions_open": (digests.get("sessions") or {}).get("open_count"),
                    "mood": brain_plan.get("mood"),
                    "confidence": brain_plan.get("confidence"),
                    "provider": brain_provider,
                }, default=str)[:12000],
                provider=brain_provider or "system",
                tick_id=tick_id,
            )

            # Execute brain actions, or fall back to rule engine if brain failed
            if brain_plan.get("ok") and (brain_plan.get("actions") or brain_plan.get("speak_product") or brain_plan.get("ford_ask")):
                actions = list(brain_plan.get("actions") or [])
                # Prefer desk channel for anything meant for Ford (not EA chat)
                desk_line = brain_plan.get("speak_product")
                if not desk_line and brain_plan.get("ford_ask"):
                    desk_line = (
                        "Ford — need you on this: " + str(brain_plan["ford_ask"])
                    )
                if desk_line:
                    actions.append({
                        "type": "speak",
                        "text": desk_line,
                        "importance": 72,
                        "rationale": "sovereign desk message (not Energy Agent)",
                    })
                decisions = execute_brain_actions(db, actions, tick_id=tick_id)
                audit(
                    db, capability="sense.product_health", decision="observe",
                    rationale=f"brain think via {brain_provider}",
                    targets={
                        "provider": brain_provider,
                        "model": brain_plan.get("model"),
                        "mood": brain_plan.get("mood"),
                        "n_actions": len(actions),
                    },
                    result="ok",
                    correlation_id=tick_id,
                )
            else:
                # Self-repair path: rule engine when both LLM providers fail
                write_note(
                    db, kind="system", title="fallback rules",
                    body=str(brain_plan.get("error") or brain_plan.get("denied_reason") or "no brain plan"),
                    provider="rules", tick_id=tick_id,
                )
                decisions = decide_and_act(db, digests) if digests else []
                brain_provider = brain_provider or "rules"

            # Full ops authority sweep (features / utilities / escalations / jobs)
            # Ford authorized thorough product control — runs every tick when enabled.
            try:
                from .energy_agent_sovereign_ops import ops_enabled, autonomous_ops_sweep
                if ops_enabled():
                    # Light cadence: every tick if queues hot, else still once
                    q = digests.get("queues") or {}
                    hot = (
                        (q.get("feature_reviewed") or 0) > 0
                        or (q.get("utility_researching") or 0) > 0
                        or (q.get("utility_reviewed") or 0) > 0
                        or (q.get("escalation_needs_ford") or 0) > 0
                        or (q.get("sovereign_jobs_queued") or 0) > 0
                    )
                    if hot or reason in ("admin_think", "admin_tick", "wake"):
                        sweep = autonomous_ops_sweep(db)
                        decisions.append({"kind": "ops_sweep", "result": {
                            "ok": sweep.get("ok"),
                            "features": (sweep.get("features") or {}).get("count"),
                            "utilities": (sweep.get("utilities") or {}).get("advanced"),
                            "escalations": (sweep.get("escalations") or {}).get("resolved"),
                            "jobs": (sweep.get("jobs") or {}).get("processed"),
                        }})
            except Exception as e:  # noqa: BLE001
                log.warning("ops sweep failed: %s", e)

            # Memory: last tick meta for continuity
            memory_set(
                db, "last_tick",
                json.dumps({
                    "at": _now().isoformat() + "Z",
                    "reason": reason,
                    "provider": brain_provider,
                    "mood": brain_plan.get("mood"),
                    "decisions": [d.get("kind") for d in decisions][:8],
                }, default=str),
                source="system",
            )
            memory_set(db, "last_cortex_at", _now().isoformat() + "Z", source="system")
            # Cortex consumed the handoff bit
            memory_set(
                db, "needs_cortex",
                json.dumps({
                    "value": False,
                    "why": "cortex_ran",
                    "at": _now().isoformat() + "Z",
                    "reason": reason,
                }),
                source="system",
            )

            state["health"] = digests.get("health") or state.get("health") or {}
            state["queues"] = digests.get("queues") or {}
            state["fleet_global"] = digests.get("fleet_global") or {}
            state["ux"] = digests.get("ux") or {}
            state["sessions"] = {
                "open_count": (digests.get("sessions") or {}).get("open_count"),
            }
            state["last_tick_at"] = _now().isoformat() + "Z"
            state["last_cortex_at"] = _now().isoformat() + "Z"
            state["last_brain_provider"] = brain_provider
            state["last_mood"] = brain_plan.get("mood")
            state["last_monologue_excerpt"] = (monologue or "")[:500]
            if heat_score is not None:
                state["heat"] = heat_score
            state["needs_cortex"] = False
            state["last_decisions"] = [
                {"kind": d.get("kind"), "ok": (d.get("result") or {}).get("ok", d.get("ok"))}
                for d in decisions
            ][:20]
            state["mode"] = "live"
            goals = db.execute(
                select(EaSovereignGoal).where(EaSovereignGoal.status == "open")
            ).scalars().all()
            state["goals"] = [
                {"id": g.id, "title": g.title, "priority": g.priority}
                for g in goals
            ]
            world_save(db, state)
            db.commit()
            return {
                "ok": True,
                "tick_id": tick_id,
                "mode": "live",
                "reason": reason,
                "enabled": True,
                "brain": {
                    "provider": brain_provider,
                    "model": brain_plan.get("model"),
                    "mood": brain_plan.get("mood"),
                    "confidence": brain_plan.get("confidence"),
                    "ok": brain_plan.get("ok"),
                    "fallback_to_rules": brain_plan.get("fallback_to_rules") or brain_provider == "rules",
                    "monologue_excerpt": (monologue or "")[:400],
                },
                "layer": "cortex",
                "heat": heat_score,
                "subconscious_tape_n": len(subconscious_tape),
                "digests": {
                    "queues": digests.get("queues"),
                    "fleet_global": digests.get("fleet_global"),
                    "ux": digests.get("ux"),
                    "sessions_open": (digests.get("sessions") or {}).get("open_count"),
                },
                "decisions": decisions,
                "revision": state.get("revision"),
            }
        except Exception as e:  # noqa: BLE001
            log.exception("sovereign_tick failed")
            try:
                db.rollback()
            except Exception:
                pass
            try:
                email_ford("[Sovereign] tick failed", str(e)[:2000])
            except Exception:
                pass
            return {
                "ok": False,
                "tick_id": tick_id,
                "mode": "error",
                "reason": reason,
                "error": str(e)[:500],
            }


# ── Public helpers used by plan_inject / plan_action ────────────────────────
def plan_inject(
    *,
    tenant_ids: list[str],
    speak: str,
    importance: int = 70,
    force: bool = False,
) -> dict[str, Any]:
    """Admin inject now lands on the Sovereign Desk (not Energy Agent)."""
    del tenant_ids, importance, force  # EA multi-tenant inject retired
    if not (speak or "").strip():
        return {"ok": False, "denied": True, "denied_reason": "empty speak"}
    with SessionLocal() as db:
        try:
            from .energy_agent_sovereign_desk import push_sovereign_message
            row = push_sovereign_message(
                db, speak.strip(),
                meta={"from": "admin_inject", "legacy": "plan_inject"},
                provider="admin",
            )
            db.commit()
            return {"ok": True, "channel": "desk", "message_id": row.id}
        except Exception as e:  # noqa: BLE001
            db.rollback()
            return {"ok": False, "error": str(e)[:300]}


def plan_action(capability: str, payload: dict | None = None) -> dict[str, Any]:
    payload = payload or {}
    if capability in NEVER_AUTONOMOUS:
        return {
            "ok": False,
            "denied": True,
            "denied_reason": f"{capability} is never fully autonomous — Ford dual-control required",
            "tier": CAPABILITIES.get(capability, {}).get("tier"),
        }
    if not capability_allowed(capability):
        return {
            "ok": False,
            "denied": True,
            "denied_reason": f"{capability} not allowed",
            "tier": CAPABILITIES.get(capability, {}).get("tier"),
        }
    with SessionLocal() as db:
        try:
            if capability == "act.soft_stage":
                out = act_stage_feature(
                    db,
                    text=payload.get("text") or payload.get("suggestion") or "",
                    tenant_id=payload.get("tenant_id"),
                    email=payload.get("email"),
                )
            elif capability in ("act.product_queue", "expand.utility_research"):
                if payload.get("feature_id"):
                    out = act_promote_feature_building(db, int(payload["feature_id"]))
                else:
                    out = act_triage_utility_queue(db)
            elif capability == "act.code_hire":
                out = act_code_hire(
                    db,
                    title=payload.get("title") or "Sovereign job",
                    brief=payload.get("brief") or payload.get("text") or "",
                    kind=payload.get("kind") or "draft_pr_brief",
                )
            elif capability == "speak.email_ford":
                ok = email_ford(
                    payload.get("subject") or "[Sovereign]",
                    payload.get("body") or "",
                )
                out = {"ok": ok}
            elif capability == "speak.session_broadcast":
                out = broadcast_open_sessions(
                    db,
                    payload.get("speak") or payload.get("text") or "",
                    importance=int(payload.get("importance") or 65),
                )
            else:
                out = {
                    "ok": False,
                    "denied": True,
                    "denied_reason": f"no worker for {capability}",
                }
            db.commit()
            return out
        except Exception as e:  # noqa: BLE001
            db.rollback()
            return {"ok": False, "error": str(e)[:400]}


# ── HTTP admin API ──────────────────────────────────────────────────────────
class WakeIn(BaseModel):
    reason: str = Field(default="admin", max_length=200)


class InjectIn(BaseModel):
    tenant_ids: list[str] = Field(default_factory=list)
    speak: str = Field(default="", max_length=2000)
    importance: int = Field(default=70, ge=0, le=100)
    force: bool = False


class ActIn(BaseModel):
    capability: str
    payload: dict = Field(default_factory=dict)


class GoalIn(BaseModel):
    id: str | None = None
    title: str
    priority: int = 50
    status: str = "open"
    detail: dict = Field(default_factory=dict)


class ModeIn(BaseModel):
    """Document-only mode report; env flags still authority of record."""
    note: str | None = None


@router.get("/admin/sovereign/state")
def sovereign_state(authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    world = _default_world()
    jobs, goals, actions = [], [], []
    notes_payload: list[dict] = []
    mem_payload: list[dict] = []
    try:
        from . import energy_agent_sovereign_brain as brain_mod
    except Exception:
        brain_mod = None  # type: ignore
    try:
        with SessionLocal() as db:
            try:
                from .db import engine
                Base.metadata.create_all(
                    bind=engine,
                    tables=[
                        EaSovereignState.__table__,
                        EaSovereignAction.__table__,
                        EaSovereignGoal.__table__,
                        EaSovereignJob.__table__,
                        EaSovereignMessageOutbox.__table__,
                        EaSovereignNote.__table__,
                        EaSovereignMemory.__table__,
                        __import__("api.energy_agent_sovereign_desk", fromlist=["EaSovereignDeskMessage"]).EaSovereignDeskMessage.__table__,
                        __import__("api.energy_agent_sovereign_subconscious", fromlist=["EaSovereignEvent"]).EaSovereignEvent.__table__,
                    ],
                )
            except Exception:
                pass
            world = world_get(db)
            try:
                jobs = db.execute(
                    select(EaSovereignJob).order_by(EaSovereignJob.created_at.desc()).limit(20)
                ).scalars().all()
                goals = db.execute(
                    select(EaSovereignGoal).order_by(EaSovereignGoal.priority.desc())
                ).scalars().all()
                actions = db.execute(
                    select(EaSovereignAction).order_by(EaSovereignAction.created_at.desc()).limit(30)
                ).scalars().all()
                notes_payload = recent_notes(db, limit=12)
                mem_payload = memory_get_all(db, limit=30)
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        world = _default_world()
        world["error"] = str(e)[:200]
    return {
        "architecture": "docs/plans/2026-07-15-energy-agent-sovereign-mind.md",
        "module": "api/energy_agent_sovereign.py",
        "mode": "live" if sovereign_enabled() else "dark",
        "enabled": sovereign_enabled(),
        "sense_enabled": sovereign_sense_enabled(),
        "speak_enabled": sovereign_speak_enabled(),
        "speak_all": sovereign_speak_all(),
        "act_enabled": sovereign_act_enabled(),
        "capabilities": {
            cid: {**meta, "allowed_now": capability_allowed(cid)}
            for cid, meta in CAPABILITIES.items()
        },
        "world": world,
        "goals": [
            {"id": g.id, "title": g.title, "status": g.status, "priority": g.priority}
            for g in goals
        ],
        "jobs": [
            {
                "id": j.id, "kind": j.kind, "status": j.status,
                "title": j.title, "created_at": j.created_at.isoformat() + "Z" if j.created_at else None,
            }
            for j in jobs
        ],
        "recent_actions": [
            {
                "id": a.id,
                "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
                "capability": a.capability,
                "decision": a.decision,
                "result": a.result,
                "rationale": (a.rationale or "")[:200],
            }
            for a in actions
        ],
        "notes_recent": notes_payload,
        "memory": mem_payload,
        "tenant_mind": "api/energy_agent_mind.py",
        "brain": {
            "module": "api/energy_agent_sovereign_brain.py",
            "primary": brain_mod.primary_provider() if brain_mod else "grok",
            "fallback": brain_mod.fallback_provider() if brain_mod else "claude",
            "enabled": brain_mod.brain_enabled() if brain_mod else False,
            "last_provider": world.get("last_brain_provider"),
            "last_mood": world.get("last_mood"),
            "monologue_excerpt": world.get("last_monologue_excerpt"),
        },
    }


@router.post("/admin/sovereign/wake")
def sovereign_wake(body: WakeIn, authorization: str | None = Header(default=None)):
    """Event-driven wake: append event → subconscious → cortex if hot."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_subconscious import wake_sovereign
    return wake_sovereign(
        body.reason or "admin_wake",
        {"source": "admin"},
        source="admin",
        force_cortex=True,
    )


@router.post("/admin/sovereign/subconscious")
def sovereign_subconscious_ep(authorization: str | None = Header(default=None)):
    """Run one subconscious tick only (notes/heat, no hard acts)."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_subconscious import subconscious_tick
    return subconscious_tick(reason="admin_subconscious", force=True)


@router.get("/admin/sovereign/events")
def sovereign_events_ep(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=30, ge=1, le=100),
):
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_subconscious import recent_events, ensure_event_tables
    ensure_event_tables()
    with SessionLocal() as db:
        return {"ok": True, "events": recent_events(db, limit=limit)}


@router.post("/admin/sovereign/tick")
def sovereign_tick_ep(authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    return sovereign_tick(reason="admin_tick")


@router.post("/admin/sovereign/inject")
def sovereign_inject(body: InjectIn, authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    return plan_inject(
        tenant_ids=body.tenant_ids,
        speak=body.speak,
        importance=body.importance,
        force=body.force,
    )


@router.post("/admin/sovereign/act")
def sovereign_act(body: ActIn, authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    return plan_action(body.capability, body.payload)


@router.get("/admin/sovereign/actions")
def sovereign_actions(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        rows = db.execute(
            select(EaSovereignAction).order_by(EaSovereignAction.created_at.desc()).limit(limit)
        ).scalars().all()
        return {
            "actions": [
                {
                    "id": a.id,
                    "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
                    "capability": a.capability,
                    "tier": a.tier,
                    "decision": a.decision,
                    "result": a.result,
                    "rationale": a.rationale,
                    "targets": json.loads(a.targets_json or "{}"),
                    "denied_reason": a.denied_reason,
                    "correlation_id": a.correlation_id,
                }
                for a in rows
            ]
        }


@router.get("/admin/sovereign/jobs")
def sovereign_jobs(authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        rows = db.execute(
            select(EaSovereignJob).order_by(EaSovereignJob.created_at.desc()).limit(50)
        ).scalars().all()
        return {
            "jobs": [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "status": j.status,
                    "title": j.title,
                    "brief": json.loads(j.brief_json or "{}"),
                    "result": json.loads(j.result_json or "null"),
                    "error": j.error,
                    "created_at": j.created_at.isoformat() + "Z" if j.created_at else None,
                }
                for j in rows
            ]
        }


@router.post("/admin/sovereign/goals")
def sovereign_goals_upsert(body: GoalIn, authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        gid = (body.id or _id("goal"))[:40]
        row = db.get(EaSovereignGoal, gid)
        if not row:
            row = EaSovereignGoal(id=gid)
            db.add(row)
        row.title = body.title[:240]
        row.priority = int(body.priority)
        row.status = (body.status or "open")[:16]
        row.detail_json = json.dumps(body.detail or {}, default=str)[:8000]
        row.updated_at = _now()
        db.commit()
        return {"ok": True, "id": gid}


@router.post("/admin/sovereign/mode")
def sovereign_mode(body: ModeIn, authorization: str | None = Header(default=None)):
    """Report mode; runtime flags remain env-based."""
    _require_sovereign_or_admin(authorization)
    from . import energy_agent_sovereign_brain as brain
    return {
        "ok": True,
        "enabled": sovereign_enabled(),
        "sense": sovereign_sense_enabled(),
        "speak": sovereign_speak_enabled(),
        "act": sovereign_act_enabled(),
        "brain_enabled": brain.brain_enabled(),
        "brain_primary": brain.primary_provider(),
        "brain_fallback": brain.fallback_provider(),
        "note": body.note or "Toggle via Railway env SOVEREIGN_* flags",
    }


@router.get("/admin/sovereign/notes")
def sovereign_notes(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=40, ge=1, le=200),
    kind: str | None = Query(default=None),
):
    """Private internal dialogue (ivory-tower monologue + decisions)."""
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        q = select(EaSovereignNote).order_by(EaSovereignNote.created_at.desc()).limit(limit)
        if kind:
            q = select(EaSovereignNote).where(EaSovereignNote.kind == kind).order_by(
                EaSovereignNote.created_at.desc()
            ).limit(limit)
        rows = db.execute(q).scalars().all()
        return {
            "notes": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
                    "kind": r.kind,
                    "title": r.title,
                    "body": r.body,
                    "provider": r.provider,
                    "tick_id": r.tick_id,
                }
                for r in rows
            ]
        }


@router.get("/admin/sovereign/memory")
def sovereign_memory(authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        return {"memory": memory_get_all(db, limit=100)}


class MemoryIn(BaseModel):
    key: str
    value: str


@router.post("/admin/sovereign/memory")
def sovereign_memory_set(body: MemoryIn, authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        memory_set(db, body.key, body.value, source="admin")
        db.commit()
        return {"ok": True, "key": body.key}


@router.post("/admin/sovereign/think")
def sovereign_think(authorization: str | None = Header(default=None)):
    """Force a full ivory-tower think cycle (same as tick with brain)."""
    _require_sovereign_or_admin(authorization)
    return sovereign_tick(reason="admin_think")


@router.post("/admin/sovereign/jobs/requeue")
def sovereign_jobs_requeue(authorization: str | None = Header(default=None)):
    """Re-queue jobs that failed for missing repo access (after worker unlock)."""
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        from .energy_agent_sovereign_ops import requeue_repo_failed_jobs
        out = requeue_repo_failed_jobs(db, limit=50)
        db.commit()
        return out


@router.post("/admin/sovereign/jobs/drain")
def sovereign_jobs_drain(authorization: str | None = Header(default=None)):
    """Run queued code jobs now (Claude Code / Grok → push/deploy)."""
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        from .energy_agent_sovereign_worker import drain_jobs
        # Ford: drain more when repo access is unlocked
        limit = int(os.getenv("SOVEREIGN_JOB_DRAIN_LIMIT", "3") or 3)
        out = drain_jobs(db, limit=max(1, min(limit, 8)))
        db.commit()
        return out
