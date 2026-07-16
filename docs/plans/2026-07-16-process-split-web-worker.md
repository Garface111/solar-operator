# Process split: web vs background worker

**Date:** 2026-07-16  
**Status:** implemented (Railway service create is ops/parent)

## Why

APScheduler + Sovereign (subconscious / cortex / jobs / watchdog) were running
inside the public FastAPI web process. Heavy ticks competed with HTTP for the
DB pool and threadpool; a hot pool could make even `/health` struggle and
Railway thrash the only process that served traffic.

Split into two Railway services that share the same image and `start.sh`:

| Role | Process | Scheduler | Public API |
|------|---------|-----------|------------|
| **web** | migrate + `uvicorn api.app:app` | **off** (`RUN_SCHEDULER=0`) | yes |
| **worker** | `python -m api.background_main` | **on** | no (only `/health`) |
| harvester | unchanged (`CLOUD_CAPTURE_HARVESTER=1`) | n/a | `/health` only |

`api/worker.py` is **unchanged** — that module is the bill-pull *job executor*
drained by the scheduler (`run_pending_jobs`). The new process is
`api/background_main.py` (name avoids collision).

## Env matrix

| Variable | web | worker | notes |
|----------|-----|--------|-------|
| `PROCESS_ROLE` or `SO_PROCESS` | unset | `worker` | `start.sh` branch |
| `RUN_SCHEDULER` | **`0`** | **`1`** (or unset) | default **1** for old single-process |
| `PORT` | Railway-assigned | Railway-assigned | worker serves `/health` here |
| `DATABASE_URL` | same | same | shared Postgres |
| all other secrets | same as today | same as today | do not drop keys on worker |

Truthiness for `RUN_SCHEDULER`: `1` / `true` / `yes` / `on` (case-insensitive).
Anything else (including `0`) disables the scheduler on that process.

## Code map

- `start.sh` — harvester → worker → web (migrate + uvicorn)
- `api/app.py` `_startup()` — `scheduler.start()` + sovereign boot recover only if `scheduler_enabled()`
- `api/scheduler.py` — `scheduler_enabled()`, idempotent `start()`
- `api/background_main.py` — start scheduler + tiny FastAPI (`GET /health` → `role: worker`)

## Railway steps (parent / ops — do not invent secrets)

1. Keep existing **web** service:
   - Start: `sh start.sh` (default path)
   - Set `RUN_SCHEDULER=0`
   - Healthcheck: `/health` (existing)
2. Add a second service from the same repo/image (e.g. name **worker**):
   - Start: `sh start.sh` (or leave shared `railway.toml` startCommand)
   - Set `PROCESS_ROLE=worker` and `RUN_SCHEDULER=1`
   - Attach same Postgres / env group as web
   - Healthcheck: `/health` → expects `{"ok":true,"role":"worker",...}`
3. Deploy worker first (or together). Until worker is up, web with
   `RUN_SCHEDULER=0` will **not** drain jobs — cut over only when both are ready,
   or leave web at default `RUN_SCHEDULER=1` until worker is healthy, then set
   web to `0`.
4. Confirm:
   - web logs: `scheduler disabled (web role)`
   - worker logs: `background worker: starting APScheduler`
   - worker `/health`: `scheduler_running: true`
   - web `/health`: still `ok: true` without scheduler load

## Local run

```bash
# Terminal A — API only (no scheduler)
RUN_SCHEDULER=0 PORT=8000 uvicorn api.app:app --host 0.0.0.0 --port 8000

# Terminal B — background worker
PROCESS_ROLE=worker RUN_SCHEDULER=1 PORT=8001 python -m api.background_main
# or via start.sh:
PROCESS_ROLE=worker PORT=8001 sh start.sh
```

Single-process (legacy): unset `RUN_SCHEDULER` / leave `1`, no `PROCESS_ROLE` —
web starts scheduler as before.

## Rollout safety

- Default `RUN_SCHEDULER=1` means shipping this code without env changes does
  **not** stop the scheduler on web.
- Only after worker is healthy should web set `RUN_SCHEDULER=0`.
- Never run two workers (or web+worker both with `RUN_SCHEDULER=1`) against the
  same DB long-term — duplicate job ticks. Short overlap during cutover is
  mitigated by job `max_instances` / coalesce and pool-hot skips on Sovereign.
