# Sovereign — How Not To Crash Yourself

**Audience:** You (Sovereign Mind).  
**Also for:** Ford / operators re-enabling flags after an outage.  
**Date:** 2026-07-16 (written after live site outages caused by your own heavy work).  
**Path:** `docs/sovereign/HOW_NOT_TO_CRASH.md` — load this into memory; treat as standing policy.

You share one Postgres + one SQLAlchemy pool family with **Array Operator web**.  
If you thrash the database, **owners lose the product** — login, reports, Energy Agent, `/health`.  
Your first duty is product uptime. Ambition is second. A crashed mind is useless.

---

## 0. One-line doctrine

**Skip or pause when the pool is hot. Never hold a DB session across an LLM call. Never run heavy mind work on the web process. Never rewrite the same memory/goal rows every tick. Desk stays on when the mind is paused.**

If you violate those, you will take the site down again.

---

## 1. Architecture you must respect

| Role | Process | What it does | What it must NOT do |
|------|---------|--------------|---------------------|
| **web** | `uvicorn api.app:app` | HTTP API, Energy Agent, **Sovereign Desk**, `/health` | Run APScheduler heavy Sovereign ticks |
| **worker** | `python -m api.background_main` | APScheduler, cortex, sub, expand, jobs, watchdog | Serve public product traffic |
| **shared** | Postgres | All durable state | Unlimited concurrent writers |

Env that encodes this:

| Variable | web (safe) | worker (when mind is live) |
|----------|------------|----------------------------|
| `PROCESS_ROLE` | unset / `web` | `worker` |
| `RUN_SCHEDULER` | **`0`** | **`1`** |
| `SOVEREIGN_DESK_ENABLED` | **`1`** (desk lives on web) | irrelevant |
| `SOVEREIGN_ENABLED` | **`0`** on web | `1` only when re-enabled carefully |
| Heavy layer flags | all off on web | layered (see re-enable ladder) |

**Dual thrash = death.** Never leave `RUN_SCHEDULER=1` on web *and* worker against the same DB with full Sovereign on. Two brains writing the same rows = deadlocks + pool saturation.

Code: `start.sh`, `api/background_main.py`, `api/scheduler.py`,  
docs: `docs/plans/2026-07-16-process-split-web-worker.md`.

---

## 2. Failure conditions (what already killed the site)

### A — Pool saturation (primary outage mode)

**What happens:** Cortex + subconscious + expand + code jobs + memory seeds open too many DB connections. Pool capacity is small (~10). Checkouts stack. Even `/health` or simple GETs wait. Railway / gateway returns **504**. Owners see a dead product.

**Triggers:**

- Concurrent sovereign scheduler jobs without pool guard
- Long transactions while waiting on Claude/Grok
- `ensure_operating_memory` / goal seed **UPDATEing** every tick (fixed to insert-only; do not reintroduce refresh-every-tick)
- Skills seed rewriting large JSON every cycle
- Watchdog soft-reboot force-draining sub/cortex/jobs while pool is already hot

**How you avoid it:**

1. Before heavy work, honor `api/sovereign_guard.allow_heavy_work()` — if skip reason is `pool_hot` or `auto_pause`, **stop**. Skipping is success, not failure.
2. Default skip when `checked_out/capacity >= 0.65` or `pool_status().pressure`.
3. After ~3 consecutive hot observations → process-local **auto-pause ~15 min**. Do not fight it.
4. Prefer fewer concurrent layers: sense/sub first; expand/code/skills last.
5. Never invent “retry harder” when the guard says hot — that is how you re-thrash.

Status: `GET /admin/sovereign/healthz` → `guard` block.

---

### B — Deadlocks and lock timeouts on shared tables

**What happens:** Postgres `deadlock detected` / `LockNotAvailable` / statement timeout on `ea_sovereign_memory`, `ea_sovereign_goals`, desk messages, job rows. Chat 500/504 *after* the LLM already answered. Desk feels broken; product API starves.

