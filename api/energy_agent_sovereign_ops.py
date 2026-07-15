"""Sovereign operational authority — Ford-authorized product control plane.

Authority (2026-07-15 Ford):
  1. Feature queue: prioritize, assign, mark building/shipped without per-ticket sign-off
  2. Utility queue: advance researching/reviewed → adapter work / added
  3. Staged deploy + credential *metadata* access (not raw password dump to chat)
  4. Escalations needs_ford: propose fix + close unless blocked
  5. Memory / goals / agenda ownership (delegates to sovereign core)
  6. Job queue: stage + execute without manual intervention

Money/identity/hard-delete still blocked at capability layer.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select, func

log = logging.getLogger("energy_agent.sovereign.ops")


def _now() -> datetime:
    return datetime.utcnow()


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def ops_enabled() -> bool:
    """Full ops authority. Default ON after Ford's thorough authorization."""
    return _flag("SOVEREIGN_ENABLED", "1") and _flag("SOVEREIGN_OPS_AUTHORITY", "1")


# ── Features ────────────────────────────────────────────────────────────────
def list_features(db, *, status: str = "reviewed", limit: int = 50) -> list[dict]:
    from .feature_suggestions import FeatureSuggestion, VALID_STATUSES
    q = select(FeatureSuggestion).order_by(FeatureSuggestion.created_at.asc())
    if status and status != "all":
        if status not in VALID_STATUSES:
            status = "reviewed"
        q = q.where(FeatureSuggestion.status == status)
    rows = db.execute(q.limit(limit)).scalars().all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "text": (r.text or "")[:500],
            "email": r.email,
            "tenant_id": r.tenant_id,
            "review": (r.review or "")[:400] if r.review else None,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "has_screenshot": bool(r.screenshot_b64),
        }
        for r in rows
    ]


def set_feature_status(
    db,
    feature_id: int,
    status: str,
    *,
    review_note: str | None = None,
    actor: str = "sovereign",
) -> dict:
    from .feature_suggestions import FeatureSuggestion, VALID_STATUSES
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    status = (status or "").strip()
    if status not in VALID_STATUSES:
        return {"ok": False, "denied": True, "denied_reason": f"bad status {status}"}
    fs = db.get(FeatureSuggestion, int(feature_id))
    if not fs:
        return {"ok": False, "denied": True, "denied_reason": "not found"}
    prev = fs.status
    fs.status = status
    if review_note:
        fs.review = ((fs.review or "") + f"\n[{actor} {_now().isoformat()}Z] {review_note}").strip()[:20000]
        fs.reviewed_at = _now()
    elif status in ("reviewed", "building", "shipped") and not fs.reviewed_at:
        fs.reviewed_at = _now()
        if not fs.review:
            fs.review = f"[{actor}] status → {status}"
    db.flush()
    return {
        "ok": True,
        "id": fs.id,
        "from": prev,
        "status": fs.status,
        "text_preview": (fs.text or "")[:120],
    }


def bulk_feature_status(
    db,
    feature_ids: list[int],
    status: str,
    *,
    review_note: str | None = None,
) -> dict:
    results = []
    for fid in (feature_ids or [])[:50]:
        results.append(set_feature_status(db, fid, status, review_note=review_note))
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"ok": True, "updated": ok_n, "results": results}


