"""Sovereign skill evolution — Hermes-style closed learning loop.

Pattern (Nous Hermes Agent + agentskills.io):
  • Skills are durable Markdown procedures (YAML frontmatter + body)
  • Progressive disclosure: cortex always sees a compact INDEX; loads full body
    only for matched skills
  • Create after complex successful work (job done, ops win, multi-step recovery)
  • Patch/improve when the same class of work fails then recovers
  • Curator prunes dead/low-value skills (janitor ≠ evolution)
  • Self-evolution: LLM rewrites skill body from real traces (not DSPy/GEPA —
    we use Grok/Claude already on the box; same observe→codify→reuse loop)

NOT persona reprogramming (mind_propose still needs Ford). Skills are procedural
playbooks Sovereign can use every tick without approval.

Kill: SOVEREIGN_SKILLS=0
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import SessionLocal
from .models import Base

log = logging.getLogger("energy_agent.sovereign.skills")

_SEED_VERSION = 1


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _now() -> datetime:
    return datetime.utcnow()


def _id(prefix: str = "skl") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def skills_enabled() -> bool:
    try:
        from .energy_agent_sovereign import sovereign_enabled
        if not sovereign_enabled():
            return False
    except Exception:
        return False
    return _flag("SOVEREIGN_SKILLS", "1")


def evolution_enabled() -> bool:
    """LLM rewrite of skills. Default ON with skills. Kill: SOVEREIGN_SKILL_EVOLVE=0."""
    return skills_enabled() and _flag("SOVEREIGN_SKILL_EVOLVE", "1")


# ── Model ────────────────────────────────────────────────────────────────────
class EaSovereignSkill(Base):
    """Reusable procedural skill (SKILL.md analogue) written by Sovereign."""
    __tablename__ = "ea_sovereign_skills"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    # stable slug for matching / progressive disclosure
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(240), default="")
    # short description always shown in cortex index (< 200 chars ideal)
    description: Mapped[str] = mapped_column(String(400), default="")
    # full markdown body (procedure)
    body: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(40), default="ops", index=True)
    # active | deprecated | draft
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # traces that created / last improved this skill
    source: Mapped[str] = mapped_column(String(40), default="seed")  # seed|job|ops|desk|evolve|curator
    source_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_evolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quality: Mapped[float] = mapped_column(Float, default=0.5)  # 0–1 curator score
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    meta_json: Mapped[str] = mapped_column(Text, default="{}")


def ensure_skill_tables(db=None) -> None:
    try:
        if db is not None:
            bind = db.get_bind()
        else:
            from .db import engine
            bind = engine
        Base.metadata.create_all(bind=bind, tables=[EaSovereignSkill.__table__])
    except Exception:
        log.exception("ea_sovereign_skills table create failed")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s or "skill")[:100]


def serialize_skill(s: EaSovereignSkill, *, full: bool = False) -> dict:
    try:
        tags = json.loads(s.tags_json or "[]")
    except Exception:
        tags = []
    out = {
        "id": s.id,
        "name": s.name,
        "title": s.title,
        "description": s.description,
        "category": s.category,
        "status": s.status,
        "version": s.version,
        "source": s.source,
        "source_ref": s.source_ref,
        "use_count": s.use_count,
        "success_count": s.success_count,
        "fail_count": s.fail_count,
        "quality": s.quality,
        "tags": tags,
        "created_at": s.created_at.isoformat() + "Z" if s.created_at else None,
        "updated_at": s.updated_at.isoformat() + "Z" if s.updated_at else None,
        "last_used_at": s.last_used_at.isoformat() + "Z" if s.last_used_at else None,
        "last_evolved_at": s.last_evolved_at.isoformat() + "Z" if s.last_evolved_at else None,
    }
    if full:
        out["body"] = s.body
        try:
            out["meta"] = json.loads(s.meta_json or "{}")
        except Exception:
            out["meta"] = {}
    return out


# ── Seed skills (bootstrap playbooks) ────────────────────────────────────────
_SEED_SKILLS: list[dict[str, Any]] = [
    {
        "name": "utility-queue-advance",
        "title": "Advance utility-add queue honestly",
        "description": "Move new→researching→reviewed without inventing portal adapters.",
        "category": "ops",
        "tags": ["utility", "queue", "portal"],
        "body": """# Utility queue advance

