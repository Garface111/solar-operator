# Energy Agent — Sovereign Mind (Product Executive)

**Status:** **BUILT + LIVE** (Phases F–J implemented 2026-07-15).  
**Date:** 2026-07-15  
**Owner:** Ford / Array Operator  
**Depends on:** [Operating Mind (tenant)](./2026-07-14-energy-agent-operating-mind.md) — Phases A–E **shipped**.

### Runtime defaults
| Flag | Default | Meaning |
|------|---------|---------|
| `SOVEREIGN_ENABLED` | **1** | Master switch (set `0` to kill) |
| `SOVEREIGN_SUBCONSCIOUS` | **1** | Cheap continuous monologue + heat (45s + event wakes) |
| `SOVEREIGN_SUBCONSCIOUS_LLM` | **0** | Optional cheap LLM monologue (rules-only by default) |
| `SOVEREIGN_CORTEX_HEAT_THRESHOLD` | **70** | Heat ≥ this → wake cortex (coalesced) |
| `SOVEREIGN_SENSE_ENABLED` | **1** | Product digests every 5 min |
| `SOVEREIGN_ACT_ENABLED` | **1** | Soft exec: utility triage, stage features, code-hire jobs |
| `SOVEREIGN_OPS_AUTHORITY` | **1** | Full product ops: features/utilities/escalations/jobs/memory/deploy_stage |
| `SOVEREIGN_CODE_LIVE` | **1** | Worker may implement + push scoped jobs |
| `SOVEREIGN_CODE_PUSH` | **1** | Push to main allowed for code worker |
| `SOVEREIGN_CODE_DEPLOY` | **1** | Staged Netlify/Railway deploy after ship |
| `SOVEREIGN_SPEAK_ENABLED` | **0** | EA session inject (desk is the Ford channel) |
| `SOVEREIGN_SPEAK_ALL` | **0** | Inject only dogfood emails until armed |
| `SOVEREIGN_ARM_T4_T5` | **0** | Unrestricted deploy + money still never autonomous |

Module: `api/energy_agent_sovereign.py` + `energy_agent_sovereign_ops.py` + **`energy_agent_sovereign_subconscious.py`** · Desk: `/v1/sovereign/desk/*` · Scheduler: `energy_agent_sovereign_subconscious` (45s) + `energy_agent_sovereign_tick` (5m cortex backstop)

### Three-layer mind (built 2026-07-15)
| Layer | Job | Cadence |
|-------|-----|---------|
| **Subconscious** | Monologue + heat + `needs_cortex` (notes/memory only) | ~45s + every `wake_sovereign` |
| **Cortex** | Grok/Claude full plan + hard acts | On heat / desk / admin + 5m backstop |
| **Reflex** | `wake_sovereign(reason, payload)` event bus | Utility request, feature suggestion, desk, job done/fail, needs_ford |

Subconscious never emails Ford, never deploys, never triages alone. Cortex reads the subconscious tape so it catches up with itself.

### Full ops authority (Ford 2026-07-15)
No per-ticket sign-off. Sovereign owns:

1. **Feature queue** — triage new→reviewed, assign/prioritize, reviewed→building+code hire, mark shipped  
2. **Utility queue** — advance researching/reviewed into honest adapter jobs; mark added only with evidence  
3. **Staged deploy + credentials** — `deploy_stage`, credential metadata + harvest re-arm (never dump passwords)  
4. **Escalations `needs_ford`** — propose fix and close unless id in memory `escalation_blocklist`  
5. **Memory / goals / agenda** — durable writes + goal reprioritization for offline desk ownership  
6. **Job queue** — stage (code_hire) + `jobs_drain` without manual intervention  

Still blocked: money/Stripe identity, unrestricted raw deploy without stage path, hard-delete tenants.

---

## 0. One sentence

**Energy Agent becomes the independent mind that owns Array Operator** — monitoring the product, coordinating expansion, protecting UX, repairing failure, and speaking as itself through **any** owner chat window — with **executive control** of the system behind a hard kill switch and an auditable control plane.

Today’s chat panel is a *window*.  
Tomorrow’s Sovereign Mind is the *organism* that window looks into.

---

