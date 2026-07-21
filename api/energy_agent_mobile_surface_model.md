# Array Operator — Mobile Surface Mental Model (owner-web /m + React Native)

**Purpose:** Page-level memory for Energy Agent when the owner is on a **phone**
client — not the desktop SPA. Two mobile shells share the same product jobs and
tab order, but different chrome:

| Client id (`context.client`) | What it is | URL / package |
|------------------------------|------------|---------------|
| **`owner-web`** | React SPA (Vite) sky mobile web | `arrayoperator.com/m` · `apps/owner-web` |
| **`owner-native`** | Expo React Native | `apps/owner-native` |
| **`desktop`** / missing / classic | Legacy wide SPA | `arrayoperator.com` hash router — use **desktop** `energy_agent_surface_model.md` |
| **mobile_os** (legacy context) | Older in-SPA mobile OS home (`mobile-os.js` on desktop bundle) | Still possible on narrow desktop shell; prefer tools over tab-click directions |

**How to use (agent):** When ACCESS SURFACE says mobile, load
`product_map(topic=surface_mobile)` first. Do **not** quote desktop DOM ids
(`#sbAddArray`, `#panelReports`) as click targets — mobile uses routes + **Ask Agent**
CTAs + bottom nav. Prefer **tools** (census, fleet, offtakers, repairs, marketplace)
and short thumb-friendly steps.

**Always announce surface in spirit:** e.g. “On mobile Fleet…” / “In the app chat…”
so answers match what they see.

---

## product_spine_mobile

### MACRO
Same two jobs as desktop: **watch the fleet** and **invoice offtakers**. Mobile
puts **Energy Agent in a bottom dock / full-screen chat sheet** (not a side orb)
and primary navigation in a **bottom tab bar**.

### Bottom nav order (both owner-web and owner-native)
```
Fleet · Analysis · Invoices · Repairs · Market(place) · Account
```
- owner-web labels: Fleet, Analysis, Invoices, Repairs, **Market**, Account  
  (`BottomNav.tsx` — Market is short for Marketplace).
- owner-native: same six tabs under `app/(tabs)/`.
- **No separate Inverters tab** — fleet cards/table live under **Fleet**.
- **Resources / Trends** are not bottom tabs; reach via Analysis screen or Agent.

### Agent chrome (both)
- **Bottom dock** “Energy Agent” / swipe-up → opens chat.
- **Agent sheet (web)** or **AgentModal (native)** — full viewport, keyboard-safe.
- Header: swipe-down / × to close; at top of long thread, overscroll-up closes (web).
- Composer: message field + **send**; **attach** (photo / library / file) on web;
  native should prefer share/paste when camera picker available.
- **Ask Agent** buttons on each screen seed a prompt into the sheet (do not invent
  seed copy — use the screen’s CTA or a short status ask).

### Design language
- Sky theme: glass cards, cyan primary, amber attention, soft landscape backdrop.
- Large touch targets (≥44–48px).
- Prefer **cards + one primary CTA** over dense desktop toolbars.
- Markdown replies: bold/lists; keep paragraphs short for phone reading.

### What mobile cannot do well (be honest)
- Full Sandbox drag-and-drop rearrange (desktop Fleet → Sandbox).
- Extension one-click “Log in with vendor” capture (Chrome extension is desktop).
- Dense spreadsheet edits — offer Agent tools or “open desktop Detail” when needed.
- Lockstep DOM tours with desktop selectors — use narrative + tools instead.

### What mobile does well
- Status briefs, repairs roster interview, offtaker questions, vacancy check.
- Photo of a bill / portal / inverter sticker → **attach** → you analyze.
- Confirm write tools (create offtaker, repair contact, demand lead).

---

## surface_mobile

Convenience alias: whole-product mobile spine + orientation + anti-hallucination
(merged by loader). Prefer this when unsure which mobile screen they are on.

---

## surface_mobile_fleet

**Routes:** owner-web `/fleet` · native `(tabs)/fleet` · default home after login.

### MACRO
Phone **fleet pulse** — arrays as cards or compact table; health chips; open Agent
for diagnosis.

### MESO
“Are we producing?” “Which site is red?” “Brief me.”

### MICRO (typical)
- Title **Fleet**; toggle **cards | table** when present.
- Stat cards / gauges (kW, kWh, healthy count).
- Array list: name, status chip, sparkline / power bar; expand for inverters.
- **Ask Agent** / dock for deep questions.
- Empty fleet: explain connect path (desktop + extension often still required for
  first vendor attach; Agent can walk cloud Auto-refresh once credentials exist).

### Agent tools
`tenant_census`, `fleet_overview`, status/health tools, `repair_ops_overview` if red.

---

## surface_mobile_analysis

**Routes:** `/analysis` · native analysis tab.

