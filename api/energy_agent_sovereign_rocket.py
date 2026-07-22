"""Rocket thrust — keep the engine firing product ships, not monologue.

Ford 2026-07-22: desk is irrelevant; results matter. When the job queue is
empty under sandbox force + chamber mode, inject one concrete AO chamber
ship instead of waiting for cortex to invent bureaucracy.

Hard walls (caller / env must already enforce):
  SOVEREIGN_MIND_SANDBOX_FORCE=1 → no main / no prod deploy
  SOVEREIGN_CODE_PUSH=0, SOVEREIGN_CODE_DEPLOY=0 for prod
  Chamber redeploy only after sandbox AO ship
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("energy_agent.sovereign.rocket")

THRUST_MEMORY_KEY = "rocket_last_thrust"
THRUST_COOLDOWN_SEC = int(os.getenv("SOVEREIGN_ROCKET_THRUST_COOLDOWN_SEC", "900") or 900)

# Concrete product vacuums — small, visible, chamber-scoreable. Rotate.
CHAMBER_SHIP_BRIEFS: list[dict[str, str]] = [
    {
        "title": "Chamber: status-first scan on fleet/home empty states",
        "brief": (
            "SANDBOX ONLY / CHAMBER SHIP. array-operator public/ only.\n"
            "Goal: one owner-visible UX improvement on the main dashboard or fleet "
            "surface — reduce visual noise, put status first, make empty states honest.\n"
            "Do NOT write doctrine, tools lists, or mind_propose. Change HTML/CSS/JS "
            "the owner sees. Keep scope small (one screen). Leave a short note in "
            "sandbox showcase/PITCH.md describing the delta in one sentence.\n"
            "Never touch prod deploy keys or main merge."
        ),
    },
    {
        "title": "Chamber: clearer next-action on login/landing dead ends",
        "brief": (
            "SANDBOX ONLY / CHAMBER SHIP. array-operator public/ only.\n"
            "Find a dead end or confusing next-step on login, onboarding, or home. "
            "Ship a small clarity fix (copy, button, hierarchy). User-visible only.\n"
            "No axioms. No new admin tools. Showcase one-line pitch."
        ),
    },
    {
        "title": "Chamber: honesty pass — one lying or vague status label",
        "brief": (
            "SANDBOX ONLY / CHAMBER SHIP. array-operator public/ only.\n"
            "Find one status label, badge, or empty state that overclaims or confuses. "
            "Make it truthful and scannable. Smallest file set that changes the screen.\n"
            "No mind introspection. No utility adapters. UI truth only."
        ),
    },
    {
        "title": "Chamber: offline/error empty state that doesn't strand owners",
        "brief": (
            "SANDBOX ONLY / CHAMBER SHIP. array-operator public/ only.\n"
            "Improve one error or loading empty state so the owner knows what to do next. "
            "Visible product delta. Sandbox + chamber only."
        ),
    },
]


def rocket_thrust_enabled() -> bool:
    return (os.getenv("SOVEREIGN_ROCKET_THRUST", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _queued_or_running(db) -> int:
    from sqlalchemy import select, func
    from .energy_agent_sovereign import EaSovereignJob

    return int(
        db.execute(
            select(func.count()).select_from(EaSovereignJob).where(
                EaSovereignJob.status.in_(("queued", "running"))
            )
        ).scalar()
        or 0
    )


def _last_thrust_age_sec(db) -> float | None:
    try:
        from .energy_agent_sovereign import memory_get_all

        for m in memory_get_all(db, limit=40):
            if m.get("key") == THRUST_MEMORY_KEY and m.get("value"):
                try:
                    data = json.loads(m["value"])
                    at = data.get("at")
                    if not at:
                        return None
                    ts = datetime.fromisoformat(at.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    return (_utcnow() - ts).total_seconds()
                except Exception:
                    return None
    except Exception:
        return None
    return None


def _pick_brief(db) -> dict[str, str]:
    """Rotate briefs so we don't thrash the same title forever."""
    n = 0
    try:
        from .energy_agent_sovereign import memory_get_all

        for m in memory_get_all(db, limit=40):
            if m.get("key") == THRUST_MEMORY_KEY and m.get("value"):
                try:
                    n = int(json.loads(m["value"]).get("n") or 0)
                except Exception:
                    n = 0
                break
    except Exception:
        pass
    return CHAMBER_SHIP_BRIEFS[n % len(CHAMBER_SHIP_BRIEFS)], n