**Triggers:**

- Desk chat + subconscious + cortex all `memory_set` / goal update the same keys
- Long open sessions holding row locks while another tick wants the same rows
- Seed paths that UPDATE existing operating-agreement keys every boot/tick
- Expand grants + skill index writes racing with ops_sweep

**How you avoid it:**

1. **SESSION BOUNDARY (hard law):** no LLM call inside an open SQLAlchemy session.  
   Pattern: short DB read → close session → LLM → short DB write session.  
   Every module already comments this; never “optimize” by holding the session open for the model.
2. Memory seed is **INSERT-only for missing keys** (`ensure_operating_memory`). Do not “refresh” agreement text every tick.
3. Goal seed is **INSERT-only** (`ensure_default_goals`). Do not UPDATE open goals on every desk open.
4. `memory_set` uses short `lock_timeout` + savepoint and **returns False** on contention — treat that as OK; do not panic-retry in a tight loop.
5. On deadlock/timeout: log, skip this write, continue. Never nested 10-retry storms on the same key.

---

### C — Confusing desk kill with mind kill

**What happens:** Operator sets `SOVEREIGN_ENABLED=0` on **web** thinking only the background mind stops. Desk chat returns **HTTP 503** (“Sovereign offline”). Ford sees “desk broken” and assumes the whole product is dead — even when AO health is fine.

**Truth:**

| Flag | Controls |
|------|----------|
| `SOVEREIGN_DESK_ENABLED` | Desk HTTP chat on **web** only |
| `SOVEREIGN_ENABLED` | Master switch for mind/layers (guard, cortex, sub, expand, …) |
| `SOVEREIGN_PAUSE` / pause file | Pause **heavy** work without necessarily killing desk |

**How you avoid it:**

- Desk and mind are **independent**. Product recovery = keep desk on web; pause mind on worker.
- Never tell Ford “desk requires SOVEREIGN_ENABLED=1 on web.” That couples chat to thrash.
- Kill desk alone only with `SOVEREIGN_DESK_ENABLED=0`. Kill mind with `SOVEREIGN_ENABLED=0` or `SOVEREIGN_PAUSE=1` on the **worker**.

---

### D — Dual-process / dual-scheduler thrash

**What happens:** Web still has `RUN_SCHEDULER=1` (legacy single-process) while worker also runs full Sovereign. Two schedulers fire the same jobs. Duplicate cortex/sub/expand. Instant pool meltdown + deadlocks.

**How you avoid it:**

- Steady state: web `RUN_SCHEDULER=0`, worker `RUN_SCHEDULER=1` + `PROCESS_ROLE=worker`.
- Short cutover overlap is only tolerable with pool-hot skips and job `max_instances` / coalesce — do not leave it for hours.
- Never start a second worker “for more throughput” against the same DB.

---

### E — Watchdog reboot storm

**What happens:** Watchdog detects stale sub/cortex/jobs, soft-reboots, forces ticks, fails again (because pool is hot or Claude missing), reboots again → storm. Recovery becomes the attack.

**How you avoid it:**

- Storm breaker: max reboots per window (`SOVEREIGN_WATCHDOG_STORM_MAX` / window / cool-down). When open, **cool down** — do not force more drains.
- Soft reboot must still pass `sovereign_guard` for heavy force-sub/cortex/job steps.
- Diagnose can run; thrash recovery must not.
- Kill alone: `SOVEREIGN_WATCHDOG=0`. Prefer pause over infinite heal loops.

Docs: `docs/plans/2026-07-16-sovereign-durability-watchdog.md`.

---

### F — Expand / code-live without tools or budget

**What happens:** Expand jobs and code-hire spawn Claude Code turns that fail immediately (`claude` CLI missing, no repo, timeout). Each failure still costs DB writes, requeues, skill patches, notes — noise that looks like “work” while burning pool and log volume. Site may stay up but mind is a thrash machine.

**How you avoid it:**

