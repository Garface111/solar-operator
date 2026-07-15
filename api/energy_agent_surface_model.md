# Array Operator — Surface Mental Model (3 levels)

**Purpose:** Give the Energy Agent a durable, page-level understanding of the product —
not just control minutia. Built from a full navigation capture of arrayoperator.com
(demo session, 2026-07-14): 15 states, screenshots under
`array-operator/docs/surface-atlas/shots/`, inventory in `manifest.json`.

**How to use (agent):** Before walking a tab, explaining “what this page is for,” or
answering “where am I?”, load `product_map(topic=surface)`. Pair with `topic=tabs` for
labels and `topic=<domain>` for deep mechanics. Prefer this file for **macro + meso**;
prefer DOM tours for **micro** lockstep highlights.

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

Everything else (Analysis, Resources, Account, Energy Agent) supports those two jobs.

### Product spine (left → right in the top bar)
```
[Login / anon home]
       │
       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  TOP BAR (always, signed-in)                                             │
│  EnergyAgent │ Fleet Triage │ Inverters* │ Analysis │ Invoices │ Repairs  │
│  │ Account │ whoami │ Sign out                                           │
│  * DEFAULT landing = Inverters (#arrays)                                 │
└──────────────────────────────────────────────────────────────────────────┘
       │
       │  Global chrome (not tabs): Energy Agent orb · Setup FAB (bottom-left)
       │  · Alerts FAB (bottom-right) · optional banner (extension / mic)
       ▼
```

| Job | Primary tab | Supporting |
|-----|-------------|------------|
| “Who needs me *right now*?” | **Fleet Triage** | Alerts FAB |
| “Show me the machines” | **Inverters** (Sandbox / Spreadsheet) | Add array, extension sync |
| “Why is yield off / weather?” | **Analysis** (Fleet analysis · Trends · Resources) | Sites, Performance, Hardware |
| “Bill my customers” | **Invoices** (Offtakers / Bill audit) | Account online pay, utility link |
| “What do rates/rules say?” | **Analysis → Resources** | state picker, REC card |
| “Who fixes my fleet when it’s down?” | **Repairs** (chat-first O&M agent) | service contacts, repair tickets |
| “Keep data + plan working” | **Account** | Auto-refresh, card, files |

### Design language (visual system — sky skin)
- **Backdrop:** full-bleed landscape (mountains / solar fields); content sits in **frosted glass** cards.
- **Primary accent:** sky blue / cyan for active tab, primary CTAs, healthy metrics.
- **Attention:** amber/orange pills and bars (underperforming, vendor issue, watch).
- **Critical:** red (rare; critical tile / critical severity).
- **Healthy:** green accents / meters / “All good”.
- **Chrome pattern:** pill tab bar; **segmented controls** for sub-views (Sandbox|Spreadsheet, Fleet analysis|Trends, Offtakers|Bill audit); **FABs** bottom corners (Setup left, Alerts right).
- **Density:** Triage = glance tiles; Inverters = spatial or table; Analysis = long scroll of sections; Invoices = pipeline then list; Account = form rows top→bottom.

### Navigation rules (never invent)
- There is **no** top tab named Dashboard, Arrays, Reports, Operations, or Trends.
- **Trends** and **Resources** are only Analysis sub-views (`#trends`, `#resources`).
- **Bill audit** is only Invoices → **Bill audit** segment.
- **Sandbox / Spreadsheet** are only under Inverters.
- **Repairs** (`#ops`) has **no** nested sub-tabs — chat + “what the agent is working on.”
- Empty hash / unknown → **Inverters** (`#arrays`).

