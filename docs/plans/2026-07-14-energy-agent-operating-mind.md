# Energy Agent — Operating Intelligence (Mind)

**Status:** Vision locked + architecture v1 (Ford, 2026-07-14).  
**North star:** *The conversation is one window into a mind that's thinking continuously.*  
**Not:** voice plus a pile of agents. **Is:** one mind that keeps long-running awareness, delegates work behind the scenes, explains when useful, always speaks as itself.

---

## 1. Principles

| Principle | Meaning in product |
|-----------|-------------------|
| **One mind** | User never hears “I spun up an agent.” Subsystems are internal. Voice and text are the same person. |
| **Continuous awareness** | Between turns, a cognitive loop still holds a world model for *this tenant*: fleet health, open intents, pending improvements, harvest status. |
| **Initiative** | May interrupt with a seamless update when something valuable landed (“Quick update: the layout proposal is ready — want a look?”). Never spam. |
| **Truthfulness** | Never invent kWh, $, or status. Never claim background work finished if it failed. Prefer “I don’t know yet — checking.” |
| **Lightweight mind, heavy tools only on win** | Voice + planner are cheap. Coding agents, deep analysis, and infra spin up only when the plan has a clear payoff. |
| **Cost per successful improvement** | Optimize for `$ / shipped win`, not `$ / conversation minute`. A $1 autonomous session that saves hours of UX thrash is a bargain. |

---

## 2. Feeling we must capture

| Old (chatbot) | New (operating intelligence) |
|---------------|------------------------------|
| Turn-bound: user speaks → tools → answer → sleep | Continuous: observe → update world model → reprioritize → decide whether to speak |
| “I’ll call a tool…” | Quiet work; spoken only when it helps the relationship or decision |
| Multiple agent names / handoffs | One voice; “meanwhile I’m looking into…” at most |
| Jump to code on “this dashboard is hard” | Form intent + objectives + tasks; keep talking to refine understanding |

### Worked example — “This dashboard is hard to use.”

**Spoken (immediate):**  
*“I think I understand. Is it finding the information, or making sense of what you see?”*

**Mind (silent plan):**
- Intent: UX friction on current surface  
- Objectives: keep conversation, improve UX, minimize churn  
- Tasks (background):
  1. Snapshot current UI context (tab, selection)  
  2. Note free-text complaint in tenant memory  
  3. Search similar past complaints / digests  
  4. (If warranted) draft UI proposal via existing judge pipeline  
  5. (Optional) analytics / attention patterns  

**Later seamless interrupt (if a task completes with value):**  
*“Quick update: I pulled similar feedback and sketched a layout that puts status first. Want me to open a proposal, or keep refining what ‘hard’ means for you?”*

Never: *“Agent 3 finished coding.”*

---

## 3. Architecture (pipeline)

```
                    ┌─────────────────────────────────────────┐
                    │           CONTINUOUS MIND LOOP          │
                    │  observe → world model → reprioritize   │
                    │       → decide (speak | wait | act)     │
                    └───────────────┬─────────────────────────┘
                                    │
   Voice / Text ──► Mind (persona) ──► Planner ──► Tools / Workers
        ▲                │                │              │
        │                │                ▼              ▼
        │                │         EaTask queue    Event stream
        │                │         (cheap → heavy)  (EaEvent)
        │                └──────────────▲──────────────┘
        │                               │
        └──────── speak only when useful ◄──────────────┘
```

### Layers

| Layer | Role | Cost posture |
|-------|------|----------------|
| **Voice** | Realtime STT/TTS, barge-in, presence | Cheap per minute (efficient model) |
| **Mind** | One persona, conversation, initiative, truth | Lightweight LLM + world model reads |
| **Planner** | Intent → objectives → task graph | Small/fast model or structured rules + LLM |
| **Tools** | Tenant census, fleet, offtakers, UI drive, product map | Existing EA tools (bounded) |
| **Workers** | Heavy: site improvement judge, deep analysis, future coding agents | Spin only on clear win |
| **Event stream** | Task started / progressed / done / failed | Persistence + optional interrupt |
| **World model** | Per-tenant durable + session scratch | Memory + structured JSON |

### Wake conditions (don’t run heavy loop every second)

- User message / voice turn  
- Task completion / failure event  
- Significant fleet change (new attention, harvest fail) — future  
- Scheduled soft tick (e.g. every N minutes while session open, or daily tenant pass)  
- Explicit “keep thinking about X” intent  

---

## 4. Data model (v1)

