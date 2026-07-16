"""Sovereign three-layer mind — subconscious + event bus + cortex handoff.

Architecture (Ford 2026-07-15):
  Subconscious — cheap / high-frequency: monologue + heat + needs_cortex (notes only)
  Cortex       — expensive / sparse: existing sovereign_tick (Grok→Claude + acts)
  Reflexes     — wake_sovereign(reason, payload) on every product touch

The subconscious is a filter and memory writer — never an actor.
Cortex is the only layer that may desk / code-hire / triage hard.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import SessionLocal
from .models import Base

log = logging.getLogger("energy_agent.sovereign.subconscious")

# ── Coalescing locks (process-local; Railway single web dyno is fine) ────────
_sub_lock = threading.Lock()
_cortex_lock = threading.Lock()
_last_sub_at: datetime | None = None
_last_cortex_at: datetime | None = None
_pending_cortex_reason: str | None = None


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _now() -> datetime:
    return datetime.utcnow()


def _id(prefix: str = "sev") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def subconscious_enabled() -> bool:
    """Master: follows SOVEREIGN_ENABLED; kill loop alone with SOVEREIGN_SUBCONSCIOUS=0."""
    from .energy_agent_sovereign import sovereign_enabled
    if not sovereign_enabled():
        return False
    return _flag("SOVEREIGN_SUBCONSCIOUS", "1")


def subconscious_llm_enabled() -> bool:
    """Grok monologue for subconscious. Default ON (Ford 2026-07-15: real mind).

    Kill: SOVEREIGN_SUBCONSCIOUS_LLM=0 (falls back to deterministic rule monologue).
    """
    return subconscious_enabled() and _flag("SOVEREIGN_SUBCONSCIOUS_LLM", "1")


def sub_interval_sec() -> int:
    return max(15, int(os.getenv("SOVEREIGN_SUBCONSCIOUS_INTERVAL_SEC", "45") or 45))


def cortex_heat_threshold() -> int:
    return max(1, min(100, int(os.getenv("SOVEREIGN_CORTEX_HEAT_THRESHOLD", "70") or 70)))


def cortex_min_interval_sec() -> int:
    """Coalesce wake-driven cortex so we don't burn Grok every event burst."""
    return max(30, int(os.getenv("SOVEREIGN_CORTEX_MIN_INTERVAL_SEC", "90") or 90))


def sub_min_interval_sec() -> int:
    return max(10, int(os.getenv("SOVEREIGN_SUB_MIN_INTERVAL_SEC", "20") or 20))


# ── Event stream ────────────────────────────────────────────────────────────
class EaSovereignEvent(Base):
    """Append-only product touch stream for reflexes + subconscious tape."""
    __tablename__ = "ea_sovereign_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    reason: Mapped[str] = mapped_column(String(80), default="", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    heat: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(40), default="product")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def ensure_event_tables() -> None:
    try:
        from .db import engine
        Base.metadata.create_all(bind=engine, tables=[EaSovereignEvent.__table__])
    except Exception as e:  # noqa: BLE001
        log.debug("ensure_event_tables: %s", e)


# Base heat by reason (reflex scorer — no LLM)
_REASON_HEAT: dict[str, int] = {
    "desk_message": 95,
    "admin_think": 90,
    "admin_tick": 80,
    "admin_wake": 85,
    "ford_escalation": 90,
    "needs_ford": 90,
    "utility_request": 72,
    "utility_status": 55,
    "feature_suggestion": 68,
    "feature_status": 50,
    "job_done": 58,
    "job_failed": 78,
    "deploy_finished": 55,
    "capture_spike": 62,
    "owner_signup": 50,
    "sentry_critical": 88,
    "scheduler": 0,
    "subconscious": 0,
    "wake": 40,
}


def score_event_heat(reason: str, payload: dict | None = None) -> int:
    """Rule heat for one event. Caps at 100. Payload may raise heat."""
    r = (reason or "wake").strip().lower()
    base = _REASON_HEAT.get(r, 40)
    # prefix matches (wake:utility_request → utility_request)
    if r.startswith("wake:"):
        base = max(base, _REASON_HEAT.get(r[5:], 45))
    p = payload or {}
    # Escalations always hot
    if p.get("status") == "needs_ford" or p.get("needs_ford"):
        base = max(base, 90)
    if p.get("failed") or p.get("error"):
        base = max(base, 75)
    if p.get("count") and int(p.get("count") or 0) >= 5:
        base = min(100, base + 10)
    return max(0, min(100, int(base)))