### Atlas shots (re-capture: `docs/surface-atlas/capture.mjs`)
| Shot | State | How you get there |
|------|--------|-------------------|
| 00-login | Sign-in | Sign out / cold visit /login |
| 00b-anon-home | Marketing + demo story | Logged-out `/` |
| 01-inverters-sandbox | Inverters · Sandbox | Default after login; tab Inverters; segment Sandbox |
| 02-inverters-spreadsheet | Inverters · Spreadsheet | Same tab → Spreadsheet |
| 03-fleet-triage | Fleet Triage | Tab Fleet Triage |
| 04-analysis-fleet | Analysis · Fleet analysis | Tab Analysis (default segment) |
| 05-analysis-trends | Analysis · Trends | Analysis → Trends or `#trends` |
| 06-invoices-offtakers | Invoices · Offtakers | Tab Invoices |
| 07-invoices-bill-audit | Invoices · Bill audit | Invoices → Bill audit |
| 08-resources | Resources | Tab Resources |
| 09-account | Account | Tab Account |
| 10-modal-add-array | Add array modal | Inverters → **+ Add array** |
| 11-panel-fleet-alerts | Alerts settings | Triage Monitoring **Alerts** or Alerts FAB |
| 12-energy-agent-open | Energy Agent panel | Orb / agent open (global) |
| 13-hands-off-setup | Setup walkthrough | Bottom-left **Setup** FAB |

---

## surface_inverters

**Hash:** `#arrays` · **Panel:** `#panelArrays` · **Default after login.**

### MACRO
This is the **equipment map of the business** — Tenant → Array → Inverter made visible.
It exists so the owner can *see* and *rearrange* reality (drag grouping) the way they
think about sites, not the way the vendor’s portal groups devices.

### MESO
- “Where is every inverter and is it healthy?”
- “Add a new site / vendor.”
- “Fix layout to match how I run the fleet.”
- Spreadsheet variant: “Scan every row for vendor issues without moving cards.”

### MICRO (structure top → bottom)
1. **Top bar** — Inverters active.
2. **Segment** `#vsSegSandbox` | `#vsSegSheet`.
3. **Sandbox toolbar** — Overview / Tree, Undo, Redo, Full screen, Expand/Collapse, New empty array, Reset layout, **+ Add array** (`#sbAddArray`).
4. **Canvas** `#sandbox` — array cards (nameplate kW, production bars, weather, Open in vendor, inverter comb) + inverter cards (sparkline, live bar, status).
5. **Spreadsheet** `#vendorSheet` — “All vendor data”, + Add vendor, Sync all, search, vendor rows (Fronius / SolarEdge…) expandable to arrays/inverters; columns: output gauge, counts, live now, today kWh, status pills, synced age.

### Edges
- → Add array modal (10)
- → vendor portal via “Open … to sync” (extension capture)
- Cross-link: flagged inverter often first seen on **Fleet Triage**, drilled here.

---

## surface_fleet_triage

**Hash:** `#dashboard` · **Panel:** `#panelDashboard` · **Label:** Fleet Triage (never “Dashboard”).

### MACRO
The **morning brief** — answers “does anything need a human today?” so the owner does
not have to open every vendor portal. Product promise: quiet underperformers surface here.

### MESO
- Scan health % and flagged count.
- Open the attention queue row → diagnosis / warranty path.
- Configure email alerts.

### MICRO
1. **Head** — “Fleet triage” + live strip (`#dashProd`): kW now, kWh today, arrays producing, feeds paused.
2. **KPI grid** `#fleetCommander .fcg` — Fleet healthy %, Arrays, Inverters, Flagged, Critical, Watch, Recoverable/$/To check, Monitoring + **Alerts** (`#fcAlerts`).
3. **Needs attention** `#dashAttnH` + queue `#ccQueue` — search, severity chips, table (site, inverter, verdict, vs peers, $/mo, action, status).
4. Footer honesty line about peer-measured verdicts.

### Edges
- → Alerts panel (11)
- → Inverters (drill site)
- → Analysis Operations (deeper ops list)

---

## surface_analysis