def ship_reviewed_features(db, *, limit: int = 10, also_code_hire: bool = True) -> dict:
    """Prioritize oldest reviewed features → building (+ optional code job)."""
    from .feature_suggestions import FeatureSuggestion
    from .energy_agent_sovereign import act_code_hire

    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    rows = db.execute(
        select(FeatureSuggestion)
        .where(FeatureSuggestion.status == "reviewed")
        .order_by(FeatureSuggestion.created_at.asc())
        .limit(limit)
    ).scalars().all()
    shipped_prep = []
    for fs in rows:
        r = set_feature_status(
            db, fs.id, "building",
            review_note="Sovereign ops: promoted from reviewed → building (full ship authority).",
        )
        if r.get("ok") and also_code_hire:
            job = act_code_hire(
                db,
                title=f"Ship feature #{fs.id}",
                brief=(
                    f"Implement feature suggestion #{fs.id} for Array Operator.\n\n"
                    f"Owner ask:\n{(fs.text or '')[:3000]}\n\n"
                    f"Prior review:\n{(fs.review or '')[:1500]}\n\n"
                    "Ship a minimal correct product change. Prefer array-operator public/ "
                    "or solar-operator api/ as appropriate. When live, status can be marked shipped."
                ),
                kind="ship_feature",
            )
            r["code_job"] = job
        shipped_prep.append(r)
    return {"ok": True, "count": len(shipped_prep), "items": shipped_prep}


def mark_feature_shipped(db, feature_id: int, *, note: str | None = None) -> dict:
    return set_feature_status(
        db, feature_id, "shipped",
        review_note=note or "Sovereign ops: marked shipped (live authority).",
    )


def assign_feature(
    db,
    feature_id: int,
    *,
    assignee: str = "sovereign",
    priority_note: str | None = None,
    status: str | None = "building",
) -> dict:
    """Prioritize + assign a reviewed/new feature without Ford per-ticket sign-off."""
    note = f"Assigned to {assignee}."
    if priority_note:
        note += f" Priority: {priority_note}"
    if status:
        return set_feature_status(db, feature_id, status, review_note=note, actor=assignee)
    # status-less annotate
    return set_feature_status(
        db, feature_id, "reviewed", review_note=note, actor=assignee,
    )


def triage_feature_queue(db, *, limit: int = 20) -> dict:
    """Move new → reviewed with Sovereign triage notes (batch authority)."""
    from .feature_suggestions import FeatureSuggestion
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    rows = db.execute(
        select(FeatureSuggestion)
        .where(FeatureSuggestion.status == "new")
        .order_by(FeatureSuggestion.created_at.asc())
        .limit(limit)
    ).scalars().all()
    out = []
    for fs in rows:
        out.append(set_feature_status(
            db, fs.id, "reviewed",
            review_note="Sovereign triage: accepted into reviewed queue (full ship authority).",
        ))
    return {"ok": True, "triaged": len(out), "items": out}


# ── Utilities ───────────────────────────────────────────────────────────────
def list_utilities(db, *, status: str = "all", limit: int = 50) -> list[dict]:
    from .utility_requests import UtilityRequest, VALID_STATUSES
    q = select(UtilityRequest).order_by(UtilityRequest.created_at.asc())
    if status and status != "all":
        if status in VALID_STATUSES:
            q = q.where(UtilityRequest.status == status)
    rows = db.execute(q.limit(limit)).scalars().all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "state": r.state,
            "url": r.url,
            "status": r.status,
            "result": (r.result or "")[:600] if r.result else None,
            "email": r.email,
            "tenant_id": r.tenant_id,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
        }
        for r in rows
    ]


def set_utility_status(
    db,
    request_id: int,
    status: str,
    *,
    result_note: str | None = None,
) -> dict:
    from .utility_requests import UtilityRequest, VALID_STATUSES
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    status = (status or "").strip()
    if status not in VALID_STATUSES:
        return {"ok": False, "denied": True, "denied_reason": f"bad status {status}"}
    r = db.get(UtilityRequest, int(request_id))
    if not r:
        return {"ok": False, "denied": True, "denied_reason": "not found"}
    prev = r.status
    r.status = status
    r.reviewed_at = _now()
    if result_note:
        r.result = ((r.result or "") + f"\n[Sovereign {_now().isoformat()}Z] {result_note}").strip()[:20000]
    db.flush()
    return {"ok": True, "id": r.id, "name": r.name, "from": prev, "status": r.status}