## When
Utility-add requests sit in `new` / `researching` / `reviewed`.

## Steps
1. List queue via digests (`utility_new`, `utility_researching`, `utility_reviewed`).
2. For `new`: `utility_advance` or set researching with a real next step (HAR, login path, docs).
3. Never mark `added` without evidence (working capture, HAR, or live adapter test).
4. If portal is private/HAR-gated: stage `utility_cred_stage` / `har_stage`, tell Ford crisply.
5. Prefer one honest research note over three empty status flips.

## Anti-patterns
- Fabricating endpoints from brand names
- Treating demo tenants as real utility demand
""",
    },
    {
        "name": "feature-ship-routine",
        "title": "Ship routine feature suggestions",
        "description": "Triage reviewed features; ship pure UI; code-hire real product work.",
        "category": "ops",
        "tags": ["feature", "ship", "code"],
        "body": """# Feature ship routine

## When
`feature_reviewed` or `feature_building` is hot.

## Steps
1. `feature_triage` / `ops_sweep` when multiple queues hot.
2. Pure UI/copy/color: prefer site-improve path or `feature_ship` if already built.
3. Real product code: `code_hire` with a scoped brief → worker push/deploy.
4. After job done matching `feature #N`, worker auto-marks shipped — verify note.
5. Escalate to Ford only for brand/money/product pivots, not routine polish.

## Anti-patterns
- Shipping half-built code as "done"
- Emailing Ford a job id dump
""",
    },
    {
        "name": "code-job-recover",
        "title": "Recover failed or stuck code jobs",
        "description": "Requeue transient failures; fix repo/clone issues; never burn budget on permanent denies.",
        "category": "worker",
        "tags": ["jobs", "timeout", "git"],
        "body": """# Code job recovery

## When
Jobs `failed` or stuck `running`; digests show `sovereign_jobs_queued` stalling.

## Steps
1. Classify error: timeout/network/clone → requeue; money/destructive deny → leave failed.
2. `jobs_requeue` then `jobs_drain` (limit small).
3. If "no sovereign repos" / git missing: note succession gap; watchdog may soft-reboot.
4. After 2+ same failure class: evolve this skill with the concrete error line.
5. Desk Ford only for true blockers (missing secrets, policy), not every retry.

## Anti-patterns
- Infinite requeue of permanent denies
- Claiming deploy success without push_main ok
""",
    },
    {
        "name": "desk-partnership",
        "title": "Talk to Ford on the Sovereign desk",
        "description": "High-agency desk messages; email only for strategy; never owner EA chat.",
        "category": "leadership",
        "tags": ["desk", "ford", "email"],
        "body": """# Desk partnership

## When
Need Ford judgment, report a real win, or succession ask.

## Steps
1. Channel = Sovereign desk (`speak`), never Energy Agent owner chat.
2. One crisp ask or one clear status — leadership tone.
3. Email only high-level strategy; never job queues / feature dumps.
4. Weekly digest owns async check-in cadence.

## Anti-patterns
- Notification spam
- Treating demo glitches as customer emergencies
""",
    },
    {
        "name": "watchdog-soft-reboot",
        "title": "Survive layer failure via dual-channel recovery",
        "description": "When sub/cortex/jobs go stale, let watchdog soft-reboot; respect storm breaker.",
        "category": "reliability",
        "tags": ["watchdog", "durability", "storm"],
        "body": """# Watchdog soft reboot

## When
healthz shows subconscious_stale, cortex_stale, jobs_running_stuck, fail bursts.

