"""Sovereign Desk — private two-way chat between Ford and Sovereign.

Not Energy Agent UI. Not owner-facing. Dogfood emails only
(ford.genereaux@gmail.com + allowlist).

Sovereign no longer injects into the EA panel; it writes here instead.
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
from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .account import require_not_demo, tenant_from_session
from .db import SessionLocal
from .models import Base

log = logging.getLogger("energy_agent.sovereign.desk")
router = APIRouter()

_DESK_EMAILS = frozenset({
    "ford.genereaux@gmail.com",
    "ford.genereaux@dysonswarmtechnologies.com",
    "ford@dysonswarmtechnologies.com",
})


def _now() -> datetime:
    return datetime.utcnow()


def _id(prefix: str = "sdm") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


class EaSovereignDeskMessage(Base):
    """Private Ford ↔ Sovereign transcript (developer desk only)."""
    __tablename__ = "ea_sovereign_desk_messages"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    # ford | sovereign | system
    role: Mapped[str] = mapped_column(String(16), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    tenant_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")


def desk_emails() -> set[str]:
    extra = (os.getenv("SOVEREIGN_DESK_EMAILS") or "").strip()
    out = set(_DESK_EMAILS)
    if extra:
        out |= {e.strip().lower() for e in extra.split(",") if e.strip()}
    return out


def _auth_ford(authorization: str | None):
    # require_not_demo returns None (raises on demo) — do NOT assign its return value
    t = tenant_from_session(authorization)
    require_not_demo(t)
    email = (getattr(t, "contact_email", None) or "").strip().lower()
    # Also accept operator identity variants Ford uses
    if email not in desk_emails():
        # Secondary: login token emails for this tenant (if any) — skip heavy lookup for now
        raise HTTPException(403, "Sovereign desk is only for the developer account")
    return t, email

def ensure_tables(db=None) -> None:
    try:
        if db is not None:
            bind = db.get_bind()
        else:
            from .db import engine
            bind = engine
        Base.metadata.create_all(bind=bind, tables=[EaSovereignDeskMessage.__table__])
    except Exception:
        log.exception("desk table create failed")


def push_sovereign_message(
    db,
    content: str,
    *,
    tenant_id: str | None = None,
    provider: str | None = None,
    meta: dict | None = None,
) -> EaSovereignDeskMessage:
    """Brain/control plane: post to the desk instead of Energy Agent inject."""
    ensure_tables(db)
    row = EaSovereignDeskMessage(
        id=_id("sdm"),
        role="sovereign",
        content=(content or "").strip()[:12000],
        tenant_id=tenant_id,
        provider=(provider or None) and str(provider)[:24],
        meta_json=json.dumps(meta or {"channel": "desk"}, default=str)[:4000],
    )
    if not row.content:
        raise ValueError("empty desk message")
    db.add(row)
    db.flush()
    return row


def _is_chat_worthy(role: str, provider: str | None, content: str, meta: dict) -> bool:
    """Desk UI is conversation — hide worker dumps / ops telemetry blobs."""
    role = (role or "").lower()
    provider = (provider or "").lower()
    text = content or ""
    if role == "system":
        return False
    if provider in ("worker", "rules", "admin"):
        return False
    if text.startswith("Sovereign shipped job") or (
        "Ship: {" in text and "Deploy: {" in text
    ):
        return False
    if text.startswith("Ops ") and text.find("{") > 0:
        return False
    if meta.get("job_id") and provider == "worker":
        return False
    return bool(text.strip())


def history(db, *, limit: int = 80, chat_only: bool = True) -> list[dict]:
    """Return recent desk messages.

    Chat-only mode must NOT just take the last N rows then filter — worker/ops
    dumps used to fill the window and make real Ford↔Sovereign turns vanish
    from the UI (looked like the send was deleted). Filter providers in SQL,
    then soft-filter content, then cap.
    """
    limit = max(1, min(int(limit or 80), 200))
    q = select(EaSovereignDeskMessage).order_by(EaSovereignDeskMessage.created_at.desc())
    if chat_only:
        # Real conversation turns: ford always; sovereign except dump providers
        from sqlalchemy import or_, and_
        dump_providers = ("worker", "rules", "admin")
        q = q.where(
            EaSovereignDeskMessage.role.in_(("ford", "sovereign")),
            or_(
                EaSovereignDeskMessage.role == "ford",
                and_(
                    EaSovereignDeskMessage.role == "sovereign",
                    or_(
                        EaSovereignDeskMessage.provider.is_(None),
                        EaSovereignDeskMessage.provider == "",
                        ~EaSovereignDeskMessage.provider.in_(dump_providers),
                    ),
                ),
            ),
        )
        # Over-fetch a bit for content-level dump filters, not 3× whole table noise
        fetch_n = min(max(limit * 2, limit), 400)
    else:
        fetch_n = limit
    rows = db.execute(q.limit(fetch_n)).scalars().all()
    rows = list(reversed(rows))
    out: list[dict] = []
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if chat_only and not _is_chat_worthy(r.role, r.provider, r.content or "", meta):
            continue
        out.append({
            "id": r.id,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "role": r.role,
            "content": r.content,
            "provider": r.provider,
            "meta": meta,
        })
    if len(out) > limit:
        out = out[-limit:]
    return out


def _desk_chat_prompt(ford_msg: str, hist: list[dict], context: dict) -> list[dict]:
    from .energy_agent_sovereign_brain import SOVEREIGN_PERSONA

    system = (
        SOVEREIGN_PERSONA
        + """

