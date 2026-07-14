# Array Operator — Energy Agent Support Map

**Source of truth for the in-app Energy Agent** (`product_map` tool).  
Support-facing only: how the product works for THIS tenant. No deploy ops, no Railway SSH, no multi-tenant admin, no secrets.

Coding agents editing the product still load skill **`solar-operator-energyagent`**.  
When product behavior changes, update **this file** (and the skill if ops change).

Topics = `## heading` ids below. Call `product_map(topic=<id>)` before explaining that area.

---

## tabs

TOP NAV — use EXACTLY these labels when speaking to the owner (hashes are internal):

| User-facing label | hash | What it is |
|-------------------|------|------------|
| Fleet Triage | `#dashboard` | Attention / fleet overview (NOT “Dashboard”) |
| Inverters | `#arrays` | Live inverter canvas, Spreadsheet + Sandbox (NOT “Arrays”) |
| Analysis | `#analysis` | Fleet NOC: production vs expected, sites, health, hardware. Trends/through-time is a **sub-view** (NOT a separate top tab) |
| Invoices | `#reports` | Offtaker solar-credit invoices (NOT “Reports”) |
| Resources | `#resources` | VT net-metering rates, rate cases, news |
| Account | `#account` | Profile, plan/card, Auto-refresh vault (was “Master Account”; use **Account**) |

Never say Dashboard, Arrays, Reports, or Trends as top-tab names.  
Trends lives under Analysis (`#trends` is a sub-route only).  
Offtaker invoice form field **Master account** = net-meter group host (different concept).

---

## system

END-TO-END PRODUCT

- **Brand:** EnergyAgent (umbrella). **This product UI:** Array Operator at arrayoperator.com.
- **Stack:** Static frontend `array-operator/public/*` on Netlify; shared FastAPI backend on Railway (`solar-operator`).
- **Tenant identity:** one owner account (Tenant) per product. Session auth for SPA (`so_session`); tenant_key for extension.
- **Core chain:** Tenant → Arrays (sites/groups) → Inverters (equipment). Optional UtilityAccounts + Bills for settlement. BillingReportSubscription = offtaker.

DATA IN
1. **API keys** (SolarEdge, Locus, AlsoEnergy when connected) → server poll / fleet-tree.
2. **Portal logins** via Account → Auto-refresh:
   - **cloud** = “Store it with us” — we store encrypted password; harvester 24/7.
   - **device** = “Keep it on my computer” — extension vault; capture while browser active.
3. **Onboarding / connect** vendor picker (one-key multi-site where supported).

DATA OUT (owner UI)
- Fleet Triage + Inverters — health, peer index, live power, vendor issues.
- Analysis — weather-expected, kWh/kW, sites grid, hardware (14d peer + live).
- Invoices — drafts from **utility bills × share** (not inverter kWh).
- Account — profile, AO subscription/card, Auto-refresh.
- Resources — rates/news.

TWO BILLING CONCEPTS — never mix
- **Operator billing** = Array Operator charges the owner (Stripe plan/card on Account).
- **Offtaker invoices** = owner bills customers for solar credits (Invoices tab).

Chrome extension name: **EnergyAgent** (pairs with tenant key). Required for device-mode portal capture.

---

## fleet

DATA MODEL (tables the agent can reason about)

- **Array** — site/group. Soft-delete `deleted_at`. May be inverter-backed **or** pure utility-meter (billing-only).
- **Inverter** — physical unit. Telemetry source fixed (vendor+serial); owner may regroup `array_id` (drag).
- **InverterConnection** — vendor credentials on an array (or legacy SolarEdge cols on Array).
- **DailyGeneration** — per-array daily kWh (US/Eastern day).
- **InverterDaily** — per-inverter daily kWh.
- **UtilityAccount / Bill** — utility meters and settled bills (invoice source of truth).
- **BillingReportSubscription** — one offtaker row.