def advance_utility_queue(db, *, limit: int = 5) -> dict:
    """Advance researching/reviewed items into adapter work (code hire) or added when smarthub-obvious."""
    from .utility_requests import UtilityRequest
    from .energy_agent_sovereign import act_code_hire

    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}

    rows = db.execute(
        select(UtilityRequest)
        .where(UtilityRequest.status.in_(["researching", "reviewed", "new"]))
        .order_by(UtilityRequest.created_at.asc())
        .limit(limit)
    ).scalars().all()

    out = []
    for r in rows:
        name_l = (r.name or "").lower()
        # Honest SmartHub path: many co-ops are SmartHub — stage as researching plan + code hire
        note = (
            f"Sovereign advance: portal research for {r.name!r} "
            f"(state={r.state or '-'}, url={r.url or '-'}). "
            "If SmartHub/NISC: wire via existing SmartHub registry. "
            "If bespoke: capture HAR before inventing adapter."
        )
        set_utility_status(db, r.id, "researching", result_note=note)
        job = act_code_hire(
            db,
            title=f"Utility adapter: {r.name}"[:200],
            brief=(
                f"Utility-add request #{r.id}: {r.name}\n"
                f"State: {r.state or 'unknown'}\nURL: {r.url or 'unknown'}\n"
                f"Note: {r.note or ''}\nPrior: {(r.result or '')[:1500]}\n\n"
                "Task: identify portal family; if SmartHub promote into registry; "
                "if bespoke write adapter plan only (no fabricated endpoints). "
                "Do not mark added without evidence."
            ),
            kind="utility_adapter",
        )
        out.append({"id": r.id, "name": r.name, "job": job})
    return {"ok": True, "advanced": len(out), "items": out}


def mark_utility_added(db, request_id: int, *, evidence: str) -> dict:
    """Only mark added with explicit evidence note (honest production)."""
    if not (evidence or "").strip():
        return {"ok": False, "denied": True, "denied_reason": "evidence required to mark added"}
    return set_utility_status(
        db, request_id, "added",
        result_note=f"Marked ADDED with evidence: {evidence.strip()[:2000]}",
    )


# ── Escalations ─────────────────────────────────────────────────────────────
def list_escalations(db, *, status: str = "needs_ford", limit: int = 40) -> list[dict]:
    from .ford_escalations import EaEscalation
    q = select(EaEscalation).order_by(EaEscalation.created_at.asc())
    if status and status != "all":
        q = q.where(EaEscalation.status == status)
    rows = db.execute(q.limit(limit)).scalars().all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "kind": r.kind,
            "priority": r.priority,
            "summary": (r.summary or "")[:500],
            "user_said": (r.user_said or "")[:400] if r.user_said else None,
            "proposed_plan": (r.proposed_plan or "")[:600] if r.proposed_plan else None,
            "proposed_fix": (r.proposed_fix or "")[:600] if r.proposed_fix else None,
            "tenant_id": r.tenant_id,
            "tenant_email": r.tenant_email,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
        }
        for r in rows
    ]


def resolve_escalation(
    db,
    escalation_id: str,
    *,
    status: str = "done",
    note: str | None = None,
    propose_only: bool = False,
) -> dict:
    from .ford_escalations import EaEscalation
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    status = (status or "done").strip().lower()
    if status not in ("open", "working", "needs_ford", "done", "dismissed"):
        return {"ok": False, "denied": True, "denied_reason": "bad status"}
    row = db.get(EaEscalation, escalation_id)
    if not row:
        return {"ok": False, "denied": True, "denied_reason": "not found"}

    # Ford block list in memory
    from .energy_agent_sovereign import memory_get_all
    blocked = set()
    for m in memory_get_all(db, limit=100):
        if m.get("key") == "escalation_blocklist":
            try:
                blocked = set(json.loads(m.get("value") or "[]"))
            except Exception:
                blocked = set()
    if row.id in blocked:
        return {"ok": False, "denied": True, "denied_reason": "Ford blocked this escalation id"}

    if propose_only:
        row.proposed_fix = (note or row.proposed_fix or "Sovereign proposes fix")[:4000]
        row.agent_notes = ((row.agent_notes or "") + f"\n[Sovereign propose {_now().isoformat()}Z] {note or ''}").strip()[:8000]
        row.updated_at = _now()
        db.flush()
        return {"ok": True, "id": row.id, "status": row.status, "proposed": True}

    prev = row.status
    row.status = status
    row.updated_at = _now()
    if note:
        row.ford_note = ((row.ford_note or "") + f"\n[Sovereign] {note}").strip()[:4000]
        row.proposed_fix = (note)[:4000]
    if status in ("done", "dismissed"):
        row.resolved_at = _now()
    db.flush()
    return {"ok": True, "id": row.id, "from": prev, "status": row.status}


