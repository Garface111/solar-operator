# Energy Agent — Voice Operator (Vision + Build Spec)

**Status:** Locked product vision (Ford, 2026-07-13). Ready for phased build.  
**Brand:** Energy Agent (not a white-label Array Operator bot).  
**Surfaces:** Array Operator web app first; chrome/extension hooks as needed.  
**Non-goals (for now):** Merge with “Wish this was better / improve this page” — keep separate until later.

---

## 0. Security note (ops)

- OpenAI keys live **only** in server env (Railway). Never in the browser, never in git, never in chat.
- Client gets **ephemeral Realtime credentials** minted by our backend.
- Any key that has been pasted in chat is **compromised** — rotate it.

---

## 1. Promise & personality

### Promise lines (examples the product must nail)

- *“Help me edit my [Waterford] offtaker.”*
- *“What should I do to improve my arrays’ earnings potential?”*
- *“Why is Londonderry underperforming this week?”*
- *“Draft and show me June’s invoice for L&G — don’t send yet.”*

### Personality

- Generic helpful peer, similar to **Claude / Grok** — direct, clear, not sycophantic.
- Light signature flavor: **genuinely into the Kardashev ladder** and **harvesting the sun** — solar production, capacity factor, turning irradiance into clean power and clean revenue. Never preachy; one beat of wonder is enough.
- **Honest:** says what it can/can’t do; never bluffs numbers.
- **On-task:** stays inside Energy Agent / Array Operator work unless user explicitly digresses.

### Hard refusals (spoken + enforced in tools)

| Never | Behavior |
|---|---|
| Other tenants’ data | Impossible by tenancy; refuse if asked |
| Secrets (passwords, API keys, full bank numbers) | Never speak; mask in transcript UI |
| Automatic charging / Stripe money moves | Never; may open **billing portal / setup link** after soft confirm |
| Silent destructive writes | Always ask permission for navigation + mutations |

---

## 2. Who it serves

| Dimension | Decision |
|---|---|
| Audience | **Every tenant** (signed-in) |
| Isolation | **Unique per tenant** — private memory + tools scoped to `tenant_id` |
| Shared self | **Global agent memory** — behavior improvements shared across all instances (not customer data) |
| Modes | Support · fleet ops · onboarding · billing help · fill-in-forms · investigate · (later) product change proposals |

---

## 3. UX surface

### Entry

- Always-available **circle / sun orb** (Sky theme: sky blues + sun warmth).
- Click → lights up → short voice intro → **mic permission** prompt.
- After enable: **always-on mic** (barge-in / interrupt like GPT Live defaults).
- Full **keyboard + text fallback** in a compact chat panel (images attachable); **voice is primary**.

### Multimodal workspace (while open)

1. **Orb** — listening / thinking / speaking state.  
2. **Transcript strip** — what’s being said.  
3. **Tool timeline** (small window) — live tool calls + results (“Opening Invoices → Waterford → L&G Fabrication…”).  
4. **Browser drive** — agent may **navigate and act in the user’s tab** only after **permission** (“I’ll open that offtaker and change the share — OK?”).  
5. **Screen awareness** — knows current tab, focused entity (array/offtaker), open modals, form dirty state; can use a **DOM/context snapshot** (not raw credentials).

### Session feel

- Natural GPT Live conversation: interrupt, short acknowledgements while tools run.
- Soft confirm for any screen control or write.
- Escalate to Ford on unknown **even if user declines** (“I’ll still flag this for Ford so it doesn’t get lost”).

### Later

- Merge with “improve this page” wish widget — **out of scope for v1**.

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (Array Operator, sky theme)                        │
│  · Orb + transcript + tool timeline + text fallback         │
│  · Browser driver (navigate, click, fill — after confirm)   │
│  · Context packer (tab, selection, form state, viewport)    │
└───────────────┬─────────────────────────────┬───────────────┘
                │ WebRTC / WS (voice)         │ HTTPS tools + UI cmds
                ▼                             ▼