def score_digest_heat(digests: dict) -> int:
    """Background heat from live queue/fleet counts (subconscious ambient)."""
    q = digests.get("queues") or {}
    heat = 0
    # Hot queues
    heat += min(40, int(q.get("utility_new") or 0) * 12)
    heat += min(25, int(q.get("utility_researching") or 0) * 5)
    heat += min(35, int(q.get("feature_reviewed") or 0) * 4)
    heat += min(25, int(q.get("feature_new") or 0) * 8)
    heat += min(40, int(q.get("escalation_needs_ford") or 0) * 20)
    heat += min(20, int(q.get("sovereign_jobs_queued") or 0) * 6)
    # Failed jobs not in digests — leave to events
    ux = digests.get("ux") or {}
    if int(ux.get("tasks_propose_ui_14d") or 0) >= 5:
        heat = min(100, heat + 15)
    return max(0, min(100, heat))


def append_event(
    db,
    reason: str,
    payload: dict | None = None,
    *,
    source: str = "product",
    heat: int | None = None,
) -> EaSovereignEvent:
    ensure_event_tables()
    h = heat if heat is not None else score_event_heat(reason, payload)
    row = EaSovereignEvent(
        id=_id("sev"),
        reason=(reason or "wake")[:80],
        payload_json=json.dumps(payload or {}, default=str)[:4000],
        heat=int(h),
        source=(source or "product")[:40],
    )
    db.add(row)
    db.flush()
    return row


def recent_events(db, *, limit: int = 12, unconsumed_only: bool = False) -> list[dict]:
    ensure_event_tables()
    q = select(EaSovereignEvent).order_by(EaSovereignEvent.created_at.desc()).limit(limit)
    if unconsumed_only:
        q = (
            select(EaSovereignEvent)
            .where(EaSovereignEvent.consumed_at.is_(None))
            .order_by(EaSovereignEvent.created_at.desc())
            .limit(limit)
        )
    rows = db.execute(q).scalars().all()
    out = []
    for r in rows:
        try:
            payload = json.loads(r.payload_json or "{}")
        except Exception:
            payload = {}
        out.append({
            "id": r.id,
            "reason": r.reason,
            "heat": r.heat,
            "payload": payload,
            "source": r.source,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "consumed": r.consumed_at is not None,
        })
    return out


def mark_events_consumed(db, event_ids: list[str]) -> int:
    n = 0
    now = _now()
    for eid in event_ids:
        row = db.get(EaSovereignEvent, eid)
        if row and row.consumed_at is None:
            row.consumed_at = now
            n += 1
    if n:
        db.flush()
    return n


def rule_monologue(
    digests: dict,
    events: list[dict],
    *,
    last_monologue: str = "",
    heat: int = 0,
) -> str:
    """Tiny free monologue from counts + recent events. No LLM. No hallucination."""
    q = digests.get("queues") or {}
    fg = digests.get("fleet_global") or {}
    parts = [
        f"heat={heat}",
        f"utility new={q.get('utility_new', 0)} researching={q.get('utility_researching', 0)} "
        f"reviewed={q.get('utility_reviewed', 0)}",
        f"feature new={q.get('feature_new', 0)} reviewed={q.get('feature_reviewed', 0)} "
        f"building={q.get('feature_building', 0)}",
        f"escalation needs_ford={q.get('escalation_needs_ford', 0)} open={q.get('escalation_open', 0)}",
        f"jobs_queued={q.get('sovereign_jobs_queued', 0)}",
        f"ao_tenants={fg.get('tenants_ao', '?')} arrays={fg.get('arrays_total', '?')}",
    ]
    if events:
        ebits = []
        for e in events[:5]:
            ebits.append(f"{e.get('reason')}(h{e.get('heat')})")
        parts.append("recent_touches: " + ", ".join(ebits))
    # Continuity with previous monologue excerpt
    if last_monologue:
        prev = last_monologue.strip().replace("\n", " ")[:160]
        parts.append(f"still_carrying: {prev}")
    # Hot call
    hot = []
    if int(q.get("escalation_needs_ford") or 0) > 0:
        hot.append("escalations need Ford or close-path")
    if int(q.get("utility_new") or 0) > 0:
        hot.append("new utilities waiting")
    if int(q.get("feature_reviewed") or 0) >= 3:
        hot.append("reviewed features ready to ship/build")
    if int(q.get("sovereign_jobs_queued") or 0) > 0:
        hot.append("code jobs waiting")
    if hot:
        parts.append("pressure: " + "; ".join(hot))
    else:
        parts.append("pressure: quiet — hold agenda, no fake urgency")
    return " · ".join(parts)[:1800]