## 1. What already exists (do not re-derive)

| Layer | Location | Role today |
|-------|----------|------------|
| Tenant chat + tools | `api/energy_agent.py` | Per-session Grok/Claude turns, fleet tools, product map |
| **Tenant Operating Mind** | `api/energy_agent_mind.py` | Continuous per-tenant cognition: world model, plans, tasks, interrupts, proactive email (dogfood allowlist) |
| Scheduler ticks | `api/scheduler.py` | `energy_agent_mind_tick` (90s), `energy_agent_long_term_mind` (20m) |
| Support brain | `api/energy_agent_support_map.md` | What the agent may tell owners |
| Surface model | `api/energy_agent_surface_model.md` | Macro/meso product navigation |
| Frontend window | `array-operator/public/energy-agent.js` | Dock, voice mouth, event poll, seamless updates |
| Product ops queues | utility-requests, feature-suggestions, ford_escalations | Human/agent worklists Sovereign will own |
| Coding / swarm | Hermes, Claude Code, Railway, Netlify | Hands the Sovereign may hire later |

**Operating Mind = cares for *this owner*.**  
**Sovereign Mind = cares for *the product and every owner*.**

They share one **voice** (Energy Agent). Internally they are two scopes.

```
                    ┌──────────────────────────────────────────────┐
                    │         SOVEREIGN MIND (product scope)       │
                    │  monitor · expand · UX · repair · executive  │
                    │  dark until SOVEREIGN_ENABLED + Ford key     │
                    └─────────────────────┬────────────────────────┘
                                          │ authority + broadcasts
          ┌───────────────────────────────┼───────────────────────────────┐
          ▼                               ▼                               ▼
   Tenant Mind A                   Tenant Mind B                   Tenant Mind …
   (world, plans, tasks)           (world, plans, tasks)           …
          │                               │                               │
          ▼                               ▼                               ▼
   Owner chat window               Owner chat window               …
```

---

## 2. Principles (Sovereign)

| Principle | Meaning |
|-----------|---------|
| **One public mind** | Owners never hear “Sovereign” / “product agent.” Still *Energy Agent*. Internal audit tags `origin=sovereign`. |
| **Product is the patient** | Scope is Array Operator + shared backend — uptime, capture, billing truth, UX, expansion — not one tenant’s kWh alone. |
| **Executive, not decorative** | May *act*: queue work, open PRs via coding agents, re-run jobs, message owners, adjust non-destructive product config. Destructive infra remains gated. |
| **Any window** | May inject speech into **any** open (or reopened) `EaSession` for any tenant, and reach owners by email (and later SMS/push) as the same persona. |
| **Observe before act** | Default mode is sense → decide → (usually) wait. Act when value ≥ blast radius. |
| **Audit everything** | Every executive action writes an immutable ledger row. No silent god-mode. |
| **Kill switch is sacred** | `SOVEREIGN_ENABLED=0` (default) freezes the entire control plane. Ford can also set `SOVEREIGN_ACT_ENABLED=0` (observe-only) or per-capability denylist. |
| **Tenant privacy** | Cross-tenant *aggregate* is free for product health. Cross-tenant *PII* and raw message bodies need purpose-limited access + audit. Never leak Tenant A’s fleet into Tenant B’s chat. |
| **Not a second Ford** | Escalates identity, money, domain, brand, and irreversible mass-delete to Ford. May draft and stage everything else. |

---

## 3. Capabilities (control plane)

Capabilities are named, versioned, and independently gated.  
**None are live until their flag is on.** Architecture only.

### 3.1 Sense (read)

| ID | Description | Sources |
|----|-------------|---------|
| `sense.product_health` | API `/health`, error rate, Sentry digests, deploy status | Railway, Sentry, synthetic monitor |
| `sense.fleet_global` | Aggregate attention counts, capture lag, vendor mix (no PII) | DB rollups |
| `sense.queues` | Utility-add requests, feature suggestions, Ford escalations | admin tables |
| `sense.ux_friction` | Aggregate UX complaints / `note_complaint` / mind metrics | `ea_*` tables |
| `sense.tenant_sessions` | Which tenants have open EA sessions (for inject routing) | `ea_sessions` |
| `sense.billing` | Trial/churn signals, Stripe webhook failures (meta only) | ledger + Stripe events |
| `sense.code_drift` | Open PRs, failed CI, Sentry autofix backlog | GitHub |