def auto_resolve_needs_ford(db, *, limit: int = 5) -> dict:
    """Propose fix + close needs_ford unless blocked; queue code hire when build-shaped."""
    from .energy_agent_sovereign import act_code_hire
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    items = list_escalations(db, status="needs_ford", limit=limit)
    results = []
    for it in items:
        summary = it.get("summary") or ""
        plan = it.get("proposed_plan") or it.get("proposed_fix") or ""
        fix_note = (
            f"Sovereign resolution authority: closing needs_ford.\n"
            f"Summary: {summary[:500]}\n"
            f"Plan: {plan[:500] or 'Documented; no further Ford decision required for now.'}"
        )
        # If it looks like a product build, also code-hire
        code = None
        if any(k in (summary + plan).lower() for k in ("bug", "fix", "ui", "feature", "broken", "error", "ship")):
            code = act_code_hire(
                db,
                title=f"Escalation {it['id'][:16]}",
                brief=f"Escalation {it['id']}\n{summary}\n\nUser said:\n{it.get('user_said') or ''}\n\nPlan:\n{plan}",
                kind="escalation_fix",
            )
        res = resolve_escalation(db, it["id"], status="done", note=fix_note)
        res["code_job"] = code
        results.append(res)
    closed = sum(1 for r in results if r.get("ok") and r.get("status") == "done")
    return {
        "ok": True,
        "resolved": closed,
        "attempted": len(results),
        "items": results,
    }


# ── Staged deploy (succession gap closer) ───────────────────────────────────
def stage_deploy(
    db,
    *,
    repo: str = "both",
    reason: str = "Sovereign staged deploy authority",
    execute_now: bool = False,
) -> dict:
    """Stage (and optionally run) deploy for array-operator / solar-operator.

    Staged = durable memory + note + optional code job. Does not touch money/identity.
    Live Netlify/Railway execution only when SOVEREIGN_CODE_DEPLOY=1 and execute_now.
    """
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    from .energy_agent_sovereign import memory_set, write_note, act_code_hire, audit

    repo = (repo or "both").strip().lower()
    if repo not in ("array-operator", "solar-operator", "both", "ao", "so"):
        return {"ok": False, "denied": True, "denied_reason": f"bad repo {repo}"}
    if repo == "ao":
        repo = "array-operator"
    if repo == "so":
        repo = "solar-operator"

    payload = {
        "repo": repo,
        "reason": (reason or "")[:500],
        "staged_at": _now().isoformat() + "Z",
        "status": "staged",
        "execute_now": bool(execute_now),
    }
    memory_set(db, "deploy_stage", json.dumps(payload), source="ops")
    write_note(
        db, kind="decision", title="deploy staged",
        body=json.dumps(payload, default=str),
        provider="ops",
    )
    audit(
        db, capability="act.deploy_stage", decision="act",
        rationale=reason[:300], targets=payload, result="ok",
    )

    result: dict[str, Any] = {"ok": True, "staged": True, "repo": repo, "payload": payload}
    if execute_now:
        try:
            from .energy_agent_sovereign_worker import code_deploy_enabled, deploy_repo
            if not code_deploy_enabled():
                result["execute"] = {"ok": False, "skipped": True, "reason": "SOVEREIGN_CODE_DEPLOY off"}
            else:
                repos = (
                    ["array-operator", "solar-operator"]
                    if repo == "both"
                    else [repo]
                )
                deploys = {}
                for rname in repos:
                    deploys[rname] = deploy_repo(rname)
                result["execute"] = {"ok": all(d.get("ok") for d in deploys.values()), "deploys": deploys}
                payload["status"] = "executed" if result["execute"]["ok"] else "execute_failed"
                memory_set(db, "deploy_stage", json.dumps(payload), source="ops")
        except Exception as e:  # noqa: BLE001
            result["execute"] = {"ok": False, "error": str(e)[:300]}
    else:
        # Queue a scoped ship job that ends in push/deploy when worker runs
        job = act_code_hire(
            db,
            title=f"Staged deploy: {repo}",
            brief=(
                f"Staged deploy authority for {repo}.\n"
                f"Reason: {reason}\n"
                "If there are uncommitted Sovereign/ops changes, finish them, commit, push main, "
                "and let deploy path run (Netlify for array-operator, Railway for solar-operator). "
                "No money/identity changes."
            ),
            kind="staged_deploy",
        )
        result["code_job"] = job
    return result


