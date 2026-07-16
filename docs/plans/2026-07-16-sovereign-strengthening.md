# Sovereign strengthening: run product + mind together safely

**Date:** 2026-07-16  
**Status:** implemented  
**Goal:** Web (AO + desk) and worker (mind) live at once without Postgres thrash.

## What shipped

| Layer | Change |
|-------|--------|
| **Asymmetric pools** | `api/db.py`: web default 15+15=30; worker default **6+4=10** when `PROCESS_ROLE=worker` |
| **Single-flight** | `api/sovereign_guard.py`: one heavy layer at a time (cortex / jobs / mission / skills / ops_sweep / watchdog force) |
| **Job drain** | Default `SOVEREIGN_JOB_DRAIN_LIMIT=2`, scheduler hard-cap 4 |
| **Guard** | Existing pool skip (0.65) + auto-pause + SOVEREIGN_PAUSE unchanged |
| **Desk ops** | Full `ops_sweep` takes single-flight |

## Env matrix (production target)

### web

```
PROCESS_ROLE=web          # or unset
RUN_SCHEDULER=0
SOVEREIGN_ENABLED=0       # mind off on web
SOVEREIGN_DESK_ENABLED=1
# Optional explicit pools (defaults already web-sized):
# DB_POOL_SIZE=15
# DB_MAX_OVERFLOW=15
```

### worker

```
PROCESS_ROLE=worker
SO_PROCESS=worker
RUN_SCHEDULER=1
DB_POOL_SIZE=6            # optional; auto when PROCESS_ROLE=worker
DB_MAX_OVERFLOW=4
SOVEREIGN_SINGLE_FLIGHT=1
SOVEREIGN_JOB_DRAIN_LIMIT=2
SOVEREIGN_POOL_SKIP_RATIO=0.65
SOVEREIGN_AUTO_PAUSE=1
```

### Mind re-enable ladder (worker only — after code deploy)

1. Observe: `SOVEREIGN_ENABLED=1`, `SOVEREIGN_SUBCONSCIOUS=1`, `SOVEREIGN_WATCHDOG=1`;  
   `CODE_LIVE=0`, `EXPAND=0`, `SKILLS=0`
2. Soft acts: `SOVEREIGN_ACT_ENABLED=1`
3. Last: expand / code / skills one at a time

## Why not a third Railway service

`worker` already exists. A third service only helps if you split **code agents** from **scheduler sense** later. Strength here is **connection budget + serialization**, not more processes.

## Health

- `GET /health` — product alive (web)
- `GET /admin/sovereign/healthz` — `guard.pool`, `guard.single_flight`, `guard.pause`
- Worker logs: `db pool role=worker size=6 overflow=4 capacity=10`

## Related

- `docs/sovereign/HOW_NOT_TO_CRASH.md`
- `docs/plans/2026-07-16-process-split-web-worker.md`
- `docs/plans/2026-07-16-sovereign-reenable-after-split.md`