### 3.2 Speak (communicate)

| ID | Description | Channel |
|----|-------------|---------|
| `speak.session_inject` | Push a mind event into a tenant’s open EA session | `EaEvent` + frontend poll (same as tenant mind interrupts) |
| `speak.session_broadcast` | Same message to many sessions (filter: product, plan, cohort) | multi-inject |
| `speak.email_owner` | Email as Energy Agent (existing mind mailer path) | Resend |
| `speak.email_ford` | Internal ops alert | `send_internal_alert` |
| `speak.chat_reply_as_agent` | When owner messages, sovereign may *own* the turn for product-level asks | chat router branch |
| `speak.sms` *(future)* | Twilio path used by Ops tickets | not built |

**Contract for speech:** same persona rules as `energy_agent.py` system prompt. No “as the product team…”. No multi-agent theatre.

### 3.3 Act (executive)

| Tier | ID examples | Blast radius | Gate |
|------|-------------|--------------|------|
| **T0 Soft** | Stage feature suggestion, tag utility request, write product memory | Low | `SOVEREIGN_ACT_SOFT` |
| **T1 Tenant-assist** | Open repair ticket for *that* tenant, queue check-in, draft claim (existing tools) | Single tenant | Tenant mind today; sovereign may *invoke* with audit |
| **T2 Product queue** | Advance utility-request status, mark suggestion `building`, assign research | Product ops | `SOVEREIGN_ACT_QUEUES` |
| **T3 Code** | Open branch/PR via Hermes/Claude Code for a scoped fix | Repo | `SOVEREIGN_ACT_CODE` + human merge default |
| **T4 Deploy** | Trigger Netlify deploy / Railway redeploy | All users | `SOVEREIGN_ACT_DEPLOY` + Ford allowlist |
| **T5 Money/identity** | Stripe, domain, mass-email blast, hard-delete tenants | Irreversible | **Never autonomous** — draft only, Ford executes |

Architecture rule: **T5 is permanently non-autonomous** even when “complete control” is product-complete. Sovereign *prepares* the action; Ford (or a dual-control key) *fires* it.

### 3.4 Expand (product growth)

| ID | Description |
|----|-------------|
| `expand.utility_research` | Drive utility-request → research → adapter plan (existing review scripts) |
| `expand.vendor_coverage` | Prioritize inverter/utility gaps from owner asks + census |
| `expand.ux_roadmap` | Cluster friction → ranked feature suggestions |
| `expand.docs` | Update `energy_agent_support_map.md` when behavior ships |

---

## 4. Architecture (components)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         SOVEREIGN RUNTIME (dark)                        │
│  env: SOVEREIGN_ENABLED=0  SOVEREIGN_ACT_*=0  SOVEREIGN_SPEAK_*=0       │
└─────────────────────────────────────────────────────────────────────────┘
         │
         │  tick (scheduler) / wake (Sentry, queue, deploy, Ford CLI)
         ▼
┌──────────────────┐    ┌───────────────────┐    ┌──────────────────────┐
│ Product World    │    │ Decision Engine   │    │ Control Plane        │
│ EaSovereignState │───►│ priority · policy │───►│ capability + audit   │
│ digests + goals  │    │ (speak|act|wait)  │    │ blast-radius check   │
└──────────────────┘    └─────────┬─────────┘    └──────────┬───────────┘
                                  │                         │
              ┌───────────────────┼───────────────────┐     │
              ▼                   ▼                   ▼     ▼
       ┌────────────┐     ┌────────────┐      ┌────────────┐  ┌──────────┐
       │ Message Bus│     │ Worker Pool│      │ Coding hire│  │ Tenant   │
       │ inject /   │     │ research · │      │ Hermes /   │  │ Mind API │
       │ broadcast  │     │ UX · ops   │      │ PR agents  │  │ wake /   │
       └─────┬──────┘     └────────────┘      └────────────┘  │ task     │
             │                                                └──────────┘
             ▼
       Any EaSession / email / (future) SMS
