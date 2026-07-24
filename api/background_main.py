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
    # Surface desk drain ownership so ops can confirm web never runs the brain.
    desk_drain = False
    return {
        "ok": True,
        "role": "worker",
        "service": "solar-operator-worker",
        "scheduler_running": bool(getattr(aps, "running", False)),
        "desk_drain_job": desk_drain,
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