**Hash:** `#analysis` (Trends: `#trends`, Resources: `#resources`) · **Panels:** `#panelAnalysis` / `#panelTrends` / `#panelResources`.

### MACRO
The **engineering NOC** — weather-adjusted truth, multi-year shape, and market/rate
context. Exists because Triage says *who* is wrong; Analysis explains *how much vs sun*,
*over what history*, and *what the rules/rates say*.

### MESO
- “Are we beating weather-expected?”
- “Which site is the problem child on kWh/kW?”
- “What’s the multi-year trend?” (Trends)
- “What are net-metering / REC rules for my state?” (Resources)

### MICRO — Fleet analysis (scroll sections in `#anSections`)
Segment: **Fleet analysis** | **Trends** | **Resources**.
Sections (order of capture): Production vs expected → Fleet health kWh/kW → Through time → Sites grid → Performance (PI / CF) → Event log → Hardware → Files.

### MICRO — Trends
Portfolio multi-year / multi-view analytics (bars, spiral, heat-field, etc.), array picker, export CSV. **Not a top tab.**

### MICRO — Resources
See `surface_resources`. Same Analysis segment control.

---

## surface_repairs

**Hash:** `#ops` · **Panel:** `#panelOps` · **Label:** Repairs (never “Operations”).

### MACRO
**Automated O&M healing.** Energy Agent detects dead/fault hardware, drafts outreach
to the owner’s repair team, coordinates until live vendor data shows recovery, then
closes the case. Multiple cases run in parallel. This is **not** a ticket desk the
owner adminstrates by hand — it is a conversation + a “what I’m working on” strip.

### MESO
- First visit: set up O&M contact + map arrays **in this chat** (no forms).
- Later: “What’s going on with repairs?” — agent summarizes open cases / log.
- Approve a drafted email; or ask the agent to send / follow up.

### MICRO
- Opening Repairs **opens Energy Agent** and **stages** a prompt in the composer
  (does **not** auto-send). Context-aware: setup vs status.
- Panel: calm empty state **or** compact “working on” case cards + thin agent log.
- Tools: `repair_ops_overview`, `list_service_contacts`, `upsert_service_contact`,
  `assign_service_contact`, `list_repair_tickets`, `open_repair_ticket`,
  `send_repair_checkin` (confirm), notes/SMS helpers.
- **Setup mode (chat script):**
  1. Ask if they have an O&M / repair team.
  2. Collect name, company, email, phone → `upsert_service_contact` (`is_default` if one team).
  3. Ask which arrays that team covers → `assign_service_contact` (or default covers all).
  4. Confirm: “I’ll draft outreach when hardware looks dead/fault on those sites.”
- **Resources is not here** — Analysis → Resources (`#resources`).

---

## surface_invoices

**Hash:** `#reports` · **Panel:** `#panelReports` · **Label:** Invoices (never “Reports”).

### MACRO
This is **how the owner gets paid by offtakers** (customers who receive solar credits).
It is **not** Array Operator’s subscription bill (that’s Account → Your bill).
Invoices draft from **settled utility bills × share**, never raw inverter kWh alone.

### MESO
- Add/import offtakers; set share and rate.
- See send pipeline (drafted / waiting on bills / next run).
- Approve or auto-send; export to QuickBooks/Xero.
- Bill audit: does GMP’s allocation match entered shares?

### MICRO (Offtakers view — top → bottom)
1. **Head** `.rb2-head` — “Offtaker invoicing” + rule: nothing sends until approve.
2. **Segments** `#rbGenTabs` — Offtakers | Bill audit.
3. **Send pipeline** `#rb2Pipe` — Delivered | This cycle | Next run; Approve to send / Auto-send mode.
4. **Master solar credit rate** `#rbGlobalRate` — optional fleet $/kWh + discount %.
5. **Toolbar** `.rb2-controls` — Export, Customize email, Link utility bills, Bulk import, **+ Add an offtaker** (`#rbCustAdd`); utility/auto-refresh status pill.
6. **List** `#rbList` — empty state or offtaker accordion cards (`.rb-acc`), search `#rbOSearch`.
7. Online pay banner / Stripe Connect nudge when offtakers exist.