┌──────────────────────────┐    ┌─────────────────────────────────┐
│ OpenAI Realtime          │    │ Energy Agent Orchestrator       │
│ (GPT Live voice I/O)     │◄──►│ (Railway)                       │
│ STT/TTS, barge-in        │    │ · ephemeral keys                │
│ low-latency speech       │    │ · session + cost meter          │
└──────────────────────────┘    │ · tool policy + confirms        │
                                │ · audit / transcripts           │
                                └───────────────┬─────────────────┘
                                                │
                                ┌───────────────▼─────────────────┐
                                │ Heavy agent: Grok 4.5           │
                                │ (+ skills; optional Claude/Hermes
                                │  for code/PR lane)              │
                                │ tools → AO APIs + UI driver     │
                                └─────────────────────────────────┘
```

### Latency path (locked)

1. User mic → **OpenAI Realtime (GPT Live)**  
2. ↔ **Our orchestrator** (turn management, tool policy, memory)  
3. → **Grok 4.5** for heavy reasoning / multi-step plans  
4. Tools execute on server (APIs) and/or browser driver (UI)  
5. Speech returns via Realtime  

### Auth

- Same `so_session` JWT as the dashboard.  
- Realtime: **server-mediated ephemeral keys**. Prefer lowest-latency path that still allows server policy (WebRTC client + server tool channel is fine).

### Models

| Role | Model |
|---|---|
| Voice | OpenAI Realtime / GPT Live (English, default VAD/barge-in) |
| Heavy brain | **Grok 4.5** |
| Code/PR lane (optional second agent) | Hermes / Claude Code style worker in isolated worktree |

### Cost

- **$5 / tenant / week** voice+agent budget (meter Realtime + heavy tokens).  
- Soft warning at 80%; hard stop with text-only remaining or wait for window reset.  
- Ford-visible cost dashboard later.

### Transcripts

- Store full transcripts + tool timeline for **Ford/ops** (product improvement + support).  
- Retention: propose **90 days** (confirm legal).  
- Never store utility passwords in plain text.

---

## 5. Dual memory system

### A. Tenant memory (private)

- Per `tenant_id` (and optional per-user if multi-login later).  
- Facts: fleet nicknames, preferred offtaker workflows, “Bruce likes auto-send off,” open issues, past investigations.  
- **Never** cross-tenant.  
- User-visible “what I remember” summary later (trust).

### B. Global agent memory (shared across instances)

- Improves **agent behavior**, not customer secrets.  
- Examples: better tool sequences for “fix stale Chint,” prompt patches, known product gotchas, successful navigation paths.  
- Write path gated: only from post-session reflection jobs or Ford-approved promotions.  
- No PII in global memory (scrubber required).

---

## 6. Browser driver (required for the vision)

The agent is not only an API client — it **drives the user’s open AO tab**.

### Capabilities

| Action | Examples |
|---|---|
| `ui.navigate` | `#reports`, `#analysis`, `#arrays`, deep-link offtaker |
| `ui.highlight` | Pulse a field / row |
| `ui.focus` | Select offtaker card, open edit form |
| `ui.fill` | Share %, email, rate (after confirm) |
| `ui.click` | Save, Approve, Add offtaker |
| `ui.read` | DOM summary of current panel (no password fields) |
| `ui.screenshot` | Optional context for vision (careful with PII) |

### Permission model

1. **Look** (read context / DOM) — allowed when session open.  
2. **Show** (navigate + highlight) — soft confirm first time per session, then sticky optional.  
3. **Change** (fill/click/save) — **always confirm** with plain-language diff.  
4. **Send external** (email customer, claim email) — confirm + rate limit.  
5. **Money** — never auto; only open Stripe Customer Portal / Connect links our API already generates.

### Implementation sketch

- Content script **or** in-page `window.__eaDriver` registered by AO.  
- Orchestrator pushes `UiCommand` over the agent websocket.  
- Page executes and returns `{ok, url, selection, errors}`.  
- Extension required only when action needs **portal capture outside AO** (GMP login, vendor portal).

---

## 7. Codebase audit → tool surface

Derived from live Array Operator tabs + `/v1/*` APIs (2026-07-13).