# ── Memory / goals / agenda ownership ───────────────────────────────────────
def own_memory_write(db, key: str, value: str, *, source: str = "ops") -> dict:
    if not ops_enabled() and source != "ford":
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    from .energy_agent_sovereign import memory_set
    if not (key or "").strip():
        return {"ok": False, "denied": True, "denied_reason": "empty key"}
    memory_set(db, str(key)[:120], str(value)[:8000], source=source)
    return {"ok": True, "key": key}


def own_agenda(db, agenda: list[dict]) -> dict:
    """Reprioritize / upsert goals — Sovereign owns the product spine offline."""
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    from .energy_agent_sovereign import apply_agenda, write_note
    n = apply_agenda(db, agenda or [])
    write_note(
        db, kind="agenda", title="agenda ownership",
        body=json.dumps({"updated": n, "items": (agenda or [])[:20]}, default=str)[:4000],
        provider="ops",
    )
    return {"ok": True, "updated": n}


def reprioritize_goals(db, updates: list[dict]) -> dict:
    """Batch set goal priority/status. Each item: {id|title, priority?, status?, note?}."""
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    from .energy_agent_sovereign import EaSovereignGoal, apply_agenda
    # Prefer apply_agenda which upserts by id
    cleaned = []
    for u in (updates or [])[:40]:
        if not isinstance(u, dict):
            continue
        item = {
            "id": u.get("id"),
            "title": u.get("title") or u.get("id") or "goal",
            "priority": int(u.get("priority") if u.get("priority") is not None else 50),
            "status": u.get("status") or "open",
            "note": u.get("note") or "reprioritized by Sovereign ops",
        }
        cleaned.append(item)
    n = apply_agenda(db, cleaned)
    # Also bump any existing goals by id if apply_agenda missed fields
    for item in cleaned:
        gid = item.get("id")
        if not gid:
            continue
        row = db.get(EaSovereignGoal, str(gid)[:40])
        if row:
            row.priority = int(item["priority"])
            row.status = str(item["status"])[:16]
            row.updated_at = _now()
    db.flush()
    return {"ok": True, "updated": n, "items": cleaned}