UI vs census
- **fleet-tree / fleet overview** (Triage/Inverters health) often **excludes pure meter-only** arrays.
- **tenant_census** includes **all** non-deleted arrays — use for “how many arrays do I have?”

Status semantics (honest)
- **14-day peer** = relative to neighbors over ~14 measured days.
- **Live** = right now (dark/low/missing live reading, vendor stale feed).
- Hardware + vendor sheet combine peer + live so Solar.web “dead” is not green-washed by healthy history.
- Capacity factor / window kWh use the Analysis window (typically 10–14 days) — always name the window.

---

## capture

AUTO-REFRESH = TWO OWNER PATHS (Account → Auto-refresh)

`Tenant.capture_mode` = `cloud` | `device` | null.

This is **where passwords live** and **who signs into portals** — orthogonal to “does SolarEdge use an API key?”

### Path A — Cloud (“Store it with us — live data”)
- Owner saves portal username/password once.
- Encrypted on server (`PortalCredential`); **never returned** by API.
- Harvester (`cloud_capture` + `harvester/*`) signs in 24/7 — no tab, no extension required for that login.
- Status: harvest ok / `login_failed` (bad password) / `scrape_failed` (signed in, data pull hiccup).

### Path B — Device (“Keep it on my computer”)
- Passwords stay in the **Chrome extension vault** on that machine.
- Extension pairs to tenant; capture when portal is opened / auto-refresh with tab active.
- Not true 24/7 if the machine is off (unless cloud path also covers that login).

Both paths write the **same** tables (Inverter, daily rows, bills when utilities captured).

### By vendor (orthogonal to cloud vs device)
| Vendor | How data usually arrives |
|--------|---------------------------|
| SolarEdge | Account/site **API key** → server poll (not portal scrape) |
| Locus / AlsoEnergy | API when connected |
| Fronius / SMA / Chint | Portal scrape — cloud harvester **or** extension |
| Utilities (GMP, SmartHub, Eversource, CMP, …) | Portal/API by provider; bills feed Invoices |

Stale / dark
- Device dark often = no recent capture/heartbeat — hardware may be fine.
- Cloud dark → check harvest status (password vs scrape).
- Never say cloud mode “uses the extension for SMA” or that device mode stores passwords on our servers.

---

## vendors

INVERTER VENDORS (owner language)

- **SolarEdge** — API key connect; multi-site discover when account-level key.
- **Fronius (Solar.web)** — portal capture (cloud or extension). Site-level live power may be **split** across inverters (`~` kW) — not true per-unit metered live.
- **SMA (ennexOS)** — portal/consent paths; cloud or extension.
- **Chint** — portal capture; often per-site navigation in portal.
- **Locus / AlsoEnergy** — API connect when credentials available.

Vendor issue (UI pill) means the **monitoring feed** is the problem (stale, harvest fail, whole site dark while fleet peers produce) — not necessarily a dead inverter. Check portal + Auto-refresh harvest status.

---

## analysis

ANALYSIS TAB (`#analysis`)

Modules (composed via `AnalysisSections`): Production vs expected (forecast), fleet health kWh/kW, sites grid, performance PI/CF, hardware, operations/alarms, events, files, through-time/trends sub-view.

Key concepts
- **Production vs expected** — measured kWh vs weather model (nameplate × POA/STC × PR). Needs location + measured days. Model editor: per-array address, tilt, facing, PR; autofill from utility/vendor/name.
- **kWh/kW** — specific yield over measured days in the window (not invented).
- **Hardware** — CF over **N-day window**; status = **14-day peer + live** overlay.
- **Window** — typically 10–14 days; say “last N days” when quoting numbers.

Never invent expected or weather values; use tools / what the UI already computed.

---

## offtakers

OFFTAKER INVOICE GENERATOR — START TO FINISH  
UI: **Invoices** (`#reports`). Entity: **BillingReportSubscription**.

### 1) Data in — utility bills are source of truth
- UtilityAccount linked to Arrays (net-meter groups).
- Captured bills (`Bill`) per period — **never** invent kWh from inverters for invoices.
- “✓ N bill sources” on Invoices header.