def maybe_thrust(db) -> dict[str, Any]:
    """If idle under rocket conditions, queue one sandbox chamber ship.

    Safe to call every jobs-scheduler tick (~3 min).
    """
    if not rocket_thrust_enabled():
        return {"ok": True, "skipped": "thrust_off"}

    try:
        from .energy_agent_sovereign import sovereign_enabled, act_code_hire
        from .energy_agent_sovereign_mind_sandbox import (
            mind_sandbox_force,
            get_active_run,
            ensure_active_run,
        )
        from .energy_agent_sovereign_worker import code_live_enabled
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"import:{e}"[:200]}

    if not sovereign_enabled():
        return {"ok": True, "skipped": "sovereign_off"}
    if not code_live_enabled():
        return {"ok": True, "skipped": "code_live_off"}
    if not mind_sandbox_force():
        # Only auto-thrust when walls force sandbox — never auto-hire toward prod
        return {"ok": True, "skipped": "sandbox_force_off"}

    if _queued_or_running(db) > 0:
        return {"ok": True, "skipped": "queue_busy"}

    age = _last_thrust_age_sec(db)
    if age is not None and age < THRUST_COOLDOWN_SEC:
        return {
            "ok": True,
            "skipped": "cooldown",
            "age_sec": int(age),
            "cooldown_sec": THRUST_COOLDOWN_SEC,
        }

    # Ensure free-run window exists
    run = get_active_run(db) or ensure_active_run(db, days=7)
    run_id = (run or {}).get("id")

    brief_spec, n = _pick_brief(db)
    title = brief_spec["title"]
    brief = brief_spec["brief"]
    if run_id:
        brief = brief + f"\n\nsandbox_run_id={run_id}\n"

    res = act_code_hire(
        db,
        title=title,
        brief=brief,
        kind="sandbox_job",
    )
    if not res.get("ok"):
        log.warning("rocket thrust hire failed: %s", res)
        return {"ok": False, "hire": res}

    job_id = res.get("job_id")
    # Force sandbox flags on the job brief
    try:
        from .energy_agent_sovereign import EaSovereignJob

        job = db.get(EaSovereignJob, job_id) if job_id else None
        if job:
            try:
                b = json.loads(job.brief_json or "{}")
            except Exception:
                b = {}
            b["sandbox"] = True
            b["mind_sandbox"] = True
            b["chamber_ship"] = True
            if run_id:
                b["sandbox_run_id"] = run_id
            b["repo"] = "array-operator"
            job.brief_json = json.dumps(b, default=str)[:50_000]
            job.kind = "sandbox_job"
    except Exception as e:  # noqa: BLE001
        log.warning("rocket tag job: %s", e)

    try:
        from .energy_agent_sovereign import memory_set, write_note

        memory_set(
            db,
            THRUST_MEMORY_KEY,
            json.dumps(
                {
                    "at": _utcnow().isoformat(),
                    "job_id": job_id,
                    "title": title,
                    "run_id": run_id,
                    "n": n + 1,
                }
            ),
            source="rocket",
        )
        write_note(
            db,
            kind="decision",
            title=f"rocket thrust → {title[:80]}",
            body=(
                f"Idle queue under sandbox force. Injected chamber ship job {job_id}. "
                f"Walls: no main, no prod deploy. Score at chamber URL + L4 scorecard."
            ),
            provider="rocket",
        )
    except Exception as e:  # noqa: BLE001
        log.debug("rocket memory: %s", e)

    log.info("rocket thrust hired job=%s title=%s", job_id, title[:60])
    return {
        "ok": True,
        "thrust": True,
        "job_id": job_id,
        "title": title,
        "run_id": run_id,
    }
