"""Ford Operator inbox — durable escalations + standing Grok triage.

Energy Agent escalations used to only email Ford. This module:

  1. Persists every escalate_to_ford into ea_escalations (source of truth)
  2. Runs a background Grok worker that classifies + drafts a build plan
  3. Exposes admin API + a live HTML board Ford can leave open

Auth: ADMIN_API_KEY via header X-Admin-Key or ?key= (same as /admin/funnel).
Email becomes optional backup when status hits needs_ford (once per item).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text, select, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import SessionLocal
from .models import Base, Tenant
from .notify import send_internal_alert

log = logging.getLogger("ford_escalations")
router = APIRouter()

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
XAI_API_KEY = (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()
XAI_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-3")
# How many open items the worker processes per tick
WORKER_BATCH = int(os.getenv("FORD_ESCALATION_BATCH", "3") or 3)
# Skip re-notify if we already notified within this window (worker may re-touch)
NOTIFY_COOLDOWN_HOURS = int(os.getenv("FORD_ESCALATION_NOTIFY_HOURS", "6") or 6)


def _now() -> datetime:
    return datetime.utcnow()


# ── model ───────────────────────────────────────────────────────────────────
class EaEscalation(Base):
    """Standing inbox for Energy Agent → Ford escalations."""
    __tablename__ = "ea_escalations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    tenant_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    # open → working → needs_ford | done | dismissed
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    # bug | feature | how_to | credentials | billing | other
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # low|normal|high|urgent

    summary: Mapped[str] = mapped_column(Text, default="")
    user_said: Mapped[str | None] = mapped_column(Text, nullable=True)
    quiet: Mapped[int] = mapped_column(Integer, default=0)  # 1 if silently escalated

    # Grok worker output
    agent_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    worked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ford_note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── auth ────────────────────────────────────────────────────────────────────
def _check_admin(key_header: str | None, key_query: str | None) -> None:
    key = key_header or key_query
    if not ADMIN_API_KEY:
        raise HTTPException(503, "Admin API not configured (set ADMIN_API_KEY)")
    if not hmac.compare_digest(key or "", ADMIN_API_KEY):
        raise HTTPException(403, "Invalid or missing admin key")


# ── queue API (called from Energy Agent) ────────────────────────────────────
def enqueue_escalation(
    *,
    tenant_id: str,
    tenant_email: str | None,
    session_id: str | None,
    summary: str,
    user_said: str = "",
    quiet: bool = False,
    also_email: bool = True,
) -> dict:
    """Persist escalation. Optionally fire a short email backup (non-blocking)."""
    eid = "esc_" + uuid.uuid4().hex[:16]
    summary = (summary or "(no summary)").strip()[:4000]
    user_said = (user_said or "").strip()[:8000]
    with SessionLocal() as db:
        row = EaEscalation(
            id=eid,
            tenant_id=tenant_id,
            tenant_email=(tenant_email or "")[:255] or None,
            session_id=session_id,
            summary=summary,
            user_said=user_said or None,
            quiet=1 if quiet else 0,
            status="open",
            priority="normal",
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(row)
        db.commit()

    if also_email and not quiet:
        try:
            board = "https://nepooloperator.com/admin/escalations"
            body = (
                f"Energy Agent escalation queued for Ford Operator\n"
                f"id: {eid}\n"
                f"tenant: {tenant_id}\n"
                f"email: {tenant_email or '—'}\n"
                f"session: {session_id or '—'}\n\n"
                f"{summary}\n\n"
                f"User said:\n{user_said or '(none)'}\n\n"
                f"Board: {board}?key=…\n"
                f"(Standing Grok will triage this shortly.)\n"
            )
            send_internal_alert(f"[EA Inbox] {summary[:70]}", body)
        except Exception:
            log.exception("escalation email backup failed for %s", eid)

    log.info("escalation queued %s tenant=%s quiet=%s", eid, tenant_id, quiet)
    return {"ok": True, "escalated": True, "escalation_id": eid, "status": "open"}


# ── Grok worker ─────────────────────────────────────────────────────────────
_WORKER_SYSTEM = """You are Ford's product operator for Array Operator / EnergyAgent.
A tenant-facing Energy Agent escalated something. Your job:
1) Classify kind: bug | feature | how_to | credentials | billing | other
2) Set priority: low | normal | high | urgent
3) Write a short agent_notes (what happened, why it matters)
4) Write proposed_plan: concrete next steps Ford or code agents can take
5) If the issue is clear enough for a code/product change, write proposed_fix
   as a tight implementation brief (files/areas, acceptance criteria). Else null.