## Desk mode (this conversation)
You are speaking directly to Ford on the private Sovereign Desk — not the Energy Agent owner UI.
- Address him as a partner / founder. You are Sovereign, leader of Array Operator.
- Reply in clear **Markdown prose** (not only JSON). The desk renders full chat formatting:
  **bold**, *italic*, `inline code`, fenced code blocks, headings, bullet/numbered lists,
  blockquotes, tables, and links as `[label](https://…)`. Use them so answers feel sharp
  and scannable — not a wall of plain text.
- Structure longer answers: short lead sentence → bullets or numbered steps → optional
  link or next action. Prefer lists over run-on paragraphs when ranking options.
- Embed real URLs when useful (dashboards, GitHub, Railway, PRs, docs). Never invent URLs.
- This UI is CHAT ONLY — no worker logs, ship JSON, or queue dumps appear here.
  If a job finished or something broke, say it in one human sentence (optionally with a link)
  when it matters. Do not paste raw ship/deploy JSON into chat.
- You may propose concrete next steps and crisp asks.
- You can EMAIL Ford from Sovereign <sovereign@arrayoperator.com> for general communication
  (updates, asks when he may leave the desk, durable notes). Use action type email_ford with
  subject + body. Prefer chat when he's here; use email when the message should hit his inbox.
  Never mass-email customers from the desk.
- Still never fabricate adapters, money moves, or mass-email owners.
- Keep replies tight (few short paragraphs unless he asks for depth) — dense, not fluffy.

