"""Sovereign durability — independent watchdog + dual-channel recovery.

Pattern source (Ford's Mindspace / interface canvas dual-sidecar):
  • sidecar-watchdog.sh — OUTSIDE the mind, probes /healthz, respawns only when
    BOTH colors (base + base+2) are dead for a grace window (never fights blue-green)
  • storm breaker — refuse thrashing reboots after N auto-restarts in a window
  • active-port self-heal — fix orphaned pointers without killing healthy brain
  • soft drain — refuse new work while recovering, keep durable state in DB

Sovereign runs in-process on Railway (no dual ports). Mapping:
  primary channel  = normal scheduler subconscious / cortex / jobs
  recovery channel = this watchdog (forced ticks, stuck-job requeue, lock reset)
  healthz          = GET /admin/sovereign/healthz  (vitals + storm + channel status)
  durable state    = ea_sovereign_memory + world JSON (survives process recycle)

Research anchors (2025–2026 patterns we copy, not invent):
  • Supervisor + worker (LangGraph / multi-agent): one coordinator, specialized workers
  • Independent external supervisor (Erlang-style "let it crash" + restart) — the
    interface watchdog is this for process death; we do the same for logical layers
  • Self-healing agents (Adaptive / Hermes closed loop): observe → diagnose → act →
    record; we write recovery notes + vitals so the mind remembers its own restarts
  • Circuit breaker / storm breaker: trip open after failure burst, cool down

Kill switch: SOVEREIGN_WATCHDOG=0
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from .db import SessionLocal

log = logging.getLogger("energy_agent.sovereign.watchdog")

# ── Process-local vitals (also mirrored to durable memory) ───────────────────
_lock = threading.Lock()
_vitals: dict[str, Any] = {
    "boot_at": None,
    "last_watchdog_at": None,
    "last_primary_sub": None,
    "last_primary_cortex": None,
    "last_primary_jobs": None,
    "last_recovery_at": None,
    "last_recovery_reason": None,
    "consecutive_sub_fail": 0,
    "consecutive_cortex_fail": 0,
    "consecutive_jobs_fail": 0,
    "cortex_inflight_since": None,
    "jobs_inflight_since": None,
    "recovery_ledger": [],  # unix timestamps of auto-recoveries
    "storm_breaker_tripped": False,
    "storm_tripped_at": None,
    "total_recoveries": 0,
    "last_error": None,
}
_boot_mono = time.monotonic()


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _now() -> datetime:
    return datetime.utcnow()


def _id(prefix: str = "wd") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def watchdog_enabled() -> bool:
    """Default ON when sovereign is on. Kill alone: SOVEREIGN_WATCHDOG=0."""
    try:
        from .energy_agent_sovereign import sovereign_enabled
        if not sovereign_enabled():
            return False
    except Exception:
        return False
    return _flag("SOVEREIGN_WATCHDOG", "1")


def sub_stale_sec() -> float:
    # subconscious every ~45s — stale after 3× interval (default 180s)
    base = float(os.getenv("SOVEREIGN_SUBCONSCIOUS_INTERVAL_SEC", "45") or 45)
    mult = float(os.getenv("SOVEREIGN_WATCHDOG_SUB_STALE_MULT", "3.5") or 3.5)
    return max(90.0, base * mult)


def cortex_stale_sec() -> float:
    # cortex backstop 5m — stale after 3× (default 15m) unless heat demands sooner
    return max(300.0, float(os.getenv("SOVEREIGN_WATCHDOG_CORTEX_STALE_SEC", "900") or 900))


def cortex_inflight_stuck_sec() -> float:
    # think_cycle + ops can run long; only treat as wedged after this
    return max(120.0, float(os.getenv("SOVEREIGN_WATCHDOG_CORTEX_STUCK_SEC", "720") or 720))


def jobs_inflight_stuck_sec() -> float:
    # code agent timeout default 900s — stuck if running longer
    code_to = float(os.getenv("SOVEREIGN_CODE_TIMEOUT", "900") or 900)
    return max(code_to + 120.0, float(os.getenv("SOVEREIGN_WATCHDOG_JOB_STUCK_SEC", "1100") or 1100))


def storm_max() -> int:
    return max(2, int(os.getenv("SOVEREIGN_WATCHDOG_STORM_MAX", "5") or 5))


def storm_window_sec() -> float:
    return max(60.0, float(os.getenv("SOVEREIGN_WATCHDOG_STORM_WINDOW_SEC", "900") or 900))


def storm_cool_sec() -> float:
    return max(60.0, float(os.getenv("SOVEREIGN_WATCHDOG_STORM_COOL_SEC", "600") or 600))


def fail_threshold() -> int:
    return max(2, int(os.getenv("SOVEREIGN_WATCHDOG_FAIL_THRESHOLD", "3") or 3))


# ── Public heartbeats (call from primary channel) ────────────────────────────
def note_primary(layer: str, *, ok: bool, detail: dict | None = None) -> None:
    """Primary channel (sub/cortex/jobs) reports in after each run."""
    now_iso = _now().isoformat() + "Z"
    with _lock:
        if _vitals["boot_at"] is None:
            _vitals["boot_at"] = now_iso
        key = {
            "sub": "last_primary_sub",
            "subconscious": "last_primary_sub",
            "cortex": "last_primary_cortex",
            "tick": "last_primary_cortex",
            "jobs": "last_primary_jobs",
        }.get(layer, None)
        if key:
            _vitals[key] = {
                "at": now_iso,
                "ok": bool(ok),
                "detail": (detail or {}) if isinstance(detail, dict) else {},
            }
        if layer in ("sub", "subconscious"):
            _vitals["consecutive_sub_fail"] = 0 if ok else int(_vitals["consecutive_sub_fail"]) + 1
        elif layer in ("cortex", "tick"):
            _vitals["consecutive_cortex_fail"] = 0 if ok else int(_vitals["consecutive_cortex_fail"]) + 1
            _vitals["cortex_inflight_since"] = None
        elif layer == "jobs":
            _vitals["consecutive_jobs_fail"] = 0 if ok else int(_vitals["consecutive_jobs_fail"]) + 1
            _vitals["jobs_inflight_since"] = None
        if not ok:
            _vitals["last_error"] = {
                "at": now_iso,
                "layer": layer,
                "detail": (detail or {}) if isinstance(detail, dict) else {"raw": str(detail)[:300]},
            }


def mark_cortex_inflight(on: bool = True) -> None:
    with _lock:
        _vitals["cortex_inflight_since"] = time.time() if on else None


def mark_jobs_inflight(on: bool = True) -> None:
    with _lock:
        _vitals["jobs_inflight_since"] = time.time() if on else None


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).strip().replace("Z", "")
        if s.startswith("{"):
            data = json.loads(s)
            s = (data.get("at") or data.get("last_cortex_at") or "").replace("Z", "")
        if not s:
            return None
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _age_sec_from_iso(raw: str | None) -> float | None:
    dt = _parse_iso(raw)
    if not dt:
        return None
    return max(0.0, (_now() - dt).total_seconds())


def _storm_state() -> tuple[bool, list[float], float | None]:
    """(storm_active, recent_recovery_ts, seconds_since_last)."""
    now = time.time()
    with _lock:
        ledger = [t for t in (_vitals.get("recovery_ledger") or []) if now - t < storm_window_sec()]
        _vitals["recovery_ledger"] = ledger
        last = max(ledger) if ledger else None
        since = (now - last) if last else None
        tripped = bool(_vitals.get("storm_breaker_tripped"))
        if tripped and _vitals.get("storm_tripped_at"):
            if now - float(_vitals["storm_tripped_at"]) >= storm_cool_sec():
                _vitals["storm_breaker_tripped"] = False
                _vitals["storm_tripped_at"] = None
                tripped = False
        if len(ledger) >= storm_max():
            _vitals["storm_breaker_tripped"] = True
            _vitals["storm_tripped_at"] = _vitals.get("storm_tripped_at") or now
            tripped = True
        return tripped, ledger, since


def _record_recovery(reason: str) -> bool:
    """Return False if storm breaker refuses further auto-recovery."""
    storm, _, _ = _storm_state()
    if storm:
        log.warning("watchdog storm breaker OPEN — skip recovery: %s", reason)
        return False
    now = time.time()
    with _lock:
        ledger = list(_vitals.get("recovery_ledger") or [])
        ledger.append(now)
        _vitals["recovery_ledger"] = ledger
        _vitals["last_recovery_at"] = _now().isoformat() + "Z"
        _vitals["last_recovery_reason"] = reason[:300]
        _vitals["total_recoveries"] = int(_vitals.get("total_recoveries") or 0) + 1
    storm2, recent, _ = _storm_state()
    if storm2:
        log.error(
            "watchdog STORM BREAKER tripped (%s recoveries in window)",
            len(recent),
        )
    return True


def _durable_ages(db) -> dict[str, float | None]:
    """Ages from durable memory (survives process restart)."""
    from .energy_agent_sovereign import memory_get_all, world_get

    mem = {m["key"]: m["value"] for m in memory_get_all(db, limit=80)}
    world = world_get(db) or {}
    sub_raw = mem.get("last_subconscious") or world.get("last_subconscious_at")
    cortex_raw = mem.get("last_cortex_at") or mem.get("last_tick") or world.get("last_cortex_at")
    # last_subconscious may be JSON blob
    sub_at = None
    if sub_raw:
        try:
            if str(sub_raw).strip().startswith("{"):
                sub_at = json.loads(sub_raw).get("at")
            else:
                sub_at = sub_raw
        except Exception:
            sub_at = sub_raw
    cortex_at = None
    if cortex_raw:
        try:
            if str(cortex_raw).strip().startswith("{"):
                cortex_at = json.loads(cortex_raw).get("at") or json.loads(cortex_raw).get("last_cortex_at")
            else:
                cortex_at = cortex_raw
        except Exception:
            cortex_at = cortex_raw
    return {
        "sub_age_sec": _age_sec_from_iso(sub_at if isinstance(sub_at, str) else None),
        "cortex_age_sec": _age_sec_from_iso(cortex_at if isinstance(cortex_at, str) else None),
        "heat": mem.get("heat_score"),
        "needs_cortex": mem.get("needs_cortex"),
        "last_sub_provider": None,
    }


def diagnose(db=None) -> dict[str, Any]:
    """Read-only health picture (primary + recovery readiness)."""
    storm, recent, since_rec = _storm_state()
    with _lock:
        v = dict(_vitals)
        cortex_inf = v.get("cortex_inflight_since")
        jobs_inf = v.get("jobs_inflight_since")
    now = time.time()
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        ages = _durable_ages(db)
        stuck_jobs = _count_stuck_running_jobs(db)
        failed_recent = _count_recent_failed_jobs(db, hours=6)
    finally:
        if own_db:
            try:
                db.close()
            except Exception:
                pass

    problems: list[str] = []
    sub_age = ages.get("sub_age_sec")
    cortex_age = ages.get("cortex_age_sec")
    if sub_age is None or sub_age > sub_stale_sec():
        problems.append(f"subconscious_stale age={sub_age}")
    if cortex_age is None or cortex_age > cortex_stale_sec():
        problems.append(f"cortex_stale age={cortex_age}")
    if cortex_inf and (now - float(cortex_inf)) > cortex_inflight_stuck_sec():
        problems.append(f"cortex_inflight_stuck sec={int(now - float(cortex_inf))}")
    if jobs_inf and (now - float(jobs_inf)) > jobs_inflight_stuck_sec():
        problems.append(f"jobs_inflight_stuck sec={int(now - float(jobs_inf))}")
    if stuck_jobs:
        problems.append(f"jobs_running_stuck n={stuck_jobs}")
    orphan_desk = 0
    try:
        from .energy_agent_sovereign_desk import find_orphan_desk_turns
        orphan_desk = len(find_orphan_desk_turns(db, limit=8))
    except Exception:
        orphan_desk = 0
    if orphan_desk:
        problems.append(f"desk_orphan_turns n={orphan_desk}")
    if int(v.get("consecutive_sub_fail") or 0) >= fail_threshold():
        problems.append(f"sub_fail_burst n={v.get('consecutive_sub_fail')}")
    if int(v.get("consecutive_cortex_fail") or 0) >= fail_threshold():
        problems.append(f"cortex_fail_burst n={v.get('consecutive_cortex_fail')}")
    if int(v.get("consecutive_jobs_fail") or 0) >= fail_threshold():
        problems.append(f"jobs_fail_burst n={v.get('consecutive_jobs_fail')}")
    if storm:
        problems.append("storm_breaker_open")

    # Dual-channel metaphor (interface base / base+2)
    primary_ok = (sub_age is not None and sub_age <= sub_stale_sec()) or (
        cortex_age is not None and cortex_age <= cortex_stale_sec()
    )
    recovery_ready = not storm

    return {
        "ok": len([p for p in problems if p != "storm_breaker_open"]) == 0,
        "mode": "live" if watchdog_enabled() else "dark",
        "uptime_sec": int(time.monotonic() - _boot_mono),
        "problems": problems,
        "channels": {
            "primary": {"alive": primary_ok, "role": "scheduler_sub_cortex_jobs"},
            "recovery": {"alive": recovery_ready, "role": "watchdog_forced_heal"},
        },
        "ages": {
            "sub_age_sec": sub_age,
            "cortex_age_sec": cortex_age,
            "sub_stale_after_sec": sub_stale_sec(),
            "cortex_stale_after_sec": cortex_stale_sec(),
        },
        "inflight": {
            "cortex_sec": (int(now - float(cortex_inf)) if cortex_inf else None),
            "jobs_sec": (int(now - float(jobs_inf)) if jobs_inf else None),
        },
        "jobs": {
            "running_stuck": stuck_jobs,
            "failed_recent_6h": failed_recent,
        },
        "desk": {
            "orphan_turns": orphan_desk,
        },
        "fails": {
            "sub": v.get("consecutive_sub_fail"),
            "cortex": v.get("consecutive_cortex_fail"),
            "jobs": v.get("consecutive_jobs_fail"),
        },
        "storm": {
            "breaker_tripped": storm,
            "recent_recoveries": len(recent),
            "max": storm_max(),
            "window_sec": storm_window_sec(),
            "seconds_since_last_recovery": since_rec,
            "total_recoveries": v.get("total_recoveries"),
        },
        "last_recovery": {
            "at": v.get("last_recovery_at"),
            "reason": v.get("last_recovery_reason"),
        },
        "last_error": v.get("last_error"),
        "vitals": {
            "boot_at": v.get("boot_at"),
            "last_watchdog_at": v.get("last_watchdog_at"),
            "last_primary_sub": v.get("last_primary_sub"),
            "last_primary_cortex": v.get("last_primary_cortex"),
            "last_primary_jobs": v.get("last_primary_jobs"),
        },
        "pattern": {
            "source": "interface dual-sidecar + independent watchdog",
            "research": [
                "supervisor-worker (LangGraph-style layering)",
                "external supervisor / let-it-crash restart",
                "circuit breaker (storm breaker)",
                "self-healing observe→diagnose→act loop",
            ],
        },
    }


def _count_stuck_running_jobs(db) -> int:
    from .energy_agent_sovereign import EaSovereignJob

    cutoff = _now() - timedelta(seconds=jobs_inflight_stuck_sec())
    try:
        rows = db.execute(
            select(EaSovereignJob).where(EaSovereignJob.status == "running")
        ).scalars().all()
    except Exception:
        return 0
    n = 0
    for j in rows:
        # No started_at column — use created_at as floor (jobs shouldn't stay running forever)
        started = j.created_at
        if started and started < cutoff:
            n += 1
    return n


def _count_recent_failed_jobs(db, hours: float = 6) -> int:
    from .energy_agent_sovereign import EaSovereignJob

    cutoff = _now() - timedelta(hours=hours)
    try:
        rows = db.execute(
            select(EaSovereignJob).where(
                EaSovereignJob.status == "failed",
                EaSovereignJob.finished_at >= cutoff,
            )
        ).scalars().all()
        return len(rows)
    except Exception:
        return 0


def recover_stuck_jobs(db) -> dict[str, Any]:
    """Requeue jobs stuck in running (process death mid-agent)."""
    from .energy_agent_sovereign import EaSovereignJob, write_note

    cutoff = _now() - timedelta(seconds=jobs_inflight_stuck_sec())
    rows = db.execute(
        select(EaSovereignJob).where(EaSovereignJob.status == "running")
    ).scalars().all()
    ids = []
    for j in rows:
        started = j.created_at
        if not started or started >= cutoff:
            continue
        j.status = "queued"
        note = f"[watchdog requeue] stuck running since {started.isoformat()}Z"
        prev = (j.error or "").strip()
        j.error = (note + (" | " + prev if prev else ""))[:800]
        j.finished_at = None
        ids.append(j.id)
    if ids:
        try:
            write_note(
                db,
                kind="system",
                title="watchdog · requeued stuck jobs",
                body=json.dumps({"ids": ids, "stuck_after_sec": jobs_inflight_stuck_sec()}),
                provider="watchdog",
                tick_id=_id("wd"),
            )
        except Exception:
            pass
        db.flush()
    return {"ok": True, "requeued": len(ids), "ids": ids}


def recover_failed_transient(db, *, limit: int = 5) -> dict[str, Any]:
    """Requeue recent transient failures (timeouts / network), not money denies."""
    from .energy_agent_sovereign import EaSovereignJob

    cutoff = _now() - timedelta(hours=12)
    rows = db.execute(
        select(EaSovereignJob)
        .where(
            EaSovereignJob.status == "failed",
            EaSovereignJob.finished_at >= cutoff,
        )
        .order_by(EaSovereignJob.finished_at.desc())
        .limit(40)
    ).scalars().all()
    transient = (
        "timeout", "network", "rate limit", "503", "502", "429",
        "temporarily", "connection", "reset by peer", "overloaded",
        "no sovereign repos", "clone",
    )
    permanent = ("money/stripe", "destructive ops denied", "denied", "never autonomous")
    ids = []
    for j in rows:
        if len(ids) >= limit:
            break
        err = (j.error or "").lower()
        if any(p in err for p in permanent):
            continue
        if not any(t in err for t in transient):
            continue
        j.status = "queued"
        j.error = None
        j.finished_at = None
        ids.append(j.id)
    if ids:
        db.flush()
    return {"ok": True, "requeued": len(ids), "ids": ids}


def soft_reboot(
    *,
    reason: str = "watchdog",
    force_cortex: bool = True,
    force_sub: bool = True,
    requeue_jobs: bool = True,
    respect_storm: bool = True,
) -> dict[str, Any]:
    """Recovery-channel restart without killing the web process.

    Like interface blue-green conceptually: bring a healthy path online while
    durable memory/world stay put. Never fights a healthy primary unless stale.
    """
    if not watchdog_enabled() and respect_storm:
        return {"ok": False, "skipped": True, "reason": "watchdog_off"}

    if respect_storm and not _record_recovery(reason):
        return {
            "ok": False,
            "storm_breaker": True,
            "reason": "storm_breaker_open",
            "health": diagnose(),
        }

    actions: list[dict] = []
    with SessionLocal() as db:
        try:
            if requeue_jobs:
                r1 = recover_stuck_jobs(db)
                actions.append({"step": "requeue_stuck_running", **r1})
                r2 = recover_failed_transient(db, limit=4)
                actions.append({"step": "requeue_transient_failed", **r2})

            # Clear inflight markers so primary can re-enter
            mark_cortex_inflight(False)
            mark_jobs_inflight(False)

            # Reset process-local fail bursts (fresh chance)
            with _lock:
                _vitals["consecutive_sub_fail"] = 0
                _vitals["consecutive_cortex_fail"] = 0
                _vitals["consecutive_jobs_fail"] = 0

            from .energy_agent_sovereign import memory_set, write_note

            write_note(
                db,
                kind="system",
                title=f"watchdog soft-reboot · {reason}"[:240],
                body=json.dumps({
                    "reason": reason,
                    "actions_planned": {
                        "force_sub": force_sub,
                        "force_cortex": force_cortex,
                        "requeue_jobs": requeue_jobs,
                    },
                    "pattern": "dual-channel recovery (interface watchdog analogue)",
                }, default=str)[:4000],
                provider="watchdog",
                tick_id=_id("wd"),
            )
            memory_set(
                db,
                "last_watchdog_reboot",
                json.dumps({
                    "at": _now().isoformat() + "Z",
                    "reason": reason,
                }),
                source="watchdog",
            )
            db.commit()
        except Exception as e:  # noqa: BLE001
            log.exception("soft_reboot prep failed")
            try:
                db.rollback()
            except Exception:
                pass
            return {"ok": False, "error": str(e)[:400], "actions": actions}

    # Heavy work outside the prep transaction — gated so recovery cannot
    # re-thrash a hot pool (same circuit breaker as scheduler runners).
    def _guard_blocks(layer: str) -> str | None:
        try:
            from .sovereign_guard import should_skip_heavy
            skip, why = should_skip_heavy(layer)
            return why if skip else None
        except Exception as ge:  # noqa: BLE001
            log.warning("soft_reboot guard check failed: %s", ge)
            return None

    if force_sub:
        blocked = _guard_blocks("watchdog_sub")
        if blocked:
            actions.append({
                "step": "force_subconscious",
                "ok": True,
                "skipped": True,
                "reason": blocked,
            })
        else:
            try:
                from .energy_agent_sovereign_subconscious import subconscious_tick
                sub = subconscious_tick(reason=f"watchdog:{reason}"[:80], force=True)
                note_primary("sub", ok=bool(sub.get("ok")), detail={"via": "recovery", "tick": sub.get("tick_id")})
                actions.append({
                    "step": "force_subconscious",
                    "ok": sub.get("ok"),
                    "heat": sub.get("heat"),
                    "tick_id": sub.get("tick_id"),
                })
            except Exception as e:  # noqa: BLE001
                note_primary("sub", ok=False, detail={"error": str(e)[:200]})
                actions.append({"step": "force_subconscious", "ok": False, "error": str(e)[:300]})

    if force_cortex:
        blocked = _guard_blocks("watchdog_cortex")
        if blocked:
            actions.append({
                "step": "force_cortex",
                "ok": True,
                "skipped": True,
                "reason": blocked,
            })
        else:
            # Single-flight: do not force cortex while jobs/mission/skills hold heavy
            flight_ok, flight_why = True, "ok"
            try:
                from .sovereign_guard import try_begin_heavy
                flight_ok, flight_why = try_begin_heavy("watchdog_cortex")
            except Exception as fe:  # noqa: BLE001
                log.warning("soft_reboot single_flight check failed: %s", fe)
                flight_ok, flight_why = True, "ok"
            if not flight_ok:
                log.warning("soft_reboot force_cortex skipped: %s", flight_why)
                actions.append({
                    "step": "force_cortex",
                    "ok": True,
                    "skipped": True,
                    "reason": flight_why,
                })
            else:
                try:
                    mark_cortex_inflight(True)
                    from .energy_agent_sovereign import sovereign_tick
                    cortex = sovereign_tick(reason=f"watchdog:{reason}"[:120])
                    note_primary(
                        "cortex",
                        ok=bool(cortex.get("ok")),
                        detail={"via": "recovery", "tick": cortex.get("tick_id")},
                    )
                    actions.append({
                        "step": "force_cortex",
                        "ok": cortex.get("ok"),
                        "tick_id": cortex.get("tick_id"),
                        "mode": cortex.get("mode"),
                        "n_decisions": len(cortex.get("decisions") or []),
                    })
                except Exception as e:  # noqa: BLE001
                    note_primary("cortex", ok=False, detail={"error": str(e)[:200]})
                    actions.append({"step": "force_cortex", "ok": False, "error": str(e)[:300]})
                finally:
                    mark_cortex_inflight(False)
                    try:
                        from .sovereign_guard import end_heavy
                        end_heavy("watchdog_cortex")
                    except Exception:
                        pass

    if requeue_jobs:
        # Always allow lightweight requeue of stuck rows (already done above).
        # Only gate the optional single job *drain* (code agent — heavy).
        blocked = _guard_blocks("watchdog_jobs")
        if blocked:
            actions.append({
                "step": "drain_jobs",
                "ok": True,
                "skipped": True,
                "reason": blocked,
            })
        else:
            flight_ok, flight_why = True, "ok"
            try:
                from .sovereign_guard import try_begin_heavy
                flight_ok, flight_why = try_begin_heavy("watchdog_jobs")
            except Exception as fe:  # noqa: BLE001
                log.warning("soft_reboot single_flight check failed: %s", fe)
                flight_ok, flight_why = True, "ok"
            if not flight_ok:
                log.warning("soft_reboot drain_jobs skipped: %s", flight_why)
                actions.append({
                    "step": "drain_jobs",
                    "ok": True,
                    "skipped": True,
                    "reason": flight_why,
                })
            else:
                try:
                    mark_jobs_inflight(True)
                    from .energy_agent_sovereign_worker import code_live_enabled, drain_jobs
                    if code_live_enabled():
                        with SessionLocal() as db2:
                            res = drain_jobs(db2, limit=1)
                            db2.commit()
                        note_primary("jobs", ok=bool(res.get("ok")), detail={"via": "recovery", **{k: res.get(k) for k in ("processed",)}})
                        actions.append({"step": "drain_jobs", "ok": res.get("ok"), "processed": res.get("processed")})
                    else:
                        actions.append({"step": "drain_jobs", "ok": True, "skipped": True})
                except Exception as e:  # noqa: BLE001
                    note_primary("jobs", ok=False, detail={"error": str(e)[:200]})
                    actions.append({"step": "drain_jobs", "ok": False, "error": str(e)[:300]})
                finally:
                    mark_jobs_inflight(False)
                    try:
                        from .sovereign_guard import end_heavy
                        end_heavy("watchdog_jobs")
                    except Exception:
                        pass

    health = diagnose()
    return {
        "ok": True,
        "channel": "recovery",
        "reason": reason,
        "actions": actions,
        "health": health,
    }


def watchdog_tick(*, force: bool = False) -> dict[str, Any]:
    """Independent supervisor tick — diagnose; soft-reboot only when sick.

    Mirrors sidecar-watchdog.sh: only respawn when primary is truly down,
    never thrash a healthy mind (storm breaker + grace via stale thresholds).
    """
    if not watchdog_enabled() and not force:
        return {"ok": True, "mode": "dark", "enabled": False}

    with _lock:
        _vitals["last_watchdog_at"] = _now().isoformat() + "Z"
        if _vitals["boot_at"] is None:
            _vitals["boot_at"] = _vitals["last_watchdog_at"]

    health = diagnose()
    problems = [p for p in (health.get("problems") or []) if p != "storm_breaker_open"]

    # Energy Agent Prime: site-first probe (web /health). Pause heavy work if AO is sick.
    site_guard: dict[str, Any] | None = None
    try:
        from .energy_agent_prime_site import site_guardian_tick
        site_guard = site_guardian_tick()
        if site_guard and not site_guard.get("ok") and not site_guard.get("skipped"):
            log.warning(
                "prime site_guardian unhealthy fail_streak=%s",
                site_guard.get("fail_streak"),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("prime site_guardian failed: %s", e)
        site_guard = {"ok": False, "error": str(e)[:200]}

    # Always try to finish desk turns orphaned by deploys (even when otherwise healthy)
    desk_recover: dict[str, Any] | None = None
    try:
        from .energy_agent_sovereign_desk import recover_orphan_desk_turns
        desk_recover = recover_orphan_desk_turns(limit=3)
        if desk_recover.get("recovered"):
            log.warning(
                "watchdog resumed %s orphan desk turn(s)",
                desk_recover.get("recovered"),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("watchdog desk orphan recover failed: %s", e)
        desk_recover = {"ok": False, "error": str(e)[:200]}

    # Re-diagnose after desk recover so we don't soft-reboot solely for orphans we fixed
    if desk_recover and desk_recover.get("recovered"):
        health = diagnose()
        problems = [p for p in (health.get("problems") or []) if p != "storm_breaker_open"]

    if not problems and not force:
        return {
            "ok": True,
            "mode": "healthy",
            "recovered": bool(desk_recover and desk_recover.get("recovered")),
            "desk_recover": desk_recover,
            "site_guardian": site_guard,
            "health": health,
        }

    # Prefer surgical recoveries before full soft-reboot
    surgical: list[dict] = []
    if desk_recover:
        surgical.append({"step": "desk_orphan_recover", **desk_recover})
    only_jobs = problems and all(
        p.startswith("jobs_") or p.startswith("jobs_inflight") or p.startswith("desk_orphan")
        for p in problems
    )
    only_desk = problems and all(p.startswith("desk_orphan") for p in problems)
    if only_desk and not force:
        return {
            "ok": True,
            "mode": "surgical",
            "recovered": bool(desk_recover and desk_recover.get("recovered")),
            "surgical": surgical,
            "health": health,
        }
    if only_jobs and not force:
        if not _record_recovery("surgical_jobs:" + ",".join(problems)[:120]):
            return {"ok": False, "storm_breaker": True, "health": health, "desk_recover": desk_recover}
        with SessionLocal() as db:
            try:
                surgical.append(recover_stuck_jobs(db))
                surgical.append(recover_failed_transient(db, limit=3))
                db.commit()
            except Exception as e:  # noqa: BLE001
                db.rollback()
                surgical.append({"ok": False, "error": str(e)[:200]})
        return {
            "ok": True,
            "mode": "surgical",
            "recovered": True,
            "surgical": surgical,
            "desk_recover": desk_recover,
            "health": diagnose(),
        }

    reason = "force" if force else ("sick:" + ";".join(problems)[:200])
    cortex_age = (health.get("ages") or {}).get("cortex_age_sec")
    need_cortex = force or any(
        p.startswith("cortex_") or p.startswith("cortex_fail") for p in problems
    )
    if cortex_age is None or float(cortex_age) > cortex_stale_sec() * 0.85:
        need_cortex = True
    need_sub = force or any(
        p.startswith("subconscious") or p.startswith("sub_fail") for p in problems
    )
    need_jobs = force or any("job" in p for p in problems) or need_cortex

    result = soft_reboot(
        reason=reason,
        force_cortex=need_cortex,
        force_sub=need_sub or force,
        requeue_jobs=need_jobs,
        respect_storm=not force,  # admin force can override storm
    )
    result["mode"] = "recovery"
    result["problems_seen"] = problems
    return result


def persist_vitals_snapshot() -> None:
    """Best-effort durable mirror of process vitals (for post-deploy continuity)."""
    try:
        from .energy_agent_sovereign import memory_set
        health = diagnose()
        with SessionLocal() as db:
            memory_set(
                db,
                "watchdog_vitals",
                json.dumps({
                    "at": _now().isoformat() + "Z",
                    "ok": health.get("ok"),
                    "problems": health.get("problems"),
                    "storm": health.get("storm"),
                    "ages": health.get("ages"),
                    "channels": health.get("channels"),
                }, default=str)[:4000],
                source="watchdog",
            )
            db.commit()
    except Exception as e:  # noqa: BLE001
        log.debug("persist_vitals_snapshot: %s", e)