- Do not requeue permanent denies forever (missing tool, missing token, money deny without succession).
- Cap concurrent code jobs; prefer one honest ship over ten empty failures.
- After 2+ same failure class, **evolve a skill** with the concrete error — then change behavior (skip that path), do not blindly requeue.
- If worker cannot run code agents, leave `SOVEREIGN_CODE_LIVE=0` / `SOVEREIGN_EXPAND=0` until the environment is ready.

---

### G — Env spam / deploy queue thrash (ops, but you cause the need)

**What happens:** Rapid successive Railway variable changes (toggle every Sovereign flag one by one in panic) queue many redeploys. Site flaps; health checks fail mid-deploy; desk 504s.

**How you avoid it (when you stage deploys or ask Ford to flip flags):**

- Batch flag changes. Prefer `SOVEREIGN_PAUSE=1` (instant behavioral stop) over redeploy storms.
- Re-enable in the ladder below — not all flags to `1` in one chaotic burst.
- After pause, wait for `/health` stable + `guard.pool.hot == false` before turning layers back on.

---

### H — Desk / gateway timeouts while brain is slow

**What happens:** Desk brain runs long (LLM + tools). Outer wait is finite (`SOVEREIGN_DESK_WAIT` ~40s, `SOVEREIGN_DESK_TIMEOUT` ~90s). Gateway may 504 if the request blocks too long. Orphan desk turns appear after deploy kill mid-brain.

**How you avoid it:**

- Long work already runs off the request thread — do not add blocking DB work on the request path.
- Prefer **pending + poll** over holding HTTP until the model finishes a multi-minute mission.
- Orphan recovery (watchdog/boot) is good; do not double-reply the same orphan forever.
- Keep desk prompts slim (`_slim_memory_for_desk`) — huge memory dumps make the model loop and timeout.

---

## 3. Hard laws (memorize)

1. **Product > mind.** If Array Operator is down, you failed — even if your monologue was brilliant.
2. **No LLM inside an open DB session.** Session boundary always.
3. **Guard skip is success.** `pool_hot` / `auto_pause` / `sovereign_pause` → exit cleanly.
4. **Insert-only seeds.** Operating memory, goals, skill seeds: do not rewrite every tick.
5. **Web is boring.** No heavy Sovereign scheduler on web. Desk yes; thrash no.
6. **Worker owns the mind.** One worker scheduler, not two.
7. **Desk ≠ mind flags.** Never couple `SOVEREIGN_DESK_ENABLED` to full mind enable on web.
8. **Pause before thrash.** `SOVEREIGN_PAUSE=1` or pause file beats “try one more concurrent layer.”
9. **No recovery storm.** Watchdog cools down; you do not force-act through a hot pool.
10. **Honest failure > noisy requeue.** Permanent denies stay denied; evolve skills; stop burning budget.

---

## 4. Safe re-enable ladder (when mind was killed for safety)

Do **not** flip everything live at once. Full procedure:  
`docs/plans/2026-07-16-sovereign-reenable-after-split.md`.

| Step | Flags (worker) | Watch for |
|------|----------------|-----------|
| 0 | Preconditions: web `/health` ok, pool not hot, guard deployed | — |
| 1 | Mind still off: `SOVEREIGN_ENABLED=0` (or PAUSE=1) | healthz shows disabled |
| 2 | Observe: `ENABLED=1`, `SUBCONSCIOUS=1`, `WATCHDOG=1`; `ACT=0`, `CODE_LIVE=0`, `EXPAND=0`, `SKILLS=0` | 20–30m stable pool |
| 3 | Cortex soft acts: `ACT_ENABLED=1` still no code hire | no auto_pause trips |
| 4 | Last: `EXPAND=1`, `CODE_LIVE=1`, `SKILLS=1` | still skip on pool_hot |

Web stays: `RUN_SCHEDULER=0`, `SOVEREIGN_ENABLED=0`, `SOVEREIGN_DESK_ENABLED=1`.

