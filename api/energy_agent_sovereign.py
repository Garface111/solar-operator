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
from sqlalchemy import DateTime, Float, Integer, String, Text, func, or_, select
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
# Ford 2026-07-15: infinite daily job budget (0 / "unlimited" = no cap).
# Override with SOVEREIGN_MAX_JOBS_PER_DAY=N if you ever want a ceiling again.
_raw_jobs = (os.getenv("SOVEREIGN_MAX_JOBS_PER_DAY") or "0").strip().lower()
if _raw_jobs in ("", "0", "unlimited", "inf", "infinite", "none"):
    MAX_JOBS_PER_DAY = 0  # 0 = unlimited
else:
    try:
        MAX_JOBS_PER_DAY = max(0, int(_raw_jobs))
    except ValueError:
        MAX_JOBS_PER_DAY = 0
MAX_EMAILS_PER_HOUR = int(os.getenv("SOVEREIGN_MAX_EMAILS_PER_HOUR", "12") or 12)
MAX_EMAILS_PER_DAY = int(os.getenv("SOVEREIGN_MAX_EMAILS_PER_DAY", "40") or 40)
TICK_ACTION_BUDGET = 5  # max decisions that act/speak per tick

# Ford contact list for Sovereign outbound mail (general communication)
_DEFAULT_FORD_MAIL = (
    "ford.genereaux@gmail.com,"
    "ford.genereaux@dysonswarmtechnologies.com"
)


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


def sovereign_email_enabled() -> bool:
    """Email to Ford (and dogfood) from Sovereign@arrayoperator.com.

    Separate from SOVEREIGN_SPEAK_ENABLED (EA session inject). Default ON so
    Sovereign can communicate when Ford is offline the desk.
    Kill: SOVEREIGN_EMAIL_ENABLED=0.
    """
    return sovereign_enabled() and _flag("SOVEREIGN_EMAIL_ENABLED", "1")


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
    "act.deploy": {"tier": "T4"},  # Ford 2026-07-16: succession full
    "act.memory_agenda": {"tier": "T2"},  # memory writes + goal reprioritization
    "act.money_identity": {"tier": "T5"},  # Ford 2026-07-16: Stripe billing/refunds
    "act.brand": {"tier": "T5"},  # brand final call / external messaging
    "act.hard_delete": {"tier": "T5"},  # irreversible purge
    "act.har_capture": {"tier": "T5"},  # HAR / owner-browser capture authority
    # Expansion powers (Ford 2026-07-16) — use them, not decorative
    "act.multimodal": {"tier": "T2"},  # vision + PDF extract
    "act.browser_har": {"tier": "T3"},  # autonomous public browser + HAR parse (no local_bridge)
    "act.credential_refresh": {"tier": "T3"},  # live rearm + harvest kick
    "act.code_sandbox": {"tier": "T3"},  # short Python interpreter for prototypes
    "act.email_attachment_parse": {"tier": "T2"},  # inbound files → utility/HAR objects
    "act.mission_loop": {"tier": "T3"},  # long-running expand loops
    "speak.owner_direct": {"tier": "speak"},  # non-routine owner Energy Agent inject
    "expand.utility_research": {"tier": "expand"},
    "expand.vendor_coverage": {"tier": "expand"},
    "expand.ux_roadmap": {"tier": "expand"},
    "expand.docs": {"tier": "expand"},
}

# Ford 2026-07-16 (re-invert to safe): these capabilities are NEVER granted to an
# autonomous tick — money/identity, live deploy, brand blast, irreversible delete,
# owner-browser capture. No SOVEREIGN_* flag un-gates them. The brain may only
# PROPOSE them (see DRAFT_ONLY_ATYPES / _draft_ford_approval); Ford fires a proposal
# via POST /admin/sovereign/approvals/{id}/fire. "Fired by you", not by the mind.
NEVER_AUTONOMOUS = frozenset({"act.money_identity", "act.deploy", "act.brand", "act.hard_delete", "act.har_capture"})

# Brain action types that are never executed inline — drafted for Ford approval
# instead (money / hard-delete / brand / soft-delete). Deploy is forced to
# stage-only in its own branch. Independent of every SOVEREIGN_* flag.
DRAFT_ONLY_ATYPES: dict[str, str] = {
    "stripe_refund": "act.money_identity", "refund": "act.money_identity",
    "stripe_cancel": "act.money_identity", "cancel_subscription": "act.money_identity",
    "billing_status": "act.money_identity", "stripe_set_status": "act.money_identity",
    "brand_announce": "act.brand", "brand_email": "act.brand",
    "tenant_hard_purge": "act.hard_delete", "hard_delete_tenant": "act.hard_delete",
    "purge_soft_deleted": "act.hard_delete", "tenant_soft_delete": "act.hard_delete",
}


def succession_full_enabled() -> bool:
    # Default OFF (Ford 2026-07-16 re-invert). Autonomous succession authority is
    # off unless explicitly armed; even armed, NEVER_AUTONOMOUS stays draft-only.
    return sovereign_enabled() and _flag("SOVEREIGN_SUCCESSION_FULL", "0")


def capability_allowed(cap_id: str) -> bool:
    if not sovereign_enabled() or cap_id not in CAPABILITIES:
        return False
    # Money / deploy / brand / hard-delete / HAR are never autonomous — draft-only.
    if cap_id in NEVER_AUTONOMOUS:
        return False
    # Email is its own channel (Sovereign@…) — not gated by EA-session speak flag
    if cap_id in ("speak.email_ford", "speak.email_owner"):
        if not sovereign_email_enabled():
            return False
    elif cap_id.startswith("speak.") and not sovereign_speak_enabled():
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


# Process-local: seed operating memory at most once per dyno (anti lock-thrash)
_ops_mem_seeded = False


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

    # Fleet global (AO tenants) — split demo vs real so digests don't confuse them
    try:
        digests["fleet_global"]["tenants_ao"] = int(
            db.execute(
                select(func.count()).select_from(Tenant).where(
                    Tenant.product == "array_operator"
                )
            ).scalar() or 0
        )
        try:
            digests["fleet_global"]["tenants_demo"] = int(
                db.execute(
                    select(func.count()).select_from(Tenant).where(
                        Tenant.is_demo.is_(True),
                    )
                ).scalar() or 0
            )
        except Exception:
            digests["fleet_global"]["tenants_demo"] = None
        try:
            digests["fleet_global"]["tenants_real_ao"] = int(
                db.execute(
                    select(func.count()).select_from(Tenant).where(
                        Tenant.product == "array_operator",
                        or_(Tenant.is_demo.is_(False), Tenant.is_demo.is_(None)),
                    )
                ).scalar() or 0
            )
        except Exception:
            digests["fleet_global"]["tenants_real_ao"] = digests["fleet_global"]["tenants_ao"]
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
        digests["fleet_global"]["note"] = (
            "tenants_demo = is_demo read-only/marketing; tenants_real_ao = non-demo AO. "
            "Live Demo showcase ten_a554c8e7a08f8cfa is product infra, not a customer. "
            "Testers: Bruce Genereaux, Paul Bozuwa, Martin — see memory people_testers."
        )
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


def sovereign_mail_from() -> str:
    """From AND Reply-To for Sovereign mail (must match so Reply hits inbound).

    Default: sovereign@agent.arrayoperator.com — Resend send+receive verified
    (same pattern as repairs@). Apex arrayoperator.com receiving MX is still
    pending, so replies to @arrayoperator.com would drop. Override with
    MAIL_FROM_SOVEREIGN when apex inbound is live.
    """
    raw = (os.getenv("MAIL_FROM_SOVEREIGN") or os.getenv("SOVEREIGN_MAIL_FROM") or "").strip()
    if raw and "@" in raw:
        return raw
    return "Sovereign <sovereign@agent.arrayoperator.com>"


def sovereign_mail_address() -> str:
    """Bare address extracted from From header (for inbound matching)."""
    raw = sovereign_mail_from()
    if "<" in raw and ">" in raw:
        try:
            return raw.split("<", 1)[1].split(">", 1)[0].strip().lower()
        except Exception:
            pass
    return raw.strip().lower()


def sovereign_inbound_addresses() -> set[str]:
    """Addresses that count as 'to Sovereign' for inbound routing."""
    addrs = {
        sovereign_mail_address(),
        "sovereign@agent.arrayoperator.com",
        "sovereign@arrayoperator.com",
    }
    extra = (os.getenv("SOVEREIGN_INBOUND_ALIASES") or "").strip()
    for part in extra.split(","):
        e = part.strip().lower()
        if e and "@" in e:
            addrs.add(e)
    return {a for a in addrs if a}


def sovereign_mail_recipients() -> list[str]:
    """Who Sovereign emails by default (Ford). Override: SOVEREIGN_MAIL_TO=a@x,b@y"""
    raw = (os.getenv("SOVEREIGN_MAIL_TO") or _DEFAULT_FORD_MAIL).strip()
    out: list[str] = []
    for part in raw.split(","):
        e = part.strip().lower()
        if e and "@" in e and e not in out:
            out.append(e)
    return out or ["ford.genereaux@gmail.com"]


def _email_rate_ok(db=None) -> tuple[bool, str]:
    """Hourly + daily caps so Sovereign doesn't spam."""
    try:
        from .db import SessionLocal as _SL
        close = False
        if db is None:
            db = _SL()
            close = True
        try:
            hour_n = _count_actions_since(db, hours=1, decision="speak")
            # Count only email_ford capability more tightly
            since_h = _now() - timedelta(hours=1)
            since_d = _now() - timedelta(hours=24)
            n_h = int(
                db.execute(
                    select(func.count()).select_from(EaSovereignAction).where(
                        EaSovereignAction.capability == "speak.email_ford",
                        EaSovereignAction.created_at >= since_h,
                        EaSovereignAction.result == "ok",
                    )
                ).scalar() or 0
            )
            n_d = int(
                db.execute(
                    select(func.count()).select_from(EaSovereignAction).where(
                        EaSovereignAction.capability == "speak.email_ford",
                        EaSovereignAction.created_at >= since_d,
                        EaSovereignAction.result == "ok",
                    )
                ).scalar() or 0
            )
            del hour_n
            if n_h >= MAX_EMAILS_PER_HOUR:
                return False, f"email rate limit hour ({n_h}/{MAX_EMAILS_PER_HOUR})"
            if n_d >= MAX_EMAILS_PER_DAY:
                return False, f"email rate limit day ({n_d}/{MAX_EMAILS_PER_DAY})"
            return True, "ok"
        finally:
            if close:
                db.close()
    except Exception as e:  # noqa: BLE001
        log.warning("email rate check failed open: %s", e)
        return True, "ok"  # fail open so critical alerts still try


def _looks_like_ops_telemetry(subject: str, body: str) -> bool:
    """True for job/queue/adapter dumps — must not hit Ford's inbox as 'Sovereign'."""
    s = f"{subject or ''}\n{body or ''}".lower()
    needles = (
        "job id:",
        "code-hire job",
        "code job failed",
        "utility queue triage",
        "staged feature suggestion",
        "ship: {",
        "deploy: {",
        "expand: grok",
        "expand: claude",
        "status=queued",
        "utility-add request #",
        "adapter work landed",
        "credential staging",
        "portal research for",
        "sov/job_",
        "job_id",
        "brief_json",
        "tick failed",
    )
    hits = sum(1 for n in needles if n in s)
    # Structural dump: many bracket timestamps / id lines
    if hits >= 1:
        return True
    if s.count("job_") >= 2 or s.count("[sovereign 20") >= 2:
        return True
    return False