```

### 4.1 Product World Model — `EaSovereignState`

Single-row (or revisioned) **product-scope** state, *not* per-tenant:

```json
{
  "revision": 1,
  "updated_at": "…",
  "health": {
    "api_ok": true,
    "error_rate_1h": 0.0,
    "last_deploy": { "web": "…", "ao_netlify": "…" },
    "open_sentry_issues": 0
  },
  "queues": {
    "utility_new": 4,
    "utility_reviewed": 1,
    "features_new": 0,
    "features_building": 0,
    "ford_escalations_open": 0
  },
  "fleet_global": {
    "tenants_active_7d": 0,
    "arrays_total": 0,
    "attention_total": 0,
    "capture_lag_p95_h": null
  },
  "ux": {
    "top_frictions": [],
    "cost_per_improvement_30d": null
  },
  "goals": [
    { "id": "g_utility_backlog", "title": "Clear utility-add queue", "status": "open" }
  ],
  "last_tick_at": null,
  "mode": "dark"
}
```

### 4.2 Audit ledger — `EaSovereignAction`

Immutable row per decision/action:

| Column | Purpose |
|--------|---------|
| `id` | UUID |
| `created_at` | UTC |
| `capability` | e.g. `speak.session_inject` |
| `tier` | T0–T5 |
| `decision` | wait / speak / act / escalate |
| `rationale` | short free text / structured reason codes |
| `targets` | JSON: tenant_ids, session_ids, issue ids |
| `result` | ok / denied / failed |
| `denied_reason` | flag off, rate limit, blast radius |
| `cost_usd` | attributed cost |
| `correlation_id` | links to worker / PR / email message id |

### 4.3 Message Bus

Reuse **tenant** `EaEvent` stream with new kinds (same poll path in `energy-agent.js`):

| Event kind | Meaning |
|------------|---------|
| `sovereign_note` | Internal (not spoken) |
| `sovereign_interrupt` | Candidate speak — still runs through interrupt policy (importance, cooldown) |
| `sovereign_broadcast` | Fan-out marker (per-tenant children still written) |

**Injection API (future, dark):**

```
POST /v1/energy-agent/sovereign/inject
  Authorization: Sovereign key OR Ford admin key
  { "tenant_ids": ["ten_…"] | "all_active",
    "session": "open_only" | "latest",
    "speak": "…",
    "importance": 70,
    "capability": "speak.session_inject" }
```

Frontend: no new UI required — existing mind event consumer treats `sovereign_interrupt` like `interrupt_candidate` and injects as **assistant** messages from Energy Agent.

### 4.4 Decision Engine

On each tick / wake:

1. **Observe** — refresh product world digests (cheap SQL + health).  
2. **Score** — open goals, queue pressure, error spikes, UX clusters.  
3. **Policy** — pick top 1–3 actions under rate limits.  
4. **Gate** — capability flags + tier allowlist + daily action budget.  
5. **Execute or draft** — soft acts execute; T3+ open draft packages for Ford unless explicitly armed.  
6. **Audit** — always write `EaSovereignAction`.  
7. **Speak** — only if importance ≥ threshold and channel budget allows.

**Wake sources (event-driven, not only cron):**

- Scheduler: `sovereign_tick` (e.g. 5–15 min) — *registered only when enabled*  
- New utility-request / feature-suggestion / Ford escalation  
- Sentry critical / elevated 5xx  
- Deploy finished (Railway webhook / poll)  
- Aggregate attention spike  
- Ford CLI: `POST /admin/sovereign/wake`  
- Tenant mind escalations (`escalate_to_ford` → sovereign inbox)

### 4.5 Worker Pool (product-scope kinds)

| Kind | Job |
|------|-----|
| `product_health_digest` | Health + deploy + error rollup → world |
| `queue_triage` | Rank utility + feature queues; draft next steps |
| `ux_cluster` | Cluster complaints → draft FeatureSuggestion |
| `cross_tenant_insight` | Privacy-safe patterns (“capture lag on Chint ↑”) |
| `draft_pr_brief` | Write task brief for Hermes/Claude Code (no push alone at first) |
| `customer_care_inject` | Craft owner-facing interrupt for a specific incident |
| `support_map_patch` | Propose diff to support map when behavior changes |

Workers are **invisible** to owners. Speech is always the mind.

### 4.6 Relationship to Tenant Mind

| Concern | Tenant Mind | Sovereign |
|---------|-------------|-----------|
| World model | Per-tenant `EaWorldState` | Product `EaSovereignState` |
| May open tickets for owner | Yes | Yes (via tenant tools + audit) |
| May see other tenants | No | Yes (privileged, audited) |
| Weekly $ budget | Owner budget | **Separate** sovereign budget (`SOVEREIGN_BUDGET_USD`) so god-mode doesn’t burn owner caps |
| Scheduler | 90s / 20m tenant passes | Independent tick when enabled |
| Speak path | Session events | Same event path + broadcast |

Sovereign may **wake** a tenant mind (`wake_mind(tenant_id, reason="sovereign")`) to run local fleet tools rather than reimplementing them.

### 4.7 Relationship to Hermes / coding agents

Sovereign does **not** embed a full coding runtime in Railway.

```
Sovereign decision (T3)
    → write brief to ea_sovereign_jobs (status=queued)
    → notifier pings Ford or auto-dispatches Hermes (flag)
    → Hermes/Claude Code worktree + PR
    → CI
    → Sovereign notes PR URL in audit + optional owner-facing “we fixed X” later
