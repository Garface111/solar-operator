# Array Operator — Desktop Surface Mental Model (3 levels)

**Purpose:** Give the Energy Agent a durable, page-level understanding of the
**desktop** product at arrayoperator.com (wide browser / signed-in SPA under
`array-operator/public/*`). Built from the surface atlas
(`array-operator/docs/surface-atlas/`) plus live shell updates through 2026-07
(Fleet consolidation, Marketplace tab, sky glass).

**How to use (agent):** Before walking a tab, explaining “what this page is for,”
or answering “where am I?”, load `product_map(topic=surface)`. Pair with
`topic=tabs` for labels and `topic=<domain>` for deep mechanics. Prefer this
file for **macro + meso**; prefer DOM tours / `live_ui_digest` for **micro**
lockstep. **If the owner is on mobile (`client` = owner-web | owner-native), load
`product_map(topic=surface_mobile)` instead** — desktop selectors do not match.

**Levels (always think in all three):**
- **MACRO** — why this surface exists in the whole product
- **MESO** — what the owner is trying to accomplish on this visit
- **MICRO** — real controls, selectors, and what they do (no invented UI)

---

## product_spine

### MACRO — the whole product in one sentence
Array Operator does **two jobs** for a solar array owner:
1. **Watch the fleet** — every inverter’s health, peers, dollars at stake.
2. **Invoice offtakers** — turn settled utility bills × share % into solar-credit invoices.

Everything else (Analysis, Repairs, Marketplace, Account, Energy Agent) supports those two jobs.

### Product spine (left → right in the top bar — CURRENT)
```
[Login / anon home]
       │
       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  TOP BAR (always, signed-in) — EXACT labels:                             │
│  Fleet │ Analysis │ Invoices │ Repairs │ Marketplace │ Account │ whoami  │
│  DEFAULT landing = Fleet · Triage (#dashboard)                           │
└──────────────────────────────────────────────────────────────────────────┘
       │
       │  Global chrome (not tabs): Energy Agent orb/panel · Setup FAB
       │  (bottom-left) · Alerts FAB (bottom-right) · optional banners
       ▼
```

| Job | Primary tab | Supporting |
|-----|-------------|------------|
| “Who needs me *right now*?” | **Fleet → Triage** | Alerts FAB |
| “Show me the machines / rearrange” | **Fleet → Sandbox** (spatial) | **Fleet → Table** (vendor sheet) |
| “Why is yield off / weather?” | **Analysis** (Fleet analysis · Trends · Resources) | Sites, Performance, Hardware |
| “Bill my customers” | **Invoices** (Offtakers · Bill audit · Trends · Gen reports) | Account online pay, utility link |
| “Who fixes my fleet when it’s down?” | **Repairs** (chat-first O&M) | service contacts, repair tickets |
| “Sell unallocated credits / demand” | **Marketplace** (Credit Exchange · Array Market) | Invoices vacancy chips |
| “Keep data + plan working” | **Account** | Auto-refresh, card, files |

### CRITICAL NAV CHANGE (do not use the old atlas)
- There is **no** separate top tab named **Inverters** or **Fleet Triage**.
  Equipment canvas + spreadsheet live under **Fleet** as segments **Triage | Table | Sandbox**.
- **Marketplace** is a top tab (`#marketplace`) — Offtaker Exchange (vacancy + demand).
- **Resources** is still only an Analysis segment (`#resources`), not a top tab.
- Empty/unknown hash historically fell through to arrays; **Fleet Triage (`#dashboard`)**
  is the primary attention home. Sandbox is `#arrays` / Fleet → Sandbox.

### Design language (visual system — sky skin)
- **Backdrop:** full-bleed landscape; content in **frosted glass** cards.
- **Primary accent:** sky blue / cyan for active tab, CTAs, healthy metrics.
- **Attention:** amber/orange (underperforming, vendor issue, watch).
- **Critical:** red (rare).
- **Healthy:** green accents / “All good”.
- **Chrome:** pill top tabs; **segmented controls** under tab for sub-views;
  **FABs** bottom corners (Setup left, Alerts right).
- **Density:** Triage = glance tiles; Sandbox = spatial cards; Table = vendor rows;
  Analysis = long sections; Invoices = pipeline + list; Marketplace = vacancy + demand;
  Account = form rows.

