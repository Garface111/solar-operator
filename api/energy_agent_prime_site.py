"""Energy Agent Prime — site guardian (uptime first).

Prime is the product mind (formerly "Sovereign" in internal code). Customer-
facing chat remains plain **Energy Agent**. Desk / mind = **Energy Agent Prime**.

Duty #1: keep Array Operator /api healthy. If Prime is thrashing the pool or
the web service is sick, pause heavy work (code jobs, cortex thrash) before
trying anything ambitious. Optionally request a single calm redeploy when
web is repeatedly down (storm-limited).

Kill: SOVEREIGN_SITE_GUARDIAN=0
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

log = logging.getLogger("energy_agent.prime.site")

# Process-local probe state
_state: dict[str, Any] = {
    "last_ok": None,
    "fail_streak": 0,
    "ok_streak": 0,
    "last_probe_at": None,
    "last_result": None,
    "last_pause_trip_at": 0.0,
    "last_revive_at": 0.0,
    "revives_total": 0,
}


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def site_guardian_enabled() -> bool:
    """Default ON when mind is on worker. Kill alone: SOVEREIGN_SITE_GUARDIAN=0."""
    if not _flag("SOVEREIGN_SITE_GUARDIAN", "1"):
        return False
    try:
        from .energy_agent_sovereign import sovereign_enabled
        return bool(sovereign_enabled())
    except Exception:
        return _flag("SOVEREIGN_ENABLED", "0")


def public_name() -> str:
    """Human name for the mind (vs tenant Energy Agent)."""
    return (os.getenv("PRIME_DISPLAY_NAME") or "Energy Agent Prime").strip() or "Energy Agent Prime"


def web_health_urls() -> list[str]:
    urls = []
    for key in ("SOVEREIGN_WEB_HEALTH_URL", "AO_API_URL", "APP_URL"):
        raw = (os.getenv(key) or "").strip().rstrip("/")
        if raw:
            if not raw.endswith("/health"):
                urls.append(raw + "/health")
            else:
                urls.append(raw)
    # Canonical prod web API
    urls.append("https://web-production-49c83.up.railway.app/health")
    # de-dupe preserve order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def probe_web_health(*, timeout: float = 4.0) -> dict[str, Any]:
    """GET /health on web. ok=True only if HTTP 200 + json.ok-ish."""
    urls = web_health_urls()
    errors = []
    for url in urls[:3]:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "EnergyAgentPrime-SiteGuardian/1.0"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(800).decode("utf-8", "replace")
                status = getattr(r, "status", 200) or 200
            ok_json = True
            pressure = False
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    if data.get("ok") is False:
                        ok_json = False
                    pressure = bool(data.get("db_pool_pressure"))
            except Exception:
                pass
            ok = status == 200 and ok_json
            return {
                "ok": ok,
                "url": url,
                "status": status,
                "pool_pressure": pressure,
                "body_snip": body[:160],
                "at": datetime.utcnow().isoformat() + "Z",
            }
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}"[:180])
    return {
        "ok": False,
        "url": urls[0] if urls else None,
        "error": "; ".join(errors)[:400],
        "at": datetime.utcnow().isoformat() + "Z",
    }


def site_is_safe_for_heavy() -> tuple[bool, str]:
    """Cheap gate for job drain / code live — uses last probe if fresh."""
    last = _state.get("last_result") or {}
    age = None
    if _state.get("last_probe_at"):
        age = time.time() - float(_state["last_probe_at"])
    # Fresh enough (<90s): trust cache
    if last and age is not None and age < 90:
        if not last.get("ok"):
            return False, "site_unhealthy"
        if last.get("pool_pressure"):
            return False, "web_pool_pressure"
        return True, "ok"
    # Probe now
    res = probe_web_health()
    _record_probe(res)
    if not res.get("ok"):
        return False, "site_unhealthy"
    if res.get("pool_pressure"):
        return False, "web_pool_pressure"
    return True, "ok"


def _record_probe(res: dict[str, Any]) -> None:
    _state["last_probe_at"] = time.time()
    _state["last_result"] = res
    if res.get("ok") and not res.get("pool_pressure"):
        _state["ok_streak"] = int(_state.get("ok_streak") or 0) + 1
        _state["fail_streak"] = 0
        _state["last_ok"] = time.time()
    else:
        _state["fail_streak"] = int(_state.get("fail_streak") or 0) + 1
        _state["ok_streak"] = 0


def max_concurrent_jobs() -> int:
    """Never more than N code projects at once. Default 1."""
    return max(1, min(3, _env_int("SOVEREIGN_MAX_CONCURRENT_JOBS", 1)))


def job_drain_limit() -> int:
    """Scheduler drain batch size. Default 1 (one project at a time)."""
    raw = _env_int("SOVEREIGN_JOB_DRAIN_LIMIT", 1)
    return max(1, min(raw, max_concurrent_jobs()))


def _trip_pause_for_site(reason: str) -> None:
    """Pause heavy Prime work when site is sick — do not thrash recovery."""
    cooldown = _env_float("SOVEREIGN_SITE_PAUSE_COOLDOWN_SEC", 120.0)
    now = time.time()
    if now - float(_state.get("last_pause_trip_at") or 0) < cooldown:
        return
    _state["last_pause_trip_at"] = now
    try:
        from .sovereign_guard import _trip_auto_pause
        _trip_auto_pause(f"site_guardian:{reason}")
    except Exception as e:  # noqa: BLE001
        log.warning("site_guardian trip pause failed: %s", e)


def _maybe_revive_web(reason: str) -> dict[str, Any]:
    """Storm-limited attempt to wake web via Railway redeploy.

    Only when SOVEREIGN_SITE_REVIVE=1 and fail_streak high. Max once per hour.
    """
    if not _flag("SOVEREIGN_SITE_REVIVE", "0"):
        return {"ok": False, "skipped": True, "reason": "revive off"}
    need = max(2, _env_int("SOVEREIGN_SITE_FAIL_STREAK", 3))
    if int(_state.get("fail_streak") or 0) < need:
        return {"ok": False, "skipped": True, "reason": "streak_low"}
    hour = 3600.0
    if time.time() - float(_state.get("last_revive_at") or 0) < hour:
        return {"ok": False, "skipped": True, "reason": "revive_cooldown"}
    # Prefer railway CLI if present
    import shutil
    import subprocess
    if not shutil.which("railway"):
        return {"ok": False, "skipped": True, "reason": "no_railway_cli"}
    try:
        # Redeploy web service — calm, one shot
        cmd = [
            "railway", "redeploy",
            "--service", "web",
            "--environment", "production",
            "-y",
        ]
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90,
            env=os.environ.copy(),
        )
        _state["last_revive_at"] = time.time()
        _state["revives_total"] = int(_state.get("revives_total") or 0) + 1
        out = {
            "ok": p.returncode == 0,
            "rc": p.returncode,
            "stdout": (p.stdout or "")[:400],
            "stderr": (p.stderr or "")[:400],
            "reason": reason,
        }
        log.warning("site_guardian revive web: %s", out)
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}


def _persist_snapshot(db, res: dict[str, Any], actions: dict[str, Any]) -> None:
    try:
        from .energy_agent_sovereign import memory_set, write_note
        payload = {
            "display_name": public_name(),
            "probe": res,
            "fail_streak": _state.get("fail_streak"),
            "ok_streak": _state.get("ok_streak"),
            "actions": actions,
            "at": datetime.utcnow().isoformat() + "Z",
        }
        memory_set(db, "prime_site_status", json.dumps(payload, default=str)[:8000], source="prime")
        memory_set(
            db, "prime_identity",
            json.dumps({
                "name": public_name(),
                "role": "product_mind",
                "customer_chat": "Energy Agent",
                "duty": "Keep Array Operator up. Never thrash the site to ship faster.",
                "max_concurrent_jobs": max_concurrent_jobs(),
            }),
            source="prime",
        )
        if not res.get("ok") or res.get("pool_pressure"):
            write_note(
                db,
                kind="observation",
                title=f"{public_name()}: site guardian alert",
                body=(
                    f"Site probe unhealthy or hot.\n"
                    f"result={json.dumps(res, default=str)[:1500]}\n"
                    f"actions={json.dumps(actions, default=str)[:800]}\n"
                    "Paused heavy work until pool/site recover. Uptime > ambition."
                ),
                provider="prime_site",
                meta={"fail_streak": _state.get("fail_streak")},
            )
    except Exception as e:  # noqa: BLE001
        log.debug("site_guardian persist: %s", e)


def site_guardian_tick(db=None) -> dict[str, Any]:
    """Probe web health; pause heavy work if sick; optional revive."""
    if not site_guardian_enabled():
        return {"ok": True, "skipped": True, "reason": "site guardian off"}

    res = probe_web_health()
    _record_probe(res)
    actions: dict[str, Any] = {}

    unhealthy = (not res.get("ok")) or bool(res.get("pool_pressure"))
    if unhealthy:
        need = max(1, _env_int("SOVEREIGN_SITE_FAIL_STREAK", 2))
        if int(_state.get("fail_streak") or 0) >= need:
            _trip_pause_for_site(
                "unhealthy" if not res.get("ok") else "pool_pressure"
            )
            actions["auto_pause"] = True
            actions["revive"] = _maybe_revive_web(
                "unhealthy" if not res.get("ok") else "pool_pressure"
            )
    else:
        actions["healthy"] = True

    own_db = False
    if db is None:
        try:
            from .db import SessionLocal
            db = SessionLocal()
            own_db = True
        except Exception:
            db = None
    if db is not None:
        try:
            _persist_snapshot(db, res, actions)
            if own_db:
                db.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("site_guardian db: %s", e)
            if own_db:
                try:
                    db.rollback()
                except Exception:
                    pass
        finally:
            if own_db:
                try:
                    db.close()
                except Exception:
                    pass

    return {
        "ok": bool(res.get("ok")) and not res.get("pool_pressure"),
        "probe": res,
        "fail_streak": _state.get("fail_streak"),
        "actions": actions,
        "display_name": public_name(),
    }


def status_snapshot() -> dict[str, Any]:
    return {
        "enabled": site_guardian_enabled(),
        "display_name": public_name(),
        "max_concurrent_jobs": max_concurrent_jobs(),
        "job_drain_limit": job_drain_limit(),
        "state": {
            "fail_streak": _state.get("fail_streak"),
            "ok_streak": _state.get("ok_streak"),
            "last_probe_at": _state.get("last_probe_at"),
            "last_result": _state.get("last_result"),
            "revives_total": _state.get("revives_total"),
        },
    }
