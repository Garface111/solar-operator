"""Circuit breaker + pool-pressure guards for heavy Sovereign work.

Central answer to: "is it safe to run heavy Sovereign work right now?"

Used by every `_run_energy_agent_sovereign_*` scheduler runner (and by
watchdog soft-reboot heavy steps) so Array Operator HTTP stays up when
Sovereign thrash returns (2026-07-16 outage: concurrent cortex/sub/jobs
saturated the SQLAlchemy pool and hung /health).

Skip heavy work if ANY of:
  - not sovereign_enabled()
  - SOVEREIGN_PAUSE=1 (or pause file present)
  - process-local auto-pause is active
  - pool checked_out/capacity >= SOVEREIGN_POOL_SKIP_RATIO (default 0.65)
    OR pool_status()['pressure'] is true

Web process with RUN_SCHEDULER=0 never calls these runners (scheduler not
driving heavy work) — process_role() still surfaces that fact on healthz.

Env (all optional):
  SOVEREIGN_PAUSE=1|0              manual pause (default off)
  SOVEREIGN_PAUSE_FILE=/path       if file exists → treat as paused
  SOVEREIGN_POOL_SKIP_RATIO=0.65   hot threshold (checked_out / capacity)
  SOVEREIGN_AUTO_PAUSE=1|0         enable auto-pause after N hot ticks (default on)
  SOVEREIGN_AUTO_PAUSE_TICKS=3     consecutive hot observations to trip
  SOVEREIGN_AUTO_PAUSE_MINUTES=15  how long process-local pause lasts
  PROCESS_ROLE=web|worker          explicit role label
  RUN_SCHEDULER=0|1                role hint (0 → web-leaning / no scheduler)
  RAILWAY_SERVICE_NAME=...         role hint (contains "worker" → worker)

Kill Sovereign entirely with SOVEREIGN_ENABLED=0 (checked via sovereign_enabled).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("energy_agent.sovereign.guard")

_lock = threading.Lock()

# Process-local auto-pause state (not durable — clears on recycle)
_state: dict[str, Any] = {
    "hot_streak": 0,
    "auto_pause_until": 0.0,  # monotonic deadline; 0 = not paused
    "auto_pause_trips": 0,
    "last_skip_reason": None,
    "last_skip_at": None,
    "last_pool_ratio": None,
    "last_allow_at": None,
    "skips_total": 0,
    "allows_total": 0,
}


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


# ── Process role ─────────────────────────────────────────────────────────────
def process_role() -> str:
    """Best-effort process role for status surfaces (web vs worker).

    Does not enforce scheduling — RUN_SCHEDULER=0 simply means the web process
    is not expected to drive heavy APScheduler sovereign jobs. See also
    `api.scheduler.scheduler_enabled` and docs/plans/2026-07-16-process-split-web-worker.md.
    """
    explicit = (os.getenv("PROCESS_ROLE") or os.getenv("SO_PROCESS") or "").strip().lower()
    if explicit in ("web", "worker", "api", "scheduler", "background"):
        if explicit in ("api",):
            return "web"
        if explicit in ("background",):
            return "worker"
        return explicit
    svc = (os.getenv("RAILWAY_SERVICE_NAME") or os.getenv("RAILWAY_SERVICE") or "").strip().lower()
    if "worker" in svc or "background" in svc:
        return "worker"
    if "web" in svc or "api" in svc:
        return "web"
    # RUN_SCHEDULER=0 on a split deploy → web-only process
    if not scheduler_should_run():
        return "web"
    # Default single-process: web + scheduler colocated
    return "web"


def scheduler_should_run() -> bool:
    """Whether this process is expected to run APScheduler jobs.

    Same rule as `api.scheduler.scheduler_enabled` (RUN_SCHEDULER default on).
    Duplicated here to avoid importing the heavy scheduler module from healthz.
    """
    rs = (os.getenv("RUN_SCHEDULER") or "1").strip().lower()
    return rs in ("1", "true", "yes", "on")


# ── Manual / file pause ──────────────────────────────────────────────────────
def pause_file_path() -> str | None:
    raw = (os.getenv("SOVEREIGN_PAUSE_FILE") or "").strip()
    return raw or None


def env_pause_active() -> bool:
    """SOVEREIGN_PAUSE=1 or pause file present."""
    if _flag("SOVEREIGN_PAUSE", "0"):
        return True
    path = pause_file_path()
    if path and os.path.exists(path):
        try:
            # Empty file or content "1"/"true"/"pause" counts; "0"/"false" clears
            with open(path, "r", encoding="utf-8") as f:
                body = (f.read() or "").strip().lower()
            if not body or body in ("1", "true", "yes", "on", "pause", "paused"):
                return True
            if body in ("0", "false", "no", "off", "clear"):
                return False
            return True  # any other content → pause
        except Exception:
            return True  # unreadable pause file → fail closed for heavy work
    return False


# ── Pool pressure ────────────────────────────────────────────────────────────
def pool_skip_ratio() -> float:
    """checked_out/capacity at or above this → skip heavy work. Default 0.65."""
    r = _env_float("SOVEREIGN_POOL_SKIP_RATIO", 0.65)
    return max(0.1, min(0.99, r))


def pool_pressure_snapshot() -> dict[str, Any]:
    """Non-checkout pool snapshot + whether Sovereign should treat it as hot."""
    try:
        from .db import pool_status
        st = pool_status() or {}
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(e)[:160],
            "hot": False,
            "ratio": None,
            "pressure_flag": False,
            "checked_out": None,
            "capacity": None,
            "skip_ratio": pool_skip_ratio(),
        }
    cap = float(st.get("capacity") or st.get("db_pool_max") or 0) or 0.0
    out_raw = st.get("checked_out")
    if out_raw is None:
        out_raw = st.get("db_pool_checked_out")
    try:
        out = float(out_raw) if out_raw is not None else 0.0
    except (TypeError, ValueError):
        out = 0.0
    ratio = (out / cap) if cap > 0 else None
    pressure_flag = bool(st.get("pressure"))
    thresh = pool_skip_ratio()
    hot = pressure_flag or (ratio is not None and ratio >= thresh)
    return {
        "ok": True,
        "hot": hot,
        "ratio": round(ratio, 4) if ratio is not None else None,
        "pressure_flag": pressure_flag,
        "checked_out": int(out) if out_raw is not None else None,
        "capacity": int(cap) if cap else None,
        "skip_ratio": thresh,
        "dialect": st.get("dialect"),
        "timeouts": st.get("timeouts"),
        "high_water": st.get("high_water"),
    }


def pool_too_hot() -> bool:
    """True when pool is under pressure or at/above skip ratio."""
    return bool(pool_pressure_snapshot().get("hot"))


# ── Auto-pause ───────────────────────────────────────────────────────────────
def auto_pause_enabled() -> bool:
    return _flag("SOVEREIGN_AUTO_PAUSE", "1")


def auto_pause_ticks() -> int:
    return max(1, _env_int("SOVEREIGN_AUTO_PAUSE_TICKS", 3))


def auto_pause_minutes() -> float:
    return max(0.5, _env_float("SOVEREIGN_AUTO_PAUSE_MINUTES", 15.0))


def auto_pause_active() -> bool:
    with _lock:
        until = float(_state.get("auto_pause_until") or 0.0)
        if until <= 0:
            return False
        if time.monotonic() >= until:
            _state["auto_pause_until"] = 0.0
            return False
        return True


def auto_pause_remaining_sec() -> float | None:
    with _lock:
        until = float(_state.get("auto_pause_until") or 0.0)
        if until <= 0:
            return None
        left = until - time.monotonic()
        if left <= 0:
            _state["auto_pause_until"] = 0.0
            return None
        return round(left, 1)


def _trip_auto_pause(reason: str) -> None:
    minutes = auto_pause_minutes()
    until = time.monotonic() + minutes * 60.0
    with _lock:
        _state["auto_pause_until"] = until
        _state["auto_pause_trips"] = int(_state.get("auto_pause_trips") or 0) + 1
        trips = _state["auto_pause_trips"]
        _state["hot_streak"] = 0
    log.warning(
        "sovereign_guard auto-pause ON for %.1fm (trip #%s) reason=%s",
        minutes, trips, reason,
    )


def clear_auto_pause() -> dict[str, Any]:
    """Clear process-local auto-pause (does not clear SOVEREIGN_PAUSE env/file)."""
    with _lock:
        was = float(_state.get("auto_pause_until") or 0.0) > time.monotonic()
        _state["auto_pause_until"] = 0.0
        _state["hot_streak"] = 0
    return {"ok": True, "cleared": was}


def note_pool_observation(*, hot: bool, detail: str | None = None) -> None:
    """Record a pool observation; trip auto-pause after N consecutive hot ticks.

    Call from the guard check path (or pool watchdog) so streak tracks reality.
    Cool observation resets streak; does not clear an already-active auto-pause
    (that expires by wall/monotonic clock only, or clear_auto_pause()).
    """
    if not auto_pause_enabled():
        with _lock:
            _state["hot_streak"] = 0
        return
    if hot:
        with _lock:
            _state["hot_streak"] = int(_state.get("hot_streak") or 0) + 1
            streak = _state["hot_streak"]
        need = auto_pause_ticks()
        if streak >= need and not auto_pause_active():
            _trip_auto_pause(detail or f"pool_hot streak={streak}")
    else:
        with _lock:
            if _state.get("hot_streak"):
                log.info(
                    "sovereign_guard pool cool (was hot_streak=%s)",
                    _state.get("hot_streak"),
                )
            _state["hot_streak"] = 0


# ── Central gate ─────────────────────────────────────────────────────────────
def allow_heavy_work(layer: str = "sovereign") -> tuple[bool, str]:
    """Return (allowed, reason). reason is 'ok' when allowed, else skip code.

    Side effect: updates pool hot-streak / may trip auto-pause when pool is hot.
    Does not open a DB connection.
    """
    # 1) Master flag
    try:
        from .energy_agent_sovereign import sovereign_enabled
        if not sovereign_enabled():
            return _skip(layer, "sovereign_disabled")
    except Exception:
        return _skip(layer, "sovereign_import_error")

    # 2) Manual / file pause
    if env_pause_active():
        return _skip(layer, "sovereign_pause")

    # 3) Process-local auto-pause
    if auto_pause_active():
        left = auto_pause_remaining_sec()
        return _skip(layer, f"auto_pause remaining_sec={left}")

    # 4) Pool pressure
    snap = pool_pressure_snapshot()
    with _lock:
        _state["last_pool_ratio"] = snap.get("ratio")
    note_pool_observation(
        hot=bool(snap.get("hot")),
        detail=(
            f"ratio={snap.get('ratio')} pressure={snap.get('pressure_flag')} "
            f"out={snap.get('checked_out')}/{snap.get('capacity')}"
        ),
    )
    # Re-check auto-pause in case this observation just tripped it
    if auto_pause_active():
        left = auto_pause_remaining_sec()
        return _skip(layer, f"auto_pause_tripped remaining_sec={left}")

    if snap.get("hot"):
        ratio = snap.get("ratio")
        return _skip(
            layer,
            f"pool_hot ratio={ratio} pressure={snap.get('pressure_flag')} "
            f"thresh={snap.get('skip_ratio')}",
        )

    with _lock:
        _state["allows_total"] = int(_state.get("allows_total") or 0) + 1
        _state["last_allow_at"] = time.time()
        _state["last_skip_reason"] = None
    return True, "ok"


def should_skip_heavy(layer: str = "sovereign") -> tuple[bool, str]:
    """Convenience: (skip, reason). skip True means do not run heavy work."""
    allowed, reason = allow_heavy_work(layer)
    return (not allowed), reason


def _skip(layer: str, reason: str) -> tuple[bool, str]:
    with _lock:
        _state["skips_total"] = int(_state.get("skips_total") or 0) + 1
        _state["last_skip_reason"] = reason
        _state["last_skip_at"] = time.time()
    log.warning("sovereign_guard skip layer=%s reason=%s", layer, reason)
    return False, reason


# ── Status surface ───────────────────────────────────────────────────────────
def guard_status() -> dict[str, Any]:
    """Worker-friendly snapshot for /admin/sovereign/healthz (no DB checkout)."""
    try:
        from .energy_agent_sovereign import (
            sovereign_enabled,
            sovereign_act_enabled,
            sovereign_sense_enabled,
            sovereign_speak_enabled,
            sovereign_email_enabled,
        )
        flags = {
            "SOVEREIGN_ENABLED": sovereign_enabled(),
            "SOVEREIGN_ACT_ENABLED": sovereign_act_enabled(),
            "SOVEREIGN_SENSE_ENABLED": sovereign_sense_enabled(),
            "SOVEREIGN_SPEAK_ENABLED": sovereign_speak_enabled(),
            "SOVEREIGN_EMAIL_ENABLED": sovereign_email_enabled(),
            "SOVEREIGN_PAUSE": env_pause_active(),
            "SOVEREIGN_AUTO_PAUSE": auto_pause_enabled(),
        }
    except Exception as e:  # noqa: BLE001
        flags = {"error": str(e)[:160]}

    pool = pool_pressure_snapshot()
    with _lock:
        st = dict(_state)

    remaining = auto_pause_remaining_sec()
    pause_active = env_pause_active() or (remaining is not None)

    # Would we allow right now? (does not double-count streak — use dry path)
    # Call allow_heavy_work would mutate streak; compute a dry reason instead.
    dry_reason = "ok"
    try:
        from .energy_agent_sovereign import sovereign_enabled
        if not sovereign_enabled():
            dry_reason = "sovereign_disabled"
        elif env_pause_active():
            dry_reason = "sovereign_pause"
        elif remaining is not None:
            dry_reason = f"auto_pause remaining_sec={remaining}"
        elif pool.get("hot"):
            dry_reason = (
                f"pool_hot ratio={pool.get('ratio')} "
                f"pressure={pool.get('pressure_flag')}"
            )
    except Exception as e:  # noqa: BLE001
        dry_reason = f"error:{str(e)[:80]}"

    return {
        "heavy_work_allowed": dry_reason == "ok",
        "skip_reason": None if dry_reason == "ok" else dry_reason,
        "process_role": process_role(),
        "run_scheduler": scheduler_should_run(),
        "sovereign_enabled_flags": flags,
        "pause": {
            "env_or_file": env_pause_active(),
            "pause_file": pause_file_path(),
            "auto_pause_enabled": auto_pause_enabled(),
            "auto_pause_active": remaining is not None,
            "auto_pause_remaining_sec": remaining,
            "auto_pause_ticks_need": auto_pause_ticks(),
            "auto_pause_minutes": auto_pause_minutes(),
            "hot_streak": st.get("hot_streak"),
            "auto_pause_trips": st.get("auto_pause_trips"),
            "any_pause": pause_active,
        },
        "pool": pool,
        "stats": {
            "skips_total": st.get("skips_total"),
            "allows_total": st.get("allows_total"),
            "last_skip_reason": st.get("last_skip_reason"),
            "last_skip_at": st.get("last_skip_at"),
            "last_allow_at": st.get("last_allow_at"),
            "last_pool_ratio": st.get("last_pool_ratio"),
        },
        "rules": {
            "skip_if": [
                "not sovereign_enabled()",
                "SOVEREIGN_PAUSE=1 or pause file",
                "process-local auto-pause active",
                f"pool ratio >= {pool_skip_ratio()} or pressure flag",
            ],
            "pool_skip_ratio": pool_skip_ratio(),
        },
    }
