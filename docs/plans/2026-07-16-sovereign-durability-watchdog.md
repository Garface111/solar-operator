# Sovereign durability (dual-channel watchdog)

**Date:** 2026-07-16  
**Status:** shipped

## Why

Sovereign must stay hardy under load, recover from stuck jobs / silent layer death,
and never thrash itself into a reboot storm.

## Reference: Mindspace dual-sidecar (`Garface111/interface`)

| Interface piece | What it does | Sovereign mapping |
|-----------------|--------------|-------------------|
| `sidecar-watchdog.sh` | Independent process; only respawns when **both** base & base+2 `/healthz` fail for grace | Scheduler job every 75s (`energy_agent_sovereign_watchdog`) |
| Blue-green ports base / base+2 | Zero-downtime self-edit reboot | **Primary** channel (sub/cortex/jobs) + **recovery** channel (forced soft-reboot) |
| Storm breaker | Cap auto-reboots per window | `SOVEREIGN_WATCHDOG_STORM_MAX` (default 5 / 15m) |
| Active-port self-heal | Fix orphan pointer without killing healthy brain | Clear inflight flags + requeue stuck `running` jobs |
| Durable SessionDB | Mind state survives process death | `ea_sovereign_memory` + world JSON + recovery notes |

## Research patterns we copied (not invented)

1. **Supervisor + workers** (LangGraph multi-agent supervisor) — one coordinator, specialized layers (subconscious / cortex / jobs).
2. **External supervisor / “let it crash”** (Erlang-style) — independent probe that restarts a failed unit without trusting it to heal itself.
3. **Circuit breaker** — after a recovery burst, open the breaker and cool down (interface reboot ledger).
4. **Self-healing observe → diagnose → act** (Adaptive multi-agent papers; Hermes closed loop) — write vitals + system notes so the mind remembers its own restarts.

## Ops

- Health: `GET /admin/sovereign/healthz`
- Force soft reboot: `POST /admin/sovereign/reboot?force=true`
- Kill watchdog: `SOVEREIGN_WATCHDOG=0`
- Stress: `scripts/stress_sovereign_watchdog.py` (+ `--live` with admin key)

## Soft reboot (does **not** kill Railway web)

1. Requeue stuck `running` jobs + transient failures  
2. Clear process inflight markers  
3. Force subconscious tick  
4. Force cortex tick if cortex was stale  
5. Optional single job drain  