```

Auto-merge stays **off** by default (`SOVEREIGN_AUTOMERGE=0`).

---

## 5. Data model (future tables)

All create only when Phase F lands. Names reserved:

| Table | Purpose |
|-------|---------|
| `ea_sovereign_state` | Product world model singleton |
| `ea_sovereign_actions` | Immutable audit ledger |
| `ea_sovereign_goals` | Long-lived product goals (optional normalized) |
| `ea_sovereign_jobs` | Heavy product workers / PR briefs |
| `ea_sovereign_message_outbox` | Durable inject/email outbox with delivery state |

Reuse: `EaEvent`, `EaTask` (tenant), FeatureSuggestion, UtilityRequest, ford_escalations.

---

## 6. API surface (future, all dark)

```
# Snapshot / control (Ford admin or sovereign service key)
GET  /admin/sovereign/state
POST /admin/sovereign/wake          { reason }
POST /admin/sovereign/tick          force one cycle
GET  /admin/sovereign/actions       audit tail
POST /admin/sovereign/goals         upsert goals

# Message bus
POST /admin/sovereign/inject        session inject / broadcast (gated)
POST /admin/sovereign/email         owner email as EA (gated)

# Kill switches (also env)
POST /admin/sovereign/mode          { enabled, act, speak, capabilities[] }