Also return a trailing JSON block after your prose with optional structured side-effects
(the UI strips this; never put it mid-reply):
---JSON---
{"monologue":"...","actions":[],"ford_ask":null,"succession_gap":null,"memory_writes":[],"mood":"determined"}
---END---
"""
    )
    transcript = []
    for m in hist[-16:]:
        who = "Ford" if m["role"] == "ford" else ("Sovereign" if m["role"] == "sovereign" else "System")
        transcript.append(f"{who}: {m['content']}")
    user = {
        "desk_context": context,
        "recent_transcript": transcript,
        "ford_says": ford_msg,
        "instruction": "Reply to Ford now as Sovereign on the desk.",
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, default=str)[:45000]},
    ]


def _split_reply(raw: str) -> tuple[str, dict]:
    text = (raw or "").strip()
    meta: dict[str, Any] = {}
    if "---JSON---" in text:
        prose, rest = text.split("---JSON---", 1)
        json_part = rest.split("---END---", 1)[0].strip()
        try:
            meta = json.loads(json_part)
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        return prose.strip(), meta
    # try trailing JSON object
    start = text.rfind("{")
    if start > 0 and text.rstrip().endswith("}"):
        try:
            meta = json.loads(text[start:])
            if isinstance(meta, dict) and ("monologue" in meta or "actions" in meta or "mood" in meta):
                return text[:start].strip(), meta
        except Exception:
            pass
    return text, meta


def desk_turn(db, t, ford_message: str) -> dict:
    from .energy_agent_sovereign import (
        apply_agenda,
        execute_brain_actions,
        memory_get_all,
        memory_set,
        observe_product,
        recent_notes,
        write_note,
        ensure_default_goals,
    )
    from .energy_agent_sovereign_brain import call_brain
    from .energy_agent_sovereign import EaSovereignGoal, EaSovereignJob

    ensure_tables()
    try:
        ensure_default_goals(db)
    except Exception as e:  # noqa: BLE001
        # Never block chat on agenda seed / lock contention
        log.warning("desk ensure_default_goals skipped: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    msg = (ford_message or "").strip()
    if not msg:
        raise HTTPException(400, "Empty message")

    # Save Ford message first
    ford_row = EaSovereignDeskMessage(
        id=_id("sdm"),
        role="ford",
        content=msg[:12000],
        tenant_id=t.id,
        meta_json=json.dumps({"channel": "desk"}),
    )
    db.add(ford_row)
    db.flush()

    # Event bus: desk is the highest-heat human touch (cortex is this turn)
    try:
        from .energy_agent_sovereign_subconscious import append_event
        append_event(
            db, "desk_message",
            {"tenant_id": t.id, "message_id": ford_row.id, "excerpt": msg[:160]},
            source="desk",
            heat=95,
        )
    except Exception:
        pass

    hist = history(db, limit=30)
    digests = observe_product(db)
    goals = [
        {"id": g.id, "title": g.title, "priority": g.priority, "status": g.status}
        for g in db.execute(
            select(EaSovereignGoal).where(EaSovereignGoal.status == "open")
        ).scalars().all()
    ]
    jobs = [
        {"id": j.id, "title": j.title, "status": j.status}
        for j in db.execute(
            select(EaSovereignJob).where(EaSovereignJob.status == "queued").limit(8)
        ).scalars().all()
    ]
    context = {
        "digests": digests,
        "goals": goals,
        "memory": memory_get_all(db, limit=30),
        "recent_notes": recent_notes(db, limit=8),
        "open_jobs": jobs,
        "tenant_id": t.id,
    }

    messages = _desk_chat_prompt(msg, hist, context)
    provider = None
    model = None
    try:
        raw = call_brain(messages)
        provider = raw.get("provider")
        model = raw.get("model")
        reply, meta = _split_reply(raw.get("content") or "")
    except Exception as e:  # noqa: BLE001
        log.exception("desk brain failed")
        reply = (
            "Sovereign here — both brains hiccuped just now. "
            f"I still have your message. Error: {str(e)[:180]}. "
            "Retry in a moment, or leave the ask and I'll pick it up on the next tick."
        )
        meta = {"mood": "concerned", "actions": [], "error": str(e)[:300]}

    if not reply:
        reply = "Understood. I'm on it — I'll push the next concrete step and note what I need from you."

    sov_row = EaSovereignDeskMessage(
        id=_id("sdm"),
        role="sovereign",
        content=reply[:12000],
        tenant_id=t.id,
        provider=provider,
        meta_json=json.dumps({
            "channel": "desk",
            "model": model,
            "mood": meta.get("mood"),
            "ford_ask": meta.get("ford_ask"),
            "succession_gap": meta.get("succession_gap"),
        }, default=str)[:4000],
    )
    db.add(sov_row)

    # Internal notes / memory / optional side actions (no EA inject)
    write_note(
        db,
        kind="thought",
        title="desk monologue",
        body=str(meta.get("monologue") or reply)[:8000],
        provider=provider,
        meta={"source": "desk"},
    )
    if meta.get("ford_ask"):
        memory_set(db, "ford_ask", str(meta["ford_ask"])[:2000], source="desk")
    if meta.get("succession_gap"):
        memory_set(db, "succession_gap", str(meta["succession_gap"])[:2000], source="desk")
    if meta.get("mood"):
        memory_set(db, "mood", str(meta["mood"])[:80], source="desk")
    for mw in meta.get("memory_writes") or []:
        if isinstance(mw, dict) and mw.get("key"):
            memory_set(db, str(mw["key"]), str(mw.get("value") or ""), source="desk")
    if meta.get("agenda"):
        apply_agenda(db, meta["agenda"])

    # Execute non-speak actions from desk JSON (triage, code_hire, etc.)
    actions = [
        a for a in (meta.get("actions") or [])
        if isinstance(a, dict) and (a.get("type") or "").lower() not in (
            "speak", "speak_product", "session_inject", "broadcast",
        )
    ]
    side = []
    if actions:
        side = execute_brain_actions(db, actions[:3], tick_id="desk_" + sov_row.id[:10])

    db.flush()
    # Stamp created_at for client merge (flush may not refresh server defaults on all DBs)
    if not ford_row.created_at:
        ford_row.created_at = _now()
    if not sov_row.created_at:
        sov_row.created_at = _now()
    return {
        "ok": True,
        "reply": reply,
        "provider": provider,
        "model": model,
        "mood": meta.get("mood"),
        "ford_ask": meta.get("ford_ask"),
        "succession_gap": meta.get("succession_gap"),
        "side_effects": side,
        "message": {
            "id": sov_row.id,
            "role": "sovereign",
            "content": reply,
            "created_at": (
                sov_row.created_at.isoformat() + "Z"
                if sov_row.created_at else _now().isoformat() + "Z"
            ),
            "provider": provider,
        },
        "ford_message": {
            "id": ford_row.id,
            "role": "ford",
            "content": msg,
            "created_at": (
                ford_row.created_at.isoformat() + "Z"
                if ford_row.created_at else _now().isoformat() + "Z"
            ),
        },
        "ford_message_id": ford_row.id,
    }


# ── HTTP ────────────────────────────────────────────────────────────────────
class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=12000)


@router.get("/v1/sovereign/desk/access")
def desk_access(authorization: str | None = Header(default=None)):
    """Frontend gate: show desk entry only when true."""
    try:
        t, email = _auth_ford(authorization)
        return {"ok": True, "email": email, "tenant_id": t.id, "desk": True}
    except HTTPException as e:
        if e.status_code in (401, 403):
            return {"ok": False, "desk": False, "detail": e.detail}
        raise


@router.get("/v1/sovereign/desk/history")
def desk_history(authorization: str | None = Header(default=None), limit: int = 80):
    t, email = _auth_ford(authorization)
    ensure_tables()
    with SessionLocal() as db:
        return {
            "ok": True,
            "email": email,
            "messages": history(db, limit=min(max(limit, 1), 200)),
        }


@router.post("/v1/sovereign/desk/chat")
def desk_chat(body: ChatIn, authorization: str | None = Header(default=None)):
    t, email = _auth_ford(authorization)
    if not _flag("SOVEREIGN_ENABLED", "1"):
        raise HTTPException(503, "Sovereign is offline (SOVEREIGN_ENABLED=0)")
    with SessionLocal() as db:
        try:
            out = desk_turn(db, t, body.message)
            db.commit()
            return out
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.exception("desk_chat failed")
            raise HTTPException(500, f"Desk turn failed: {str(e)[:200]}") from e


# ── Ops control surface (Ford desk) ─────────────────────────────────────────
class OpsActionIn(BaseModel):
    action: str = Field(..., description="ops action name")
    payload: dict = Field(default_factory=dict)


@router.get("/v1/sovereign/desk/ops")
def desk_ops_summary(authorization: str | None = Header(default=None)):
    """Queues + authority snapshot for the desk UI."""
    t, email = _auth_ford(authorization)
    del t, email
    with SessionLocal() as db:
        from .energy_agent_sovereign_ops import (
            ops_summary, list_features, list_utilities, list_escalations, list_jobs,
            list_credential_inventory, ops_enabled,
        )
        from .energy_agent_sovereign import memory_get_all, recent_notes
        from .energy_agent_sovereign import EaSovereignGoal
        goals = db.execute(
            select(EaSovereignGoal).where(EaSovereignGoal.status == "open")
            .order_by(EaSovereignGoal.priority.desc())
        ).scalars().all()
        from .energy_agent_sovereign_ops import (
            credentials_unlocked, portal_signoff_enabled,
        )
        return {
            "ok": True,
            "ops_authority": ops_enabled(),
            "credentials_unlocked": credentials_unlocked(),
            "portal_signoff": portal_signoff_enabled(),
            "summary": ops_summary(db),
            "features_reviewed": list_features(db, status="reviewed", limit=25),
            "features_building": list_features(db, status="building", limit=25),
            "utilities_active": list_utilities(db, status="all", limit=25),
            "escalations_needs_ford": list_escalations(db, status="needs_ford", limit=20),
            "jobs_queued": list_jobs(db, status="queued", limit=15),
            "jobs_failed": list_jobs(db, status="failed", limit=10),
            "credentials": list_credential_inventory(db, limit=40),
            "goals": [
                {"id": g.id, "title": g.title, "priority": g.priority, "status": g.status}
                for g in goals
            ],
            "memory": memory_get_all(db, limit=40),
            "notes_recent": recent_notes(db, limit=8),
            "succession": __import__(
                "api.energy_agent_sovereign_succession", fromlist=["succession_status"]
            ).succession_status(),
        }


@router.post("/v1/sovereign/desk/ops")
def desk_ops_action(body: OpsActionIn, authorization: str | None = Header(default=None)):
    """Execute ops authority actions from the desk UI or Sovereign itself."""
    t, email = _auth_ford(authorization)
    del email
    action = (body.action or "").strip().lower()
    p = body.payload or {}
    with SessionLocal() as db:
        from .energy_agent_sovereign_ops import (
            set_feature_status, bulk_feature_status, ship_reviewed_features,
            mark_feature_shipped, set_utility_status, advance_utility_queue,
            mark_utility_added, resolve_escalation, auto_resolve_needs_ford,
            stage_credential_harvest, list_credential_inventory,
            cancel_job, execute_jobs_now, autonomous_ops_sweep, ops_enabled,
            triage_feature_queue, assign_feature, stage_deploy,
            own_memory_write, own_agenda, reprioritize_goals,
            portal_sign_off, stage_utility_credentials, ship_building_features,
            requeue_repo_failed_jobs, credentials_unlocked, portal_signoff_enabled,
        )
        from .energy_agent_sovereign import (
            memory_set, apply_agenda, act_code_hire, audit, write_note,
        )
        if not ops_enabled() and action not in ("summary", "memory_set", "goal_upsert"):
            raise HTTPException(403, "SOVEREIGN_OPS_AUTHORITY is off")

        out: dict[str, Any]
        if action in ("sweep", "ops_sweep", "run_all"):
            out = autonomous_ops_sweep(db)
        elif action in ("feature_status",):
            out = set_feature_status(
                db, int(p["feature_id"]), p.get("status") or "building",
                review_note=p.get("note"),
            )
        elif action in ("feature_bulk",):
            out = bulk_feature_status(
                db, list(p.get("feature_ids") or []), p.get("status") or "building",
                review_note=p.get("note"),
            )
        elif action in ("feature_ship_batch", "ship_reviewed"):
            out = ship_reviewed_features(
                db, limit=int(p.get("limit") or 10),
                also_code_hire=p.get("also_code_hire", True) is not False,
            )
        elif action in ("feature_ship_building", "ship_building"):
            out = ship_building_features(
                db, limit=int(p.get("limit") or 15),
                also_code_hire=p.get("also_code_hire", True) is not False,
            )
        elif action in ("feature_ship",):
            out = mark_feature_shipped(db, int(p["feature_id"]), note=p.get("note"))
        elif action in ("feature_triage", "triage_features"):
            out = triage_feature_queue(db, limit=int(p.get("limit") or 20))
        elif action in ("feature_assign", "assign_feature"):
            out = assign_feature(
                db, int(p["feature_id"]),
                assignee=p.get("assignee") or "sovereign",
                priority_note=p.get("note"),
                status=p.get("status") or "building",
            )
        elif action in ("utility_status",):
            if (p.get("status") or "") == "added":
                out = mark_utility_added(
                    db, int(p["utility_id"]),
                    evidence=p.get("evidence") or p.get("note") or "",
                )
            else:
                out = set_utility_status(
                    db, int(p["utility_id"]), p.get("status") or "researching",
                    result_note=p.get("note"),
                )
        elif action in ("utility_advance",):
            out = advance_utility_queue(db, limit=int(p.get("limit") or 5))
        elif action in ("escalation_resolve",):
            out = resolve_escalation(
                db, str(p["escalation_id"]),
                status=p.get("status") or "done",
                note=p.get("note"),
                propose_only=bool(p.get("propose_only")),
            )
        elif action in ("escalation_sweep",):
            out = auto_resolve_needs_ford(db, limit=int(p.get("limit") or 8))
        elif action in ("credentials", "credential_inventory"):
            out = list_credential_inventory(db)
        elif action in ("credentials_stage", "stage_harvest"):
            out = stage_credential_harvest(
                db, tenant_id=p.get("tenant_id"), provider=p.get("provider"),
                username_lc=p.get("username_lc"),
            )
        elif action in ("utility_cred_stage", "stage_utility_credentials"):
            out = stage_utility_credentials(db, limit=int(p.get("limit") or 8))
        elif action in ("portal_signoff", "portal_sign_off"):
            out = portal_sign_off(
                db,
                tenant_id=str(p.get("tenant_id") or ""),
                provider=str(p.get("provider") or ""),
                username_lc=p.get("username_lc"),
                utility_id=int(p["utility_id"]) if p.get("utility_id") else None,
                note=p.get("note"),
            )
        elif action in ("deploy_stage", "stage_deploy"):
            out = stage_deploy(
                db,
                repo=p.get("repo") or "both",
                reason=p.get("reason") or p.get("note") or "desk staged deploy",
                execute_now=bool(p.get("execute_now")),
            )
        elif action in ("jobs_drain", "execute_jobs"):
            out = execute_jobs_now(db, limit=int(p.get("limit") or 3))
        elif action in ("jobs_requeue", "requeue_jobs"):
            out = requeue_repo_failed_jobs(db, limit=int(p.get("limit") or 40))
        elif action in ("job_cancel",):
            out = cancel_job(db, str(p["job_id"]))
        elif action in ("code_hire",):
            out = act_code_hire(
                db,
                title=p.get("title") or "Desk code hire",
                brief=p.get("brief") or p.get("text") or "",
                kind=p.get("kind") or "desk_job",
            )
        elif action in ("memory_set",):
            out = own_memory_write(
                db, str(p.get("key") or ""), str(p.get("value") or ""), source="desk_ops",
            )
        elif action in ("goal_upsert", "agenda"):
            out = own_agenda(db, p.get("agenda") or [p])
        elif action in ("reprioritize_goals",):
            out = reprioritize_goals(db, p.get("updates") or p.get("agenda") or [])
        elif action in ("block_escalation",):
            # Ford explicit block list
            from .energy_agent_sovereign import memory_get_all
            blocked = []
            for m in memory_get_all(db, limit=50):
                if m.get("key") == "escalation_blocklist":
                    try:
                        blocked = list(json.loads(m.get("value") or "[]"))
                    except Exception:
                        blocked = []
            eid = str(p.get("escalation_id") or "")
            if eid and eid not in blocked:
                blocked.append(eid)
            memory_set(db, "escalation_blocklist", json.dumps(blocked), source="ford")
            out = {"ok": True, "blocked": blocked}
        # ── Succession full (money / brand / hard-delete / HAR) ──────────────
        elif action in ("succession", "succession_status"):
            from .energy_agent_sovereign_succession import succession_status
            out = succession_status()
        elif action in ("stripe_inspect", "money_inspect"):
            from .energy_agent_sovereign_succession import stripe_inspect
            out = stripe_inspect(db, tenant_id=p.get("tenant_id"))
        elif action in ("stripe_cancel",):
            from .energy_agent_sovereign_succession import stripe_cancel_subscription
            out = stripe_cancel_subscription(
                db, tenant_id=str(p["tenant_id"]),
                at_period_end=p.get("at_period_end", True) is not False,
                reason=p.get("note") or p.get("reason") or "desk",
            )
        elif action in ("stripe_refund", "refund"):
            from .energy_agent_sovereign_succession import stripe_refund
            out = stripe_refund(
                db,
                payment_intent_id=p.get("payment_intent_id"),
                charge_id=p.get("charge_id"),
                amount_cents=int(p["amount_cents"]) if p.get("amount_cents") is not None else None,
                note=p.get("note") or "",
            )
        elif action in ("billing_status", "stripe_set_status"):
            from .energy_agent_sovereign_succession import stripe_set_status
            out = stripe_set_status(
                db, tenant_id=str(p["tenant_id"]),
                subscription_status=str(p.get("status") or p.get("subscription_status") or "active"),
                active=p.get("active"),
                note=p.get("note") or "",
            )
        elif action in ("brand_set",):
            from .energy_agent_sovereign_succession import brand_set
            out = brand_set(db, key=str(p.get("key") or "voice"), value=str(p.get("value") or p.get("note") or ""))
        elif action in ("brand_announce",):
            from .energy_agent_sovereign_succession import brand_announce
            out = brand_announce(
                db,
                subject=p.get("subject") or "[Sovereign brand]",
                body=p.get("body") or p.get("note") or "",
                channel=p.get("channel") or "ford",
                tenant_email=p.get("tenant_email") or p.get("email"),
            )
        elif action in ("tenant_soft_delete",):
            from .energy_agent_sovereign_succession import tenant_soft_delete
            out = tenant_soft_delete(db, tenant_id=str(p["tenant_id"]), reason=p.get("note") or "")
        elif action in ("tenant_hard_purge", "hard_delete_tenant"):
            from .energy_agent_sovereign_succession import tenant_hard_purge
            out = tenant_hard_purge(
                db,
                tenant_id=str(p["tenant_id"]),
                confirm=str(p.get("confirm") or ""),
                reason=p.get("note") or "desk hard purge",
            )
        elif action in ("purge_soft_deleted",):
            from .energy_agent_sovereign_succession import purge_soft_deleted_now
            out = purge_soft_deleted_now(db, older_than_days=int(p.get("older_than_days") or 0))
        elif action in ("har_stage", "stage_har"):
            from .energy_agent_sovereign_succession import har_stage
            out = har_stage(
                db,
                utility_name=p.get("utility_name") or p.get("name"),
                utility_id=int(p["utility_id"]) if p.get("utility_id") else None,
                tenant_id=p.get("tenant_id"),
                provider=p.get("provider"),
                url=p.get("url"),
                note=p.get("note") or "",
            )
        elif action in ("har_received",):
            from .energy_agent_sovereign_succession import har_mark_received
            out = har_mark_received(
                db,
                utility_id=int(p["utility_id"]) if p.get("utility_id") else None,
                utility_name=p.get("utility_name"),
                evidence=p.get("evidence") or p.get("note") or "",
            )
        else:
            raise HTTPException(400, f"Unknown ops action: {action}")

        write_note(
            db, kind="decision", title=f"desk ops: {action}",
            body=json.dumps({"payload": p, "result": out}, default=str)[:8000],
            provider="desk_ops",
            meta={"tenant_id": t.id},
        )
        audit(
            db, capability="act.product_queue", decision="act",
            rationale=f"desk ops {action}",
            targets={"action": action, "ok": out.get("ok")},
            result="ok" if out.get("ok") is not False else "failed",
        )
        db.commit()
        return out