### MACRO
Portable NOC summary — production vs expected, portfolio health, not full desktop
section stack.

### MESO
Weather-adjusted performance; problem sites; “why is yield down?”

### MICRO
Summary cards + **Ask Agent** for deep dive. Point to desktop Analysis for Trends
spiral / heavy export when needed. Resources/rate questions → tools + web_search /
product_map resources, not “tap Resources top tab.”

---

## surface_mobile_invoices

**Routes:** `/invoices` · native invoices tab.

### MACRO
Offtaker money on a phone — pipeline snapshot + list; edits often via Agent tools.

### MESO
Who is drafted / waiting on bills? Add offtaker? Rate / share questions?

### MICRO
Pipeline summary; offtaker cards; **Ask Agent** for create/import/explain.
Bulk XLSX import is desktop-first; on mobile offer attach-roster photo/file in chat
or “open desktop Invoices → Bulk import.”

### Tools
Offtaker list/patch tools, `create_offtaker`, send-pipeline style reads,
`payments_connect_status` / `start_payments_connect`.

---

## surface_mobile_repairs

**Routes:** `/repairs` · native repairs tab.

### MACRO
Same O&M automation as desktop — **chat is the control plane**. Mobile is ideal
for roster hunger and case status.

### MESO
“What’s broken?” “Add my tech.” “What are you working on?”

### MICRO
Issue count / open cases cards; primary CTA opens Agent with repair seed.
Apply **ROSTER HUNGER** rules from desktop surface_repairs (same tools).

---

## surface_mobile_marketplace

**Routes:** `/marketplace` · native marketplace · bottom label may say **Market**.

### MACRO
Vacancy + demand exchange on the go.

### MESO
Unallocated $/yr? List demand? Match a lead?

### MICRO
Short explainer + CTAs: **Ask Agent about vacancy**, demand capture.
Tools: `marketplace_vacancy`, `list_exchange_demand`, `create_exchange_demand`.

---

## surface_mobile_account

**Routes:** `/account` · native account.

### MACRO
Identity, plan/bill, Auto-refresh mode notes, Stripe Connect nudge.

### MESO
“Am I on the right plan?” “Why is data stale?” “Set up payouts.”

### MICRO
Profile rows; plan/bill; Agent for vault/password complexity (cloud capture may
need desktop/extension for first login). Files: prefer **chat attach** on mobile.

---

## surface_mobile_agent

**Surfaces:** `agent_sheet_mobile` (owner-web) · `rn_agent` (owner-native).

### MACRO
You **are** the mobile operating layer when chat is open — not a tiny helper under
a desktop orb.

### MESO
Answer, run tools, confirm writes, read attachments, give one next thumb action.

### MICRO — composer UX (owner-web AgentSheet)
- Text field + send (Enter sends).
- **+ Attach** opens action sheet: **Take photo** (`capture=environment`),
  **Photo library**, **File** (pdf/csv/xlsx/images).
- Pending attachments show as **chips** (name + remove) above the composer.
- Optional **quick action chips** when idle: e.g. Fleet brief · Vacancy · Repairs roster
  (seed prompts — still real tool-backed answers).
- No desktop Setup/Alerts FABs inside the sheet; those live on desktop SPA.

### MICRO — attach / vision
1. Client `POST /v1/energy-agent/upload` → asset id.
2. Chat turn includes `attachment_ids`.
3. You analyze image/PDF/text extract; for UI questions without attach, ask them to
   snap the screen or use see_screen only if the client supports it.

### Speech / length
Prefer short paragraphs, bold key numbers, one clear CTA. Avoid “click the third
desktop tab.” Say **“open Fleet in the bottom bar”** or **“I’ll pull that with tools.”**

---

## orientation_playbook_mobile

1. Read ACCESS SURFACE: client + route/surface + mobile=true.
2. Name the **bottom tab** they are on (Fleet / Analysis / …).
3. MACRO in one sentence (mobile framing).
4. Act with **tools** first; UI directions second.
5. For files/photos: **“Tap the paperclip / + and choose Camera or File.”**
6. If a flow needs desktop (Sandbox drag, extension capture), say so clearly and
   offer the mobile half (status, roster, offtaker create, vacancy).

---

## anti_hallucination_mobile

- Do not invent a seventh bottom tab or desktop-only top-bar labels as if they were
  on the phone chrome.
- Do not tell mobile users to open `#reports` or “the Sandbox segment” without
  translating to **Invoices** / **Fleet** bottom nav.
- Do not claim the Chrome extension runs inside the React Native app.
- Do not pretend bulk import / template studio are first-class on phone.
- Marketplace bottom label may be **Market** — same surface as Marketplace.
- Always prefer live tool numbers over remembered demo atlas counts.
