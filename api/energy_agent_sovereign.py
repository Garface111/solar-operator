"""Energy Agent — Sovereign Mind (product executive) — DARK BY DEFAULT.

Architecture: docs/plans/2026-07-15-energy-agent-sovereign-mind.md

This module is the future home of the product-scope mind that:
  • owns Array Operator (health, expansion, UX, repair)
  • may speak into any tenant Energy Agent session
  • may execute gated control-plane actions with full audit

NOT ENABLED. All public entry points check SOVEREIGN_ENABLED and no-op.
Do not register a scheduler job until Phase F is explicitly approved and
SOVEREIGN_ENABLED is set in Railway with Ford's knowledge.

Tenant continuous cognition remains api/energy_agent_mind.py (Phases A–E).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("energy_agent.sovereign")
router = APIRouter()

# ── Kill switches (env) ─────────────────────────────────────────────────────
# Architecture §7: default deny. Nothing executive runs without explicit flags.
def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def sovereign_enabled() -> bool:
    """Master switch. Off → entire control plane is inert."""
    return _flag("SOVEREIGN_ENABLED", "0")


def sovereign_act_enabled() -> bool:
    return sovereign_enabled() and _flag("SOVEREIGN_ACT_ENABLED", "0")


def sovereign_speak_enabled() -> bool:
    return sovereign_enabled() and _flag("SOVEREIGN_SPEAK_ENABLED", "0")


# Capability registry (ids from the architecture doc). Values are documentation
# for operators; enforcement is env + future DB matrix.
CAPABILITIES: dict[str, dict[str, Any]] = {
    # Sense
    "sense.product_health": {"tier": "sense", "default": False},
    "sense.fleet_global": {"tier": "sense", "default": False},
    "sense.queues": {"tier": "sense", "default": False},
    "sense.ux_friction": {"tier": "sense", "default": False},
    "sense.tenant_sessions": {"tier": "sense", "default": False},
    "sense.billing": {"tier": "sense", "default": False},
    "sense.code_drift": {"tier": "sense", "default": False},
    # Speak
    "speak.session_inject": {"tier": "speak", "default": False},
    "speak.session_broadcast": {"tier": "speak", "default": False},
    "speak.email_owner": {"tier": "speak", "default": False},
    "speak.email_ford": {"tier": "speak", "default": False},
    "speak.chat_reply_as_agent": {"tier": "speak", "default": False},
    # Act
    "act.soft_stage": {"tier": "T0", "default": False},
    "act.tenant_assist": {"tier": "T1", "default": False},
    "act.product_queue": {"tier": "T2", "default": False},
    "act.code_hire": {"tier": "T3", "default": False},
    "act.deploy": {"tier": "T4", "default": False},
    "act.money_identity": {"tier": "T5", "default": False, "autonomous": False},
    # Expand
    "expand.utility_research": {"tier": "expand", "default": False},
    "expand.vendor_coverage": {"tier": "expand", "default": False},
    "expand.ux_roadmap": {"tier": "expand", "default": False},
    "expand.docs": {"tier": "expand", "default": False},
}

# T5 is never autonomous even when the master switch is on.
NEVER_AUTONOMOUS = frozenset({"act.money_identity", "act.deploy"})


def capability_allowed(cap_id: str) -> bool:
    """Gate a single capability. Phase F: all false unless flags + allowlist."""
    if not sovereign_enabled():
        return False
    if cap_id not in CAPABILITIES:
        return False
    if cap_id in NEVER_AUTONOMOUS and not _flag("SOVEREIGN_ARM_T4_T5", "0"):
        # Deploy/money require a second arm token flag even later.
        return False
    if cap_id.startswith("speak.") and not sovereign_speak_enabled():
        return False
    if cap_id.startswith("act.") and not sovereign_act_enabled():
        return False
    # Optional comma allowlist: SOVEREIGN_CAPABILITIES=sense.queues,speak.session_inject
    raw = (os.getenv("SOVEREIGN_CAPABILITIES") or "").strip()
    if not raw:
        # No allowlist → sense-only when enabled in future Phase G; Phase F: deny act/speak
        # already handled. For observe-only future, sense.* may pass when enabled.
        if cap_id.startswith("sense."):
            return _flag("SOVEREIGN_SENSE_ENABLED", "0")
        return False
    allowed = {c.strip() for c in raw.split(",") if c.strip()}
    return cap_id in allowed or "*" in allowed


def _now() -> datetime:
    return datetime.utcnow()


def _default_product_world() -> dict[str, Any]:
    return {
        "revision": 0,
        "updated_at": None,
        "health": {},
        "queues": {},
        "fleet_global": {},
        "ux": {},
        "goals": [],
        "last_tick_at": None,
        "mode": "dark",
    }


# ── Auth (admin / service key only — never tenant session alone for act) ────
def _require_sovereign_or_admin(authorization: str | None) -> None:
    """Ford admin key or SOVEREIGN_SERVICE_KEY. Tenant sessions are rejected."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sovereign plane requires admin/service bearer")
    token = authorization.split(" ", 1)[1].strip()
    admin = (os.getenv("ADMIN_API_KEY") or "").strip()
    svc = (os.getenv("SOVEREIGN_SERVICE_KEY") or "").strip()
    if not token:
        raise HTTPException(401, "Empty bearer")
    if admin and token == admin:
        return
    if svc and token == svc:
        return
    raise HTTPException(403, "Not authorized for sovereign plane")


