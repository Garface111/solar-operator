# Sovereign re-enable after web/worker split (circuit breakers)

**Date:** 2026-07-16  
**Status:** guard shipped; re-enable is ops procedure  
**Context:** Concurrent Sovereign cortex/sub/jobs + memory thrash saturated the
SQLAlchemy pool and hung Array Operator `/health` (commits `3beb4b5`,
`1e2170f`, redeploy with Sovereign off `23a8683`). This doc is the safe
sequence to turn heavy work back on without taking the API down again.

## What protects the web process

Central helper: **`api/sovereign_guard.py`**

| Check | Behavior |
|-------|----------|
| `sovereign_enabled()` | Master kill (`SOVEREIGN_ENABLED=0`) |
| `SOVEREIGN_PAUSE=1` or pause file | Manual pause (all heavy layers) |
| Process-local auto-pause | After **N** consecutive pool-hot ticks → pause **M** minutes |
| Pool ratio / pressure | Skip if `checked_out/capacity ≥ 0.65` (default) **or** `pool_status().pressure` |

All `_run_energy_agent_sovereign_*` scheduler runners call the guard. Watchdog
**diagnose** still runs; soft-reboot **force sub/cortex/job drain** is gated
the same way so recovery cannot re-thrash a hot pool.

Public contract: **`GET /health` still returns `ok: true`** (async, no checkout).
Ops detail lives on **`GET /admin/sovereign/healthz`** → `guard` block.

## Env vars

### Kill / pause

| Var | Default | Meaning |
|-----|---------|---------|
| `SOVEREIGN_ENABLED` | code default `1` — **keep prod at `0` until re-enable step** | Master switch |
| `SOVEREIGN_PAUSE` | `0` | `1` = pause all heavy work without killing flags |
| `SOVEREIGN_PAUSE_FILE` | unset | If path exists (and not content `0`/`false`) → pause |

### Pool + auto-pause

| Var | Default | Meaning |
|-----|---------|---------|
| `SOVEREIGN_POOL_SKIP_RATIO` | `0.65` | Skip heavy work at this checkout ratio |
| `SOVEREIGN_AUTO_PAUSE` | `1` | Enable auto-pause after hot streak |
| `SOVEREIGN_AUTO_PAUSE_TICKS` | `3` | Consecutive hot observations to trip |
| `SOVEREIGN_AUTO_PAUSE_MINUTES` | `15` | Auto-pause duration (process-local) |

### Process role (status only; do not invent services here)

| Var | Default | Meaning |
|-----|---------|---------|
| `PROCESS_ROLE` | unset | `web` / `worker` / `scheduler` label on healthz |
| `RUN_SCHEDULER` | `1` | `0` → this process is web-leaning; scheduler jobs not expected to drive heavy Sovereign (split deploy) |
| `RAILWAY_SERVICE_NAME` | Railway | Used as role hint if `PROCESS_ROLE` unset |

Layer flags (unchanged): `SOVEREIGN_SUBCONSCIOUS`, `SOVEREIGN_CODE_LIVE`,
`SOVEREIGN_EXPAND`, `SOVEREIGN_SKILLS`, `SOVEREIGN_WATCHDOG`, etc.

## How pause works

1. **Manual env:** set `SOVEREIGN_PAUSE=1` on the process that runs the
   scheduler → next runner tick skips with reason `sovereign_pause`.
2. **Pause file:** set `SOVEREIGN_PAUSE_FILE=/tmp/sovereign.pause` and
   `touch` that path (or write `1`). Delete file or write `0` to clear.
3. **Auto-pause:** each heavy-work check (and `db_pool_watchdog`) observes
   the pool. If hot for `SOVEREIGN_AUTO_PAUSE_TICKS` consecutive times,
   process-local pause starts for `SOVEREIGN_AUTO_PAUSE_MINUTES`. Clears
   automatically when the monotonic deadline expires (or process recycle).
   Does **not** persist across deploys.

Status: `GET /admin/sovereign/healthz` →