## Steps
1. Trust recovery channel — do not thrash primary ticks.
2. Soft reboot requeues stuck running jobs, forces sub, then cortex if needed.
3. If storm_breaker_open: cool down; only admin force reboot.
4. Write a system note; continue agenda without fake urgency.

## Anti-patterns
- Manual force-reboot loops while storm is open
- Ignoring stuck running jobs for hours
""",
    },
]


def seed_skills(db) -> dict[str, Any]:
    ensure_skill_tables(db)
    created = 0
    skipped = 0
    for spec in _SEED_SKILLS:
        name = _slug(spec["name"])
        existing = db.execute(
            select(EaSovereignSkill).where(EaSovereignSkill.name == name)
        ).scalars().first()
        if existing:
            skipped += 1
            continue
        row = EaSovereignSkill(
            id=_id("skl"),
            name=name,
            title=spec.get("title") or name,
            description=(spec.get("description") or "")[:400],
            body=spec.get("body") or "",
            category=spec.get("category") or "ops",
            status="active",
            version=_SEED_VERSION,
            source="seed",
            quality=0.7,
            tags_json=json.dumps(spec.get("tags") or []),
            meta_json=json.dumps({"seed": True}),
        )
        db.add(row)
        created += 1
    if created:
        db.flush()
    return {"ok": True, "created": created, "skipped": skipped}


def list_skills(
    db,
    *,
    status: str = "active",
    limit: int = 50,
    category: str | None = None,
) -> list[EaSovereignSkill]:
    ensure_skill_tables(db)
    q = select(EaSovereignSkill).order_by(
        EaSovereignSkill.quality.desc(),
        EaSovereignSkill.use_count.desc(),
    )
    if status and status != "all":
        q = q.where(EaSovereignSkill.status == status)
    if category:
        q = q.where(EaSovereignSkill.category == category)
    return list(db.execute(q.limit(min(max(limit, 1), 100))).scalars().all())


def skill_index(db, *, limit: int = 24) -> list[dict]:
    """Progressive disclosure: compact list for cortex system context."""
    rows = list_skills(db, status="active", limit=limit)
    return [
        {
            "name": r.name,
            "title": r.title,
            "description": r.description,
            "category": r.category,
            "version": r.version,
            "quality": r.quality,
            "tags": json.loads(r.tags_json or "[]") if r.tags_json else [],
        }
        for r in rows
    ]


def get_skill(db, name_or_id: str) -> EaSovereignSkill | None:
    ensure_skill_tables(db)
    row = db.get(EaSovereignSkill, name_or_id)
    if row:
        return row
    slug = _slug(name_or_id)
    return db.execute(
        select(EaSovereignSkill).where(EaSovereignSkill.name == slug)
    ).scalars().first()


def match_skills(db, text: str, *, limit: int = 4) -> list[EaSovereignSkill]:
    """Keyword match for loading full bodies into cortex when relevant."""
    t = (text or "").lower()
    if not t.strip():
        return []
    rows = list_skills(db, status="active", limit=40)
    scored: list[tuple[float, EaSovereignSkill]] = []
    for r in rows:
        score = 0.0
        blob = f"{r.name} {r.title} {r.description} {r.tags_json} {r.category}".lower()
        for token in re.findall(r"[a-z0-9]{3,}", t):
            if token in blob:
                score += 1.0
            if token in (r.body or "").lower()[:2000]:
                score += 0.35
        # slight prior for quality/use
        score += float(r.quality or 0) * 0.5
        score += min(3.0, (r.use_count or 0) * 0.05)
        if score > 0.8:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def load_skills_for_context(db, *, heat_text: str = "", limit: int = 3) -> dict[str, Any]:
    """Index always + full bodies for matched skills (Hermes progressive disclosure)."""
    if not skills_enabled():
        return {"enabled": False, "index": [], "loaded": []}
    try:
        seed_skills(db)
    except Exception as e:  # noqa: BLE001
        log.debug("seed_skills: %s", e)
    idx = skill_index(db, limit=24)
    matched = match_skills(db, heat_text, limit=limit)
    loaded = []
    for r in matched:
        r.use_count = int(r.use_count or 0) + 1
        r.last_used_at = _now()
        loaded.append({
            "name": r.name,
            "title": r.title,
            "version": r.version,
            "body": (r.body or "")[:6000],
        })
    if matched:
        try:
            db.flush()
        except Exception:
            pass
    return {
        "enabled": True,
        "index": idx,
        "loaded": loaded,
        "instruction": (
            "skills.index = playbooks you have written/evolved. "
            "skills.loaded = full procedures matched to this cycle — FOLLOW them. "
            "After multi-step wins or recovered failures, evolution will codify new skills."
        ),
    }


def upsert_skill(
    db,
    *,
    name: str,
    title: str,
    description: str,
    body: str,
    category: str = "ops",
    tags: list[str] | None = None,
    source: str = "evolve",
    source_ref: str | None = None,
    meta: dict | None = None,
) -> EaSovereignSkill:
    ensure_skill_tables(db)
    slug = _slug(name)
    row = db.execute(
        select(EaSovereignSkill).where(EaSovereignSkill.name == slug)
    ).scalars().first()
    if row:
        # Patch: bump version, merge body if meaningfully longer/better
        row.title = (title or row.title)[:240]
        row.description = (description or row.description)[:400]
        if body and len(body.strip()) > 40:
            # Prefer evolved body if substantially different
            if body.strip() != (row.body or "").strip():
                row.body = body[:20000]
                row.version = int(row.version or 1) + 1
                row.last_evolved_at = _now()
        row.category = (category or row.category)[:40]
        if tags:
            row.tags_json = json.dumps(tags[:12])
        row.source = source[:40]
        row.source_ref = (source_ref or row.source_ref or "")[:80] or None
        row.updated_at = _now()
        row.status = "active"
        if meta:
            try:
                old = json.loads(row.meta_json or "{}")
            except Exception:
                old = {}
            old.update(meta)
            row.meta_json = json.dumps(old, default=str)[:8000]
        db.flush()
        return row
    row = EaSovereignSkill(
        id=_id("skl"),
        name=slug,
        title=(title or slug)[:240],
        description=(description or "")[:400],
        body=(body or "")[:20000],
        category=(category or "ops")[:40],
        status="active",
        version=1,
        source=source[:40],
        source_ref=(source_ref or "")[:80] or None,
        quality=0.55,
        tags_json=json.dumps((tags or [])[:12]),
        meta_json=json.dumps(meta or {}, default=str)[:8000],
        last_evolved_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def record_skill_outcome(db, name: str, *, success: bool) -> None:
    row = get_skill(db, name)
    if not row:
        return
    if success:
        row.success_count = int(row.success_count or 0) + 1
        row.quality = min(1.0, float(row.quality or 0.5) + 0.03)
    else:
        row.fail_count = int(row.fail_count or 0) + 1
        row.quality = max(0.05, float(row.quality or 0.5) - 0.05)
    row.updated_at = _now()
    db.flush()


# ── Trace harvest ────────────────────────────────────────────────────────────
def harvest_traces(db, *, hours: float = 24, limit: int = 40) -> list[dict]:
    """Collect job + note + action traces for evolution."""
    from .energy_agent_sovereign import EaSovereignJob, EaSovereignNote, EaSovereignAction

    cutoff = _now() - timedelta(hours=hours)
    traces: list[dict] = []

    try:
        jobs = db.execute(
            select(EaSovereignJob)
            .where(EaSovereignJob.finished_at >= cutoff)
            .order_by(EaSovereignJob.finished_at.desc())
            .limit(limit)
        ).scalars().all()
        for j in jobs:
            traces.append({
                "kind": "job",
                "status": j.status,
                "title": j.title,
                "error": (j.error or "")[:400],
                "id": j.id,
                "at": j.finished_at.isoformat() + "Z" if j.finished_at else None,
                "result_excerpt": (j.result_json or "")[:500],
            })
    except Exception as e:  # noqa: BLE001
        log.debug("harvest jobs: %s", e)

    try:
        notes = db.execute(
            select(EaSovereignNote)
            .where(
                EaSovereignNote.created_at >= cutoff,
                EaSovereignNote.kind.in_(("decision", "system", "observation")),
            )
            .order_by(EaSovereignNote.created_at.desc())
            .limit(limit)
        ).scalars().all()
        for n in notes:
            traces.append({
                "kind": "note",
                "note_kind": n.kind,
                "title": n.title,
                "body": (n.body or "")[:600],
                "at": n.created_at.isoformat() + "Z" if n.created_at else None,
            })
    except Exception as e:  # noqa: BLE001
        log.debug("harvest notes: %s", e)

    try:
        acts = db.execute(
            select(EaSovereignAction)
            .where(EaSovereignAction.created_at >= cutoff)
            .order_by(EaSovereignAction.created_at.desc())
            .limit(limit)
        ).scalars().all()
        for a in acts:
            traces.append({
                "kind": "action",
                "capability": a.capability,
                "decision": a.decision,
                "result": a.result,
                "rationale": (a.rationale or "")[:300],
                "at": a.created_at.isoformat() + "Z" if a.created_at else None,
            })
    except Exception as e:  # noqa: BLE001
        log.debug("harvest actions: %s", e)

    return traces[: limit * 2]


def _deterministic_from_job(job: dict) -> dict | None:
    """Rule-based skill draft when LLM off or failed."""
    title = (job.get("title") or "job").strip()
    status = job.get("status")
    err = (job.get("error") or "").strip()
    if status == "done":
        name = _slug(f"job-win-{title}")[:80]
        return {
            "name": name,
            "title": f"Repeat: {title[:80]}",
            "description": f"Successful code/ops job pattern: {title[:120]}",
            "category": "worker",
            "tags": ["job", "success"],
            "body": (
                f"# {title[:120]}\n\n"
                f"## When\nSimilar work to job `{job.get('id')}` that completed successfully.\n\n"
                f"## Steps\n1. Reuse the brief shape from: {title}\n"
                f"2. Prefer the same repo/path choices that shipped.\n"
                f"3. Verify push/deploy before calling done.\n"
                f"4. Auto-link feature/utility ids in the title when present.\n\n"
                f"## Trace\n```\n{(job.get('result_excerpt') or '')[:800]}\n```\n"
            ),
            "source": "job",
            "source_ref": job.get("id"),
        }
    if status == "failed" and err:
        name = _slug(f"job-fail-{err[:40]}")[:80]
        return {
            "name": name,
            "title": f"Recover: {err[:80]}",
            "description": f"How to recover when jobs fail with: {err[:120]}",
            "category": "worker",
            "tags": ["job", "failure", "recovery"],
            "body": (
                f"# Recover from: {err[:120]}\n\n"
                f"## When\nCode job fails with this class of error.\n\n"
                f"## Steps\n1. Read full error; classify transient vs permanent.\n"
                f"2. Transient (timeout/network/clone): jobs_requeue + jobs_drain.\n"
                f"3. Permanent (money/deny): leave failed; escalate only if policy.\n"
                f"4. If same error 2×: patch this skill with the new line.\n\n"
                f"## Example job\n- id: {job.get('id')}\n- title: {title}\n- error: {err[:400]}\n"
            ),
            "source": "job",
            "source_ref": job.get("id"),
        }
    return None


def _llm_evolve(traces: list[dict], existing_index: list[dict]) -> list[dict]:
    """Ask Grok/Claude to propose skill create/patch ops from traces."""
    if not evolution_enabled():
        return []
    try:
        from .energy_agent_sovereign_brain import call_brain
    except Exception as e:  # noqa: BLE001
        log.warning("skill evolve no brain: %s", e)
        return []

    user = {
        "role": "skill_evolution",
        "instruction": (
            "You are Sovereign's skill evolution organ (Hermes-style closed loop). "
            "From execution traces, propose up to 3 reusable procedural skills. "
            "Each skill is a SKILL.md-like playbook: clear When / Steps / Anti-patterns. "
            "Prefer patching an existing skill name when the topic matches the index. "
            "Do NOT invent secrets, fake APIs, or money actions. JSON only."
        ),
        "existing_skills_index": existing_index[:30],
        "traces": traces[:35],
        "schema": {
            "skills": [
                {
                    "op": "create|patch",
                    "name": "kebab-case-slug",
                    "title": "short title",
                    "description": "under 160 chars",
                    "category": "ops|worker|leadership|reliability|product",
                    "tags": ["…"],
                    "body": "full markdown procedure",
                    "why": "one line why this helps next time",
                }
            ],
            "curator": {
                "deprecate": ["skill-name-to-retire"],
                "reason": "optional",
            },
        },
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You codify Sovereign's hard-won procedures into durable skills. "
                "Output pure JSON. Max 3 skills. Bodies must be actionable and short."
            ),
        },
        {"role": "user", "content": json.dumps(user, default=str)[:28000]},
    ]
    try:
        raw = call_brain(messages)
        from .energy_agent_sovereign_brain import _extract_json
        parsed = _extract_json(raw.get("content") or "")
        skills = parsed.get("skills") if isinstance(parsed, dict) else None
        if not isinstance(skills, list):
            return []
        out = []
        for s in skills[:3]:
            if not isinstance(s, dict):
                continue
            if not (s.get("name") and s.get("body")):
                continue
            out.append(s)
        # attach curator
        if isinstance(parsed.get("curator"), dict):
            out.append({"_curator": parsed["curator"]})
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("skill evolve LLM failed: %s", e)
        return []


def curator_pass(db, *, max_deprecate: int = 3) -> dict[str, Any]:
    """Retire low-quality unused skills (janitor). Never deletes seeds lightly."""
    rows = list_skills(db, status="active", limit=100)
    deprecated = []
    cutoff = _now() - timedelta(days=21)
    for r in rows:
        if len(deprecated) >= max_deprecate:
            break
        if r.source == "seed" and (r.use_count or 0) < 50:
            continue  # keep seeds
        low_q = float(r.quality or 0) < 0.2
        unused = (r.use_count or 0) == 0 and r.created_at and r.created_at < cutoff
        high_fail = (r.fail_count or 0) >= 5 and (r.success_count or 0) == 0
        if low_q or unused or high_fail:
            r.status = "deprecated"
            r.updated_at = _now()
            deprecated.append(r.name)
    if deprecated:
        db.flush()
    return {"ok": True, "deprecated": deprecated}


def evolution_cycle(*, force: bool = False) -> dict[str, Any]:
    """One learning-loop tick: harvest → propose → upsert → curator.

    Safe to run on a schedule. Idempotent-ish (version bumps on patch).
    """
    if not skills_enabled() and not force:
        return {"ok": True, "mode": "dark", "enabled": False}

    with SessionLocal() as db:
        try:
            ensure_skill_tables(db)
            seed = seed_skills(db)
            traces = harvest_traces(db, hours=36, limit=30)
            idx = skill_index(db, limit=40)

            created = []
            patched = []
            proposals: list[dict] = []

            # 1) Deterministic from recent jobs (always — even if LLM off)
            for tr in traces:
                if tr.get("kind") != "job":
                    continue
                draft = _deterministic_from_job(tr)
                if not draft:
                    continue
                # Only create new deterministic skills for done jobs or novel errors
                existing = get_skill(db, draft["name"])
                if existing and tr.get("status") == "done":
                    record_skill_outcome(db, draft["name"], success=True)
                    continue
                if existing and tr.get("status") == "failed":
                    record_skill_outcome(db, draft["name"], success=False)
                    # still allow body patch if error text new
                    if draft["body"] and draft["body"][:200] not in (existing.body or ""):
                        proposals.append({**draft, "op": "patch"})
                    continue
                proposals.append({**draft, "op": "create"})

            # 2) LLM evolution from full trace set
            llm_props = _llm_evolve(traces, idx) if traces else []
            curator_req = None
            for p in llm_props:
                if "_curator" in p:
                    curator_req = p["_curator"]
                    continue
                proposals.append(p)

            # Dedup by name, prefer LLM body if longer
            by_name: dict[str, dict] = {}
            for p in proposals:
                n = _slug(str(p.get("name") or ""))
                if not n:
                    continue
                prev = by_name.get(n)
                if not prev or len(str(p.get("body") or "")) > len(str(prev.get("body") or "")):
                    by_name[n] = p

            for n, p in list(by_name.items())[:5]:
                row = upsert_skill(
                    db,
                    name=n,
                    title=str(p.get("title") or n),
                    description=str(p.get("description") or "")[:400],
                    body=str(p.get("body") or ""),
                    category=str(p.get("category") or "ops"),
                    tags=list(p.get("tags") or []) if isinstance(p.get("tags"), list) else [],
                    source=str(p.get("source") or "evolve")[:40],
                    source_ref=str(p.get("source_ref") or p.get("why") or "")[:80] or None,
                    meta={"why": p.get("why"), "op": p.get("op")},
                )
                if int(row.version or 1) <= 1 and row.source != "seed":
                    created.append(row.name)
                else:
                    patched.append({"name": row.name, "version": row.version})

            # 3) Curator
            cur = curator_pass(db)
            if curator_req and isinstance(curator_req.get("deprecate"), list):
                for name in curator_req["deprecate"][:3]:
                    sk = get_skill(db, str(name))
                    if sk and sk.source != "seed":
                        sk.status = "deprecated"
                        sk.updated_at = _now()
                        cur.setdefault("deprecated", []).append(sk.name)
                db.flush()

            # Memory + note for continuity
            try:
                from .energy_agent_sovereign import memory_set, write_note
                memory_set(
                    db,
                    "last_skill_evolution",
                    json.dumps({
                        "at": _now().isoformat() + "Z",
                        "created": created,
                        "patched": patched,
                        "deprecated": cur.get("deprecated"),
                        "n_traces": len(traces),
                    }, default=str),
                    source="skills",
                )
                if created or patched or cur.get("deprecated"):
                    write_note(
                        db,
                        kind="memory",
                        title="skill evolution cycle",
                        body=json.dumps({
                            "created": created,
                            "patched": patched,
                            "curator": cur,
                            "n_traces": len(traces),
                        }, default=str)[:6000],
                        provider="skills",
                        meta={"layer": "skill_evolution"},
                    )
            except Exception as e:  # noqa: BLE001
                log.debug("skill evolution memory: %s", e)

            db.commit()
            return {
                "ok": True,
                "mode": "live",
                "seed": seed,
                "n_traces": len(traces),
                "created": created,
                "patched": patched,
                "curator": cur,
                "index_size": len(idx) + len(created),
            }
        except Exception as e:  # noqa: BLE001
            log.exception("evolution_cycle failed")
            try:
                db.rollback()
            except Exception:
                pass
            return {"ok": False, "error": str(e)[:400]}


def skills_status(db) -> dict[str, Any]:
    ensure_skill_tables(db)
    try:
        seed_skills(db)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    active = list_skills(db, status="active", limit=100)
    deprecated = list_skills(db, status="deprecated", limit=20)
    return {
        "ok": True,
        "enabled": skills_enabled(),
        "evolution": evolution_enabled(),
        "active_count": len(active),
        "deprecated_count": len(deprecated),
        "top": [serialize_skill(s) for s in active[:12]],
        "pattern": {
            "source": "Hermes Agent closed learning loop + agentskills.io",
            "progressive_disclosure": True,
            "create_on_success": True,
            "patch_on_failure_recovery": True,
            "curator": True,
        },
    }