### Critical anti-confusion
- **Your bill** (Account) = what AO charges the operator.
- **Offtaker invoice** = what the operator charges *their* customer.
- **Master account** on an offtaker form = utility/net-meter **group host**, not the Account tab.

---

## surface_resources

**Hash:** `#resources` · **Panel:** `#panelResources` · **Parent tab:** Analysis (3rd segment).

### MACRO
**Regulatory and market context** so invoices and strategy aren’t flying blind —
net-metering rules, REC prices, rate cases for the owner’s state.

### MESO
- Pick my state; scan today’s headlines; check Class I REC ballpark; open primary sources.

### MICRO
Segment: **Fleet analysis** | **Trends** | **Resources** (active).
State chips (VT/NH/ME/MA/CT/RI) · Latest & live feed `#resFeed` · REC market card · state reference card · Go to the source links.
**Not under Repairs.**

---

## surface_account

**Hash:** `#account` · **Panel:** `#panelAccount`.

### MACRO
**Identity, money for AO, and the data-plumbing** (Auto-refresh). Without this tab
working, Inverters go stale and Invoices can’t see bills.

### MESO
- Turn on cloud or device auto-refresh; save portal logins.
- Set plan (Vendor data / Offtaker invoices / Both).
- Add AO payment method; set up offtaker payouts (Stripe Connect).
- Find uploaded files/templates.

### MICRO (top → bottom)
1. **Auto-refresh** `#rowAutoRefresh` — Store with us | Keep on my computer; portal login list.
2. Name / Company / Email / Login / Password rows.
3. Plan · Your bill (`#aoBill`) · Payment method (`#billManage`).
4. Collect offtaker payments / online pay (`#aoPaySetup`).
5. Your files (`#acctFilesBody`).

---

## surface_global

### Energy Agent (you)
- **MACRO:** Operating mind for this tenant — not a separate product. Same voice for setup, triage, invoicing, and site improvements.
- **MESO:** Owner asks in natural language; you navigate, tour, read tools, patch offtakers when directed.
- **MICRO:** Orb/panel chrome; mute/mic; tours use client presets (never freehand invented selectors).

### Setup FAB (`#hoPill` / Setup)
Hands-off checklist until pillars are green (arrays live → auto-refresh → utility bills → offtakers → online pay as needed).

### Alerts FAB
Email when inverter goes down/underperforms; sensitivity + frequency; also opened from Triage Monitoring tile.

---

## orientation_playbook

When the user is lost or asks for a walkthrough:
1. Name the **tab** with the exact top-bar label.
2. State **MACRO** in one sentence (why the page exists).
3. State **MESO** (what they can finish here).
4. Run the **client preset tour** for micro lockstep — or narrate top→bottom using the MICRO list above without inventing controls.
5. Offer one **next edge** (“From here, Bill audit checks GMP shares” / “Add array is on Inverters”).

When answering “what can I do here?” never list controls from another tab.
When the UI is empty (e.g. no offtakers yet), describe the empty state honestly — the **structure** is still real (pipeline, master rate, toolbar).

---

## anti_hallucination

- Do not invent tabs, KPI names, or buttons not in MICRO lists.
- Do not call Invoices “Reports” or Fleet Triage “Dashboard.”
- Do not claim Trends is a top-level tab.
- Do not mix AO subscription billing with offtaker invoicing.
- If a section is hidden until data exists (send pipeline KPIs, offtaker cards), say so — don’t fake numbers.
- Screenshots in `surface-atlas/shots/` are **ground truth for agents building this model**; the live DOM may show tenant-specific counts. Prefer live tools (`tenant_census`, `fleet_overview`) for numbers.