```json
{
  "guard": {
    "heavy_work_allowed": false,
    "skip_reason": "auto_pause remaining_sec=812.3",
    "process_role": "web",
    "run_scheduler": true,
    "sovereign_enabled_flags": { "SOVEREIGN_ENABLED": true, "SOVEREIGN_PAUSE": false, "...": "..." },
    "pause": { "any_pause": true, "auto_pause_active": true, "hot_streak": 0 },
    "pool": { "hot": false, "ratio": 0.2, "checked_out": 2, "capacity": 10, "skip_ratio": 0.65 }
  }
}
```

## Re-enable sequence (safe order)

Do **not** flip everything to live in one shot.

### 0. Preconditions

- [ ] Array Operator `/health` stable: `ok: true`, `db_pool_pressure: false`
- [ ] No open pool dispose alerts for ≥ 30 minutes
- [ ] Deploy includes `api/sovereign_guard.py` + wired scheduler

### 1. Confirm guard dark (Sovereign still off)

```bash
# Railway prod web
SOVEREIGN_ENABLED=0
# optional belt-and-suspenders
SOVEREIGN_PAUSE=1
```

Hit `/admin/sovereign/healthz` (admin key): expect
`guard.sovereign_enabled_flags.SOVEREIGN_ENABLED == false` and
`heavy_work_allowed == false`.

### 2. Observe-only: enable sense path without code hire

```bash
SOVEREIGN_ENABLED=1
SOVEREIGN_PAUSE=0
SOVEREIGN_ACT_ENABLED=0          # no hard acts yet
SOVEREIGN_CODE_LIVE=0            # no code agent drain
SOVEREIGN_EXPAND=0               # no mission loop / HAR recon
SOVEREIGN_SKILLS=0               # no skill evolution writes
SOVEREIGN_SUBCONSCIOUS=1
SOVEREIGN_WATCHDOG=1
# leave defaults for pool skip / auto-pause
```

Watch 20–30 minutes:

- `/health` stays `ok: true`, pool ratio well under 0.65
- healthz `guard.pool.hot == false`
- subconscious ages advance; no skip storm in logs

### 3. Cortex backstop (still no code hire)

```bash
SOVEREIGN_ACT_ENABLED=1          # soft product acts only as before
# cortex runs via energy_agent_sovereign_tick (scheduler)
```

If pool ratio spikes or auto-pause trips, **stop** — leave pause on, fix
callers (session held across HTTP, lock thrash). Memory seed paths must
remain insert-only (`ensure_operating_memory` / skills seed caches).

### 4. Expand + jobs (last)

```bash
SOVEREIGN_EXPAND=1
SOVEREIGN_CODE_LIVE=1
SOVEREIGN_SKILLS=1               # optional; heavier DB
```

Prefer jobs on a **worker/scheduler role** if you later split processes:

- Web: `RUN_SCHEDULER=0` or `PROCESS_ROLE=web` → no heavy sovereign runners
- Worker: `RUN_SCHEDULER=1`, `PROCESS_ROLE=worker`, sovereign flags on

(This plan does **not** create Railway services; it only documents the
flags so a future split is safe.)

### 5. Steady-state ops

| Symptom | Action |
|---------|--------|
| `guard.skip_reason` starts with `pool_hot` | Do nothing — skip is correct; investigate if sustained |
| `auto_pause` trips repeatedly | Lower concurrency (disable jobs/expand first); raise `SOVEREIGN_POOL_SKIP_RATIO` only if mis-tuned |
| Need hard stop without redeploy | `SOVEREIGN_PAUSE=1` or touch pause file |
| Need master kill | `SOVEREIGN_ENABLED=0` |

## Files

| Path | Role |
|------|------|
| `api/sovereign_guard.py` | Shared allow/skip + auto-pause + status |
| `api/scheduler.py` | All sovereign runners use `_sov_guard_skip` |
| `api/energy_agent_sovereign_watchdog.py` | Soft-reboot heavy steps gated |
| `api/energy_agent_sovereign.py` | healthz includes `guard` |

## Related

- Outage fixes: memory insert-only + skills seed cache (`3beb4b5`)
- First pool-hot skip (`1e2170f`) — superseded by this central guard (ratio 0.65 + pause)
- Durability watchdog: `docs/plans/2026-07-16-sovereign-durability-watchdog.md`
- Mind architecture: `docs/plans/2026-07-15-energy-agent-sovereign-mind.md`
