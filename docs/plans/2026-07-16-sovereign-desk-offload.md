# Sovereign desk offload (web never runs the brain)

**Date:** 2026-07-16  
**Status:** implemented  
**Why:** Desk chat was still running `call_brain` inside the **web** process
(`_desk_pool` + up to ~40s wait). That competed with AO HTTP for the sync
threadpool and Postgres pool and helped produce “Access check failed” / hung
`/health` even after APScheduler moved to the worker.

## Target architecture

| Process | Owns | Desk path |
|---------|------|-----------|
| **web** | Public API, desk *HTTP* (access/history/chat enqueue) | `SOVEREIGN_DESK_OFFLOAD` default **on** for `PROCESS_ROLE=web` — enqueue only |
| **worker** | APScheduler, Sovereign mind, desk **brain drain** | `drain_pending_desk_turns` every ~12s |

```
Ford browser → POST /v1/sovereign/desk/chat (web)
            → INSERT ford row turn_status=thinking  (no LLM)
            → 200 pending:true
worker      → recover_orphan_desk_turns(min_age≈2s)
            → desk_turn → call_brain → sovereign reply
Ford poll   → GET /desk/turn or poll_only chat → complete
```

## Env

| Variable | web | worker | notes |
|----------|-----|--------|-------|
| `SOVEREIGN_DESK_OFFLOAD` | default on via role | default off | force `0` only for single-process debug |
| `SOVEREIGN_DESK_ENABLED` | `1` | `1` | kill desk alone |
| `SOVEREIGN_DESK_DRAIN` | n/a | `1` (default) | kill worker drain |
| `SOVEREIGN_DESK_DRAIN_LIMIT` | — | `2` | max turns per tick |
| `SOVEREIGN_DESK_DRAIN_MIN_AGE_SEC` | — | `2` | avoid racing web commit |
| `SOVEREIGN_ENABLED` | **`0`** | `1` | mind heavy work stays off web |

## Ops checks

```bash
# web must stay lean
curl -s https://web-production-49c83.up.railway.app/health | jq .db_pool_pressure
# worker runs scheduler
curl -s https://worker-production-8059.up.railway.app/health | jq .
```

Single-process local: unset `PROCESS_ROLE` and leave `RUN_SCHEDULER=1` →
offload **off**, desk still thinks inline.

## Code map

- `api/energy_agent_sovereign_desk.py` — `desk_offload_enabled`, `enqueue_desk_message`,
  `drain_pending_desk_turns`, `desk_chat` branch
- `api/scheduler.py` — job `energy_agent_sovereign_desk_drain` (12s)
- Tests: `tests/test_energy_agent_sovereign_desk.py` (offload + enqueue)