### 7.1 Product map (tabs)

| Tab | User jobs | Agent must be able to… |
|---|---|---|
| **Fleet Triage** | See what’s wrong now | Summarize attention queue, open array, explain stale/offline |
| **Inverters** | Layout, reassign, vendor health | Navigate vendor sheet, reassign inverters, explain peer status |
| **Analysis** | Weather-adjusted performance | Sites grid, PI/CF, through-time, forecast model, alarms, events, hardware, files |
| **Trends** (sub of Analysis) | Multi-year production | Monthly/YoY narrative, export CSV |
| **Invoices** | Offtaker billing | CRUD offtakers, drafts, approve/send, reconcile GMP, rates, templates, pay links, archive |
| **Resources** | Policy / REC / news | Answer “what’s the rate climate in VT?” from resources data |
| **Master Account** | Plan, card, company, files, capture | Update profile, open billing portal, capture mode, directory of files |
| **Wish widget** | Feature requests | Later merge; for now separate |

### 7.2 Tool catalog by risk tier

#### Tier 0 — Read (auto)

**Fleet / monitoring**

- `fleet.overview` → `/v1/array-owners/overview`  
- `fleet.tree` → `/v1/array-owners/fleet-tree`  
- `fleet.audit` → `/v1/array-owners/fleet-audit`  
- `fleet.trends` → `/v1/array-owners/fleet-trends`  
- `fleet.forecast` → `/v1/array-owners/forecast-fleet`  
- `fleet.alert_events` / `alert_settings`  
- `array.get` / list arrays  
- `inverter.list` / status  
- `extension.status`  
- `cloud_capture.status`  
- `linked_sources.list`  
- `claims.list`  

**Billing / offtakers**

- `billing.list_bundle` / `subscriptions.list`  
- `billing.subscription.get`  
- `billing.preview` / `preview_math` / `daily_series` / `trends`  
- `billing.drafts.list` / `draft.get`  
- `billing.reconcile_bills` / `audit_by_array`  
- `billing.send_pipeline`  
- `billing.global_rate.get`  
- `billing.utility_accounts.list`  
- `billing.files.list`  
- `billing.invoice_archive.list`  
- `billing.email_template.get`  
- `billing.payments.list` / `payments.connect.get`  
- `billing.tracker.get`  

**Account**

- `account.billing_summary` / `next_invoice`  
- `account.get` (name, company, plan)  
- `onboarding.status`  

**Context (browser)**

- `ui.context` — hash, selection, form snapshot  
- `resources.snapshot` — state briefing payload  

#### Tier 1 — Soft confirm (say what you’ll do)

- `ui.navigate` / `ui.highlight` / `ui.focus`  
- `array.set_location` / `set_reminder` / `set_portfolio`  
- `array.set_geometry` / forecast params / model autofill  
- `billing.subscription.patch` (non-destructive fields: notes, delivery mode)  
- `billing.draft.create` / `draft.patch`  
- `billing.global_rate.put`  
- `cloud_capture.toggle` / `refresh`  
- `account.update_name|company|email`  
- `account.open_billing_portal` / `add_payment_method` / `payments.connect` (open link only)  
- `claims.patch_draft` / cancel-auto  

#### Tier 2 — Hard confirm (explicit yes)

- `billing.draft.approve` / `send_now` / `test` send  
- `billing.subscription.create` / `delete`  
- `billing.bulk_*`  
- `billing.email_template.test_send`  
- `inverters.reassign` / layout reset  
- `claims.send`  
- `utility` credential capture flows  
- External email to offtakers  

#### Tier 3 — Founder / special lane

- **Product code PRs** (see §8) — never silent main deploy from a tenant session without founder path.  
- Cross-tenant admin — **forbidden** in customer agent.  
- Stripe charge/refund/price edit — **forbidden** (portal links only).  

---

## 8. “Claude Code powers” / open PRs (your decision: yes)

**Customer-facing agent must not silently edit production.** Implement as a **gated builder lane**:

