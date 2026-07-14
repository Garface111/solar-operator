"""Energy Agent — Operating Mind (continuous cognition).

North star: conversation is one window into a mind that thinks continuously.
Not "voice plus agents" — one mind, background tasks, seamless updates.

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
from sqlalchemy import DateTime, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .account import require_not_demo, tenant_from_session
from .db import SessionLocal
from .models import Base

log = logging.getLogger("energy_agent.mind")
router = APIRouter()


def _now() -> datetime:
    return datetime.utcnow()


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
    # plan_created | task_queued | task_done | task_failed | mind_note | interrupt_candidate
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


def _world_get(db, tenant_id: str) -> dict:
    row = db.get(EaWorldState, tenant_id)
    if not row:
        return {"revision": 0, "open_intents": [], "notes": {}, "fleet_digest": None}
    try:
        data = json.loads(row.state_json or "{}")
    except Exception:
        data = {}
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


def classify_and_plan(
    db,
    tenant_id: str,
    session_id: str | None,
    user_text: str,
    context: dict | None = None,
) -> dict | None:
    """If utterance warrants continuous work, create plan + cheap tasks.

    Returns plan summary for the mind to optionally acknowledge, or None.
    Heavy workers (propose_ui) are queued only for clear UX intents with confirm later.
    """
    text = (user_text or "").strip()
    if len(text) < 8:
        return None
    ctx = context or {}

    plan_kind = None
    objectives: list[str] = []
    tasks: list[dict] = []

    if _UX_FRICTION.search(text):
        plan_kind = "ux_friction"
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
            # Heavy path is queued as candidate — executor may skip without budget/value
            {
                "kind": "propose_ui_candidate",
                "title": "Consider a UI improvement proposal",
                "priority": 80,
                "payload": {"text": text, "context": ctx},
                "speak_hint": (
                    "I sketched a direction based on what you said about the layout. "
                    "Want me to open a proposal, or keep refining what feels hard?"
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

    _world_patch(db, tenant_id, {
        "last_intent": plan_kind,
        "last_user_utterance": text[:500],
        "open_plan_id": plan_id,
    })

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


# ── task executor (cheap workers only in-process) ───────────────────────────
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
            _world_patch(db, task.tenant_id, {
                "last_context": payload.get("context") or payload,
            })
            result["snapshotted"] = True

        elif task.kind == "search_similar":
            # Lightweight: scan recent tenant memory for overlapping words
            from .energy_agent import _mem_get
            notes = _mem_get(db, f"tenant:{task.tenant_id}", limit=80)
            needle = set(re.findall(r"[a-z]{4,}", (payload.get("text") or "").lower()))
            hits = []
            for n in notes:
                blob = (n.get("value") or "").lower()
                score = len(needle.intersection(set(re.findall(r"[a-z]{4,}", blob))))
                if score >= 2:
                    hits.append({"key": n.get("key"), "score": score, "value": (n.get("value") or "")[:200]})
            hits.sort(key=lambda x: -x["score"])
            result["hits"] = hits[:5]
            if hits:
                speak = (
                    speak
                    or "I found earlier notes that sound related to what you described. "
                       "We can build on those if you want."
                )

        elif task.kind == "fleet_pulse":
            from .models import Tenant
            tenant = db.get(Tenant, task.tenant_id)
            if tenant is None:
                result = {"ok": False, "skipped": True, "reason": "tenant not found"}
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

        elif task.kind == "propose_ui_candidate":
            # Do not auto-run heavy judge pipeline — park candidate for mind/user
            result = {
                "ok": True,
                "deferred": True,
                "reason": "Heavy UI proposals wait for clear user yes or high expected value",
                "suggestion": payload.get("text"),
            }
            speak = (
                "I held off spinning up a full UI redesign until we pin down what feels hard. "
                "Finding vs understanding — which is closer?"
            )

        else:
            result = {"ok": True, "skipped": True, "kind": task.kind}

        task.status = "done"
        task.result_json = json.dumps(result, default=str)[:12000]
        task.finished_at = _now()
        _emit(
            db, task.tenant_id, "task_done",
            task.title,
            session_id=task.session_id, ref_id=task.id,
            payload=result,
            speak_as_mind=speak,
        )
        if speak:
            _emit(
                db, task.tenant_id, "interrupt_candidate",
                task.title,
                session_id=task.session_id, ref_id=task.id,
                speak_as_mind=speak,
                payload={"task_kind": task.kind},
            )
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


def mind_tick(db, tenant_id: str, session_id: str | None = None) -> dict:
    """One cognitive cycle: drain tasks, update tick time, return interrupt candidates."""
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

    # Unconsumed interrupt candidates
    q = (
        select(EaEvent)
        .where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "interrupt_candidate",
            EaEvent.consumed == 0,
        )
        .order_by(EaEvent.id.asc())
        .limit(5)
    )
    if session_id:
        # Prefer session-scoped, but allow tenant-wide soft updates
        pass
    events = db.execute(q).scalars().all()
    interrupts = []
    for ev in events:
        interrupts.append({
            "event_id": ev.id,
            "summary": ev.summary,
            "speak": ev.speak_as_mind,
            "ref_id": ev.ref_id,
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


# ── HTTP ────────────────────────────────────────────────────────────────────
class TickIn(BaseModel):
    session_id: str | None = None


class ConsumeIn(BaseModel):
    event_ids: list[int] = Field(default_factory=list)


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
            "world": world,
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
                    "created_at": e.created_at.isoformat() + "Z" if e.created_at else None,
                    "consumed": bool(e.consumed),
                }
                for e in recent
            ],
        }


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
            "events": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "summary": e.summary,
                    "speak_as_mind": e.speak_as_mind,
                    "ref_id": e.ref_id,
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
        for eid in body.event_ids or []:
            ev = db.get(EaEvent, int(eid))
            if ev and ev.tenant_id == t.id and not ev.consumed:
                ev.consumed = 1
                n += 1
        db.commit()
        return {"ok": True, "consumed": n}