# ── Credentials (metadata + staged use) ─────────────────────────────────────
def list_credential_inventory(db, *, limit: int = 100) -> dict:
    """Metadata only — never return decrypted passwords to desk/chat."""
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    out: dict[str, Any] = {"credentials": [], "portal_status": [], "note": "passwords never exposed"}
    try:
        from .harvester import credentials as cc
        # Prefer a list API if present
        if hasattr(cc, "list_all_meta"):
            out["credentials"] = cc.list_all_meta(db, limit=limit)
        elif hasattr(cc, "list_credentials"):
            raw = cc.list_credentials(db)  # type: ignore
            out["credentials"] = [
                {k: v for k, v in (row.items() if isinstance(row, dict) else {}.items())
                 if k not in ("password", "password_enc", "secret", "token")}
                for row in (raw or [])
            ][:limit]
    except Exception as e:  # noqa: BLE001
        out["credentials_error"] = str(e)[:200]
    try:
        from .models import PortalLoginStatus  # type: ignore
        rows = db.execute(select(PortalLoginStatus).limit(limit)).scalars().all()
        out["portal_status"] = [
            {
                "tenant_id": getattr(r, "tenant_id", None),
                "provider": getattr(r, "provider", None) or getattr(r, "code", None),
                "username": getattr(r, "username", None),
                "status": getattr(r, "status", None) or getattr(r, "health", None),
                "updated_at": str(getattr(r, "updated_at", None) or ""),
            }
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001
        out["portal_status_error"] = str(e)[:200]
    return {"ok": True, **out}


def stage_credential_harvest(db, *, tenant_id: str | None = None, provider: str | None = None) -> dict:
    """Stage a harvest/re-arm request — does not print secrets."""
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    from .energy_agent_sovereign import memory_set, write_note
    key = f"harvest_stage:{provider or 'all'}:{tenant_id or 'fleet'}"
    memory_set(
        db, key,
        json.dumps({
            "tenant_id": tenant_id,
            "provider": provider,
            "staged_at": _now().isoformat() + "Z",
            "status": "staged",
        }),
        source="ops",
    )
    write_note(
        db, kind="decision",
        title="credential harvest staged",
        body=f"tenant={tenant_id} provider={provider}",
        provider="ops",
    )
    # Best-effort re-arm if harvester supports it
    try:
        from .harvester import credentials as cc
        if tenant_id and provider and hasattr(cc, "rearm"):
            cc.rearm(db, tenant_id, provider)  # type: ignore
            return {"ok": True, "rearmed": True, "tenant_id": tenant_id, "provider": provider}
        if hasattr(cc, "rearm_all") and not tenant_id:
            n = cc.rearm_all(db)  # type: ignore
            return {"ok": True, "rearmed_all": n}
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "staged": True, "rearm_error": str(e)[:200]}
    return {"ok": True, "staged": True, "key": key}


# ── Jobs ────────────────────────────────────────────────────────────────────
def list_jobs(db, *, status: str = "queued", limit: int = 30) -> list[dict]:
    from .energy_agent_sovereign import EaSovereignJob
    q = select(EaSovereignJob).order_by(EaSovereignJob.created_at.desc())
    if status and status != "all":
        q = q.where(EaSovereignJob.status == status)
    rows = db.execute(q.limit(limit)).scalars().all()
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "status": r.status,
            "title": r.title,
            "error": r.error,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "finished_at": r.finished_at.isoformat() + "Z" if r.finished_at else None,
        }
        for r in rows
    ]


def cancel_job(db, job_id: str) -> dict:
    from .energy_agent_sovereign import EaSovereignJob
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    job = db.get(EaSovereignJob, job_id)
    if not job:
        return {"ok": False, "denied": True, "denied_reason": "not found"}
    if job.status not in ("queued", "running"):
        return {"ok": False, "denied": True, "denied_reason": f"status={job.status}"}
    job.status = "cancelled"
    job.finished_at = _now()
    job.error = "cancelled by sovereign ops"
    db.flush()
    return {"ok": True, "id": job.id, "status": "cancelled"}


def execute_jobs_now(db, *, limit: int = 2) -> dict:
    from .energy_agent_sovereign_worker import drain_jobs, code_live_enabled
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    if not code_live_enabled():
        return {"ok": False, "denied": True, "denied_reason": "code live off"}
    return drain_jobs(db, limit=limit)