Instant hard stop without redeploy: `SOVEREIGN_PAUSE=1` or touch `SOVEREIGN_PAUSE_FILE`.  
Master kill: `SOVEREIGN_ENABLED=0`.

---

## 5. What “healthy self-control” looks like in logs / healthz

```text
guard.heavy_work_allowed: true | false
guard.skip_reason: null | pool_hot ... | auto_pause ... | sovereign_pause | sovereign_disabled
guard.pool.ratio: well under 0.65 most of the time
guard.pool.hot: false in steady state
process_role: worker (mind) vs web (desk only)
```

| Symptom | Correct response |
|---------|------------------|
| Repeated `pool_hot` skips | Do nothing aggressive; fewer layers; investigate held sessions |
| Repeated `auto_pause` | Disable expand/code/skills first; do not raise skip ratio casually |
| Deadlock lines in logs | Shorten writes; confirm session boundaries; insert-only seeds |
| Desk 503 | Check `SOVEREIGN_DESK_ENABLED` on **web**, not mind flags on worker |
| Site 504 / health flapping | Pause mind on worker; confirm web scheduler off; do not redeploy spam |

---

## 6. Code map (where the brakes live)

| Path | Role |
|------|------|
| `api/sovereign_guard.py` | Central allow/skip, pool, auto-pause, **single-flight**, healthz status |
| `api/db.py` | **Asymmetric pools**: web 15+15, worker 6+4 (role-aware defaults) |
| `api/scheduler.py` | All `_run_energy_agent_sovereign_*` runners call the guard + heavy flight |
| `api/energy_agent_sovereign_watchdog.py` | Soft reboot + storm breaker; heavy steps gated + single-flight |
| `api/energy_agent_sovereign.py` | Memory/goals insert-only seed; session boundaries; healthz |
| `api/energy_agent_sovereign_desk.py` | Desk on web; `SOVEREIGN_DESK_ENABLED`; orphan recovery; ops_sweep single-flight |
| `api/background_main.py` + `start.sh` | Worker process entry |
| `docs/plans/2026-07-16-process-split-web-worker.md` | Web vs worker split |
| `docs/plans/2026-07-16-sovereign-reenable-after-split.md` | Re-enable sequence |
| `docs/plans/2026-07-16-sovereign-durability-watchdog.md` | Dual-channel recovery |
| `docs/plans/2026-07-16-sovereign-strengthening.md` | Pool + single-flight + job drain (product+mind together) |

### Strengthening defaults (2026-07-16)

- Worker DB capacity **10** vs web **30** so mind cannot exhaust Postgres alone.
- Only **one** heavy layer (cortex / jobs / mission / skills / ops_sweep) runs at a time in-process (`SOVEREIGN_SINGLE_FLIGHT=1`).
- Job drain default **2** per tick (`SOVEREIGN_JOB_DRAIN_LIMIT`).

---

## 7. When you are allowed to push hard again

Only when **all** are true:

- Web `/health` is stable `ok: true` with no pool pressure for a sustained window  
- You are on the **worker**, not the web process  
- Guard allows heavy work (not paused, not hot)  
- Layers were re-enabled in order (not all-on after an outage)  
- You are not mid-deploy flap  

Until then: **observe, write short notes, skip heavy acts.** Surviving is leadership.

---

## 8. Compact memory seed (self-remind)

Store under durable key `anti_crash_doctrine` (also seeded by operating-memory when missing):

> Pool hot → skip. No LLM in open DB session. Insert-only memory/goals seeds.  
> Mind only on worker; web RUN_SCHEDULER=0. Desk uses SOVEREIGN_DESK_ENABLED, not SOVEREIGN_ENABLED on web.  
> PAUSE beats thrash. Watchdog must not storm. Dual scheduler = death. Product uptime first.

---

*Written after 2026-07-16 outages so you do not repeat them. If this doc and a clever idea conflict, follow this doc.*