def subconscious_model() -> str:
    """Grok model for subconscious monologue.

    Default: cheap non-reasoning (Ford 2026-07-15 — save weekly Grok quota).
    Cortex stays on SOVEREIGN_GROK_MODEL / grok-4.5.
    """
    return (
        (os.getenv("SOVEREIGN_SUBCONSCIOUS_MODEL") or "").strip()
        or "grok-4-1-fast-non-reasoning"
    )


def _cheap_llm_monologue(
    digests: dict,
    events: list[dict],
    last_monologue: str,
    heat: int,
) -> dict[str, Any]:
    """Grok subconscious: monologue + needs_cortex bit. Never hard actions.

    # SESSION BOUNDARY: no LLM inside open session
    Pure HTTP — callers must not hold SessionLocal across this call.
    """
    if not subconscious_llm_enabled():
        return {"ok": False, "skipped": True}
    model = subconscious_model()
    try:
        from .energy_agent_sovereign_brain import XAI_API_KEY, XAI_BASE, _http_json, _extract_json
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}

    # Prefer Build OIDC / refreshed bearer (same path as cortex Grok)
    bearer = None
    try:
        from .xai_auth import get_xai_bearer
        bearer = get_xai_bearer()
    except Exception as e:  # noqa: BLE001
        if XAI_API_KEY:
            bearer = XAI_API_KEY
        else:
            return {"ok": False, "error": f"no_xai:{e}"[:200]}
    if not bearer:
        return {"ok": False, "error": "no_xai"}

    user = {
        "role": "subconscious",
        "instruction": (
            "You are Sovereign's subconscious — continuous inner monologue, NOT the executive actor. "
            "Rephrase digests into a short rolling monologue (partner tone, no fake urgency). "
            "Raise/lower heat honestly 0–100. needs_cortex=true only if expensive executive "
            "thought or hard action is actually warranted. Never invent customer crises from demos. "
            "Never propose deploy/email/code yourself. Output pure JSON only."
        ),
        "heat_hint": heat,
        "digests_queues": digests.get("queues"),
        "digests_fleet": digests.get("fleet_global"),
        "recent_events": events[:6],
        "last_monologue": (last_monologue or "")[:400],
        "schema": {
            "monologue": "one short paragraph, factual, continuous thought",
            "heat": 0,
            "needs_cortex": False,
            "why": "one line",
            "memory_writes": [{"key": "sub_focus", "value": "optional tiny note"}],
        },
    }
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the SUBCONSCIOUS of Sovereign (Array Operator product mind). "
                    "JSON only. No actions. No owner speech. No fake urgency. "
                    "Tiny monologue — think continuously, don't act."
                ),
            },
            {"role": "user", "content": json.dumps(user, default=str)[:6000]},
        ],
        "temperature": 0.35,
        "max_tokens": 500,
    }
    try:
        out = _http_json(
            f"{XAI_BASE}/chat/completions",
            {
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
            },
            body,
            timeout=45,
        )
        content = (((out.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        parsed = _extract_json(content)
        parsed["ok"] = True
        parsed["provider"] = "grok_subconscious"
        parsed["model"] = model
        return parsed
    except Exception as e:  # noqa: BLE001
        log.warning("subconscious grok failed: %s", e)
        return {"ok": False, "error": str(e)[:300]}


def _memory_get(db, key: str) -> str | None:
    from .energy_agent_sovereign import EaSovereignMemory
    row = db.get(EaSovereignMemory, key)
    return row.value if row else None


def _last_cortex_age_sec(db) -> float | None:
    """Seconds since last cortex tick (memory or world)."""
    raw = _memory_get(db, "last_cortex_at") or _memory_get(db, "last_tick")
    if not raw:
        return None
    try:
        if raw.strip().startswith("{"):
            data = json.loads(raw)
            iso = data.get("at") or data.get("last_cortex_at")
        else:
            iso = raw.strip()
        if not iso:
            return None
        iso = iso.replace("Z", "")
        dt = datetime.fromisoformat(iso)
        return max(0.0, (_now() - dt).total_seconds())
    except Exception:
        return None


def decide_needs_cortex(
    *,
    heat: int,
    reason: str,
    force: bool = False,
    last_cortex_age_sec: float | None = None,
    digests: dict | None = None,
    unconsumed_events: int = 0,
) -> tuple[bool, str]:
    """Cortex handoff bit.

    Event wakes + hard reasons escalate. Ambient scheduler monologue does NOT
    re-fire expensive cortex every cycle just because feature_reviewed stays high
    forever — the 5m cortex backstop owns steady-state queues.
    """
    if force:
        return True, "forced"
    r = (reason or "").lower()
    thr = cortex_heat_threshold()
    q = (digests or {}).get("queues") or {}

    # Hard human / failure wakes
    hard = (
        r in (
            "desk_message", "admin_think", "admin_tick", "admin_wake",
            "needs_ford", "ford_escalation", "job_failed", "sentry_critical",
        )
        or r.startswith("desk")
        or "sentry" in r
    )
    # Fresh product touches (not pure scheduler ambient)
    event_wake = r in (
        "utility_request", "feature_suggestion", "job_done", "job_failed",
        "deploy_finished", "capture_spike", "owner_signup", "wake",
    ) or r.startswith("wake:")

    # Absolute product emergencies still break through ambient
    emergency = (
        int(q.get("escalation_needs_ford") or 0) > 0
        or int(q.get("utility_new") or 0) > 0
    )

    # Pure scheduler / pre_cortex: only escalate on emergency or brand-new events
    if r in ("scheduler", "subconscious", "pre_cortex", "admin_subconscious"):
        if emergency and heat >= thr:
            pass  # fall through to coalesce check
        elif unconsumed_events > 0 and heat >= thr:
            pass
        else:
            return False, f"ambient heat={heat} (5m backstop owns steady queues)"

    if heat >= thr or hard or event_wake:
        # Cooldown: unless super-hot (>=90) or hard desk/admin, respect min interval
        if last_cortex_age_sec is not None and last_cortex_age_sec < cortex_min_interval_sec():
            if heat < 90 and not hard:
                return False, f"coalesce heat={heat} age={int(last_cortex_age_sec)}s"
        if hard:
            return True, f"hard_wake:{r}"
        if event_wake:
            return True, f"event_wake:{r} heat={heat}"
        return True, f"heat={heat}>={thr}"
    return False, f"cool heat={heat}<{thr}"


def subconscious_tick(
    *,
    reason: str = "scheduler",
    payload: dict | None = None,
    force: bool = False,
    skip_llm: bool = False,
) -> dict[str, Any]:
    """One cheap observe → monologue → heat → notes/memory. Never acts hard."""
    global _last_sub_at
    tick_id = _id("sub")
    if not subconscious_enabled():
        return {
            "ok": True,
            "tick_id": tick_id,
            "mode": "dark",
            "enabled": False,
            "reason": reason,
        }

    # Process-local throttle
    with _sub_lock:
        if not force and _last_sub_at is not None:
            age = (_now() - _last_sub_at).total_seconds()
            if age < sub_min_interval_sec():
                return {
                    "ok": True,
                    "tick_id": tick_id,
                    "mode": "throttled",
                    "reason": reason,
                    "age_sec": age,
                    "min_sec": sub_min_interval_sec(),
                }
        _last_sub_at = _now()

    from .energy_agent_sovereign import (
        observe_product,
        write_note,
        memory_set,
        world_get,
        world_save,
        sovereign_sense_enabled,
    )

    # ── Phase 1: short DB read ─────────────────────────────────────────────
    digests: dict = {}
    events: list[dict] = []
    unconsumed: list[dict] = []
    last_mono = ""
    age: float | None = None
    digest_heat = 0
    event_heat = 0
    heat = 0

    try:
        with SessionLocal() as db:
            ensure_event_tables()
            digests = observe_product(db) if sovereign_sense_enabled() else {}
            events = recent_events(db, limit=10)
            unconsumed = [e for e in events if not e.get("consumed")]

            digest_heat = score_digest_heat(digests)
            event_heat = max([e.get("heat") or 0 for e in unconsumed] + [0])
            if payload:
                event_heat = max(event_heat, score_event_heat(reason, payload))
            heat = max(digest_heat, event_heat)

            last_mono = _memory_get(db, "subconscious_monologue") or ""
            age = _last_cortex_age_sec(db)
            try:
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
        # SESSION BOUNDARY: no LLM inside open session
    except Exception as e:  # noqa: BLE001
        log.exception("subconscious_tick read phase failed")
        return {
            "ok": False,
            "tick_id": tick_id,
            "mode": "error",
            "reason": reason,
            "error": f"read_phase: {e}"[:400],
        }

    mono = rule_monologue(
        digests, events, last_monologue=last_mono, heat=heat,
    )
    provider = "rules"
    model = None
    llm_needs: bool | None = None
    why = ""
    llm: dict[str, Any] = {}

    # ── Phase 2: cheap LLM monologue — connection pool free ────────────────
    # SESSION BOUNDARY: no LLM inside open session
    if not skip_llm and subconscious_llm_enabled():
        llm = _cheap_llm_monologue(digests, events, last_mono, heat)
        if llm.get("ok") and (llm.get("monologue") or "").strip():
            mono = str(llm["monologue"]).strip()[:1800]
            provider = llm.get("provider") or "subconscious_llm"
            model = llm.get("model")
            if "heat" in llm:
                try:
                    # Blend: never trust LLM heat alone above rule heat+15
                    llm_h = int(llm["heat"])
                    heat = max(heat, min(llm_h, heat + 15, 100))
                except Exception:
                    pass
            if "needs_cortex" in llm:
                llm_needs = bool(llm["needs_cortex"])
            why = str(llm.get("why") or "")[:240]

    needs, needs_why = decide_needs_cortex(
        heat=heat,
        reason=reason,
        force=False,
        last_cortex_age_sec=age,
        digests=digests,
        unconsumed_events=len(unconsumed),
    )
    if llm_needs is True and heat >= max(40, cortex_heat_threshold() - 20):
        needs = True
        needs_why = (needs_why + "; llm_flag").strip("; ")
    elif llm_needs is False and heat < cortex_heat_threshold() and reason == "scheduler":
        needs = False
        needs_why = "llm_cool+" + needs_why

    if why:
        needs_why = f"{needs_why} | {why}"[:300]

    # ── Phase 3: short DB write ────────────────────────────────────────────
    with SessionLocal() as db:
        try:
            if llm.get("ok"):
                for mw in llm.get("memory_writes") or []:
                    if isinstance(mw, dict) and mw.get("key"):
                        k = str(mw["key"])[:80]
                        if k.startswith("sub_") or k in (
                            "pressure_point", "ambient_note", "subconscious_focus",
                        ):
                            memory_set(
                                db, k, str(mw.get("value") or "")[:500],
                                source="subconscious",
                            )

            write_note(
                db,
                kind="subconscious",
                title=f"sub · {reason}"[:240],
                body=mono[:8000],
                provider=provider,
                tick_id=tick_id,
                meta={
                    "heat": heat,
                    "needs_cortex": needs,
                    "why": needs_why,
                    "digest_heat": digest_heat,
                    "event_heat": event_heat,
                    "n_events": len(events),
                },
            )
            memory_set(db, "subconscious_monologue", mono[:2000], source="subconscious")
            memory_set(db, "heat_score", str(heat), source="subconscious")
            memory_set(
                db, "needs_cortex",
                json.dumps({
                    "value": needs,
                    "why": needs_why,
                    "heat": heat,
                    "at": _now().isoformat() + "Z",
                    "reason": reason,
                }),
                source="subconscious",
            )
            memory_set(
                db, "last_subconscious",
                json.dumps({
                    "at": _now().isoformat() + "Z",
                    "reason": reason,
                    "heat": heat,
                    "needs_cortex": needs,
                    "provider": provider,
                    "tick_id": tick_id,
                }, default=str),
                source="subconscious",
            )

            mark_events_consumed(db, [e["id"] for e in unconsumed if e.get("id")])

            try:
                state = world_get(db)
                state["heat"] = heat
                state["needs_cortex"] = needs
                state["last_subconscious_at"] = _now().isoformat() + "Z"
                state["last_subconscious_excerpt"] = mono[:300]
                world_save(db, state)
            except Exception as e:  # noqa: BLE001
                log.debug("world patch skip: %s", e)

            db.commit()
            return {
                "ok": True,
                "tick_id": tick_id,
                "mode": "live",
                "reason": reason,
                "heat": heat,
                "needs_cortex": needs,
                "why": needs_why,
                "monologue_excerpt": mono[:400],
                "provider": provider,
                "model": model,
                "digests": {"queues": digests.get("queues")},
            }
        except Exception as e:  # noqa: BLE001
            log.exception("subconscious_tick write phase failed")
            try:
                db.rollback()
            except Exception:
                pass
            return {
                "ok": False,
                "tick_id": tick_id,
                "mode": "error",
                "reason": reason,
                "error": str(e)[:400],
            }


def _run_cortex_if_needed(
    sub: dict,
    *,
    reason: str,
    force_cortex: bool = False,
) -> dict[str, Any] | None:
    """Fire cortex (sovereign_tick) when subconscious says so, with coalesce."""
    global _last_cortex_at, _pending_cortex_reason
    needs = bool(sub.get("needs_cortex")) or force_cortex
    if not needs or not sub.get("ok"):
        return None

    with _cortex_lock:
        if not force_cortex and _last_cortex_at is not None:
            age = (_now() - _last_cortex_at).total_seconds()
            if age < cortex_min_interval_sec() and int(sub.get("heat") or 0) < 90:
                _pending_cortex_reason = reason
                return {
                    "ok": True,
                    "deferred": True,
                    "age_sec": age,
                    "reason": f"coalesce:{reason}",
                }
        _last_cortex_at = _now()
        _pending_cortex_reason = None

    from .energy_agent_sovereign import sovereign_tick
    wake_reason = f"wake:{reason}" if not reason.startswith("wake") else reason
    try:
        cortex = sovereign_tick(reason=wake_reason[:120])
        # Stamp last cortex
        try:
            from .energy_agent_sovereign import memory_set
            with SessionLocal() as db:
                memory_set(
                    db, "last_cortex_at",
                    _now().isoformat() + "Z",
                    source="system",
                )
                db.commit()
        except Exception:
            pass
        return cortex
    except Exception as e:  # noqa: BLE001
        log.exception("cortex from wake failed")
        return {"ok": False, "error": str(e)[:400]}


def wake_sovereign(
    reason: str,
    payload: dict | None = None,
    *,
    source: str = "product",
    force_cortex: bool = False,
    run_subconscious: bool = True,
) -> dict[str, Any]:
    """Product-scoped wake (mirror of tenant wake_mind).

    1. Append event to stream
    2. Subconscious tick (monologue + heat)
    3. Cortex now if heat/force (else 5m backstop)
    """
    from .energy_agent_sovereign import sovereign_enabled
    if not sovereign_enabled():
        return {"ok": True, "mode": "dark", "reason": reason, "enabled": False}

    r = (reason or "wake").strip()[:80]
    p = payload or {}
    event_id = None
    try:
        with SessionLocal() as db:
            ev = append_event(db, r, p, source=source)
            event_id = ev.id
            db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("wake_sovereign append_event failed: %s", e)

    sub: dict[str, Any] = {"ok": True, "skipped": True}
    if run_subconscious and subconscious_enabled():
        sub = subconscious_tick(reason=r, payload=p, force=True)
    else:
        # Still compute heat for cortex decision without full tick
        heat = score_event_heat(r, p)
        needs, why = decide_needs_cortex(heat=heat, reason=r, force=force_cortex)
        sub = {
            "ok": True,
            "heat": heat,
            "needs_cortex": needs or force_cortex,
            "why": why,
            "monologue_excerpt": "",
        }

    cortex = _run_cortex_if_needed(
        sub, reason=r, force_cortex=force_cortex,
    )
    return {
        "ok": True,
        "mode": "live",
        "reason": r,
        "event_id": event_id,
        "subconscious": {
            "heat": sub.get("heat"),
            "needs_cortex": sub.get("needs_cortex"),
            "why": sub.get("why"),
            "monologue_excerpt": sub.get("monologue_excerpt"),
            "tick_id": sub.get("tick_id"),
            "provider": sub.get("provider"),
        },
        "cortex": (
            None if cortex is None
            else {
                "ok": cortex.get("ok"),
                "deferred": cortex.get("deferred"),
                "tick_id": cortex.get("tick_id"),
                "reason": cortex.get("reason"),
                "mode": cortex.get("mode"),
                "n_decisions": len(cortex.get("decisions") or []),
                "brain": cortex.get("brain"),
            }
        ),
    }


def fire_and_forget_wake(reason: str, payload: dict | None = None, **kwargs: Any) -> None:
    """Best-effort wake from request handlers — never raises into product path."""
    try:
        if not _flag("SOVEREIGN_ENABLED", "1"):
            return
        # Avoid blocking HTTP: run in daemon thread
        def _run() -> None:
            try:
                wake_sovereign(reason, payload, **kwargs)
            except Exception as e:  # noqa: BLE001
                log.warning("async wake_sovereign(%s) failed: %s", reason, e)

        t = threading.Thread(target=_run, name=f"wake_sov_{reason[:20]}", daemon=True)
        t.start()
    except Exception as e:  # noqa: BLE001
        log.debug("fire_and_forget_wake: %s", e)