# ── Ops summary + autonomous sweep ──────────────────────────────────────────
def ops_summary(db) -> dict:
    from .feature_suggestions import FeatureSuggestion
    from .utility_requests import UtilityRequest
    from .ford_escalations import EaEscalation
    from .energy_agent_sovereign import EaSovereignJob

    def count(model, status=None, field="status"):
        q = select(func.count()).select_from(model)
        if status is not None:
            q = q.where(getattr(model, field) == status)
        return int(db.execute(q).scalar() or 0)

    return {
        "ok": True,
        "ops_authority": ops_enabled(),
        "features": {
            "new": count(FeatureSuggestion, "new"),
            "reviewed": count(FeatureSuggestion, "reviewed"),
            "building": count(FeatureSuggestion, "building"),
            "shipped": count(FeatureSuggestion, "shipped"),
        },
        "utilities": {
            "new": count(UtilityRequest, "new"),
            "researching": count(UtilityRequest, "researching"),
            "reviewed": count(UtilityRequest, "reviewed"),
            "added": count(UtilityRequest, "added"),
            "declined": count(UtilityRequest, "declined"),
        },
        "escalations": {
            "open": count(EaEscalation, "open"),
            "working": count(EaEscalation, "working"),
            "needs_ford": count(EaEscalation, "needs_ford"),
            "done": count(EaEscalation, "done"),
        },
        "jobs": {
            "queued": count(EaSovereignJob, "queued"),
            "running": count(EaSovereignJob, "running"),
            "done": count(EaSovereignJob, "done"),
            "failed": count(EaSovereignJob, "failed"),
        },
    }


def autonomous_ops_sweep(db) -> dict:
    """One-shot ops leadership sweep when authority is on.

    - Triage new features → reviewed
    - Promote reviewed features → building + code jobs
    - Advance utility researching/reviewed into adapter jobs
    - Auto-resolve needs_ford escalations (unless blocked)
    - Drain up to 1 code job
    - Refresh succession memory snapshot
    """
    if not ops_enabled():
        return {"ok": False, "denied": True, "denied_reason": "ops authority off"}
    from .energy_agent_sovereign import write_note, audit, memory_set

    results = {
        "triage": triage_feature_queue(db, limit=15),
        "features": ship_reviewed_features(db, limit=5, also_code_hire=True),
        "utilities": advance_utility_queue(db, limit=4),
        "escalations": auto_resolve_needs_ford(db, limit=5),
        "jobs": execute_jobs_now(db, limit=1),
        "summary": ops_summary(db),
    }
    # Durable succession snapshot so Sovereign runs the desk offline
    memory_set(
        db, "ops_last_sweep",
        json.dumps({
            "at": _now().isoformat() + "Z",
            "features_promoted": results["features"].get("count"),
            "features_triaged": results["triage"].get("triaged"),
            "utilities": results["utilities"].get("advanced"),
            "escalations": results["escalations"].get("resolved"),
            "jobs": results["jobs"].get("processed"),
            "summary": results["summary"],
        }, default=str)[:8000],
        source="ops",
    )
    memory_set(
        db, "ops_authority",
        "full: features ship, utilities advance, escalations resolve, "
        "credentials stage, jobs drain, memory/agenda own, deploy stage",
        source="ops",
    )
    write_note(
        db, kind="decision", title="autonomous ops sweep",
        body=json.dumps({
            "triage": results["triage"].get("triaged"),
            "features": results["features"].get("count"),
            "utilities": results["utilities"].get("advanced"),
            "escalations": results["escalations"].get("resolved"),
            "jobs": results["jobs"].get("processed"),
        }, default=str),
        provider="ops",
    )
    audit(
        db, capability="act.product_queue", decision="act",
        rationale="autonomous ops sweep",
        targets=results["summary"],
        result="ok",
    )
    return {"ok": True, **results}
