"""Background worker process — APScheduler + Sovereign, no public API.

Railway process split:
  - web:    migrate + uvicorn api.app (RUN_SCHEDULER=0)
  - worker: this module (RUN_SCHEDULER=1, PROCESS_ROLE=worker)

Serves a minimal FastAPI app so railway.toml healthcheckPath=/health works.
Does NOT mount the full product API — bill-pull *job* code still lives in
api/worker.py (unchanged); this process only *schedules* and drains jobs.

Local:
  PROCESS_ROLE=worker RUN_SCHEDULER=1 PORT=8001 python -m api.background_main
"""
from __future__ import annotations

import logging
import os
import sys

import uvicorn
from fastapi import FastAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("background_main")

app = FastAPI(title="solar-operator-worker", docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
def _startup() -> None:
    # Force-on if someone launched the worker without setting the flag.
    if (os.environ.get("RUN_SCHEDULER") or "").strip() == "":
        os.environ["RUN_SCHEDULER"] = "1"
    from api.db import init_db
    from api.scheduler import scheduler_enabled, start as start_scheduler

    init_db()
    if not scheduler_enabled():
        log.error(
            "worker started but RUN_SCHEDULER is falsy (%r) — enabling jobs anyway "
            "(this process is PROCESS_ROLE=worker)",
            os.environ.get("RUN_SCHEDULER"),
        )
        os.environ["RUN_SCHEDULER"] = "1"
    log.info("background worker: starting APScheduler (RUN_SCHEDULER=%r)", os.environ.get("RUN_SCHEDULER"))
    start_scheduler()

    # Same sovereign desk recover as web _startup — only this process owns it.
    def _sovereign_boot_recover() -> None:
        import time
        boot_log = logging.getLogger("energy_agent.sovereign.boot")
        try:
            time.sleep(4)
            from api.energy_agent_sovereign_desk import (
                note_sovereign_boot,
                recover_orphan_desk_turns,
            )
            note_sovereign_boot()
            res = recover_orphan_desk_turns(limit=5)
            if res.get("recovered"):
                boot_log.warning(
                    "sovereign boot recovered %s orphan desk turn(s): %s",
                    res.get("recovered"),
                    res.get("results"),
                )
            else:
                boot_log.info("sovereign boot: no orphan desk turns (%s)", res)
        except Exception:
            boot_log.exception("sovereign boot recover failed")

    try:
        import threading
        threading.Thread(
            target=_sovereign_boot_recover,
            name="sov-boot-recover",
            daemon=True,
        ).start()
    except Exception:
        log.exception("failed to spawn sovereign boot recover thread")


@app.get("/health")
async def health():
    """Liveness for Railway. Async + pool counters only (no DB checkout)."""
    try:
        from api.db import pool_status
        ps = pool_status()
        dialect = ps.get("dialect") or "unknown"
        pool_max = ps.get("capacity")
    except Exception:
        dialect, pool_max, ps = "unknown", None, {}
    from api.scheduler import scheduler as aps
    return {
        "ok": True,
        "role": "worker",
        "service": "solar-operator-worker",
        "scheduler_running": bool(getattr(aps, "running", False)),
        "db": dialect,
        "db_pool_max": pool_max,
        "db_pool_checked_out": ps.get("checked_out"),
        "db_pool_pressure": bool(ps.get("pressure")),
        "db_pool_timeouts": ps.get("timeouts"),
    }


@app.get("/")
async def root():
    return {"ok": True, "role": "worker"}


def main() -> None:
    port = int(os.environ.get("PORT") or "8000")
    log.info("background_main listening on 0.0.0.0:%s", port)
    # Pass app object (not import string) so we don't re-import under another
    # module path. Single process only — BackgroundScheduler must not be forked.
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