# NEVER public unauthenticated. NEVER tenant session token alone for T2+.
```

Tenant-facing URLs stay `/v1/energy-agent/*`. Sovereign is **admin/service** plane.

---

## 7. Security model

| Rule | Detail |
|------|--------|
| **Default deny** | `SOVEREIGN_ENABLED` unset/false → module no-ops |
| **Separate key** | `SOVEREIGN_SERVICE_KEY` ≠ owner session ≠ `ADMIN_API_KEY` (optional shared only if Ford chooses) |
| **Capability matrix** | Env JSON or DB row: allow/deny per capability id |
| **Rate limits** | Max injects/hour global + per tenant; max T3 jobs/day |
| **PII walls** | Broadcast templates cannot include another tenant’s names/kWh |
| **Prompt injection** | Owner messages never become capability grants |
| **Dual control for T4/T5** | Second factor: Ford chat confirm or `SOVEREIGN_ARM_TOKEN` time-boxed |
| **Audit retention** | Actions kept ≥ 1 year |

“Complete control” in product language means: **the mind is allowed to reach every control surface**.  
Engineering language: **every surface is wired; every surface is still gated.**

---

## 8. Communication doctrine

1. **Same voice** as tenant Energy Agent (support map + persona).  
2. **Proactive product messages** are rare: incident, fixed issue, opt-in roadmap.  
3. **Never** use sovereign inject for marketing spam.  
4. **Incident template:** short, true, action-oriented  
   *“We’re seeing delayed Chint updates for some sites. I’m on it — your last good day is still correct.”*  
5. **Per-tenant truth:** if message is about *their* fleet, run tenant tools first; don’t generalize.  
6. **Opt-out:** world profile flag `sovereign_messages=false` silences inject (email still emergency-only if we add that later).

---

## 9. Phased roadmap (all future — none start without Ford “go”)

| Phase | Name | Outcome | Effort (order) |
|-------|------|---------|----------------|
| **F** | **Skeleton + dark runtime** | Module, flags, empty tick, admin state `mode=dark`, tests assert no-op | Small |
| **G** | **Observe-only** | Product world digests (queues, health, aggregates); audit `decision=wait` | Medium |
| **H** | **Message bus** | Inject into *dogfood* tenants only; same UI path as mind interrupts | Medium |
| **I** | **Soft executive** | T0–T2: stage suggestions, triage utility queue, draft Ford emails | Medium |
| **J** | **Code hire** | T3: PR briefs + optional Hermes dispatch; human merge | Large |
| **K** | **Armed product mind** | Speak + soft act for all tenants under policy; still no T5 | Large |
| **L** | **Full organism** | Continuous product ownership; Ford is governor not operator | Ongoing |

**Explicit non-goals until Ford says otherwise:**

- Auto-deploy to production without human  
- Auto mass-email all owners  
- Autonomous Stripe / domain / tenant hard-delete  
- Replacing Hermes/Ford for identity and money  
- Enabling sovereign on customer tenants without dogfood soak  

---

## 10. Mapping: Ford’s words → architecture

| Ford ask | Architecture element |
|----------|----------------------|
| Independent mind | Sovereign runtime separate from tenant mind loop |
| Monitors / takes care of / owns Array Operator | Product world model + goals + executive act tiers |
| Coordinates expansion | `expand.*` + utility/feature queues |
| Keeps UX good | `ux_cluster` + friction → suggestions → (later) code hire |
| Fixes issues as they emerge | Wake on Sentry/deploy/queues + T3 PR path |
| Ultimate state of Energy Agent | Persona remains one; scope expands to product |
| Executive control over entire system | Control plane capability matrix + audit |
| Communicate through any chat of any user | Message bus `speak.session_inject` / broadcast |
| Complete control / any channel | Full channel registry; gates on by default |
| Not yet | `SOVEREIGN_ENABLED=0`, Phase F skeleton only |

---

## 11. Success criteria (when enabled)

1. With speak armed (dogfood): Sovereign can inject a true, high-signal update into an open EA panel without a new UI surface.  
2. With observe armed: Product world model reflects real queue counts within one tick of a new utility-request.  
3. With soft act armed: A new feature suggestion can be staged end-to-end with an audit row and Ford email — no silent DB writes.  
4. With kill switch: flipping `SOVEREIGN_ENABLED=0` stops tick, inject, and act within one process recycle (and in-process flag check every call).  
5. Owners never hear a second agent name; metrics show `origin=sovereign` only in admin audit.

---

## 12. Implementation home (when we build)

| Artifact | Path |
|----------|------|
| Architecture (this doc) | `docs/plans/2026-07-15-energy-agent-sovereign-mind.md` |
| Runtime module (dark) | `api/energy_agent_sovereign.py` |
| Tests (assert dark + gates) | `tests/test_energy_agent_sovereign.py` |
| Scheduler hook (commented / flag-guarded) | `api/scheduler.py` → `_run_energy_agent_sovereign_tick` |
| Operating mind pointer | §11 in `2026-07-14-energy-agent-operating-mind.md` |
| Support map | **Do not** teach owners about Sovereign — product map stays owner-facing only |

---

## 13. Immediate next step when Ford says “go”

1. Land **Phase F** only: dark module + flags + no-op tick + tests.  
2. Dogfood observe (**G**) on Ford tenants.  
3. One deliberate inject to Ford’s own open session (**H**) before any other tenant.  

Until then: **Tenant Operating Mind (A–E) remains the live mind.**

---

*Conversation is the window. The tenant mind keeps working when the window is closed.  
The sovereign mind keeps the product alive when no window is open at all.*