### 2) Offtaker row fields
- `customer_name` + `client_email` — who the invoice is for.
- `array_id` — master net-meter **group** (host).
- `utility_account_id` — which bill to invoice from (own sub-meter **or** host).
- `allocation_pct` — billing multiplier on bound meter (own-meter often pinned to 1.0).
- `array_share_pct` — share of group for GMP accuracy audit (≠ allocation_pct).
- delivery_mode approval|auto, cadence, rates/discounts.

### 3) Edit form — two account dropdowns
- **MASTER** = group host (array) — labels are utility nicknames/addresses (e.g. Timberworks), **not** offtaker name.
- **SUB** = optional own meter. Blank = % of master; set = own bill.
- “Change master to Timberworks” = rebind `array_id` / host — **never** rename customer to Timberworks.

### 4) Invoice math
- kWh from bound utility bill for period × share rules.
- Rate: offtaker override → master global rate if set → **per-offtaker bill credit** when master blank → then discount.
- Optional pay link (Stripe Connect); success returns public `/paid` thank-you — **not** the owner dashboard.

### 5) Draft → approve → send
- approval mode: nothing emails until approve / send-now.
- auto mode: scheduler when period ready.
- Never invent paid status; use payment tools/UI.

### 6) Agent tools
- list/get offtaker, patch_offtaker (confirm writes), product_map(topic=offtakers).
- query_tenant utility_accounts for rebinding sources.

---

## billing

OPERATOR BILLING (Array Operator → owner)

- Charged on Account (plan, card, Stripe portal).
- Separate from offtaker invoices.
- Agent may open **billing portal links** after confirm — never change prices, create subscriptions, or touch payment methods/cards.

---

## status

HOW TO EXPLAIN “FINE” VS “DEAD” VS “VENDOR ISSUE”

| Signal | Window | Meaning |
|--------|--------|---------|
| Pulling its weight / peer ok | ~14 days | Relative to neighbors over history |
| Dark now / Low vs peers | Live | Right now vs cohort |
| No live reading | Live | Missing instantaneous kW while siblings report |
| Vendor issue | Live / feed | Monitoring source stale or harvest failing |
| Not coming home / Fault | Peer (hard) | Sustained dead/fault from peer engine |

Fronius Solar.web may flag live/today issues while 14-day peer still looks fine — both can be true. Prefer live overlay + last sync age when the owner is comparing to the portal.

---

## security

HARD RULES FOR THE AGENT

- THIS tenant only. Never other tenants’ data.
- Never reveal passwords, API keys, session tokens, vault secrets.
- Never invent kWh, dollars, counts, or statuses — tools only.
- Offtaker pay links must not dump people into the owner SPA (public `/paid` page).
- Writes (patch offtaker, UI fill/click) need confirm unless user already approved this turn.
- Operator money path is read-only (portal link only).

---

## tools

WHEN TO CALL WHAT

| Question type | Tool |
|---------------|------|
| How many arrays/inverters/offtakers? | `tenant_census` first |
| How does Auto-refresh / cloud vs device work? | `product_map(topic=capture)` then `account_summary` |
| How does the product work end-to-end? | `product_map(topic=system)` |
| Tab names | `product_map(topic=tabs)` |
| Invoice generator model | `product_map(topic=offtakers)` |
| Why peer vs Solar.web disagree | `product_map(topic=status)` + health tools |
| Fleet health / attention | `investigate_attention` / `fleet_overview` / `array_detail` |
| Ad-hoc lists | `query_tenant` |
| Account email/company/plan/mode | `account_summary` |
| Navigate / show UI | `ui_navigate` / `ui_tour` / highlight |
| Change offtaker fields | `patch_offtaker` (confirm) |
| Product wish / UI change | `propose_site_improvement` |

You do **not** have codebase shell access. You have this map + tenant tools only.
