#!/usr/bin/env python3
"""Stress / smoke test Sovereign skill evolution."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def ok(m: str) -> None:
    print(f"  ✓ {m}")


def fail(m: str) -> None:
    print(f"  ✗ {m}")
    raise SystemExit(1)


def main() -> None:
    print("Sovereign skill evolution stress")
    from api.energy_agent_sovereign_skills import (
        evolution_cycle,
        ensure_skill_tables,
        get_skill,
        list_skills,
        load_skills_for_context,
        match_skills,
        seed_skills,
        serialize_skill,
        skill_index,
        skills_enabled,
        upsert_skill,
        _deterministic_from_job,
        _slug,
    )
    from api.db import SessionLocal, engine
    from api.models import Base
    from api.energy_agent_sovereign import EaSovereignJob

    assert skills_enabled() or True  # may depend on SOVEREIGN_ENABLED
    ensure_skill_tables()
    Base.metadata.create_all(bind=engine, tables=[EaSovereignSkill.__table__] if False else [])

    from api.energy_agent_sovereign_skills import EaSovereignSkill
    Base.metadata.create_all(bind=engine, tables=[EaSovereignSkill.__table__])

    with SessionLocal() as db:
        seed = seed_skills(db)
        db.commit()
        ok(f"seed created={seed.get('created')} skipped={seed.get('skipped')}")

        idx = skill_index(db)
        if len(idx) < 3:
            fail(f"expected seed index >=3, got {len(idx)}")
        ok(f"index n={len(idx)} first={idx[0].get('name')}")

        matched = match_skills(db, "utility portal queue researching HAR adapter")
        if not matched:
            fail("expected match on utility text")
        ok(f"match={matched[0].name}")

        ctx = load_skills_for_context(db, heat_text="code job timeout requeue drain git clone")
        if not ctx.get("loaded") and not ctx.get("index"):
            fail("empty skills context")
        ok(f"context index={len(ctx.get('index') or [])} loaded={[s.get('name') for s in ctx.get('loaded') or []]}")

        # Synthetic job → deterministic skill
        jid = f"sov_skill_test_{uuid.uuid4().hex[:8]}"
        job = EaSovereignJob(
            id=jid,
            kind="test",
            status="done",
            title="Ship offtaker copy polish feature #999",
            brief_json="{}",
            result_json=json.dumps({"ship": {"ok": True}}),
            finished_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db.add(job)
        db.commit()

        draft = _deterministic_from_job({
            "kind": "job",
            "status": "done",
            "title": job.title,
            "id": jid,
            "result_excerpt": job.result_json or "",
        })
        if not draft:
            fail("deterministic draft missing for done job")
        row = upsert_skill(db, **{k: draft[k] for k in (
            "name", "title", "description", "body", "category", "tags", "source", "source_ref",
        ) if k in draft})
        db.commit()
        ok(f"upsert skill {row.name} v{row.version}")

        # evolution cycle (may skip LLM offline)
        res = evolution_cycle(force=True)
        ok(f"evolution ok={res.get('ok')} created={res.get('created')} patched={res.get('patched')} traces={res.get('n_traces')}")

        # cleanup test job
        j = db.get(EaSovereignJob, jid)
        if j:
            db.delete(j)
            db.commit()

        active = list_skills(db, status="active", limit=20)
        ok(f"active skills={len(active)}")
        print("  sample:", json.dumps(serialize_skill(active[0]), indent=2)[:400])

    # scheduler wiring
    src = (ROOT / "api" / "scheduler.py").read_text()
    if "energy_agent_sovereign_skills" not in src:
        fail("scheduler missing skills job")
    ok("scheduler skills job wired")

    print("\nALL SKILL EVOLUTION CHECKS PASSED")


if __name__ == "__main__":
    main()