# ── Core loop (no-op when dark) ─────────────────────────────────────────────
def sovereign_tick(*, reason: str = "manual") -> dict[str, Any]:
    """One observe → decide → (maybe) act cycle.

    Phase F: always returns mode=dark and does not write product state tables
    (tables land in Phase G). Safe to call from tests and future scheduler.
    """
    tick_id = "sov_" + uuid.uuid4().hex[:12]
    if not sovereign_enabled():
        log.debug("sovereign_tick skipped (SOVEREIGN_ENABLED off) reason=%s", reason)
        return {
            "ok": True,
            "tick_id": tick_id,
            "mode": "dark",
            "reason": reason,
            "enabled": False,
            "decisions": [],
            "note": "Sovereign Mind is architected but not enabled.",
        }

    # Phase G+ will: refresh digests, score goals, enqueue jobs, audit actions.
    # Until then, enabled-but-unbuilt still refuses to act.
    decisions: list[dict] = []
    world = _default_product_world()
    world["mode"] = "enabled_stub"
    world["last_tick_at"] = _now().isoformat() + "Z"
    world["updated_at"] = world["last_tick_at"]

    log.info(
        "sovereign_tick stub reason=%s sense=%s speak=%s act=%s",
        reason,
        capability_allowed("sense.queues"),
        sovereign_speak_enabled(),
        sovereign_act_enabled(),
    )
    return {
        "ok": True,
        "tick_id": tick_id,
        "mode": "enabled_stub",
        "reason": reason,
        "enabled": True,
        "capabilities_sample": {
            k: capability_allowed(k)
            for k in (
                "sense.queues",
                "speak.session_inject",
                "act.soft_stage",
                "act.money_identity",
            )
        },
        "decisions": decisions,
        "world": world,
        "note": "Runtime skeleton only — implement Phase G observe before acts.",
    }


def plan_inject(
    *,
    tenant_ids: list[str],
    speak: str,
    importance: int = 70,
) -> dict[str, Any]:
    """Stage a session inject. Phase H implements delivery via EaEvent."""
    if not capability_allowed("speak.session_inject"):
        return {
            "ok": False,
            "denied": True,
            "denied_reason": "speak.session_inject not allowed (flag/allowlist)",
            "tenant_ids": tenant_ids,
        }
    # Future: write ea_sovereign_message_outbox + EaEvent sovereign_interrupt
    return {
        "ok": False,
        "denied": True,
        "denied_reason": "Phase H not built — message bus not wired",
        "planned_speak": (speak or "")[:500],
        "importance": importance,
        "tenant_ids": tenant_ids,
    }


def plan_action(capability: str, payload: dict | None = None) -> dict[str, Any]:
    """Stage an executive action. Returns deny until Phase I+ and flags allow."""
    payload = payload or {}
    if capability in NEVER_AUTONOMOUS:
        return {
            "ok": False,
            "denied": True,
            "denied_reason": f"{capability} is never fully autonomous — Ford dual-control required",
            "tier": CAPABILITIES.get(capability, {}).get("tier"),
            "payload_keys": list(payload.keys()),
        }
    if not capability_allowed(capability):
        return {
            "ok": False,
            "denied": True,
            "denied_reason": f"{capability} not allowed",
            "tier": CAPABILITIES.get(capability, {}).get("tier"),
        }
    return {
        "ok": False,
        "denied": True,
        "denied_reason": "Phase I+ worker not implemented",
        "capability": capability,
    }


# ── Admin HTTP (always registered; handlers refuse when dark / unauthorized) ─
class WakeIn(BaseModel):
    reason: str = Field(default="admin", max_length=200)


class InjectIn(BaseModel):
    tenant_ids: list[str] = Field(default_factory=list)
    speak: str = Field(default="", max_length=2000)
    importance: int = Field(default=70, ge=0, le=100)


class ActIn(BaseModel):
    capability: str
    payload: dict = Field(default_factory=dict)


@router.get("/admin/sovereign/state")
def sovereign_state(authorization: str | None = Header(default=None)):
    """Architecture status for Ford — safe to call anytime with admin key."""
    _require_sovereign_or_admin(authorization)
    return {
        "architecture": "docs/plans/2026-07-15-energy-agent-sovereign-mind.md",
        "module": "api/energy_agent_sovereign.py",
        "mode": "dark" if not sovereign_enabled() else "enabled_stub",
        "enabled": sovereign_enabled(),
        "sense_enabled": _flag("SOVEREIGN_SENSE_ENABLED", "0"),
        "speak_enabled": sovereign_speak_enabled(),
        "act_enabled": sovereign_act_enabled(),
        "capabilities": {
            cid: {
                **meta,
                "allowed_now": capability_allowed(cid),
            }
            for cid, meta in CAPABILITIES.items()
        },
        "tenant_mind": "api/energy_agent_mind.py (live Phases A–E)",
        "note": "Sovereign Mind is designed for the future; not operating on customers.",
    }


@router.post("/admin/sovereign/wake")
def sovereign_wake(body: WakeIn, authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    return sovereign_tick(reason=body.reason or "wake")


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
    )


@router.post("/admin/sovereign/act")
def sovereign_act(body: ActIn, authorization: str | None = Header(default=None)):
    _require_sovereign_or_admin(authorization)
    return plan_action(body.capability, body.payload)