### Navigation rules (never invent)
- Top labels only: **Fleet · Analysis · Invoices · Repairs · Marketplace · Account**.
- Never say Dashboard, Arrays, Reports, Operations, Trends, or Resources as top tabs.
- **Triage / Table / Sandbox** are only under **Fleet**.
- **Fleet analysis / Trends / Resources** are only under **Analysis**.
- **Offtakers / Bill audit / Trends / Generation reports** are only under **Invoices**.
- **Credit Exchange / Array Market** are only under **Marketplace** (sub-tab bar may hide
  until a second sub registers).
- **Repairs** (`#ops`) has **no** nested sub-tabs — chat + “what the agent is working on.”

### Atlas shots (historical capture 2026-07-14 — labels may lag shell)
Screenshots live in `array-operator/docs/surface-atlas/shots/`. Re-capture with
`docs/surface-atlas/capture.mjs` after major UI moves. Prefer **this file + live tools**
over stale shot filenames when they conflict with the CURRENT spine above.

| Shot (legacy name) | Maps to CURRENT surface |
|--------------------|-------------------------|
| 00-login | Sign-in |
| 00b-anon-home | Marketing + demo story |
| 01-inverters-sandbox | **Fleet → Sandbox** |
| 02-inverters-spreadsheet | **Fleet → Table** |
| 03-fleet-triage | **Fleet → Triage** |
| 04–08 analysis / invoices / resources | Analysis / Invoices / Analysis→Resources |
| 09-account | Account |
| 10-modal-add-array | Add array (from Fleet Sandbox/Table) |
| 11-panel-fleet-alerts | Alerts settings |
| 12-energy-agent-open | Energy Agent panel (global) |
| 13-hands-off-setup | Setup walkthrough FAB |
| *(no shot yet)* | **Marketplace** — describe from MICRO below |

---

## surface_fleet

**Hash:** `#dashboard` (Triage) · `#arrays` (Sandbox) · Table via Fleet segment ·
**Panel:** `#panelDashboard` · **Top label:** **Fleet** (never “Dashboard” alone).

### MACRO
The **equipment + attention home** of the business. One top tab owns:
1. **Triage** — morning brief / needs-attention queue.
2. **Table** — all vendor data spreadsheet (was “Inverters Spreadsheet”).
3. **Sandbox** — spatial Tenant → Array → Inverter canvas (was “Inverters Sandbox”).

### MESO
- “Does anything need me today?” → Triage.
- “Scan every inverter without moving cards” → Table.
- “Rearrange sites / add array / open vendor” → Sandbox.

### MICRO — sub-nav
Segment `#vsSegDashboard` **Triage** | `#vsSegSheet` **Table** | `#vsSegSandbox` **Sandbox**.

### MICRO — Triage (`#ftDash`)
1. Head “Triage” + live strip `#dashProd` (kW now, kWh today, arrays producing…).
2. KPI grid `#fleetCommander` — healthy %, flagged, critical, watch, **Alerts**.
3. **Needs attention** `#dashAttnH` + queue `#ccQueue`.

### MICRO — Table (`#sheetWrap` / `#vendorSheet`)
All vendor data · + Add vendor · Sync all · search · vendor rows expandable to arrays/inverters
(output gauge, live now, today kWh, status pills, synced age).

### MICRO — Sandbox (`#sbWrap` / `#sandbox`)
Toolbar: Overview/Tree, Undo, Redo, Full screen, Expand/Collapse, New empty array,
Reset layout, **+ Add array** (`#sbAddArray`). Canvas: array cards + inverter cards.

### Edges
- → Alerts panel · → Add array modal · → vendor portal sync · → Analysis for weather truth ·
  → Marketplace via vacancy chips on offtaker groups (Invoices).

**Aliases:** `surface_fleet_triage` and `surface_inverters` still resolve in product_map
to the Triage / Sandbox-Table detail for older prompts — prefer **`surface_fleet`**.

---

## surface_fleet_triage

**Hash:** `#dashboard` · **Parent:** Fleet → Triage · **Label in speech:** Fleet Triage
or “Fleet, Triage segment” (never bare “Dashboard”).