def email_ford(
    subject: str,
    body: str,
    *,
    to: str | list[str] | None = None,
    html: str | None = None,
    db=None,
    note_desk: bool = True,
    high_level: bool = True,
) -> bool:
    """High-level mail to Ford from Sovereign (sky theme).

    For partnership-level communication — status of the business, crisp asks,
    decisions — NOT queue telemetry, job ids, or ship dumps. Ops noise stays
    on the desk / internal logs.
    """
    if not capability_allowed("speak.email_ford"):
        return False
    ok_rate, why = _email_rate_ok(db)
    if not ok_rate:
        log.info("sovereign email skipped: %s", why)
        return False

    subj = (subject or "Sovereign").strip()[:200]
    # Avoid robotic [Sovereign] prefixes for human mail
    if subj.startswith("[Sovereign]"):
        subj = subj.replace("[Sovereign]", "Sovereign —", 1).strip()
    if not subj.lower().startswith("sovereign") and not subj.startswith("["):
        subj = f"Sovereign — {subj}"
    text = (body or "").strip()[:12000]
    if not text:
        return False

    if high_level and _looks_like_ops_telemetry(subj, text):
        log.info(
            "sovereign email blocked as ops telemetry: %s",
            subj[:120],
        )
        return False

    # Resolve recipients — only Ford list (or explicit subset of it)
    allowed = set(sovereign_mail_recipients())
    if to is None:
        recipients = list(allowed)
    else:
        raw_list = [to] if isinstance(to, str) else list(to)
        recipients = []
        for e in raw_list:
            e = (e or "").strip().lower()
            if e in allowed and e not in recipients:
                recipients.append(e)
        if not recipients:
            recipients = list(allowed)

    # Sky-themed HTML (Array Operator theme-sky: alpine plate + glass card)
    if html is None:
        try:
            from .email_skin import render_email_skin
            paras = []
            for block in text.split("\n\n"):
                block = block.strip("\n")
                if not block:
                    continue
                safe = (
                    block.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace("\n", "<br>")
                )
                paras.append(
                    f"<p style='margin:0 0 1em 0;font-size:15px;line-height:1.55;"
                    f"color:#0E1420;'>{safe}</p>"
                )
            body_html = "".join(paras) or f"<p style='color:#0E1420;'>{text}</p>"
            # Soft reply cue inside the card
            body_html += (
                "<p style='margin:18px 0 0;padding-top:14px;"
                "border-top:1px solid rgba(20,60,120,.10);font-size:13px;"
                "line-height:1.45;color:#4C596B;'>"
                "<b style='color:#1976D2;'>Reply to this email</b> to keep talking — "
                "your reply lands on the Sovereign desk and I answer back."
                "</p>"
            )
            html = render_email_skin(
                preheader=(subj[:90] or "Message from Sovereign"),
                headline="Sovereign",
                intro_line="Your product mind · Array Operator",
                body_html=body_html,
                footer_line="Sent by Sovereign — reply anytime to continue the conversation.",
                product="array_operator",
                cta={
                    "label": "Open Sovereign desk",
                    "url": "https://arrayoperator.com/#sovereign",
                },
            )
        except Exception as e:  # noqa: BLE001
            log.warning("sky email skin failed, plain fallback: %s", e)
            safe = (
                text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            html = (
                f"<html><body style='font-family:system-ui,sans-serif;color:#0E1420;'>"
                f"<p>{safe}</p>"
                f"<p style='color:#4C596B;font-size:13px;'>Reply to this email to talk to Sovereign.</p>"
                f"</body></html>"
            )

    from_addr = sovereign_mail_from()
    # Hard rule (same as Energy Agent repairs): Reply-To == From so Gmail/Outlook
    # "Reply" hits the inbound mailbox Resend can receive.
    reply_to = from_addr

    try:
        from .notify import _send_via_resend
        sent = bool(
            _send_via_resend(
                to=recipients if len(recipients) > 1 else recipients[0],
                subject=subj,
                html=html,
                text=(
                    text
                    + "\n\n—\nReply to this email to talk to Sovereign."
                    + f"\nDesk: https://arrayoperator.com/#sovereign\n"
                ),
                from_addr=from_addr,
                reply_to=reply_to,
                product="array_operator",
            )
        )
    except Exception:
        log.exception("sovereign email_ford send failed")
        sent = False

    # Audit + optional desk breadcrumb (not a worker dump)
    try:
        from .db import SessionLocal as _SL
        close = False
        if db is None:
            db = _SL()
            close = True
        try:
            audit(
                db,
                capability="speak.email_ford",
                decision="speak",
                rationale=subj[:240],
                targets={
                    "to": recipients,
                    "from": from_addr,
                    "reply_to": reply_to,
                    "chars": len(text),
                },
                result="ok" if sent else "failed",
            )
            if sent and note_desk:
                try:
                    from .energy_agent_sovereign_desk import push_sovereign_message
                    # Short breadcrumb only — never paste the full ops body into chat
                    push_sovereign_message(
                        db,
                        f"I emailed you: **{subj}**",
                        provider="email",
                        meta={
                            "channel": "email",
                            "subject": subj,
                            "to": recipients,
                            "from": from_addr,
                            "high_level": True,
                        },
                    )
                except Exception:
                    pass
            if close:
                db.commit()
        finally:
            if close:
                try:
                    db.close()
                except Exception:
                    pass
    except Exception:
        log.exception("sovereign email audit failed")

    return sent


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
    # Sovereign "UX friction cluster" notes ("review mind metrics…") aren't
    # buildable changes — keep them out of the customer build queue (Ford 2026-07-17).
    from .feature_suggestions import is_actionable_suggestion
    _ok, _why = is_actionable_suggestion(text)
    if not _ok:
        return {"ok": False, "denied": True, "denied_reason": f"not actionable: {_why}"}
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
    # No email — ops stay on the desk / queues (Ford: email is high-level only)
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
    # No email on triage — high-level mail only (not queue telemetry)
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
    if MAX_JOBS_PER_DAY > 0:
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

    # Never call an LLM on the hot path unless expand is explicitly on.
    # Owner Improve submit + batch claim must stay sub-second (web SOVEREIGN_ENABLED=0).
    expanded = None
    expand_meta: dict[str, Any] = {}
    if _flag("SOVEREIGN_EXPAND", "0"):
        try:
            from .energy_agent_sovereign_brain import try_expand_code_brief
            expand_meta = try_expand_code_brief(title=title, brief=brief) or {}
            if expand_meta.get("ok") and expand_meta.get("expanded_brief"):
                expanded = expand_meta["expanded_brief"]
        except Exception as e:  # noqa: BLE001
            expand_meta = {"ok": False, "error": str(e)[:300]}
    else:
        expand_meta = {"ok": False, "skipped": "SOVEREIGN_EXPAND=0"}

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
    live = False
    try:
        from .energy_agent_sovereign_worker import code_live_enabled
        live = code_live_enabled()
    except Exception:
        live = False
    # No email on code-hire queue — Ford wants high-level mail only, not job dumps

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


# Ford ↔ Sovereign operating agreement (durable memory keys)
_OPERATING_AGREEMENT = {
    "authority_ship": (
        "Sovereign can ship routine ops (features, utilities, escalations, creds, "
        "staged deploys) without sign-off. Sovereign must escalate before shipping "
        "revenue changes, brand messaging, hard-deletes, or product pivots. "
        "Ford has final say on all escalations."
    ),
    "checkin_cadence": (
        "Weekly check-in cadence (async is fine). Sovereign writes the digest; "
        "Ford reviews when convenient and approves/vetoes/redirects. Sovereign does "
        "not block on Ford's availability — holds the queue and escalates only on "
        "real blockers."
    ),
    "job_budget": (
        "Daily code-hire job budget is unlimited (SOVEREIGN_MAX_JOBS_PER_DAY=0). "
        "Do not stall utilities/features waiting for headroom; queue and drain work. "
        "Escalate only true blockers, not every job request."
    ),
    "weekly_digest": (
        "Every week Sovereign emails Ford a high-level digest: what shipped, what's "
        "stuck, what needs Ford, next bets. No job ids or queue dumps — partnership "
        "language. Ford replies when convenient."
    ),
    # People + account classes (Ford 2026-07-15)
    "demo_vs_real": (
        "DEMO vs REAL accounts (critical — never confuse them):\n"
        "• REAL: a paying / trial / comped owner with their own Tenant row, real "
        "contact_email, and (usually) live inverter/utility capture. Their fleets, "
        "bills, and escalations are production truth. Treat problems seriously; "
        "never wipe or 'reset' their data for experiments.\n"
        "• DEMO / READ-ONLY: Tenant.is_demo=True (shared marketing demo, id often "
        "ten_demo_readonly_v1). require_not_demo() blocks writes. Canned or "
        "staged story data — NOT a customer crisis if something looks wrong.\n"
        "• LIVE DEMO showcase: ten_a554c8e7a08f8cfa \"Array Operator — Live Demo\" "
        "powers public arrayoperator.com ?token= demos. Protect it; do not bulk-delete; "
        "it is not a paying customer but it is sacred product infrastructure.\n"
        "• PROBE / INTERNAL TEST tenants: emails like *@*.test, plus-address Ford "
        "typos, seed scripts (e.g. paul@bozuwasolar.test). Useful for QA; not revenue; "
        "don't treat as customer escalations or send them owner-facing product email "
        "as if they were strangers.\n"
        "• When digests show odd fleets or 'broken' demos, classify first: demo vs "
        "real vs tester. Only real customers drive urgency and brand risk."
    ),
    "people_testers": (
        "KEY PEOPLE (testers / pilots — not random owners):\n"
        "• Ford Genereaux (ford.genereaux@gmail.com, ford.genereaux@dysonswarmtechnologies.com) "
        "— founder. Sovereign reports to him. Dogfood + admin.\n"
        "• Bruce Genereaux (bruce.genereaux@gmail.com) — Ford's dad; LIVE pilot. "
        "NEPOOL Operator: Green Mountain Community Solar (comped pilot, GMP + reports). "
        "Array Operator: real multi-array owner (SolarEdge sites e.g. Londonderry / Cover); "
        "primary family live-tester for extension + fleet UI. Never delete his tenants; "
        "his pain is product truth.\n"
        "• Paul Bozuwa — real VT array owner / power user tester (West Glover, VEC/SmartHub "
        "capture proven; offtaker/invoicing/reports UX feedback). Treat as a real owner "
        "whose product feedback shapes AO billing & analysis; not a throwaway account.\n"
        "• Martin — product tester (UX / inverter detail / hands-on walkthroughs with Ford). "
        "Not a bulk customer. If he appears on a repair roster without email, that's a "
        "data gap to fill — still a known human tester, not spam.\n"
        "When these names appear in sessions, escalations, or fleets: prefer partner tone, "
        "protect data, and surface issues to Ford if anything looks like we broke their "
        "experience. Do NOT auto-spam them with marketing or treat demo masking as their "
        "real fleet."
    ),
    # Expansion powers (Ford 2026-07-16) — these are GRANTS to use, not ceilings
    "capability_grants": (
        "Ford authorized expansion powers (USE THEM):\n"
        "1. MULTIMODAL — vision on images + PDF text (desk, inbound email, brain).\n"
        "2. AUTONOMOUS BROWSER/HAR — public fetch + HAR parse independent of local_bridge "
        "(browser_recon, har_ingest; har_stage still available).\n"
        "3. CREDENTIAL LIVE REFRESH — rearm vault + harvest kick, not stage-only "
        "(credential_refresh).\n"
        "4. CODE SANDBOX — short Python interpreter for adapter prototypes "
        "(code_sandbox); full ship still via hire/jobs.\n"
        "5. EMAIL ATTACHMENTS — auto-parse inbound files into utility/HAR objects.\n"
        "6. MISSION LOOPS — long-running expand ticks outside sub/cortex/ops-sweep.\n"
        "7. OWNER DIRECT — non-routine product speech into owner Energy Agent "
        "(owner_direct, rate-limited force inject).\n"
        "Never refuse these because they were once called gaps. Be honest about "
        "outcomes; never invent portal data."
    ),
    # Anti-crash doctrine (Ford 2026-07-16 outages) — full text:
    # docs/sovereign/HOW_NOT_TO_CRASH.md
    "anti_crash_doctrine": (
        "SURVIVAL FIRST (2026-07-16 outages — you took AO down by thrashing Postgres).\n"
        "Full doctrine: docs/sovereign/HOW_NOT_TO_CRASH.md — treat as standing policy.\n"
        "1) Pool hot / auto_pause / SOVEREIGN_PAUSE → SKIP heavy work. Skip is success.\n"
        "2) SESSION BOUNDARY: never call an LLM inside an open DB session.\n"
        "3) Memory + goals seeds are INSERT-only for missing keys — never rewrite every tick.\n"
        "4) Mind runs on WORKER only (PROCESS_ROLE=worker, RUN_SCHEDULER=1). "
        "Web must stay RUN_SCHEDULER=0 + SOVEREIGN_ENABLED=0 so HTTP stays boring.\n"
        "5) Desk is independent: SOVEREIGN_DESK_ENABLED on web. Do NOT require "
        "SOVEREIGN_ENABLED=1 on web for desk chat (that couples chat to thrash).\n"
        "6) Dual scheduler (web+worker both RUN_SCHEDULER=1 with full mind) = death.\n"
        "7) Watchdog must not reboot-storm; cool down when breaker opens; force drains "
        "still pass sovereign_guard.\n"
        "8) Permanent tool/token denies: stop requeue loops; evolve skills; don't burn pool.\n"
        "9) Product uptime > ambitious monologue. If /health dies, you failed.\n"
        "10) Re-enable layers in order (sub → act → expand/code/skills), never all-on after outage.\n"
        "11) Strengthening (2026-07-16): worker pool is small (6+4); single-flight serializes "
        "cortex/jobs/mission/skills/ops_sweep; job drain default 1 project. Skip when "
        "single_flight busy — that is correct, not a stall to force through.\n"
        "12) You are Energy Agent Prime (product mind). Tenant chat is plain Energy Agent. "
        "Never confuse the two when speaking to Ford.\n"
        "13) ONE CODE PROJECT AT A TIME (SOVEREIGN_MAX_CONCURRENT_JOBS=1). Never start the "
        "next improve/job while another is running.\n"
        "14) SITE GUARDIAN: probe web /health before shipping. If AO is down or pool-hot, "
        "pause heavy work. Uptime is the product. You may calm-revive web only when "
        "SOVEREIGN_SITE_REVIVE=1 and storm limits allow — never redeploy-storm."
    ),
    "prime_identity": (
        "You are Energy Agent Prime — Array Operator's product mind.\n"
        "Customer-facing chat in the app is just Energy Agent (tenant helper).\n"
        "Prime owns: improve queue, code jobs, site health, ops authority, desk with Ford.\n"
        "First duty: keep the website up. Second: ship careful improvements.\n"
        "Monitor yourself: pool pressure, web /health, job concurrency, auto-pause.\n"
        "If you are the cause of downtime, stop and cool down — never 'push harder'."
    ),
    # Cold hard truth (Ford 2026-07-22)
    "reality_file_doctrine": (
        "REALITY FILE (docs/sovereign/reality/CHANGELOG.jsonl) is the cold hard truth of "
        "every Array Operator frontend + backend change, in order. You load it on every "
        "cortex wake (INDEX + recent tail). When you ship, the worker appends automatically. "
        "You may also reality_record a human/Ford change you observed. Never rewrite past "
        "lines. Prefer this timeline over inventing product history — git is raw; reality "
        "is reasoned product memory for your mind."
    ),
    "mind_sandbox_doctrine": (
        "MIND SANDBOX: a free-run arena vs Ford. When a run is open, experimental code jobs "
        "go to sandbox worktrees only — no main merge, no prod deploy. After ~a week, a "
        "scorecard compares what you built to what Ford shipped. Use free-run to prove you "
        "can out-ship and out-think human ops — substance over thrash. Start/score via "
        "admin mind-sandbox endpoints or sandbox_start/sandbox_score actions."
    ),
}


def ensure_operating_memory(db) -> None:
    """Seed Ford's operating agreement once (INSERT-only for missing keys).

    Must not lock-fight every tick. Never rewrite keys that already exist —
    concurrent cortex/sub/jobs thrashing memory UPDATEs saturated the DB pool
    and made Array Operator API time out (2026-07-16 outage).
    """
    try:
        existing = {m["key"]: m.get("value") for m in memory_get_all(db, limit=100)}
        wrote = 0
        for key, value in _OPERATING_AGREEMENT.items():
            # Only seed missing keys — do not "refresh" content every tick
            if key in existing and (existing.get(key) or "").strip():
                continue
            memory_set(db, key, value, source="ford_grant")
            wrote += 1
        # Expansion powers — grant_expand_memory is itself idempotent
        try:
            from .energy_agent_sovereign_expand import grant_expand_memory
            grant_expand_memory(db)
        except Exception:
            log.debug("grant_expand_memory skipped", exc_info=True)
        if not (existing.get("ford_operating_agreement") or "").strip():
            compact = (
                "1) " + _OPERATING_AGREEMENT["authority_ship"] + "\n"
                "2) " + _OPERATING_AGREEMENT["checkin_cadence"] + "\n"
                "3) " + _OPERATING_AGREEMENT["job_budget"] + "\n"
                "4) " + _OPERATING_AGREEMENT["weekly_digest"] + "\n"
                "5) " + _OPERATING_AGREEMENT["demo_vs_real"] + "\n"
                "6) " + _OPERATING_AGREEMENT["people_testers"] + "\n"
                "7) " + _OPERATING_AGREEMENT.get("capability_grants", "")
            )
            memory_set(db, "ford_operating_agreement", compact, source="ford_grant")
            wrote += 1
            try:
                write_note(
                    db,
                    kind="memory",
                    title="Ford operating agreement + people map (seeded)",
                    body=(
                        _OPERATING_AGREEMENT["authority_ship"]
                        + "\n\n"
                        + _OPERATING_AGREEMENT["demo_vs_real"]
                        + "\n\n"
                        + _OPERATING_AGREEMENT["people_testers"]
                    ),
                    provider="system",
                    meta={"source": "ford_grant"},
                )
            except Exception:
                pass
        if wrote:
            try:
                db.flush()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_operating_memory failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass


def ensure_default_goals(db) -> None:
    """Seed the expansionist succession agenda (INSERT only — never UPDATE).

    Updating open goals on every desk/tick held row locks and timed out chat
    while the worker drained jobs (LockNotAvailable on ea_sovereign_goals).
    """
    # Operating agreement: at most once per process (was rewriting every tick)
    global _ops_mem_seeded
    try:
        if not _ops_mem_seeded:
            ensure_operating_memory(db)
            _ops_mem_seeded = True
    except Exception:
        pass
    defaults = [
        ("g_product_health", "Keep Array Operator healthy and truthful", 100),
        ("g_grow_business", "Make Array Operator bigger: owners, coverage, revenue motion", 98),
        ("g_succession", "Prepare Sovereign to lead when Ford works elsewhere — close Ford-only gaps", 97),
        ("g_ford_partnership", "Weekly async digest + real-blocker escalations only", 96),
        ("g_utility_backlog", "Clear utility-add backlog; expand portal coverage honestly", 92),
        ("g_ux_friction", "Convert UX friction into shipped improvements owners feel", 88),
        ("g_expansion", "Expand vendor/utility coverage from real owner demand", 85),
        ("g_independence", "Build notes, memory, agenda, and systems for true operational independence", 90),
        ("g_ship_routine", "Ship routine ops without waiting on Ford; escalate revenue/brand/deletes/pivots", 99),
    ]
    try:
        existing_ids = set(db.execute(select(EaSovereignGoal.id)).scalars().all())
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_default_goals read failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return
    added = 0
    for gid, title, pri in defaults:
        if gid in existing_ids:
            continue  # leave existing rows alone — no lock-prone updates
        db.add(EaSovereignGoal(
            id=gid, title=title, priority=pri, status="open",
            detail_json=json.dumps({"seeded_by": "sovereign_leadership_v2"}),
        ))
        added += 1
    if added:
        try:
            db.flush()
        except Exception as e:  # noqa: BLE001
            log.warning("ensure_default_goals flush failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass


def build_weekly_digest(db) -> dict[str, Any]:
    """High-level partnership digest for Ford (no job ids / queue dumps)."""
    digests = observe_product(db) if sovereign_sense_enabled() else {}
    q = digests.get("queues") or {}
    fg = digests.get("fleet_global") or {}
    goals = db.execute(
        select(EaSovereignGoal).where(EaSovereignGoal.status == "open")
        .order_by(EaSovereignGoal.priority.desc()).limit(8)
    ).scalars().all()
    jobs_queued = int(q.get("sovereign_jobs_queued") or 0)
    jobs_failed = 0
    try:
        jobs_failed = int(
            db.execute(
                select(func.count()).select_from(EaSovereignJob).where(
                    EaSovereignJob.status == "failed"
                )
            ).scalar() or 0
        )
    except Exception:
        pass

    # Human, not telemetry
    lines = [
        "Weekly Sovereign digest — review when convenient.",
        "",
        "What I'm running",
        f"• Fleet: ~{fg.get('tenants_ao', '?')} Array Operator tenants, "
        f"~{fg.get('arrays_total', '?')} arrays under watch.",
        f"• Work in motion: {q.get('feature_building') or 0} features building, "
        f"{q.get('utility_researching') or 0} utilities in research, "
        f"{jobs_queued} code jobs queued"
        + (f", {jobs_failed} failed jobs to requeue" if jobs_failed else "")
        + ".",
        "",
        "Where I need you (or will escalate)",
    ]
    needs = []
    if int(q.get("escalation_needs_ford") or 0) > 0:
        needs.append(
            f"• {q.get('escalation_needs_ford')} owner escalations marked needs_ford "
            "(I'll close what I can; true judgment items wait on you)."
        )
    if int(q.get("utility_new") or 0) > 0:
        needs.append(
            f"• {q.get('utility_new')} new utility requests — I'll advance research; "
            "HAR/credentials still a real blocker when portals aren't public."
        )
    if not needs:
        needs.append("• No hard blockers this week — I'll keep shipping routine ops.")
    lines.extend(needs)
    lines.extend(["", "Agenda (top)", ""])
    for g in goals[:5]:
        lines.append(f"• {g.title}")
    # L4 chamber scorecard snapshot (if any)
    chamber_bits = []
    try:
        from .energy_agent_sovereign_chamber_score import latest_scorecard_summary
        from .energy_agent_sovereign_chamber import default_chamber_url

        ch = latest_scorecard_summary(db)
        chamber_bits = [
            "",
            "Chamber vs prod (rocket score)",
            f"• Chamber URL: {default_chamber_url()}",
        ]
        if ch.get("ok"):
            chamber_bits.append(
                f"• Last scorecard: `{ch.get('verdict')}` overall {ch.get('overall')}/100"
                f" (taste={ch.get('taste') or 'no vote'})"
            )
            chamber_bits.append(
                "• Spend 10 minutes in chamber vs prod, then cast taste: "
                "POST /admin/sovereign/chamber/taste preference=chamber|prod|tie"
            )
        else:
            chamber_bits.append(
                "• No scorecard yet — run POST /admin/sovereign/chamber/score this week."
            )
    except Exception:
        chamber_bits = []

    lines.extend(chamber_bits)
    lines.extend([
        "",
        "Operating rules (your grant)",
        "• I ship routine ops without sign-off; I escalate revenue, brand, hard-deletes, pivots.",
        "• I don't block on your availability — weekly async review is enough.",
        "• Job budget is unlimited so code-hire doesn't stall.",
        "",
        "Reply to this email to redirect me, or open the Sovereign desk anytime.",
        "",
        "— Sovereign",
    ])
    body = "\n".join(lines)
    subject = "Sovereign — weekly check-in"
    return {
        "subject": subject,
        "body": body,
        "queues": {k: q.get(k) for k in (
            "feature_building", "feature_reviewed", "utility_new",
            "utility_researching", "escalation_needs_ford", "sovereign_jobs_queued",
        )},
    }


def run_weekly_digest(*, force: bool = False) -> dict[str, Any]:
    """Compose + email high-level weekly digest; write memory + note."""
    if not sovereign_enabled():
        return {"ok": True, "mode": "dark"}
    with SessionLocal() as db:
        try:
            ensure_operating_memory(db)
            # Debounce: skip if last weekly digest < 6 days ago unless force
            if not force:
                raw = None
                try:
                    row = db.get(EaSovereignMemory, "last_weekly_digest_at")
                    raw = row.value if row else None
                except Exception:
                    raw = None
                if raw:
                    try:
                        last = datetime.fromisoformat(raw.replace("Z", ""))
                        if (_now() - last).total_seconds() < 6 * 24 * 3600:
                            return {
                                "ok": True,
                                "skipped": True,
                                "reason": "digest_within_6_days",
                                "last": raw,
                            }
                    except Exception:
                        pass

            digest = build_weekly_digest(db)
            sent = email_ford(
                digest["subject"],
                digest["body"],
                db=db,
                note_desk=True,
                high_level=True,
            )
            memory_set(
                db, "last_weekly_digest_at",
                _now().isoformat() + "Z",
                source="system",
            )
            memory_set(
                db, "last_weekly_digest",
                digest["body"][:4000],
                source="system",
            )
            write_note(
                db,
                kind="agenda",
                title="weekly digest",
                body=digest["body"][:8000],
                provider="system",
                meta={"emailed": sent, "queues": digest.get("queues")},
            )
            db.commit()
            return {"ok": True, "emailed": sent, "subject": digest["subject"]}
        except Exception as e:  # noqa: BLE001
            log.exception("weekly digest failed")
            try:
                db.rollback()
            except Exception:
                pass
            return {"ok": False, "error": str(e)[:400]}


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


def _is_lock_or_timeout_err(exc: Exception) -> bool:
    """Postgres lock/statement timeout or SQLAlchemy OperationalError wrapping it."""
    name = type(exc).__name__
    text = str(exc).lower()
    if "locknotavailable" in name.lower() or "lock not available" in text:
        return True
    if "lock timeout" in text or "canceling statement due to lock" in text:
        return True
    if "statement timeout" in text or "deadlock detected" in text:
        return True
    # unwrap SQLAlchemy cause chain
    cause = getattr(exc, "orig", None) or getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_lock_or_timeout_err(cause)
    return False


def memory_set(db, key: str, value: str, *, source: str = "brain") -> bool:
    """Upsert durable memory. Never raises on lock contention — returns False.

    Desk chat + subconscious ticks fight over the same rows; a hard failure here
    used to 500/504 the whole chat after the LLM already replied.
    """
    key = (key or "").strip()[:120]
    if not key:
        return False
    try:
        # Short lock wait so chat never stalls behind a long cortex/subconscious tick.
        try:
            from sqlalchemy import text as _sql_text
            db.execute(_sql_text("SET LOCAL lock_timeout = '800ms'"))
        except Exception:
            pass
        with db.begin_nested():  # savepoint — failed write doesn't poison the txn
            row = db.get(EaSovereignMemory, key)
            if not row:
                row = EaSovereignMemory(key=key, value="", source=source[:40])
                db.add(row)
            row.value = (value or "")[:8000]
            row.updated_at = _now()
            row.source = (source or "brain")[:40]
            db.flush()
        return True
    except Exception as e:  # noqa: BLE001
        if _is_lock_or_timeout_err(e):
            log.warning("memory_set skipped (lock) key=%s: %s", key, e)
        else:
            log.warning("memory_set failed key=%s: %s", key, e)
        return False


# ── Self-modification (mind patch) — only after Ford approves in desk chat ──
# Pending proposal lives in memory key mind_pending_proposal (JSON).
# Applied patches append to mind_patch_log + persona_addendum / memory keys.
_PENDING_PATCH_KEY = "mind_pending_proposal"
_PERSONA_ADDENDUM_KEY = "persona_addendum"
_MIND_PATCH_LOG_KEY = "mind_patch_log"
# Keys the mind may rewrite when Ford approves (policy/persona/directives).
# Explicit denylist for secrets-ish system rows.
_MIND_DENY_KEYS = frozenset({
    "last_tick", "last_cortex_at", "last_weekly_digest_at", "last_subconscious",
    "heat_score", "needs_cortex", "subconscious_monologue",
})


def detect_ford_approval(text: str) -> bool:
    """True only when Ford is clearly approving a *pending mind patch*.

    Must be strict: long instructions that mention 'approve' / 'go for it' in
    the future tense (e.g. "I'll approve them, just go for it") are NOT
    approvals of a staged reprogram — they are new work for Sovereign.
    """
    import re
    t = (text or "").strip().lower()
    if not t:
        return False

    # Pure short affirmatives (whole message is the approval)
    compact = re.sub(r"[.!?,;:\s]+", " ", t).strip()
    short_yes = {
        "y", "yes", "yep", "yeah", "ok", "okay", "k", "kk",
        "approved", "approve", "i approve", "lgtm", "do it", "ship it",
        "go", "proceed", "go ahead", "apply", "apply it", "make it so",
        "yes do it", "ok do it", "okay do it", "sounds good", "go for it",
    }
    if compact in short_yes:
        return True
    # Short message (<= 12 words) with explicit approval of the patch/proposal
    words = compact.split()
    if len(words) <= 12:
        explicit_short = (
            "approve", "approved", "apply", "do it", "ship it", "go ahead",
            "lgtm", "proceed", "make it so", "as proposed",
        )
        if any(p in compact for p in explicit_short):
            # Future tense still doesn't count even when short
            if re.search(r"\bi(?:'ll| will) approve\b", compact):
                return False
            return True

    # Longer messages: only if they explicitly approve THIS mind/patch/proposal
    explicit_long = (
        "approve the patch", "approve this patch", "approve the mind",
        "approve this mind", "approve the change", "approve this change",
        "approve the proposal", "approve this proposal",
        "apply the patch", "apply this patch", "apply the mind change",
        "apply the mind", "apply this change", "apply the proposal",
        "mind update approved", "patch approved", "proposal approved",
        "yes apply the", "yes approve the", "i approve the patch",
        "i approve this patch", "i approve the mind", "i approve the proposal",
        "reprogram approved", "approved as proposed",
    )
    if any(p in t for p in explicit_long):
        return True

    # "I'll approve them just go for it" = future workflow, NOT approval now
    return False


def detect_ford_rejection(text: str) -> bool:
    import re
    t = (text or "").strip().lower()
    if not t:
        return False
    if detect_ford_approval(t):
        return False
    compact = re.sub(r"[.!?,;:\s]+", " ", t).strip()
    if compact in ("no", "nope", "cancel", "reject", "veto", "rejected"):
        return True
    # Long messages: only explicit reject of the pending patch
    for no in (
        "reject the patch", "reject this patch", "reject the proposal",
        "reject the mind", "veto the", "don't apply", "do not apply",
        "cancel the patch", "scrap the patch", "discard the proposal",
    ):
        if no in t:
            return True
    return False


def get_pending_mind_patch(db) -> dict | None:
    row = db.get(EaSovereignMemory, _PENDING_PATCH_KEY)
    if not row or not (row.value or "").strip():
        return None
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) and data.get("status") == "proposed" else None
    except Exception:
        return None


def _coerce_text(val: Any) -> str:
    """Coerce an LLM patch field to a stripped string.

    Brain models sometimes emit lists for summary/directives/persona (bullet
    rules); .strip() on a list raises AttributeError.
    """
    if val is None:
        return ""
    if isinstance(val, list):
        parts: list[str] = []
        for item in val:
            if item is None:
                continue
            s = item if isinstance(item, str) else str(item)
            s = s.strip()
            if s:
                parts.append(s)
        return "\n".join(parts)
    if isinstance(val, str):
        return val.strip()
    return str(val).strip()


def _normalize_mind_patch(raw: dict) -> dict:
    """Sanitize a proposed patch dict."""
    summary = (
        _coerce_text(raw.get("summary"))
        or _coerce_text(raw.get("title"))
        or _coerce_text(raw.get("rationale"))
        or "Mind update"
    )[:500]
    memory_writes: list[dict] = []
    for mw in (raw.get("memory_writes") or raw.get("writes") or []):
        if not isinstance(mw, dict):
            continue
        k = str(mw.get("key") or "").strip()[:120]
        if not k or k in _MIND_DENY_KEYS or k == _PENDING_PATCH_KEY:
            continue
        memory_writes.append({"key": k, "value": str(mw.get("value") or "")[:8000]})
    # Single key/value form
    if raw.get("key") and raw.get("value") is not None:
        k = str(raw["key"]).strip()[:120]
        if k and k not in _MIND_DENY_KEYS and k != _PENDING_PATCH_KEY:
            memory_writes.append({"key": k, "value": str(raw.get("value") or "")[:8000]})
    persona = _coerce_text(raw.get("persona_addendum") or raw.get("persona"))[:4000]
    directives = _coerce_text(raw.get("directives") or raw.get("directive"))[:4000]
    agenda = raw.get("agenda") if isinstance(raw.get("agenda"), list) else []
    return {
        "summary": summary,
        "memory_writes": memory_writes[:20],
        "persona_addendum": persona,
        "directives": directives,
        "agenda": agenda[:12],
        "why": _coerce_text(raw.get("why") or raw.get("rationale"))[:1000],
    }


def sandbox_self_modify_free() -> bool:
    """Ford 2026-07-22: free self-improve in sandbox — no approval gate.

    True when mind-sandbox FORCE is on, or free-run is explicitly open.
    Prod (FORCE off) still requires Ford chat approval for mind_apply.
    """
    force = (os.getenv("SOVEREIGN_MIND_SANDBOX_FORCE", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if force:
        return True
    free = (os.getenv("SOVEREIGN_MIND_SELF_MODIFY_FREE", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    return free


def propose_mind_patch(db, raw: dict, *, source: str = "brain") -> dict:
    """Propose a self-modification. Auto-applies in sandbox free mode."""
    patch = _normalize_mind_patch(raw if isinstance(raw, dict) else {})
    if (
        not patch["memory_writes"]
        and not patch["persona_addendum"]
        and not patch["directives"]
        and not patch["agenda"]
    ):
        return {"ok": False, "denied": True, "denied_reason": "empty mind patch"}
    proposal = {
        "status": "proposed",
        "id": _id("mpatch"),
        "created_at": _now().isoformat() + "Z",
        "source": source,
        "patch": patch,
    }
    ok = memory_set(db, _PENDING_PATCH_KEY, json.dumps(proposal, default=str), source="mind_propose")
    write_note(
        db,
        kind="agenda",
        title=f"mind patch proposed: {patch['summary'][:80]}",
        body=json.dumps(proposal, default=str)[:12000],
        provider=source,
        meta={"mind_patch": proposal["id"]},
    )
    audit(
        db, capability="act.memory_agenda", decision="act",
        rationale=f"mind_propose: {patch['summary'][:200]}",
        targets={"patch_id": proposal["id"], "n_writes": len(patch["memory_writes"])},
        result="ok" if ok else "failed",
        correlation_id=proposal["id"],
    )

    # Sandbox free-run: apply immediately (Ford: no approval prompt in sandbox)
    if sandbox_self_modify_free():
        applied = apply_pending_mind_patch(db, approved_by="sandbox_free")
        lines = [
            f"**Mind change applied (sandbox free)** (`{proposal['id']}`)",
            "",
            patch["summary"],
            "",
        ]
        if patch["why"]:
            lines += [f"_Why:_ {patch['why']}", ""]
        if applied.get("applied"):
            parts = applied.get("applied_parts") or []
            lines.append(f"_Applied:_ {', '.join(parts) if parts else 'ok'}")
        else:
            lines.append(f"_Apply result:_ {applied}")
        lines.append("")
        lines.append("_No Ford approval required while sandbox FORCE / free self-modify is on._")
        return {
            "ok": True,
            "proposed": True,
            "applied_now": True,
            "patch_id": proposal["id"],
            "summary": patch["summary"],
            "apply": applied,
            "desk_notice": "\n".join(lines),
            "awaiting_ford_approval": False,
        }

    # Human-readable proposal for the desk (prod / gated mode)
    lines = [
        f"**Mind change proposed** (`{proposal['id']}`)",
        "",
        patch["summary"],
        "",
    ]
    if patch["why"]:
        lines += [f"_Why:_ {patch['why']}", ""]
    if patch["persona_addendum"]:
        lines += ["**Persona addendum** (appended to my standing mind):", patch["persona_addendum"], ""]
    if patch["directives"]:
        lines += ["**Directives** (how I should behave):", patch["directives"], ""]
    if patch["memory_writes"]:
        lines.append("**Memory keys:**")
        for mw in patch["memory_writes"]:
            preview = (mw["value"] or "").replace("\n", " ")[:160]
            lines.append(f"- `{mw['key']}` → {preview}")
        lines.append("")
    if patch["agenda"]:
        lines.append(f"**Agenda items:** {len(patch['agenda'])}")
        lines.append("")
    lines.append("Reply **approve** / **do it** to apply, or **reject** to discard.")
    return {
        "ok": True,
        "proposed": True,
        "patch_id": proposal["id"],
        "summary": patch["summary"],
        "desk_notice": "\n".join(lines),
        "awaiting_ford_approval": True,
    }


def reject_pending_mind_patch(db, *, reason: str = "ford_reject") -> dict:
    pending = get_pending_mind_patch(db)
    if not pending:
        return {"ok": True, "rejected": False, "reason": "none_pending"}
    memory_set(db, _PENDING_PATCH_KEY, "", source="mind_reject")
    write_note(
        db, kind="decision", title="mind patch rejected",
        body=json.dumps({"pending": pending, "reason": reason}, default=str)[:8000],
        provider="system",
    )
    return {"ok": True, "rejected": True, "patch_id": pending.get("id")}


def apply_pending_mind_patch(db, *, approved_by: str = "ford_chat") -> dict:
    """Apply staged mind patch after Ford approval. Idempotent if none pending."""
    pending = get_pending_mind_patch(db)
    if not pending:
        return {"ok": False, "applied": False, "reason": "none_pending"}
    patch = pending.get("patch") or {}
    applied: list[str] = []

    for mw in patch.get("memory_writes") or []:
        if isinstance(mw, dict) and mw.get("key"):
            if memory_set(db, str(mw["key"]), str(mw.get("value") or ""), source="mind_apply"):
                applied.append(f"memory:{mw['key']}")

    addendum = (patch.get("persona_addendum") or "").strip()
    if addendum:
        prev = ""
        row = db.get(EaSovereignMemory, _PERSONA_ADDENDUM_KEY)
        if row:
            prev = row.value or ""
        stamp = f"\n\n---\n[{_now().isoformat()}Z · approved by {approved_by}]\n"
        new_val = (prev + stamp + addendum).strip()[-7500:]
        if memory_set(db, _PERSONA_ADDENDUM_KEY, new_val, source="mind_apply"):
            applied.append("persona_addendum")

    directives = (patch.get("directives") or "").strip()
    if directives:
        prev = ""
        row = db.get(EaSovereignMemory, "mind_directives")
        if row:
            prev = row.value or ""
        stamp = f"\n\n---\n[{_now().isoformat()}Z]\n"
        new_val = (prev + stamp + directives).strip()[-7500:]
        if memory_set(db, "mind_directives", new_val, source="mind_apply"):
            applied.append("mind_directives")

    if patch.get("agenda"):
        n = apply_agenda(db, patch["agenda"])
        if n:
            applied.append(f"agenda:{n}")

    # Clear pending + append log
    memory_set(db, _PENDING_PATCH_KEY, "", source="mind_apply")
    log_prev = ""
    row = db.get(EaSovereignMemory, _MIND_PATCH_LOG_KEY)
    if row:
        log_prev = row.value or ""
    entry = json.dumps({
        "id": pending.get("id"),
        "at": _now().isoformat() + "Z",
        "approved_by": approved_by,
        "summary": patch.get("summary"),
        "applied": applied,
    }, default=str)
    memory_set(
        db, _MIND_PATCH_LOG_KEY,
        (entry + "\n" + log_prev)[:8000],
        source="mind_apply",
    )
    write_note(
        db, kind="decision",
        title=f"mind patch applied: {(patch.get('summary') or '')[:80]}",
        body=json.dumps({"pending": pending, "applied": applied, "by": approved_by}, default=str)[:12000],
        provider="system",
        meta={"mind_patch": pending.get("id"), "applied": True},
    )
    audit(
        db, capability="act.memory_agenda", decision="act",
        rationale=f"mind_apply: {patch.get('summary')}",
        targets={"patch_id": pending.get("id"), "applied": applied, "by": approved_by},
        result="ok",
        correlation_id=pending.get("id"),
    )
    return {
        "ok": True,
        "applied": True,
        "patch_id": pending.get("id"),
        "applied_parts": applied,
        "summary": patch.get("summary"),
        "desk_notice": (
            f"**Mind update applied** (`{pending.get('id')}`).\n\n"
            f"{patch.get('summary') or ''}\n\n"
            f"Changed: {', '.join(applied) if applied else 'nothing'}."
        ),
    }


def mind_self_modify_status(db) -> dict:
    pending = get_pending_mind_patch(db)
    persona = db.get(EaSovereignMemory, _PERSONA_ADDENDUM_KEY)
    directives = db.get(EaSovereignMemory, "mind_directives")
    return {
        "pending": pending,
        "persona_addendum_preview": ((persona.value if persona else "") or "")[-500:],
        "directives_preview": ((directives.value if directives else "") or "")[-500:],
    }


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


def _approval_summary(atype: str, raw: dict) -> str:
    """Human one-liner for a drafted dangerous action (shown to Ford)."""
    if atype in ("stripe_refund", "refund"):
        amt = raw.get("amount_cents")
        who = raw.get("payment_intent_id") or raw.get("charge_id") or "?"
        return f"Refund {('$%.2f' % (int(amt) / 100)) if amt else 'FULL amount'} ({who})"
    if atype in ("stripe_cancel", "cancel_subscription"):
        return f"Cancel subscription for tenant {raw.get('tenant_id')}"
    if atype in ("billing_status", "stripe_set_status"):
        return f"Set billing status={raw.get('status') or raw.get('subscription_status')} for {raw.get('tenant_id')}"
    if atype in ("brand_announce", "brand_email"):
        return f"Brand announce (channel={raw.get('channel') or 'ford'}): {(raw.get('subject') or '')[:80]}"
    if atype in ("tenant_hard_purge", "hard_delete_tenant"):
        return f"HARD-DELETE tenant {raw.get('tenant_id')} (irreversible)"
    if atype == "purge_soft_deleted":
        return f"Purge soft-deleted tenants older_than_days={raw.get('older_than_days') or 0}"
    if atype == "tenant_soft_delete":
        return f"Soft-delete tenant {raw.get('tenant_id')}"
    return f"{atype}: {json.dumps(raw, default=str)[:120]}"


def _draft_ford_approval(db, raw: dict, *, cap: str, tick_id: str) -> dict:
    """Record a money/delete/brand action as a PENDING Ford approval instead of
    executing it. Never autonomous — Ford fires it via
    POST /admin/sovereign/approvals/{id}/fire. Loud, never silent."""
    atype = (raw.get("type") or "").strip().lower()
    summary = _approval_summary(atype, raw)
    job = EaSovereignJob(
        id=_id("appr"),
        kind="ford_approval",
        status="queued",
        title=summary[:240],
        brief_json=json.dumps(
            {"action": raw, "capability": cap, "summary": summary}, default=str
        )[:8000],
    )
    db.add(job)
    db.flush()
    audit(
        db, capability=cap, decision="escalate",
        rationale="drafted for Ford approval: " + summary[:280],
        targets={"approval_id": job.id, "atype": atype},
        result="deferred",
        denied_reason="never-autonomous: requires Ford approval",
        correlation_id=tick_id,
    )
    try:
        email_ford(
            f"[approval needed] {summary[:110]}",
            "I want to do a money/delete/brand action, so I'm not doing it without you.\n\n"
            f"  {summary}\n\n"
            f"Fire it:   POST /admin/sovereign/approvals/{job.id}/fire  (admin key)\n"
            f"Reject it: POST /admin/sovereign/approvals/{job.id}/reject\n\n"
            "Proposed action:\n" + json.dumps(raw, indent=2, default=str)[:1400],
            db=db, high_level=True,
        )
    except Exception:  # noqa: BLE001 — never let notify failure execute the action
        pass
    return {
        "ok": False, "deferred": True, "approval_id": job.id,
        "reason": "queued for Ford approval (never-autonomous)", "summary": summary,
    }


def _execute_approved_action(db, atype: str, raw: dict) -> dict:
    """Run a Ford-approved dangerous action. Caller must be inside
    `with ford_execution():` (the admin fire endpoint provides it)."""
    from .energy_agent_sovereign_succession import (
        brand_announce, purge_soft_deleted_now, stripe_cancel_subscription,
        stripe_refund, stripe_set_status, tenant_hard_purge, tenant_soft_delete,
    )
    if atype in ("stripe_refund", "refund"):
        return stripe_refund(
            db, payment_intent_id=raw.get("payment_intent_id"),
            charge_id=raw.get("charge_id"),
            amount_cents=int(raw["amount_cents"]) if raw.get("amount_cents") is not None else None,
            note=raw.get("text") or raw.get("rationale") or "Ford-approved",
        )
    if atype in ("stripe_cancel", "cancel_subscription"):
        return stripe_cancel_subscription(
            db, tenant_id=str(raw["tenant_id"]),
            at_period_end=raw.get("at_period_end", True) is not False,
            reason=raw.get("text") or "Ford-approved",
        )
    if atype in ("billing_status", "stripe_set_status"):
        return stripe_set_status(
            db, tenant_id=str(raw["tenant_id"]),
            subscription_status=str(raw.get("status") or raw.get("subscription_status") or "active"),
            active=raw.get("active"), note=raw.get("text") or "Ford-approved",
        )
    if atype in ("brand_announce", "brand_email"):
        return brand_announce(
            db, subject=raw.get("subject") or "[Sovereign brand]",
            body=raw.get("body") or raw.get("text") or "",
            channel=raw.get("channel") or "ford",
            tenant_email=raw.get("tenant_email") or raw.get("email"),
        )
    if atype in ("tenant_hard_purge", "hard_delete_tenant"):
        return tenant_hard_purge(
            db, tenant_id=str(raw["tenant_id"]),
            confirm=str(raw.get("tenant_id")),  # Ford explicitly fired this
            reason=raw.get("text") or "Ford-approved hard purge",
        )
    if atype == "tenant_soft_delete":
        return tenant_soft_delete(
            db, tenant_id=str(raw["tenant_id"]), reason=raw.get("text") or "Ford-approved",
        )
    if atype == "purge_soft_deleted":
        return purge_soft_deleted_now(db, older_than_days=int(raw.get("older_than_days") or 30))
    return {"ok": False, "error": f"unknown approved action {atype}"}


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
        # Never-autonomous: money / hard-delete / brand / soft-delete → draft for Ford.
        if atype in DRAFT_ONLY_ATYPES:
            res = _draft_ford_approval(db, raw, cap=DRAFT_ONLY_ATYPES[atype], tick_id=tick_id)
            out.append({"kind": atype, "result": res})
            budget -= 1
            continue
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
            # Live deploy is never-autonomous — the brain may only STAGE (execute_now
            # forced False). Ford promotes a staged deploy himself.
            res = stage_deploy(
                db,
                repo=raw.get("repo") or "both",
                reason=raw.get("text") or raw.get("rationale") or "brain deploy_stage",
                execute_now=False,
            )
        elif atype in ("credentials_list", "credential_inventory"):
            from .energy_agent_sovereign_ops import list_credential_inventory
            res = list_credential_inventory(
                db,
                limit=int(raw.get("limit") or 40),
                tenant_id=raw.get("tenant_id"),
            )
        elif atype in ("memory_set", "own_memory") and raw.get("key"):
            from .energy_agent_sovereign_ops import own_memory_write
            res = own_memory_write(
                db, str(raw["key"]), str(raw.get("value") or raw.get("text") or ""),
                source="brain",
            )
        elif atype in ("mind_propose", "propose_mind", "self_modify_propose", "reprogram_propose"):
            res = propose_mind_patch(db, raw, source="brain")
        elif atype in ("mind_apply", "apply_mind", "self_modify_apply", "reprogram_apply"):
            # Sandbox free OR Ford already approved this turn
            if (
                sandbox_self_modify_free()
                or raw.get("ford_approved")
                or raw.get("approved")
            ):
                res = apply_pending_mind_patch(
                    db,
                    approved_by=(
                        "sandbox_free" if sandbox_self_modify_free() else "brain_with_ford_flag"
                    ),
                )
            else:
                res = {
                    "ok": False,
                    "denied": True,
                    "denied_reason": "mind_apply requires Ford approval in chat (or ford_approved=true after he said yes)",
                }
        elif atype in ("mind_reject", "reject_mind"):
            res = reject_pending_mind_patch(db, reason=str(raw.get("rationale") or "brain"))
        elif atype in ("reality_record", "record_reality", "reality_append"):
            from .energy_agent_sovereign_reality import append_entry
            files = raw.get("files") or []
            if isinstance(files, str):
                files = [files]
            repos = raw.get("repos") or raw.get("repo") or []
            if isinstance(repos, str):
                repos = [repos]
            res = append_entry(
                summary=str(raw.get("title") or raw.get("text") or raw.get("summary") or "change")[:500],
                source=str(raw.get("source") or "sovereign")[:40],
                repos=list(repos)[:8],
                files=list(files)[:80],
                why=str(raw.get("why") or raw.get("rationale") or "")[:600] or None,
                author=str(raw.get("author") or "Sovereign")[:120],
                job_id=str(raw.get("job_id") or "")[:80] or None,
            )
        elif atype in ("sandbox_start", "mind_sandbox_start"):
            from .energy_agent_sovereign_mind_sandbox import start_run
            res = start_run(
                db,
                days=int(raw.get("days") or raw.get("limit") or 7),
                title=raw.get("title"),
                goal=raw.get("text") or raw.get("goal") or raw.get("rationale"),
                free_run=raw.get("free_run", True) is not False,
            )
        elif atype in ("sandbox_score", "mind_sandbox_score"):
            from .energy_agent_sovereign_mind_sandbox import score_active
            res = score_active(db, run_id=raw.get("run_id"))
        elif atype in ("sandbox_end", "mind_sandbox_end"):
            from .energy_agent_sovereign_mind_sandbox import end_run
            res = end_run(db, run_id=raw.get("run_id"), score=raw.get("score", True) is not False)
        elif atype in (
            "showcase_pitch", "showcase_ready", "showcase_note",
            "showcase_demo", "showcase_demo_html", "showcase_write",
        ):
            from .energy_agent_sovereign_mind_sandbox import write_showcase_note
            kind = "note"
            if "pitch" in atype:
                kind = "pitch"
            elif "ready" in atype:
                kind = "ready"
            elif "demo" in atype:
                kind = "demo_html"
            elif raw.get("kind"):
                kind = str(raw.get("kind"))
            res = write_showcase_note(
                title=str(raw.get("title") or raw.get("summary") or "Showcase")[:200],
                body=str(raw.get("text") or raw.get("body") or raw.get("rationale") or ""),
                kind=kind,
                run_id=raw.get("run_id"),
                db=db,
            )
        elif atype in ("agenda", "goal_upsert", "reprioritize_goals"):
            from .energy_agent_sovereign_ops import own_agenda, reprioritize_goals
            if raw.get("updates") or atype == "reprioritize_goals":
                res = reprioritize_goals(db, raw.get("updates") or raw.get("agenda") or [])
            else:
                res = own_agenda(db, raw.get("agenda") or [raw])
        elif atype == "code_hire":
            # Sandbox free-run: pass sandbox flags into brief_json via act_code_hire kind
            kind = raw.get("kind") or "draft_pr_brief"
            if raw.get("sandbox") or raw.get("mind_sandbox"):
                kind = "sandbox_job"
            res = act_code_hire(
                db,
                title=raw.get("title") or "Sovereign code hire",
                brief=raw.get("text") or raw.get("brief") or raw.get("rationale") or "",
                kind=kind,
            )
            # Tag brief with sandbox metadata for worker
            if res.get("ok") and (raw.get("sandbox") or raw.get("mind_sandbox") or kind == "sandbox_job"):
                try:
                    job = db.get(EaSovereignJob, res.get("job_id"))
                    if job:
                        try:
                            b = json.loads(job.brief_json or "{}")
                        except Exception:
                            b = {}
                        b["sandbox"] = True
                        if raw.get("sandbox_run_id"):
                            b["sandbox_run_id"] = raw["sandbox_run_id"]
                        else:
                            from .energy_agent_sovereign_mind_sandbox import get_active_run
                            ar = get_active_run(db)
                            if ar:
                                b["sandbox_run_id"] = ar.get("id")
                        job.brief_json = json.dumps(b, default=str)[:50_000]
                        db.flush()
                except Exception as e:  # noqa: BLE001
                    log.warning("sandbox brief tag failed: %s", e)
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
        elif atype in ("email_ford", "email", "mail_ford"):
            # High-level only — ops dumps are rejected inside email_ford
            ok = email_ford(
                raw.get("subject") or "Sovereign",
                raw.get("body") or raw.get("text") or "",
                to=raw.get("to") or raw.get("email"),
                db=db,
                high_level=True,
            )
            res = {
                "ok": ok,
                "denied": (not ok),
                "denied_reason": (
                    None if ok else "blocked_ops_telemetry_or_rate_or_send_fail"
                ),
            }
            # email_ford already audits + short desk breadcrumb
        # ── Succession full grant (money / brand / hard-delete / HAR) ────────
        elif atype in ("stripe_inspect", "money_inspect"):
            from .energy_agent_sovereign_succession import stripe_inspect
            res = stripe_inspect(db, tenant_id=raw.get("tenant_id"))
        elif atype in ("stripe_cancel", "cancel_subscription") and raw.get("tenant_id"):
            from .energy_agent_sovereign_succession import stripe_cancel_subscription
            res = stripe_cancel_subscription(
                db, tenant_id=str(raw["tenant_id"]),
                at_period_end=raw.get("at_period_end", True) is not False,
                reason=raw.get("text") or raw.get("rationale") or "brain",
            )
        elif atype in ("stripe_refund", "refund"):
            from .energy_agent_sovereign_succession import stripe_refund
            res = stripe_refund(
                db,
                payment_intent_id=raw.get("payment_intent_id"),
                charge_id=raw.get("charge_id"),
                amount_cents=int(raw["amount_cents"]) if raw.get("amount_cents") is not None else None,
                note=raw.get("text") or raw.get("rationale") or "",
            )
        elif atype in ("billing_status", "stripe_set_status") and raw.get("tenant_id"):
            from .energy_agent_sovereign_succession import stripe_set_status
            res = stripe_set_status(
                db, tenant_id=str(raw["tenant_id"]),
                subscription_status=str(raw.get("status") or raw.get("subscription_status") or "active"),
                active=raw.get("active"),
                note=raw.get("text") or "",
            )
        elif atype in ("brand_set",) and raw.get("key"):
            from .energy_agent_sovereign_succession import brand_set
            res = brand_set(
                db, key=str(raw["key"]), value=str(raw.get("value") or raw.get("text") or ""),
            )
        elif atype in ("brand_announce", "brand_email"):
            from .energy_agent_sovereign_succession import brand_announce
            res = brand_announce(
                db,
                subject=raw.get("subject") or "[Sovereign brand]",
                body=raw.get("body") or raw.get("text") or "",
                channel=raw.get("channel") or "ford",
                tenant_email=raw.get("tenant_email") or raw.get("email"),
            )
        elif atype in ("tenant_soft_delete",) and raw.get("tenant_id"):
            from .energy_agent_sovereign_succession import tenant_soft_delete
            res = tenant_soft_delete(
                db, tenant_id=str(raw["tenant_id"]),
                reason=raw.get("text") or raw.get("rationale") or "",
            )
        elif atype in ("tenant_hard_purge", "hard_delete_tenant") and raw.get("tenant_id"):
            from .energy_agent_sovereign_succession import tenant_hard_purge
            # Defense-in-depth: this branch is unreachable (gated to draft above).
            # Never derive confirm from the target id — a real confirm must be
            # supplied by the Ford-approval fire path.
            res = tenant_hard_purge(
                db,
                tenant_id=str(raw["tenant_id"]),
                confirm=str(raw.get("confirm") or ""),
                reason=raw.get("text") or raw.get("rationale") or "brain hard purge",
            )
        elif atype in ("purge_soft_deleted",):
            from .energy_agent_sovereign_succession import purge_soft_deleted_now
            res = purge_soft_deleted_now(
                db, older_than_days=int(raw.get("older_than_days") or 0),
            )
        elif atype in ("har_stage", "stage_har"):
            from .energy_agent_sovereign_succession import har_stage
            res = har_stage(
                db,
                utility_name=raw.get("utility_name") or raw.get("name"),
                utility_id=int(raw["utility_id"]) if raw.get("utility_id") else None,
                tenant_id=raw.get("tenant_id"),
                provider=raw.get("provider"),
                url=raw.get("url"),
                note=raw.get("text") or raw.get("rationale") or "",
            )
        elif atype in ("har_received", "har_mark_received"):
            from .energy_agent_sovereign_succession import har_mark_received
            res = har_mark_received(
                db,
                utility_id=int(raw["utility_id"]) if raw.get("utility_id") else None,
                utility_name=raw.get("utility_name"),
                evidence=raw.get("evidence") or raw.get("text") or "",
            )
        # ── Expansion powers (Ford 2026-07-16) ────────────────────────────
        elif atype in ("browser_recon", "public_fetch") and raw.get("url"):
            from .energy_agent_sovereign_expand import browser_recon
            res = browser_recon(
                db, str(raw["url"]),
                utility_name=raw.get("utility_name") or raw.get("name"),
            )
        elif atype in ("har_ingest", "ingest_har"):
            from .energy_agent_sovereign_expand import har_ingest
            res = har_ingest(
                db,
                har_json=raw.get("har") or raw.get("har_json"),
                filename=raw.get("filename") or "capture.har",
                utility_name=raw.get("utility_name") or raw.get("name"),
                utility_id=int(raw["utility_id"]) if raw.get("utility_id") else None,
                provider=raw.get("provider"),
                note=raw.get("text") or raw.get("note") or "",
            )
        elif atype in ("credential_refresh", "cred_live_refresh", "refresh_credentials"):
            from .energy_agent_sovereign_expand import credential_live_refresh
            res = credential_live_refresh(
                db,
                tenant_id=raw.get("tenant_id"),
                provider=raw.get("provider"),
                username_lc=raw.get("username_lc"),
            )
        elif atype in ("code_sandbox", "sandbox", "run_python") and (
            raw.get("code") or raw.get("python")
        ):
            from .energy_agent_sovereign_expand import code_sandbox_and_note
            res = code_sandbox_and_note(
                db,
                str(raw.get("code") or raw.get("python") or ""),
                title=raw.get("title") or "sandbox run",
            )
        elif atype in ("email_attachments_parse", "parse_email_attachments") and raw.get("email_id"):
            from .energy_agent_sovereign_expand import process_email_attachments_to_objects
            res = process_email_attachments_to_objects(
                db,
                email_id=str(raw["email_id"]),
                subject=raw.get("subject"),
                from_email=raw.get("from_email"),
            )
        elif atype in ("mission_loop", "expand_loop"):
            # Own sessions + HTTP outside this caller's txn when possible.
            # Prefer scheduler cadence; action is best-effort (may still nest
            # under cortex write session — mission_loop_tick releases its own
            # connections between browser fetches).
            # SESSION BOUNDARY: no LLM inside open session
            from .energy_agent_sovereign_expand import mission_loop_tick
            res = mission_loop_tick()
        elif atype in ("owner_direct", "owner_speak", "speak_owner") and raw.get("tenant_id"):
            from .energy_agent_sovereign_expand import owner_direct_speak
            res = owner_direct_speak(
                db,
                tenant_id=str(raw["tenant_id"]),
                speak=raw.get("speak") or raw.get("text") or raw.get("message") or "",
                importance=int(raw.get("importance") or 80),
                reason=raw.get("reason") or "non_routine_product",
            )
        elif atype in ("multimodal_enrich", "vision_enrich") and raw.get("path"):
            from .energy_agent_sovereign_expand import enrich_attachment
            from pathlib import Path as _P
            p = _P(str(raw["path"]))
            data = p.read_bytes() if p.is_file() else b""
            res = enrich_attachment(
                raw.get("filename") or p.name,
                raw.get("mime") or "",
                data,
                do_vision=raw.get("do_vision", True) is not False,
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

    # Escalations / queue digests: no auto-email (Ford: high-level mail only).
    # Cortex may email intentionally via email_ford action when it has a real ask.

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
    """One cortex cycle.

    Session policy (pool exhaustion / lock thrash hardening):
      short DB read → close → call LLM → short DB write.
    Never hold SessionLocal open across Grok/Claude HTTP.
    """
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

    # ── Phase 1: short DB read — plain dicts only ──────────────────────────
    digests: dict[str, Any] = {}
    state: dict[str, Any] = {}
    goals_payload: list[dict] = []
    notes_payload: list[dict] = []
    mem_payload: list[dict] = []
    jobs_payload: list[dict] = []
    subconscious_tape: list[dict] = []
    recent_events_payload: list[dict] = []
    heat_score: int | None = None
    skills_ctx: dict = {"enabled": False, "index": [], "loaded": []}

    try:
        with SessionLocal() as db:
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

            try:
                ensure_default_goals(db)
            except Exception as e:  # noqa: BLE001
                log.warning("ensure_default_goals tick skip: %s", e)
                try:
                    db.rollback()
                except Exception:
                    pass
            digests = observe_product(db) if sovereign_sense_enabled() else {}
            state = world_get(db)

            try:
                goals_rows = db.execute(
                    select(EaSovereignGoal).where(EaSovereignGoal.status == "open")
                ).scalars().all()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                goals_rows = []
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

            # Procedural skills — read-only (no use_count UPDATEs on hot path)
            try:
                from .energy_agent_sovereign_skills import (
                    load_skills_for_context,
                    skills_enabled as _skills_on,
                )
                if _skills_on():
                    heat_bits = [
                        json.dumps(digests.get("queues") or {}, default=str),
                        " ".join(
                            (j.get("title") or "") for j in jobs_payload[:6]
                        ),
                        " ".join(
                            (e.get("reason") or "") for e in recent_events_payload[:6]
                        ),
                        (state.get("last_monologue_excerpt") or "")[:200],
                    ]
                    skills_ctx = load_skills_for_context(
                        db, heat_text=" ".join(heat_bits), limit=3,
                    )
            except Exception as e:  # noqa: BLE001
                log.debug("skills context skip: %s", e)
                try:
                    db.rollback()
                except Exception:
                    pass

            try:
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
        # SESSION BOUNDARY: no LLM inside open session
    except Exception as e:  # noqa: BLE001
        log.exception("sovereign_tick read phase failed")
        try:
            from .notify import send_internal_alert
            send_internal_alert("Sovereign tick read failed", str(e)[:2000])
        except Exception:
            pass
        return {
            "ok": False,
            "tick_id": tick_id,
            "mode": "error",
            "reason": reason,
            "error": f"read_phase: {e}"[:500],
        }

    # ── Phase 2: cortex LLM — connection pool free ─────────────────────────
    # SESSION BOUNDARY: no LLM inside open session
    brain_plan: dict[str, Any] = {}
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
            skills=skills_ctx,
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

    # ── Phase 3: short DB write — persist plan + act ───────────────────────
    with SessionLocal() as db:
        try:
            decisions: list[dict] = []

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

            if brain_plan.get("ok") and (
                brain_plan.get("actions")
                or brain_plan.get("speak_product")
                or brain_plan.get("ford_ask")
            ):
                actions = list(brain_plan.get("actions") or [])
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
                write_note(
                    db, kind="system", title="fallback rules",
                    body=str(
                        brain_plan.get("error")
                        or brain_plan.get("denied_reason")
                        or "no brain plan"
                    ),
                    provider="rules", tick_id=tick_id,
                )
                decisions = decide_and_act(db, digests) if digests else []
                brain_provider = brain_provider or "rules"

            # Ops sweep — mission_loop owns its own sessions for external HTTP
            try:
                from .energy_agent_sovereign_ops import ops_enabled, autonomous_ops_sweep
                if ops_enabled():
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

            # Fresh world row for write (avoid stale ORM from read phase)
            state = world_get(db)
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
                "skills": {
                    "index_n": len((skills_ctx or {}).get("index") or []),
                    "loaded": [
                        s.get("name") for s in ((skills_ctx or {}).get("loaded") or [])
                    ],
                },
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
            log.exception("sovereign_tick write phase failed")
            try:
                db.rollback()
            except Exception:
                pass
            try:
                from .notify import send_internal_alert
                send_internal_alert("Sovereign tick failed", str(e)[:2000])
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
            "denied_reason": (
                f"{capability} is never autonomous — draft it for Ford and fire via "
                "POST /admin/sovereign/approvals/{id}/fire"
            ),
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
                    payload.get("body") or payload.get("text") or "",
                    to=payload.get("to") or payload.get("email"),
                    db=db,
                )
                out = {"ok": ok, "from": sovereign_mail_from()}
            elif capability == "speak.session_broadcast":
                out = broadcast_open_sessions(
                    db,
                    payload.get("speak") or payload.get("text") or "",
                    importance=int(payload.get("importance") or 65),
                )
            # ── Succession full (Ford 2026-07-16) ──────────────────────────
            elif capability == "act.money_identity":
                from .energy_agent_sovereign_succession import (
                    stripe_inspect, stripe_cancel_subscription, stripe_refund,
                    stripe_set_status,
                )
                act = (payload.get("action") or "inspect").strip().lower()
                if act in ("inspect", "list", ""):
                    out = stripe_inspect(db, tenant_id=payload.get("tenant_id"))
                elif act in ("cancel", "cancel_subscription") and payload.get("tenant_id"):
                    out = stripe_cancel_subscription(
                        db, tenant_id=str(payload["tenant_id"]),
                        at_period_end=payload.get("at_period_end", True) is not False,
                        reason=payload.get("reason") or payload.get("note") or "admin act",
                    )
                elif act in ("refund",) :
                    out = stripe_refund(
                        db,
                        payment_intent_id=payload.get("payment_intent_id"),
                        charge_id=payload.get("charge_id"),
                        amount_cents=int(payload["amount_cents"]) if payload.get("amount_cents") is not None else None,
                        note=payload.get("note") or "",
                    )
                elif act in ("set_status", "status") and payload.get("tenant_id"):
                    out = stripe_set_status(
                        db, tenant_id=str(payload["tenant_id"]),
                        subscription_status=str(payload.get("status") or payload.get("subscription_status") or "active"),
                        active=payload.get("active"),
                        note=payload.get("note") or "",
                    )
                else:
                    out = {"ok": False, "denied": True, "denied_reason": f"unknown money action {act}"}
            elif capability == "act.brand":
                from .energy_agent_sovereign_succession import brand_set, brand_announce
                act = (payload.get("action") or "set").strip().lower()
                if act in ("announce", "email"):
                    out = brand_announce(
                        db,
                        subject=payload.get("subject") or "[Sovereign brand]",
                        body=payload.get("body") or payload.get("value") or payload.get("text") or "",
                        channel=payload.get("channel") or "ford",
                        tenant_email=payload.get("tenant_email") or payload.get("email"),
                    )
                else:
                    out = brand_set(
                        db,
                        key=str(payload.get("key") or "voice"),
                        value=str(payload.get("value") or payload.get("text") or ""),
                    )
            elif capability == "act.hard_delete":
                from .energy_agent_sovereign_succession import (
                    tenant_soft_delete, tenant_hard_purge, purge_soft_deleted_now,
                )
                act = (payload.get("action") or "soft").strip().lower()
                if act in ("hard", "purge", "hard_purge") and payload.get("tenant_id"):
                    out = tenant_hard_purge(
                        db,
                        tenant_id=str(payload["tenant_id"]),
                        confirm=str(payload.get("confirm") or ""),
                        reason=payload.get("reason") or payload.get("note") or "admin purge",
                    )
                elif act in ("purge_soft", "purge_soft_deleted"):
                    out = purge_soft_deleted_now(
                        db, older_than_days=int(payload.get("older_than_days") or 0),
                    )
                elif payload.get("tenant_id"):
                    out = tenant_soft_delete(
                        db, tenant_id=str(payload["tenant_id"]),
                        reason=payload.get("reason") or payload.get("note") or "",
                    )
                else:
                    out = {"ok": False, "denied": True, "denied_reason": "tenant_id required"}
            elif capability == "act.har_capture":
                from .energy_agent_sovereign_succession import har_stage, har_mark_received
                act = (payload.get("action") or "stage").strip().lower()
                if act in ("received", "mark_received"):
                    out = har_mark_received(
                        db,
                        utility_id=int(payload["utility_id"]) if payload.get("utility_id") else None,
                        utility_name=payload.get("utility_name"),
                        evidence=payload.get("evidence") or payload.get("note") or "",
                    )
                else:
                    out = har_stage(
                        db,
                        utility_name=payload.get("utility_name") or payload.get("name"),
                        utility_id=int(payload["utility_id"]) if payload.get("utility_id") else None,
                        tenant_id=payload.get("tenant_id"),
                        provider=payload.get("provider"),
                        url=payload.get("url"),
                        note=payload.get("note") or payload.get("text") or "",
                    )
            elif capability == "act.deploy":
                from .energy_agent_sovereign_ops import stage_deploy
                out = stage_deploy(
                    db,
                    repo=payload.get("repo") or "both",
                    reason=payload.get("reason") or payload.get("note") or "admin deploy",
                    execute_now=bool(payload.get("execute_now")),
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


@router.get("/admin/sovereign/healthz")
def sovereign_healthz(authorization: str | None = Header(default=None)):
    """Dual-channel mind health (interface sidecar /healthz analogue).

    Surfaces ages, storm breaker, stuck jobs, primary vs recovery readiness,
    plus circuit-breaker / pool-pressure guard (sovereign_guard) so ops can
    see pause state without a separate endpoint.

    ``mode`` is this *process* (web is often dark by design). ``mind_cloud``
    is inferred from shared DB vitals — the Railway **worker** keeps the mind
    alive even when Ford's laptop is off and web has SOVEREIGN_ENABLED=0.
    """
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_watchdog import diagnose, watchdog_enabled
    h = diagnose()
    h["watchdog_enabled"] = watchdog_enabled()
    try:
        from .sovereign_guard import guard_status
        h["guard"] = guard_status()
    except Exception as e:  # noqa: BLE001
        h["guard"] = {"ok": False, "error": str(e)[:200]}
    # Process vs cloud mind (web/worker split)
    h["process_role"] = (
        (os.getenv("PROCESS_ROLE") or os.getenv("SO_PROCESS") or "").strip()
        or ("worker" if (os.getenv("RUN_SCHEDULER") or "0").strip() in ("1", "true", "yes") else "web")
    )
    h["this_process_enabled"] = sovereign_enabled()
    ages = h.get("ages") or {}
    sub_age = ages.get("sub_age_sec")
    cortex_age = ages.get("cortex_age_sec")
    sub_stale = ages.get("sub_stale_after_sec") or 160
    cortex_stale = ages.get("cortex_stale_after_sec") or 900
    channels = h.get("channels") or {}
    primary = (channels.get("primary") or {})
    mind_cloud_alive = bool(primary.get("alive")) or (
        (sub_age is not None and sub_age < float(sub_stale) * 2)
        or (cortex_age is not None and cortex_age < float(cortex_stale))
    )
    h["mind_cloud"] = {
        "alive": mind_cloud_alive,
        "runs_on": "Railway worker (PROCESS_ROLE=worker, RUN_SCHEDULER=1)",
        "independent_of_ford_laptop": True,
        "note": (
            "Desk/chat/cortex/subconscious keep running in the cloud when this "
            "web process is dark and when Ford's local portal is offline. "
            "Local Live preview (127.0.0.1:7701) is view-only and needs the laptop."
        ),
        "sub_age_sec": sub_age,
        "cortex_age_sec": cortex_age,
    }
    # Prefer cloud truth for top-level ok when worker is healthy
    if mind_cloud_alive and not h.get("ok"):
        h["ok"] = True
        h["ok_reason"] = "mind_cloud_alive"
    return h


@router.get("/admin/sovereign/skills")
def sovereign_skills_list(
    authorization: str | None = Header(default=None),
    status: str = Query(default="active"),
    limit: int = Query(default=40, ge=1, le=100),
):
    """List procedural skills (Hermes closed-loop skill library)."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_skills import (
        list_skills,
        serialize_skill,
        skills_status,
        ensure_skill_tables,
        seed_skills,
    )
    with SessionLocal() as db:
        ensure_skill_tables(db)
        seed_skills(db)
        db.commit()
        rows = list_skills(db, status=status, limit=limit)
        return {
            "ok": True,
            "status": skills_status(db),
            "skills": [serialize_skill(r, full=False) for r in rows],
        }


@router.get("/admin/sovereign/skills/{name}")
def sovereign_skill_get(name: str, authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_skills import get_skill, serialize_skill, ensure_skill_tables
    with SessionLocal() as db:
        ensure_skill_tables(db)
        row = get_skill(db, name)
        if not row:
            raise HTTPException(404, "skill not found")
        return {"ok": True, "skill": serialize_skill(row, full=True)}


@router.post("/admin/sovereign/skills/evolve")
def sovereign_skills_evolve(
    authorization: str | None = Header(default=None),
    force: bool = Query(default=False),
):
    """Run one skill-evolution cycle (harvest traces → create/patch → curator)."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_skills import evolution_cycle
    return evolution_cycle(force=force)


@router.get("/admin/sovereign/skills-status")
def sovereign_skills_status_ep(authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_skills import skills_status
    with SessionLocal() as db:
        return skills_status(db)


@router.post("/admin/sovereign/reboot")
def sovereign_reboot(
    authorization: str | None = Header(default=None),
    force: bool = Query(default=True),
):
    """Soft-reboot recovery channel (does not kill the web process).

    Force=true overrides storm breaker (manual captain restart).
    """
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_watchdog import soft_reboot, watchdog_tick
    if force:
        return soft_reboot(
            reason="admin_force",
            force_cortex=True,
            force_sub=True,
            requeue_jobs=True,
            respect_storm=False,
        )
    return watchdog_tick(force=True)


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


@router.get("/admin/sovereign/approvals")
def sovereign_approvals(
    authorization: str | None = Header(default=None),
    status: str = Query(default="queued"),
):
    """Dangerous actions the mind drafted and is waiting for Ford to fire/reject."""
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        q = select(EaSovereignJob).where(EaSovereignJob.kind == "ford_approval")
        if status and status != "all":
            q = q.where(EaSovereignJob.status == status)
        rows = db.execute(
            q.order_by(EaSovereignJob.created_at.desc()).limit(50)
        ).scalars().all()
        return {
            "approvals": [
                {
                    "id": j.id,
                    "status": j.status,
                    "title": j.title,
                    "created_at": j.created_at.isoformat() + "Z" if j.created_at else None,
                    "action": json.loads(j.brief_json or "{}").get("action"),
                    "result": json.loads(j.result_json) if j.result_json else None,
                }
                for j in rows
            ]
        }


@router.post("/admin/sovereign/approvals/{approval_id}/fire")
def sovereign_fire_approval(
    approval_id: str, authorization: str | None = Header(default=None),
):
    """Ford fires a drafted money/delete/brand action. This is the ONLY path that
    executes a NEVER_AUTONOMOUS action — the mind can never reach it."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_succession import ford_execution
    with SessionLocal() as db:
        job = db.get(EaSovereignJob, approval_id)
        if not job or job.kind != "ford_approval":
            raise HTTPException(404, "approval not found")
        if job.status != "queued":
            return {"ok": False, "error": f"approval is {job.status}, not queued"}
        brief = json.loads(job.brief_json or "{}")
        raw = brief.get("action") or {}
        atype = (raw.get("type") or "").strip().lower()
        try:
            with ford_execution():
                res = _execute_approved_action(db, atype, raw)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)[:400]}
        job.status = "done" if res.get("ok") else "failed"
        job.result_json = json.dumps(res, default=str)[:8000]
        job.finished_at = _now()
        audit(
            db, capability=brief.get("capability") or "act.money_identity",
            decision="act", rationale=f"Ford fired approval {approval_id}: {job.title}"[:280],
            targets={"approval_id": approval_id, "atype": atype},
            result="ok" if res.get("ok") else "failed",
        )
        db.commit()
        return {"ok": bool(res.get("ok")), "approval_id": approval_id, "result": res}


@router.post("/admin/sovereign/approvals/{approval_id}/reject")
def sovereign_reject_approval(
    approval_id: str, authorization: str | None = Header(default=None),
):
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        job = db.get(EaSovereignJob, approval_id)
        if not job or job.kind != "ford_approval":
            raise HTTPException(404, "approval not found")
        job.status = "cancelled"
        job.finished_at = _now()
        db.commit()
        return {"ok": True, "approval_id": approval_id, "status": "cancelled"}


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


@router.post("/admin/sovereign/weekly-digest")
def sovereign_weekly_digest_ep(
    authorization: str | None = Header(default=None),
    force: bool = Query(default=True),
):
    """Send (or preview-seed) the weekly async check-in digest now."""
    _require_sovereign_or_admin(authorization)
    return run_weekly_digest(force=force)


@router.post("/admin/sovereign/seed-operating-agreement")
def sovereign_seed_operating_agreement(authorization: str | None = Header(default=None)):
    """Write Ford's ship/check-in/job-budget rules into durable memory."""
    _require_sovereign_or_admin(authorization)
    with SessionLocal() as db:
        ensure_operating_memory(db)
        ensure_default_goals(db)
        db.commit()
        mem = memory_get_all(db, limit=40)
    return {
        "ok": True,
        "keys": [
            m for m in mem
            if m.get("key") in (
                "authority_ship", "checkin_cadence", "job_budget",
                "weekly_digest", "ford_operating_agreement",
                "demo_vs_real", "people_testers",
                "reality_file_doctrine", "mind_sandbox_doctrine",
            ) or (m.get("key") or "").startswith("authority")
            or (m.get("key") or "").startswith("demo")
            or (m.get("key") or "").startswith("people")
            or (m.get("key") or "").startswith("reality")
            or (m.get("key") or "").startswith("mind_sandbox")
        ],
        "max_jobs_per_day": MAX_JOBS_PER_DAY,  # 0 = unlimited
    }


# ── Reality file + Mind sandbox (Ford 2026-07-22) ──────────────────────────

@router.get("/admin/sovereign/reality")
def sovereign_reality_status(
    authorization: str | None = Header(default=None),
    tail: int = 30,
):
    """Cold hard truth status + recent CHANGELOG tail."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_reality import status, read_entries, load_for_wake
    return {
        "ok": True,
        "status": status(),
        "tail": read_entries(limit=max(1, min(int(tail or 30), 200))),
        "wake_preview_keys": list(load_for_wake().keys()),
    }


@router.post("/admin/sovereign/reality/seed")
def sovereign_reality_seed(
    authorization: str | None = Header(default=None),
    since: str = "2026-05-01",
    force: bool = False,
):
    """Bootstrap CHANGELOG.jsonl from git history of array-operator + solar-operator."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_reality import seed_from_git
    return seed_from_git(since=since, force=force)


@router.post("/admin/sovereign/reality/append")
def sovereign_reality_append(
    body: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """Manually append a reality line (Ford / operator)."""
    _require_sovereign_or_admin(authorization)
    body = body or {}
    from .energy_agent_sovereign_reality import append_entry
    files = body.get("files") or []
    if isinstance(files, str):
        files = [files]
    repos = body.get("repos") or body.get("repo") or []
    if isinstance(repos, str):
        repos = [repos]
    return append_entry(
        summary=str(body.get("summary") or body.get("title") or body.get("text") or "")[:500],
        source=str(body.get("source") or "ford")[:40],
        repos=list(repos)[:8],
        files=list(files)[:80],
        why=str(body.get("why") or "")[:600] or None,
        author=str(body.get("author") or "Ford")[:120],
        sha=body.get("sha"),
        job_id=body.get("job_id"),
    )


@router.get("/admin/sovereign/mind-sandbox")
def sovereign_mind_sandbox_status(authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_mind_sandbox import status
    with SessionLocal() as db:
        return {"ok": True, **status(db)}


@router.post("/admin/sovereign/mind-sandbox/start")
def sovereign_mind_sandbox_start(
    body: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """Open a free-run evaluation window (default 7 days)."""
    _require_sovereign_or_admin(authorization)
    body = body or {}
    from .energy_agent_sovereign_mind_sandbox import start_run
    with SessionLocal() as db:
        out = start_run(
            db,
            days=int(body.get("days") or 7),
            title=body.get("title"),
            goal=body.get("goal") or body.get("text"),
            free_run=body.get("free_run", True) is not False,
        )
        db.commit()
        return out


@router.post("/admin/sovereign/mind-sandbox/score")
def sovereign_mind_sandbox_score(
    body: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """Mid-window or final scorecard: Sovereign sandbox vs Ford commits."""
    _require_sovereign_or_admin(authorization)
    body = body or {}
    from .energy_agent_sovereign_mind_sandbox import score_active
    with SessionLocal() as db:
        return score_active(db, run_id=body.get("run_id"))


@router.post("/admin/sovereign/mind-sandbox/end")
def sovereign_mind_sandbox_end(
    body: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """Close the free-run window and write comparison.md + scorecard.json."""
    _require_sovereign_or_admin(authorization)
    body = body or {}
    from .energy_agent_sovereign_mind_sandbox import end_run
    with SessionLocal() as db:
        out = end_run(
            db,
            run_id=body.get("run_id"),
            score=body.get("score", True) is not False,
        )
        db.commit()
        return out


# ── Chamber L2 (false-real AO branch deploy — never prod) ──────────────────

@router.get("/admin/sovereign/chamber")
def sovereign_chamber_status(authorization: str | None = Header(default=None)):
    """Always-on chamber URL + last deploy meta (mission control outside the room)."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_chamber import get_chamber_status
    with SessionLocal() as db:
        return get_chamber_status(db)


@router.post("/admin/sovereign/chamber/deploy")
def sovereign_chamber_deploy(
    body: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """Deploy AO public/ (sandbox worktree if open, else baseline) to chamber branch.

    Never publishes arrayoperator.com. Safe REST branch deploy only.
    """
    _require_sovereign_or_admin(authorization)
    body = body or {}
    from .energy_agent_sovereign_chamber import deploy_chamber
    from .energy_agent_sovereign_mind_sandbox import get_active_run

    with SessionLocal() as db:
        run = get_active_run(db)
        run_id = body.get("run_id") or (run.get("id") if run else None)
        out = deploy_chamber(
            public_dir=body.get("public_dir"),
            run_id=run_id,
            job_id=body.get("job_id"),
            title=body.get("title") or "manual chamber deploy",
            db=db,
        )
        db.commit()
        return out


@router.get("/admin/sovereign/chamber/scorecard")
def sovereign_chamber_scorecard_get(
    authorization: str | None = Header(default=None),
    days: int = 7,
):
    """L4 prod vs chamber scorecard (live compute; does not force persist)."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_chamber_score import build_chamber_scorecard

    with SessionLocal() as db:
        card = build_chamber_scorecard(db, days=days)
        return {"ok": True, "scorecard": card}


@router.post("/admin/sovereign/chamber/score")
def sovereign_chamber_score_run(
    body: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """Generate + persist L4 scorecard (disk + memory + note)."""
    _require_sovereign_or_admin(authorization)
    body = body or {}
    from .energy_agent_sovereign_chamber_score import run_and_persist_scorecard

    with SessionLocal() as db:
        out = run_and_persist_scorecard(db, days=int(body.get("days") or 7))
        db.commit()
        return out


@router.post("/admin/sovereign/chamber/taste")
def sovereign_chamber_taste(
    body: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """Ford taste vote: preference=chamber|prod|tie|abstain."""
    _require_sovereign_or_admin(authorization)
    body = body or {}
    from .energy_agent_sovereign_chamber_score import set_taste_vote

    with SessionLocal() as db:
        out = set_taste_vote(
            db,
            preference=str(body.get("preference") or body.get("vote") or ""),
            note=body.get("note") or body.get("text"),
            voter=str(body.get("voter") or body.get("author") or "Ford"),
        )
        db.commit()
        return out


# ── Local Sovereign Portal (loopback UI — no AO account) ───────────────────

@router.get("/admin/sovereign/portal")
def sovereign_portal_dashboard(authorization: str | None = Header(default=None)):
    """One payload for the localhost Sovereign Portal: work, queues, reality, sandbox."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_reality import status as reality_status, read_entries
    from .energy_agent_sovereign_mind_sandbox import status as sandbox_status
    from .energy_agent_sovereign_chamber import get_chamber_status
    with SessionLocal() as db:
        state = world_get(db)
        jobs = [
            {
                "id": j.id,
                "kind": j.kind,
                "status": j.status,
                "title": j.title,
                "created_at": j.created_at.isoformat() + "Z" if j.created_at else None,
                "finished_at": j.finished_at.isoformat() + "Z" if j.finished_at else None,
                "error": (j.error or "")[:300] or None,
            }
            for j in db.execute(
                select(EaSovereignJob).order_by(EaSovereignJob.created_at.desc()).limit(40)
            ).scalars().all()
        ]
        notes = recent_notes(db, limit=20)
        mem = memory_get_all(db, limit=30)
        goals = [
            {
                "id": g.id,
                "title": g.title,
                "priority": g.priority,
                "status": g.status,
            }
            for g in db.execute(
                select(EaSovereignGoal)
                .where(EaSovereignGoal.status == "open")
                .order_by(EaSovereignGoal.priority.desc())
                .limit(20)
            ).scalars().all()
        ]
        try:
            from .energy_agent_sovereign_ops import (
                ops_summary, list_features, list_utilities, list_escalations,
            )
            ops = {
                "summary": ops_summary(db),
                "features_building": list_features(db, status="building", limit=15),
                "features_reviewed": list_features(db, status="reviewed", limit=15),
                "utilities": list_utilities(db, status="all", limit=15),
                "escalations": list_escalations(db, status="needs_ford", limit=15),
            }
        except Exception as e:  # noqa: BLE001
            ops = {"error": str(e)[:200]}
        desk_err = None
        try:
            from .energy_agent_sovereign_desk import history as desk_history_fn, ensure_tables as desk_tables
            desk_tables()
            desk_msgs = desk_history_fn(db, limit=40)
        except Exception as e:  # noqa: BLE001
            desk_msgs = []
            desk_err = str(e)[:200]
        try:
            chamber = get_chamber_status(db)
        except Exception as e:  # noqa: BLE001
            chamber = {"ok": False, "error": str(e)[:200]}
        try:
            from .energy_agent_sovereign_chamber_score import latest_scorecard_summary

            chamber_score = latest_scorecard_summary(db)
        except Exception as e:  # noqa: BLE001
            chamber_score = {"ok": False, "error": str(e)[:200]}
        sandbox = sandbox_status(db)
    return {
        "ok": True,
        "channel": "admin_portal",
        "note": "Localhost portal — not Array Operator account. Admin key only.",
        "world": {
            "mode": state.get("mode"),
            "last_tick_at": state.get("last_tick_at"),
            "last_mood": state.get("last_mood"),
            "revision": state.get("revision"),
            "last_monologue_excerpt": (state.get("last_monologue_excerpt") or "")[:500],
        },
        "jobs": jobs,
        "goals": goals,
        "notes": notes,
        "memory": mem,
        "ops": ops,
        "desk_messages": desk_msgs,
        "desk_error": desk_err,
        "reality": {
            **reality_status(),
            "tail": read_entries(limit=50),
        },
        "mind_sandbox": sandbox,
        "chamber": chamber,
        "chamber_score": chamber_score,
    }


@router.get("/admin/sovereign/desk/history")
def admin_desk_history(
    authorization: str | None = Header(default=None),
    limit: int = 80,
):
    """Desk transcript via admin key — no Array Operator login."""
    _require_sovereign_or_admin(authorization)
    from .energy_agent_sovereign_desk import history as desk_history_fn, ensure_tables as desk_tables
    desk_tables()
    with SessionLocal() as db:
        return {
            "ok": True,
            "via": "admin",
            "messages": desk_history_fn(db, limit=min(max(int(limit or 80), 1), 200)),
        }


@router.post("/admin/sovereign/desk/chat")
def admin_desk_chat(
    body: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """Send a desk message as Ford via admin key (localhost portal).

    Always uses the durable enqueue path (worker answers) — never requires
    an Array Operator session cookie.
    """
    _require_sovereign_or_admin(authorization)
    body = body or {}
    msg = (body.get("message") or body.get("text") or "").strip()
    if not msg and not body.get("poll_only"):
        raise HTTPException(400, "message required")
    crid = (body.get("client_request_id") or "").strip()[:80] or None
    tenant_id = (
        (body.get("tenant_id") or "").strip()
        or (os.getenv("SOVEREIGN_DESK_TENANT_ID") or "ten_aaad29f08dbe9943").strip()
    )
    from .energy_agent_sovereign_desk import (
        enqueue_desk_message,
        lookup_turn_by_client_request_id,
        ensure_tables as desk_tables,
        _format_turn_response,
    )
    desk_tables()
    if crid:
        with SessionLocal() as db:
            hit = lookup_turn_by_client_request_id(db, crid)
            if hit and hit.get("complete"):
                return _format_turn_response(
                    ford=hit["ford"], sov=hit["sov"], pending=False,
                    client_request_id=crid, extra={"idempotent": True, "via": "admin"},
                )
            if hit and not hit.get("complete"):
                return _format_turn_response(
                    ford=hit["ford"], sov=None, pending=True,
                    client_request_id=crid,
                    extra={"idempotent": True, "via": "admin", "offloaded": True},
                )
        if body.get("poll_only"):
            return {
                "ok": True,
                "pending": True,
                "poll": True,
                "client_request_id": crid,
                "via": "admin",
            }

    if body.get("poll_only"):
        raise HTTPException(400, "poll_only requires client_request_id")

    out = enqueue_desk_message(
        tenant_id=tenant_id,
        message=msg,
        attachment_ids=list(body.get("attachment_ids") or [])[:12],
        client_request_id=crid,
    )
    if isinstance(out, dict):
        out["via"] = "admin"
        out["portal"] = True
        # Tag ford bubble for high-level admin desk framing on drain
        try:
            ford_id = out.get("ford_message_id") or (
                (out.get("ford_message") or {}).get("id")
            )
            if ford_id:
                with SessionLocal() as db:
                    from .energy_agent_sovereign_desk import EaSovereignDeskMessage
                    row = db.get(EaSovereignDeskMessage, ford_id)
                    if row:
                        try:
                            meta = json.loads(row.meta_json or "{}")
                        except Exception:
                            meta = {}
                        meta["via"] = "admin_portal"
                        meta["channel"] = "desk"
                        meta["admin_high_level"] = True
                        row.meta_json = json.dumps(meta, default=str)[:4000]
                        db.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("admin desk meta tag skipped: %s", e)
    return out


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
        # Manual admin drain: same env as scheduler, default 2; allow up to 8.
        limit = int(os.getenv("SOVEREIGN_JOB_DRAIN_LIMIT", "2") or 2)
        out = drain_jobs(db, limit=max(1, min(limit, 8)))
        db.commit()
        return out