| Entity | Purpose |
|--------|---------|
| `EaWorldState` | Per-tenant world model blob + revision (fleet snapshot digests, open intents, preferences) |
| `EaTask` | Background unit of work: kind, status, priority, parent plan, result, cost |
| `EaEvent` | Append-only stream: mind/task/system events for interrupt decisions + UI “activity” |
| `EaPlan` | Optional parent of tasks for a user intent (objectives JSON) |
| Existing `EaMemory` / `EaSession` / `EaMessage` / `EaCostLedger` | Conversation + dual memory + budget |

Task kinds (initial):
- `note_complaint` — memory write  
- `snapshot_context` — store UI context  
- `search_similar` — memory / past digests (lightweight)  
- `fleet_pulse` — census + attention summary into world model  
- `propose_ui` — existing `propose_site_improvement` path (heavy, gated)  
- `analyze_focus` — deeper fleet query (medium)  

---

## 5. Conversation contract (always one mind)

1. Acknowledge + refine understanding **before** claiming work is done.  
2. May say *“I’m looking into that in the background”* once — not a status dashboard of agents.  
3. Interrupts are rare, high-signal, and optional to act on.  
4. Background failures: absorb or soft-mention; never invent success.  
5. Money / destructive UI: same hard rules as today (confirm, no Stripe mutations).  

---

## 6. Cost doctrine

| Meter | What |
|-------|------|
| Voice minutes | Realtime path (existing ledger) |
| Mind turns | Chat/planner LLM (existing weekly budget) |
| Worker runs | Task-level cost attribute + ledger reason `worker:<kind>` |
| **North star KPI** | Cost per **successful improvement** (UI shipped, issue resolved, invoice fixed) — not cost per chat turn |

Gating:
- Planner default: rules + one small LLM classify  
- Heavy workers: require `expected_value` ≥ threshold or user confirm for costly paths  
- Weekly $ cap still hard-stops mind+voice; workers respect same budget  

---

## 7. Mapping to today’s Energy Agent

| Today | Becomes |
|-------|---------|
| Turn-bound `_agent_turn` | Mind **foreground** turn (still primary) |
| Tools in process | Tools + **async EaTask** for multi-step work |
| `EaMemory` | Part of world model + explicit notes |
| `propose_site_improvement` | Heavy worker kind `propose_ui` |
| Realtime voice | Voice layer only — no separate “voice agent” identity |
| Session context JSON | Feeds world model + task snapshot |

Non-goals for v1:
- Full multi-agent orchestration UI  
- Autonomous production code deploys without existing judge  
- Replacing hands-off setup / capture with the mind  

---

## 8. Phased build

### Phase A — Foundations (this build)
- [x] Vision doc  
- [x] Tables + mind API (`EaWorldState` / `EaPlan` / `EaTask` / `EaEvent`)  
- [x] Planner on chat: detect intents that spawn background tasks  
- [x] Event stream + “seamless update” channel  
- [x] Persona principles: one mind, continuous awareness, initiative, truth  
- [x] Frontend: subtle mind activity + inject background updates as same voice  

### Phase B — Cognitive tick
- [x] Scheduler soft tick (90s observe → reprioritize → drain for open EA sessions)  
- [x] Interrupt policy hardening (importance score, cooldown, max/hour + max/day)  
- [x] `interrupt_suppressed` events when policy holds a speak back  

### Phase C — Richer workers
- [x] Similar-complaint search across memory + past plans + feature suggestions  
- [x] UX refine intents (finding vs understanding)  
- [x] Full UX proposal loop: friction → clarify → user yes → `propose_ui` → judge pipeline → refresh-and-ask speak  
- [x] `analyze_focus` worker for fleet concerns  

### Phase D — Metrics
- [x] `GET /v1/energy-agent/mind/metrics` — cost per successful improvement, task success rate, interrupt accept rate  
- [x] Interrupt outcomes (`shown` / `accepted` / `dismissed`) for accept-rate  
- [x] Sync shipped feature suggestions → `improvement_win` events  
- [x] Frontend: action chips + soft metrics line in panel footer  

---

## 9. Example API surface (v1)

```
GET  /v1/energy-agent/mind                 world snapshot + open tasks + recent events
POST /v1/energy-agent/mind/tick            run one observe/reprioritize cycle (auth + budget)
GET  /v1/energy-agent/mind/events          poll events since cursor (for seamless updates)
POST /v1/energy-agent/mind/tasks           (internal) enqueue task
POST /v1/energy-agent/chat                 unchanged URL; planner side-effects enqueue tasks
```

---

## 10. Success criteria

1. User can complain about UX and get a clarifying question **and** later a seamless, high-signal update without hearing “agents.”  
2. Background tasks appear only when the mind chooses; costs show on ledger.  
3. Account/fleet truthfulness rules unchanged.  
4. Under API pressure, mind fails soft (no blank Account tab; budget respected).  

---

*Conversation is the window. The mind keeps working when the window is closed.*