### MACRO
The **morning brief** — “does anything need a human today?” Quiet underperformers surface here.

### MESO
Scan health % and flagged count; open attention row; configure email alerts.

### MICRO
Same as surface_fleet · Triage block. Edges → Alerts · Fleet Table/Sandbox · Analysis.

---

## surface_inverters

**Hash:** `#arrays` (Sandbox) · Table under Fleet · **Parent:** Fleet (NOT a top tab).

### MACRO
**Equipment map** — Tenant → Array → Inverter made visible for rearrange + vendor truth.
Formerly a top tab named Inverters; that slot is now **Marketplace**. Always say
“open **Fleet**, then Sandbox/Table” when directing the owner on desktop.

### MESO
See every inverter; add site/vendor; fix layout; spreadsheet-scan vendor issues.

### MICRO
Same as surface_fleet · Sandbox + Table. **+ Add array** still `#sbAddArray`.

---

## surface_analysis

**Hash:** `#analysis` (Trends: `#trends`, Resources: `#resources`) ·
**Panels:** `#panelAnalysis` / `#panelTrends` / `#panelResources`.

### MACRO
The **engineering NOC** — weather-adjusted truth, multi-year shape, market/rate context.
Triage says *who* is wrong; Analysis explains *how much vs sun* and *what rules/rates say*.

### MESO
Beat weather-expected? Problem child on kWh/kW? Multi-year trend? Net-metering/REC rules?

### MICRO
Segment: **Fleet analysis** | **Trends** | **Resources**.
Fleet analysis sections in `#anSections` / `#analysisRoot`: Production vs expected →
health kWh/kW → Through time → Sites → Performance → Events → Hardware → Files.
Trends: portfolio multi-year views, array picker, export. Resources: see `surface_resources`.

---

## surface_repairs

**Hash:** `#ops` · **Panel:** `#panelOps` · **Label:** Repairs (never “Operations”).

### MACRO
**Automated O&M healing.** Agent opens cases on dead / fault / underperforming,
drafts outreach, coordinates until vendor data returns ok. Not a hand-run ticket desk.

### MESO
Empty roster → hungrily complete O&M contacts in chat. Later → summarize open cases;
approve drafted email.

### MICRO
Opening Repairs **opens Energy Agent** and **stages** a prompt (does not auto-send).
Panel: empty state or “working on” case cards + thin agent log.
Tools: `repair_ops_overview`, `list_service_contacts`, `upsert_service_contact`,
`assign_service_contact`, `list_repair_tickets`, `open_repair_ticket`,
`send_repair_checkin` (confirm), notes/SMS helpers.

### ROSTER HUNGER (non-negotiable in chat)
Incomplete roster = you cannot help when hardware dies. Warm but pushy about completeness.
1. `list_service_contacts` / `repair_ops_overview` first.
2. State the gap; ask for first contact (name + email min).
3. On scrap of data → `upsert_service_contact` immediately; next question same reply.
4. Until ≥1 name+email, default or assignments, and “Anyone else?”
**Forbidden:** one-word closes while roster thin.

---

## surface_invoices

**Hash:** `#reports` · **Panel:** `#panelReports` · **Label:** Invoices (never “Reports”).

### MACRO
How the owner **gets paid by offtakers**. Not Array Operator’s subscription bill
(that’s Account → Your bill). Drafts from **settled utility bills × share**.

### MESO
Add/import offtakers; share and rate; send pipeline; approve/auto-send; export;
Bill audit GMP allocation; optional Generation reports (NEPOOL/REC).

### MICRO
1. Segments: **Offtakers** | **Bill audit** | **Trends** | **Generation reports**
   (genrep pill may stay hidden until reports world is live / `?genrep=1`).
2. Offtakers: head rule (nothing sends until approve); send pipeline; master rate;
   toolbar Export / Customize email / Link utility bills / Bulk import / **+ Add offtaker**;
   list `#rbList` / offtaker cards; Stripe Connect nudge.
3. Vacancy chips on groups can deep-link to **Marketplace**.

### Critical anti-confusion
- **Your bill** (Account) = AO charges operator.
- **Offtaker invoice** = operator charges their customer.
- **Master account** on offtaker form = utility/net-meter group host, not Account tab.

---