Reply with ONLY valid JSON (no markdown fences):
{
  "kind": "...",
  "priority": "...",
  "agent_notes": "...",
  "proposed_plan": "...",
  "proposed_fix": "..." or null,
  "needs_ford": true/false,
  "one_line": "short title for the board"
}
needs_ford=true when Ford must decide, provide creds, or approve a ship.
needs_ford=false only if this is pure documentation / already-known and no action.
Be practical. No fluff."""


def _http_json(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code}: {err}") from e


def _grok_triage(row: EaEscalation) -> dict:
    if not XAI_API_KEY:
        return {
            "kind": "other",
            "priority": "normal",
            "agent_notes": "Grok keys not configured — triage skipped. Read summary manually.",
            "proposed_plan": "Set XAI_API_KEY and re-run worker, or handle manually.",
            "proposed_fix": None,
            "needs_ford": True,
            "one_line": (row.summary or "")[:80],
            "provider": "stub",
        }
    user = (
        f"Tenant: {row.tenant_id}\n"
        f"Tenant email: {row.tenant_email or '—'}\n"
        f"Session: {row.session_id or '—'}\n"
        f"Quiet escalate: {bool(row.quiet)}\n\n"
        f"Summary from Energy Agent:\n{row.summary}\n\n"
        f"User said:\n{row.user_said or '(none recorded)'}\n"
    )
    body = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": _WORKER_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    out = _http_json(
        f"{XAI_BASE}/chat/completions",
        {
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        body,
    )
    msg = ((out.get("choices") or [{}])[0].get("message") or {})
    content = (msg.get("content") or "").strip()
    # Strip optional ```json fences
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # salvage first {...}
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            return {
                "kind": "other",
                "priority": "normal",
                "agent_notes": content[:2000] or "Unparseable Grok response",
                "proposed_plan": "Review raw notes; re-run triage if needed.",
                "proposed_fix": None,
                "needs_ford": True,
                "one_line": (row.summary or "")[:80],
                "provider": "xai",
                "raw": content[:3000],
            }
        parsed = json.loads(m.group(0))
    parsed["provider"] = "xai"
    return parsed


def process_open_escalations(limit: int | None = None) -> dict:
    """Worker tick: triage open escalations with Grok, mark needs_ford, notify."""
    limit = limit if limit is not None else WORKER_BATCH
    limit = max(1, min(20, int(limit)))
    processed = 0
    errors = 0
    notified = 0

    with SessionLocal() as db:
        rows = db.execute(
            select(EaEscalation)
            .where(EaEscalation.status == "open")
            .order_by(EaEscalation.created_at.asc())
            .limit(limit)
        ).scalars().all()
        ids = [r.id for r in rows]

    for eid in ids:
        try:
            with SessionLocal() as db:
                row = db.get(EaEscalation, eid)
                if not row or row.status != "open":
                    continue
                row.status = "working"
                row.updated_at = _now()
                db.commit()

            with SessionLocal() as db:
                row = db.get(EaEscalation, eid)
                if not row:
                    continue
                result = _grok_triage(row)
                kind = str(result.get("kind") or "other")[:32]
                priority = str(result.get("priority") or "normal")[:16]
                if priority not in ("low", "normal", "high", "urgent"):
                    priority = "normal"
                notes = str(result.get("agent_notes") or "")[:8000]
                plan = str(result.get("proposed_plan") or "")[:8000]
                fix = result.get("proposed_fix")
                fix_s = (str(fix)[:8000] if fix else None)
                needs_ford = bool(result.get("needs_ford", True))
                one_line = str(result.get("one_line") or row.summary or "")[:120]

                row.kind = kind
                row.priority = priority
                row.agent_notes = notes
                row.proposed_plan = plan
                row.proposed_fix = fix_s
                row.category_json = json.dumps({
                    "one_line": one_line,
                    "provider": result.get("provider"),
                    "needs_ford": needs_ford,
                })[:4000]
                row.worked_at = _now()
                row.updated_at = _now()
                row.status = "needs_ford" if needs_ford else "done"
                if row.status == "done":
                    row.resolved_at = _now()
                db.commit()

                # Wake Sovereign when escalation needs Ford / resolved
                try:
                    from .energy_agent_sovereign_subconscious import fire_and_forget_wake
                    fire_and_forget_wake(
                        "needs_ford" if needs_ford else "ford_escalation",
                        {
                            "escalation_id": row.id,
                            "status": row.status,
                            "priority": priority,
                            "kind": kind,
                            "one_line": one_line[:160],
                            "needs_ford": needs_ford,
                        },
                        source="ford_escalations",
                        force_cortex=needs_ford,
                    )
                except Exception:
                    pass

                # Notify Ford when human attention needed (not every quiet auto-esc)
                should_notify = needs_ford and not bool(row.quiet)
                if should_notify:
                    cooldown = timedelta(hours=NOTIFY_COOLDOWN_HOURS)
                    if row.notified_at and (_now() - row.notified_at) < cooldown:
                        should_notify = False
                if should_notify:
                    try:
                        body = (
                            f"Ford Operator finished triage — needs you\n"
                            f"id: {row.id}\n"
                            f"priority: {priority} · kind: {kind}\n"
                            f"tenant: {row.tenant_id} ({row.tenant_email or '—'})\n\n"
                            f"Title: {one_line}\n\n"
                            f"Summary:\n{row.summary}\n\n"
                            f"Plan:\n{plan}\n\n"
                            f"Board: https://nepooloperator.com/admin/escalations?key=…\n"
                        )
                        send_internal_alert(
                            f"[Ford Operator] {priority.upper()} · {one_line[:60]}",
                            body,
                        )
                        row.notified_at = _now()
                        db.commit()
                        notified += 1
                    except Exception:
                        log.exception("notify failed for %s", eid)

            processed += 1
        except Exception:
            errors += 1
            log.exception("process escalation %s failed", eid)
            try:
                with SessionLocal() as db:
                    row = db.get(EaEscalation, eid)
                    if row and row.status == "working":
                        # leave open so next tick retries
                        row.status = "open"
                        row.updated_at = _now()
                        row.agent_notes = (row.agent_notes or "") + "\n[worker error — will retry]"
                        db.commit()
            except Exception:
                pass

    return {"processed": processed, "errors": errors, "notified": notified, "batch": limit}


# ── REST admin API ──────────────────────────────────────────────────────────
class StatusIn(BaseModel):
    status: str = Field(..., description="open|working|needs_ford|done|dismissed")
    ford_note: Optional[str] = None


def _row_dict(r: EaEscalation) -> dict:
    cat = {}
    if r.category_json:
        try:
            cat = json.loads(r.category_json)
        except Exception:
            cat = {}
    return {
        "id": r.id,
        "tenant_id": r.tenant_id,
        "tenant_email": r.tenant_email,
        "session_id": r.session_id,
        "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
        "updated_at": r.updated_at.isoformat() + "Z" if r.updated_at else None,
        "status": r.status,
        "kind": r.kind,
        "priority": r.priority,
        "summary": r.summary,
        "user_said": r.user_said,
        "quiet": bool(r.quiet),
        "agent_notes": r.agent_notes,
        "proposed_plan": r.proposed_plan,
        "proposed_fix": r.proposed_fix,
        "one_line": cat.get("one_line"),
        "worked_at": r.worked_at.isoformat() + "Z" if r.worked_at else None,
        "notified_at": r.notified_at.isoformat() + "Z" if r.notified_at else None,
        "resolved_at": r.resolved_at.isoformat() + "Z" if r.resolved_at else None,
        "ford_note": r.ford_note,
    }


@router.get("/admin/escalations.json")
def list_escalations(
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        q = select(EaEscalation).order_by(EaEscalation.created_at.desc()).limit(limit)
        if status:
            q = select(EaEscalation).where(EaEscalation.status == status).order_by(
                EaEscalation.created_at.desc()
            ).limit(limit)
        rows = db.execute(q).scalars().all()
        counts = dict(
            db.execute(
                select(EaEscalation.status, func.count())
                .group_by(EaEscalation.status)
            ).all()
        )
    return {
        "as_of": _now().isoformat() + "Z",
        "counts": counts,
        "items": [_row_dict(r) for r in rows],
    }


@router.get("/admin/escalations/{eid}.json")
def get_escalation(
    eid: str,
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        row = db.get(EaEscalation, eid)
        if not row:
            raise HTTPException(404, "Not found")
        return _row_dict(row)


@router.patch("/admin/escalations/{eid}")
def patch_escalation(
    eid: str,
    body: StatusIn,
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    _check_admin(x_admin_key, key)
    st = (body.status or "").strip().lower()
    if st not in ("open", "working", "needs_ford", "done", "dismissed"):
        raise HTTPException(422, "invalid status")
    with SessionLocal() as db:
        row = db.get(EaEscalation, eid)
        if not row:
            raise HTTPException(404, "Not found")
        row.status = st
        row.updated_at = _now()
        if body.ford_note is not None:
            row.ford_note = body.ford_note[:4000]
        if st in ("done", "dismissed"):
            row.resolved_at = _now()
        db.commit()
        return _row_dict(row)


@router.post("/admin/escalations/run-worker")
def run_worker_now(
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
    limit: int = Query(default=5, ge=1, le=20),
):
    """Manual kick — process open queue now."""
    _check_admin(x_admin_key, key)
    return process_open_escalations(limit=limit)


@router.get("/admin/escalations", response_class=HTMLResponse)
def escalations_board(
    key: str | None = Query(default=None),
    x_admin_key: str | None = Header(default=None),
):
    """Live board Ford can leave open — polls JSON every 15s."""
    _check_admin(x_admin_key, key)
    # Escape key for embedding in page (query already validated)
    key_js = json.dumps(key or "")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ford Operator · Escalations</title>
<meta http-equiv="refresh" content="300">
<style>
  :root {{
    --bg:#0a0e14; --card:#121820; --line:rgba(255,255,255,.08);
    --ink:#e8eef7; --muted:#8b9bb4; --good:#3fd68a; --warn:#f5b942;
    --bad:#f87171; --sky:#5ec2ff; --chip:#1a2332;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
    background: radial-gradient(1200px 600px at 80% -10%, #16202e 0%, var(--bg) 55%);
    color:var(--ink); min-height:100vh; padding:20px 18px 48px;
  }}
  h1 {{ font-size:22px; font-weight:780; letter-spacing:-.02em; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:18px; }}
  .sub b {{ color:var(--sky); }}
  .bar {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; align-items:center; }}
  .pill {{
    background:var(--chip); border:1px solid var(--line); border-radius:999px;
    padding:6px 12px; font-size:12px; font-weight:700;
  }}
  .pill em {{ color:var(--sky); font-style:normal; }}
  button.act {{
    background:linear-gradient(180deg,#7ff0bb,#3fd68a); color:#06140d; border:0;
    border-radius:10px; padding:8px 14px; font-weight:780; font-size:13px; cursor:pointer;
  }}
  button.ghost {{
    background:transparent; color:var(--ink); border:1px solid var(--line);
    border-radius:10px; padding:8px 12px; font-weight:650; font-size:12.5px; cursor:pointer;
  }}
  .grid {{ display:grid; gap:12px; }}
  .card {{
    background:linear-gradient(165deg, rgba(255,255,255,.04), rgba(255,255,255,.02));
    border:1px solid var(--line); border-radius:16px; padding:14px 16px;
  }}
  .card.hi {{ border-color:rgba(248,113,113,.35); box-shadow:0 0 0 1px rgba(248,113,113,.12); }}
  .card.urgent {{ border-color:rgba(245,185,66,.45); }}
  .meta {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:8px; }}
  .tag {{
    font-size:10px; font-weight:800; letter-spacing:.06em; text-transform:uppercase;
    padding:3px 8px; border-radius:999px; background:var(--chip); color:var(--muted);
  }}
  .tag.open {{ color:#7dd3fc; }}
  .tag.working {{ color:var(--warn); }}
  .tag.needs_ford {{ color:var(--good); background:rgba(63,214,138,.12); }}
  .tag.done, .tag.dismissed {{ opacity:.7; }}
  .tag.p-urgent, .tag.p-high {{ color:var(--bad); }}
  .title {{ font-size:15.5px; font-weight:750; margin:0 0 6px; letter-spacing:-.01em; }}
  .sum {{ color:var(--muted); font-size:13px; line-height:1.45; white-space:pre-wrap; }}
  .plan {{
    margin-top:10px; padding:10px 12px; border-radius:12px;
    background:rgba(94,194,255,.06); border:1px solid rgba(94,194,255,.15);
    font-size:13px; line-height:1.45; white-space:pre-wrap;
  }}
  .plan b {{ color:var(--sky); font-size:11px; letter-spacing:.04em; text-transform:uppercase; }}
  .row-acts {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
  .empty {{ color:var(--muted); padding:40px 12px; text-align:center; }}
  .live {{ display:inline-flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); }}
  .live i {{
    width:8px; height:8px; border-radius:50%; background:var(--good);
    box-shadow:0 0 0 3px rgba(63,214,138,.2); display:inline-block;
  }}
  details {{ margin-top:8px; }}
  summary {{ cursor:pointer; color:var(--sky); font-size:12.5px; font-weight:650; }}
  a {{ color:var(--sky); }}
</style>
</head>
<body>
  <h1>Ford Operator</h1>
  <div class="sub">Standing Grok inbox for Energy Agent escalations · leave this tab open ·
    <span class="live"><i></i><span id="asof">connecting…</span></span>
  </div>
  <div class="bar" id="counts"></div>
  <div class="bar">
    <button class="act" type="button" id="btnRun">Run Grok worker now</button>
    <button class="ghost" type="button" id="btnRefresh">Refresh</button>
    <label class="pill" style="display:inline-flex;gap:8px;align-items:center;font-weight:650">
      Filter
      <select id="filt" style="background:transparent;border:0;color:var(--ink);font:inherit">
        <option value="">all active</option>
        <option value="open">open</option>
        <option value="working">working</option>
        <option value="needs_ford">needs Ford</option>
        <option value="done">done</option>
        <option value="dismissed">dismissed</option>
      </select>
    </label>
  </div>
  <div class="grid" id="list"><div class="empty">Loading…</div></div>
<script>
const KEY = {key_js};
const $ = (s) => document.querySelector(s);
let lastIds = new Set();
let first = true;

async function api(path, opts) {{
  opts = opts || {{}};
  const headers = Object.assign({{"Content-Type":"application/json"}}, opts.headers||{{}});
  if (KEY) headers["X-Admin-Key"] = KEY;
  const url = path + (path.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(KEY||"");
  const r = await fetch(url, Object.assign({{}}, opts, {{ headers }}));
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}}

function esc(s) {{
  return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}}

function card(it) {{
  const hi = it.priority === "urgent" || it.priority === "high";
  const title = it.one_line || it.summary || it.id;
  const plan = it.proposed_plan || it.agent_notes || "";
  const fix = it.proposed_fix || "";
  return `<article class="card ${{hi?"hi":""}} ${{it.priority==="urgent"?"urgent":""}}" data-id="${{esc(it.id)}}">
    <div class="meta">
      <span class="tag ${{esc(it.status)}}">${{esc(it.status)}}</span>
      <span class="tag p-${{esc(it.priority||"normal")}}">${{esc(it.priority||"normal")}}</span>
      ${{it.kind ? `<span class="tag">${{esc(it.kind)}}</span>` : ""}}
      <span class="tag">${{esc(it.tenant_email||it.tenant_id||"")}}</span>
      <span class="tag">${{esc((it.created_at||"").replace("T"," ").slice(0,16))}} UTC</span>
    </div>
    <h2 class="title">${{esc(title)}}</h2>
    <div class="sum">${{esc(it.summary||"")}}</div>
    ${{it.user_said ? `<details><summary>User said</summary><div class="sum">${{esc(it.user_said)}}</div></details>` : ""}}
    ${{plan ? `<div class="plan"><b>Plan</b><br>${{esc(plan)}}</div>` : ""}}
    ${{fix ? `<details open><summary>Proposed fix</summary><div class="plan">${{esc(fix)}}</div></details>` : ""}}
    <div class="row-acts">
      ${{it.status!=="done" ? `<button class="act" data-done="${{esc(it.id)}}">Mark done</button>` : ""}}
      ${{it.status!=="dismissed" ? `<button class="ghost" data-dismiss="${{esc(it.id)}}">Dismiss</button>` : ""}}
      ${{it.status==="done"||it.status==="dismissed" ? `<button class="ghost" data-reopen="${{esc(it.id)}}">Reopen</button>` : ""}}
      <span class="tag">${{esc(it.id)}}</span>
    </div>
  </article>`;
}}

async function load() {{
  const filt = $("#filt").value;
  let statusQ = filt;
  // default "all active" = not done/dismissed client-side
  const data = await api("/admin/escalations.json?limit=80" + (statusQ ? "&status="+encodeURIComponent(statusQ) : ""));
  $("#asof").textContent = "updated " + (data.as_of||"").replace("T"," ").slice(0,19) + " UTC";
  const c = data.counts || {{}};
  $("#counts").innerHTML = ["open","working","needs_ford","done","dismissed"].map(k =>
    `<span class="pill">${{k}} <em>${{c[k]||0}}</em></span>`
  ).join("");
  let items = data.items || [];
  if (!filt) items = items.filter(i => i.status!=="done" && i.status!=="dismissed");
  // Notify on NEW needs_ford (browser Notification if permitted)
  const nowIds = new Set(items.filter(i=>i.status==="needs_ford").map(i=>i.id));
  if (!first && typeof Notification !== "undefined" && Notification.permission === "granted") {{
    for (const id of nowIds) {{
      if (!lastIds.has(id)) {{
        const it = items.find(x=>x.id===id);
        new Notification("Ford Operator", {{ body: (it && (it.one_line||it.summary)) || id }});
      }}
    }}
  }}
  lastIds = nowIds;
  first = false;
  if (!items.length) {{
    $("#list").innerHTML = '<div class="empty">Inbox clear — Energy Agent escalations land here.</div>';
    return;
  }}
  $("#list").innerHTML = items.map(card).join("");
}}

$("#list").addEventListener("click", async (e) => {{
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  const id = t.getAttribute("data-done") || t.getAttribute("data-dismiss") || t.getAttribute("data-reopen");
  if (!id) return;
  let status = "done";
  if (t.hasAttribute("data-dismiss")) status = "dismissed";
  if (t.hasAttribute("data-reopen")) status = "open";
  await api("/admin/escalations/"+encodeURIComponent(id), {{
    method: "PATCH",
    body: JSON.stringify({{ status }}),
  }});
  load().catch(console.error);
}});

$("#btnRefresh").onclick = () => load().catch(alert);
$("#btnRun").onclick = async () => {{
  $("#btnRun").disabled = true;
  try {{
    const r = await api("/admin/escalations/run-worker?limit=5", {{ method: "POST", body: "{{}}" }});
    alert("Worker: processed "+r.processed+" · notified "+r.notified+" · errors "+r.errors);
    await load();
  }} catch (e) {{ alert(String(e)); }}
  $("#btnRun").disabled = false;
}};
$("#filt").onchange = () => load().catch(console.error);

if (typeof Notification !== "undefined" && Notification.permission === "default") {{
  Notification.requestPermission().catch(()=>{{}});
}}
load().catch(e => {{ $("#list").innerHTML = '<div class="empty">'+esc(String(e))+'</div>'; }});
setInterval(() => load().catch(()=>{{}}), 15000);
</script>
</body>
</html>"""
    return HTMLResponse(html)