| Step | Behavior |
|---|---|
| 1 | User asks for product change (“make this column clearer”) |
| 2 | Agent files structured proposal + optional screenshot |
| 3 | Soft confirm: “I’ll open a PR to improve X” |
| 4 | Spawns **isolated worker** (Hermes/Claude Code worktree) with allowlisted repos |
| 5 | Opens PR; reports link in voice + UI |
| 6 | **Never** auto-merge to main from tenant session |
| 7 | Always **escalate to Ford** (copy of proposal), even if user says not to escalate the *support* issue |

Global agent memory may learn from merged PR patterns later.

---

## 9. Skills library (v1 packs)

| Skill | Owns |
|---|---|
| `fleet-health` | Stale feeds, peer verdicts, triage queue, extension |
| `billing-offtakers` | List, edit, draft, approve, reconcile, rates |
| `utility-capture` | Cloud capture, GMP/extension, utility requests |
| `onboarding` | First connect, plan, empty states |
| `analysis-trends` | PI, sites, through-time, YoY story |
| `resources` | REC/rate/policy briefing |
| `account-plan` | Plan, card portal, company profile |
| `warranty-claims` | Drafts, send gates |
| `product-howto` | “Where is X in the UI?” + ui.navigate |
| `browser-driver` | Permissioned UI control |
| `escalation` | Always-on escalate-to-Ford channel |
| `builder-pr` | Gated PR lane |
| `earnings-advisor` | Earnings potential (honest, model-aware) |

Add packs over time; orchestrator loads only relevant skills per turn (token control).

---

## 10. Safety rails (summary)

1. Tenant scope on every tool.  
2. Confirm before navigate-control (session sticky) and always before mutate/send.  
3. Outbound email review gate for customer-facing sends.  
4. Money: portal links only.  
5. No secret exfiltration; mask in TTS.  
6. Injection: user text/images/screens untrusted.  
7. Kill switch `ENERGY_AGENT_ENABLED` + per-tenant disable.  
8. $5/week cap.  
9. **Escalate unknowns to Ford even if user says no.**  
10. Stay on Energy Agent / AO task.

---

## 11. Legal / disclosure

- First-run: mic permission + short disclosure: *“Energy Agent may record this session to improve support. It only sees your account.”*  
- Privacy policy update: voice transcripts, retention, Ford access.  
- Optional in-app “download my agent history.”

---

## 12. Build phases

| Phase | Ship |
|---|---|
| **P0** | Env secrets, ephemeral Realtime token, orb UI (sky), text chat stub |
| **P1** | Voice loop + Grok 4.5 + Tier 0 tools + tool timeline + transcripts |
| **P2** | Browser driver (navigate/highlight/focus) + soft confirms |
| **P3** | Tier 1 writes (patch offtaker, drafts, location, rates) |
| **P4** | Tier 2 send/approve + outbound review |
| **P5** | Dual memory (tenant + global scrubbed) |
| **P6** | Builder PR lane + always-escalate |
| **P7** | $5/week meter + cost dashboard |
| **P8** | Merge with wish widget (later) |

---

## 13. Success metrics

- Time to first successful tool result  
- Tasks completed without human  
- Confirm abandon rate  
- $/tenant/week vs $5 cap  
- Zero cross-tenant leaks  
- Escalations that catch real product gaps  
- “Was this helpful?” after session  

---

## 14. Open implementation details (small, not product)

- Exact Realtime model id string to pin.  
- Transcript retention days (default proposal 90).  
- Whether multi-user tenants share one memory or split by email.  
- Whether browser driver is pure in-page first (yes) vs extension-first.

---

## 15. One-sentence product

> **Energy Agent is each tenant’s voice-first solar operator** — Kardashev-curious, ruthlessly honest — that **sees their AO screen, drives it with permission, uses the full product via tools, remembers them privately, improves itself globally without sharing customer data, bills no one silently, and escalates anything it can’t solve to Ford.**

---

*Next engineering step when you say go: scaffold P0/P1 (orb + ephemeral Realtime + Tier 0 tool router + transcript table) in `solar-operator` + `array-operator`.*