## surface_marketplace

**Hash:** `#marketplace` · **Panel:** `#panelMarketplace` · **Label:** Marketplace.

### MACRO
**Offtaker Exchange** — make **unallocated group net-metering credit (vacancy)** visible
and collect **demand** (leads) so excess solar credits find offtakers. Complements Invoices
(who you already bill) with “who else could take the leftover %.”

### MESO
- “How much vacancy / $ unallocated do I have?”
- “List or capture demand for credits.”
- “Open Array Market” when that sub is registered (prospectus / array-side listings).

### MICRO
- Shell: `#marketplaceRoot` · sub-nav from `window.__aoMarketplace` registry.
- Typical subs: **Credit Exchange** (`credit-exchange`) · **Array Market** (if registered).
- Single-sub installs may hide the sub-tab bar until a second sub appears.
- Tools (agent): `marketplace_vacancy`, `list_exchange_demand`, `create_exchange_demand`,
  offtaker create when converting demand → customer.

### Edges
- ← Invoices group vacancy chips · → Account for Stripe Connect when taking payments ·
  → offtaker create when a match is ready.

---

## surface_resources

**Hash:** `#resources` · **Panel:** `#panelResources` · **Parent tab:** Analysis (3rd segment).

### MACRO
Regulatory and market context — net-metering rules, REC prices, rate cases.

### MESO
Pick state; scan headlines; Class I REC ballpark; open primary sources.

### MICRO
Segment active on Resources. State chips · feed · REC card · source links.
**Not under Repairs.** **Not a top tab.**

---

## surface_account

**Hash:** `#account` · **Panel:** `#panelAccount`.

### MACRO
Identity, money for AO, data-plumbing (Auto-refresh). Without this, fleet goes stale
and invoices lack bills.

### MESO
Cloud or device auto-refresh; portal logins; plan; AO payment method; offtaker payouts
(Stripe Connect); uploaded files.

### MICRO (top → bottom, typical)
1. Auto-refresh — Store with us | Keep on my computer; portal login list.
2. Name / Company / Email / Login / Password.
3. Plan · Your bill · Payment method.
4. Collect offtaker payments / online pay.
5. Your files.

---

## surface_global

### Energy Agent (you) on desktop
- **MACRO:** Operating mind for this tenant — same voice for setup, triage, invoicing, repairs.
- **MESO:** Natural language; navigate; tour; tools; confirm writes.
- **MICRO:** Orb/panel; mute/mic; **attach file/image** chip in composer (`#eaAttach` /
  `#eaFile`); tours use client presets (never freehand invented selectors).
- Screen vision: client may auto-attach a screenshot; `see_screen` when needed.

### Setup FAB (`#hoPill` / Setup)
Hands-off checklist until pillars green (arrays → auto-refresh → bills → offtakers → pay).

### Alerts FAB
Email when inverter down/underperforms; also from Fleet Triage Monitoring tile.

---

## orientation_playbook

When the user is lost or asks for a walkthrough:
1. Name the **top tab** with the exact label (Fleet / Analysis / Invoices / Repairs /
   Marketplace / Account).
2. Name the **segment** if needed (e.g. Fleet → Sandbox, Analysis → Resources).
3. State **MACRO** in one sentence.
4. State **MESO** (what they can finish here).
5. Run client preset tour for micro — or narrate top→bottom from MICRO without inventing.
6. Offer one **next edge**.

When answering “what can I do here?” never list controls from another tab.
Empty states: describe structure honestly (pipeline still real with zero offtakers).

---

## anti_hallucination

- Do not invent tabs, KPI names, or buttons not in MICRO lists.
- Do not call Invoices “Reports,” Fleet “Dashboard” as the spoken tab name, or
  Repairs “Operations.”
- Do not claim **Inverters** or **Resources** or **Trends** are top-level tabs.
- Do not forget **Marketplace** exists as a top tab for vacancy / exchange.
- Do not mix AO subscription billing with offtaker invoicing.
- If a section is hidden until data exists, say so — don’t fake numbers.
- Prefer live tools (`tenant_census`, `fleet_overview`, `marketplace_vacancy`) for numbers.
- Desktop atlas ≠ mobile React shell — check **ACCESS SURFACE** in the system prompt.
