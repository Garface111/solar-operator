# Sovereign runs in the cloud (laptop optional)

**Yes — the mind keeps running when Ford's computer shuts down.**

## Where things run

| Component | Where | Survives laptop power-off? |
|-----------|--------|----------------------------|
| Subconscious / cortex ticks | Railway **worker** | **Yes** |
| Desk brain drain (replies) | Railway **worker** | **Yes** |
| Code jobs (sandbox-only) | Railway **worker** `/tmp/sovereign-sandbox` | **Yes** (ephemeral disk per deploy) |
| Grok brain API | xAI cloud | **Yes** |
| Product DB (memory, jobs, desk transcript) | Railway Postgres | **Yes** |
| Web API (accept desk/admin chat) | Railway **web** | **Yes** |
| Local Sovereign Portal UI (`127.0.0.1:7701`) | Ford laptop | **No** (viewing only) |
| Local Live AO iframe | Ford laptop | **No** (viewing only) |

## Process split (do not break)

| Service | Flags |
|---------|--------|
| **web** | `PROCESS_ROLE=web`, `RUN_SCHEDULER=0`, `SOVEREIGN_ENABLED=0`, `SOVEREIGN_DESK_ENABLED=1`, `SOVEREIGN_DESK_OFFLOAD=1` |
| **worker** | `PROCESS_ROLE=worker`, `RUN_SCHEDULER=1`, `SOVEREIGN_ENABLED=1`, `SOVEREIGN_PAUSE=0`, `SOVEREIGN_DESK_DRAIN=1`, `SOVEREIGN_SUBCONSCIOUS=1`, `SOVEREIGN_BRAIN_PRIMARY=grok` |

Web stays boring so AO HTTP never dies. Worker owns the mind.

## Sandbox-only still on in cloud

```
SOVEREIGN_MIND_SANDBOX=1
SOVEREIGN_MIND_SANDBOX_FORCE=1
SOVEREIGN_CODE_LIVE=1
SOVEREIGN_CODE_PUSH=0
SOVEREIGN_CODE_DEPLOY=0
SOVEREIGN_MIND_SANDBOX_ROOT=/tmp/sovereign-sandbox
SOVEREIGN_REPO_CACHE=/tmp/sovereign-repos
```

No main merge / no prod deploy from free-run, even while cloud is up.

## How to talk when laptop is off

- Desk messages enqueue on **web** (admin key or AO desk when enabled) and sit in Postgres.
- **Worker** drains them every ~12s and replies with Grok.
- When you power the laptop back on, Portal Desk history shows the thread.

## Verify cloud mind

```bash
# Worker process
curl -s https://worker-production-8059.up.railway.app/health
# expect: scheduler_running true, desk_drain_job true

# Shared vitals (via web admin)
curl -s -H "Authorization: Bearer $ADMIN_API_KEY" \
  https://web-production-49c83.up.railway.app/admin/sovereign/healthz
# expect: mind_cloud.alive true, sub_age_sec small, channels.primary.alive true
```

## What dies with the laptop

Only the **local glass**: portal UI, Live iframe, local sandbox *mirror* under `/tmp` on the PC.  
The worker has its own sandbox trees; they are not the same disk as the laptop.
