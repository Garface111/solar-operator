# Array Operator — Energy Agent Support Map

**Source of truth for the in-app Energy Agent** (`product_map` tool).
Support-facing: how the product works for THIS tenant, end to end. No deploy ops, no Railway SSH, no multi-tenant admin, no secrets.

Coding agents editing the product still load skill **`solar-operator-energyagent`**.
When product behavior changes, update **this file** (regenerate with the **product-map-cartographer** agent, or edit by hand).

Topics = `## heading` ids below. Call `product_map(topic=<id>)` before explaining that area.
Available topics: `tabs · system · fleet · capture · vendors · analysis · health · offtakers · billing · plans · onboarding · resources · status · agent · api · datamodel · glossary · security · tools · surface · product_spine · surface_*`.

**Page-level understanding** (macro / meso / micro): `product_map(topic=surface)` or
`surface_invoices` / `surface_inverters` / `surface_fleet_triage` / `surface_analysis` /
`surface_account` / `surface_resources`. Full atlas + screenshots:
`array-operator/docs/surface-atlas/` (source: `energy_agent_surface_model.md`).

---

## tabs

TOP NAV — use EXACTLY these labels when speaking to the owner (hashes are internal):

| User-facing label | hash | What it is |
|-------------------|------|------------|
| Fleet Triage | `#dashboard` | Attention / fleet overview (NOT “Dashboard”) |
| Inverters | `#arrays` | Live inverter canvas; **Sandbox** (spatial fleet tree) + **Spreadsheet** sub-views (NOT “Arrays”). This is the default landing tab. |
| Analysis | `#analysis` | Fleet NOC: production vs expected, sites, health, hardware. **Trends / through-time is a sub-view** (`#trends`), NOT a separate top tab |
| Invoices | `#reports` | Offtaker solar-credit invoices (NOT “Reports”) |
| Resources | `#resources` | VT/New England net-metering rates, rate cases, news |
| Account | `#account` | Profile, plan/card, Auto-refresh vault (was “Master Account”; use **Account**) |

Never say Dashboard, Arrays, Reports, or Trends as top-tab names. `#trends` routes into Analysis as a sub-view only. The offtaker invoice form field **Master account** is a different concept (net-meter group host — see `offtakers`).

APP SHELL (how the SPA works, for accurate “where do I click” answers):
- Static frontend; scripts load in order (`session-tabscope.js` first, then `fleet-store.js`, `app.js`, `sandbox.js`, view modules, `energy-agent.js`). The router lives in `sandbox.js`: `hashchange` → toggles the matching panel + lazy-loads that view. Empty/unknown hash → `#arrays`.
- **Auth:** session token in `localStorage["so_session"]`, sent as `Authorization: Bearer …`. `session-tabscope.js` makes the token per-browser-tab, so two tabs can be signed into different accounts.
- **FleetStore** (`fleet-store.js`) is the single source of truth for the Arrays domain: reactive store, optimistic in-memory edits + background persist, plus an `ao_fleet_cache` snapshot for instant first paint before the `/v1/array-owners/fleet-tree` refetch. Sandbox and Fleet Triage both read it, so views never drift.
- **Mobile:** a fixed bottom nav (≤600px) mirrors the same tabs on **Detail mode**. **AI home (default, ≤960px, signed-in):** Energy Agent *is* the operating layer (`mobile-os.js`) — full-screen chat/voice, setup checklist chips until hands-off, then systems overview cards (inverters / auto-refresh / offtaker send rates). **Detail** at the bottom unlocks the full tab UI. Context JSON includes `mobile_os` + phase for the agent. A version-check banner offers a manual reload when a newer bundle ships (never auto-reloads).

---

## system

END-TO-END PRODUCT

- **Brand:** EnergyAgent (umbrella). **This product UI:** Array Operator at arrayoperator.com.
- **Stack:** static frontend (`array-operator/public/*`) on Netlify; shared FastAPI backend (`solar-operator`) on Railway; Postgres.
- **Tenant identity:** one owner account (Tenant) per product. SPA session auth (`so_session`); the Chrome extension pairs with a tenant key.
- **Core chain:** Tenant → Arrays (sites/net-meter groups) → Inverters (equipment). Optional UtilityAccounts + Bills for settlement. BillingReportSubscription = one offtaker.

DATA IN (orthogonal sources — all write the same tables)
1. **API keys** (SolarEdge, Locus/AlsoEnergy) → server polls telemetry directly.
2. **Portal logins** via Account → Auto-refresh (see `capture`):
   - **cloud** = “Store it with us” — encrypted password on server; headless harvester logs in 24/7.
   - **device** = “Keep it on my computer” — password stays in the Chrome extension vault; capture while a signed-in browser is active.
3. **One-click “Log in with &lt;vendor&gt;” via the EnergyAgent Chrome extension** (see `capture` → Path C). Owner signs into the real portal once; the extension **auto-captures** sites/inverters and POSTs them up. **Does not require** that login to be in the cloud vault.
4. **Onboarding / connect** vendor picker (sync-all or one key discovers many sites where the vendor supports it).

CRITICAL ANTI-CONFUSION
- **Arrays on the fleet for SMA/Fronius/Chint do NOT prove a cloud vault login exists** for that vendor. They often arrived via extension one-click capture or onboarding.
- **Cloud vault roster ≠ full fleet.** Account may show only Chint in cloud capture while Inverters still lists SMA arrays from an earlier extension capture.
- When the owner says “I never entered that login into cloud capture,” believe them and explain Path C (extension auto-capture), not “the harvester must have pulled it.”

DATA OUT (owner UI)
- **Fleet Triage** + **Inverters** — health, peer index, live power, vendor issues.
- **Analysis** — weather-expected vs actual, kWh/kW specific yield, sites grid, hardware, trends.
- **Invoices** — offtaker drafts computed from **utility bills × share** (never raw inverter kWh).
- **Account** — profile, AO subscription/card, Auto-refresh vault, plan.
- **Resources** — net-metering rates + regional news.

TWO BILLING CONCEPTS — never mix (see `billing` vs `offtakers`)
- **Operator billing** = Array Operator charges the owner (Stripe plan/card on Account).
- **Offtaker invoices** = the owner bills their own customers for solar credits (Invoices tab).

Chrome extension name: **EnergyAgent** (pairs with the tenant key).
- Required for **device-mode** vault auto-refresh.
- Also powers **one-click “Log in with…”** capture even when Auto-refresh mode is **cloud** (for vendors that are not already covered by a cloud login).
- Heartbeats ~every 60s when present (`Tenant.extension_heartbeat_at`). A recent heartbeat means some browser with the extension is paired to this account — not that every vendor is cloud-vaulted.
- When present, the extension can **automatically open / reach vendor portals** to capture (or re-capture) data: armed capture intent → portal tab → content scripts read authenticated responses → POST `/v1/array-owners/inverter-capture` (or utility capture). Owner usually just needs to sign in once if the portal session is cold.

---

## fleet

FLEET DATA MODEL + INVERTERS CANVAS

- **Array** — a generation site/group that maps to one or more utility meters (e.g. one array = three GMP meters summed). Soft-deleted via `deleted_at`. May be **inverter-backed** OR **pure utility-meter** (kept for offtaker billing only).
- **Inverter** — the first-class, owner-arrangeable device. It splits two concerns cleanly: the **telemetry source is immutable** (vendor + serial identify the physical feed and never change when the owner moves it), while the **owner grouping is mutable** (`array_id` + `position`, edited by dragging in the Sandbox). Discovery is idempotent on (vendor, serial) and never clobbers the owner’s arrangement.
- **InverterConnection** — one row per array: that array’s connection/credentials for an API vendor (config is encrypted).
- **DailyGeneration** — per **(array, day)** kWh, keyed to the fleet-**local** day (US/Eastern). Authoritative for monthly totals.
- **InverterDaily** — per **(inverter, day)** kWh; needed for extension-captured vendors that can’t be re-pulled, so peer analysis still works.
- **UtilityAccount / Bill** — utility meters and settled bills (the invoice source of truth).
- **BillingReportSubscription** — one offtaker row (see `offtakers`).

REGROUPING: The **Inverters → Sandbox** canvas is the schema made visible (Tenant → Array → Inverter). Dragging an inverter to another array persists server-side and changes its peer cohort, its reports, and per-array billing rollup. The **Spreadsheet** sub-view shows the same fleet as a table; both read FleetStore so they never disagree.

UI vs census
- **fleet-tree / fleet overview** (Triage + Inverters health) **excludes pure meter-only** arrays.
- **tenant_census** includes **all** non-deleted arrays — use it for “how many arrays/inverters do I have?”

Honesty
- Live power for portal vendors (Fronius/SMA/Chint) is the site total split by each inverter’s energy share — an **estimate** (shown with `~`), only surfaced while fresh (≈24h) so overnight captures age out to “—”.
- Local-day bucketing everywhere — never quote a “today” number off a UTC date.

---

## capture

AUTO-REFRESH modes on Account choose **where portal passwords live** for scheduled refresh. `Tenant.capture_mode` = `cloud` | `device` | null. This is **orthogonal** to “does this vendor use an API key?” and **orthogonal** to one-click extension capture (Path C).

### Path A — Cloud (“Store it with us — live data”)
- Owner saves a portal username/password once. Stored **encrypted at rest** on the server (`PortalCredential`); **never returned** by any API; deleting it is a hard delete of the ciphertext. Requires explicit consent, and the server refuses to accept a password unless encryption-at-rest is armed.
- A headless browser farm (`cloud_capture` + `harvester/*`) signs in on a schedule 24/7 — **no tab, no extension needed** for that login. Inverter logins refresh on a tight cadence (target: never more than ~5 minutes stale); utility logins refresh roughly every 12h (bills are monthly; polling harder invites lockouts). It reuses a warm session to avoid tripping “suspicious sign-in” alerts.

### Path B — Device (“Keep it on my computer”)
- Passwords stay in the **Chrome extension vault** (encrypted in the browser, AES-GCM) on that machine and **never reach our servers** — if we’re breached, there are zero portal passwords to steal.
- The extension pairs to the tenant and captures when the portal is opened (or during auto-refresh with a tab active), then POSTs the data up. It heartbeats ~every 60s. Not true 24/7 if the machine is asleep and no cloud path covers that login.

### Path C — One-click portal capture (EnergyAgent Chrome extension) — VERY COMMON
This is how SMA / Fronius / Chint arrays often first appear **even when Auto-refresh is set to cloud** and the owner never saved that login in the cloud vault.

Flow:
1. Owner (or onboarding) clicks **Log in with SMA / Fronius / Chint / SolarEdge…** (or the extension is already paired and a portal tab is opened with capture intent).
2. Extension **arms capture intent**, opens the real vendor site, and (when the owner is signed in) **automatically reads** the portal’s authenticated API/DOM responses.
3. Extension POSTs the snapshot to `/v1/array-owners/inverter-capture` (or utility capture paths). Arrays + inverters land on the tenant **immediately**.
4. **No cloud vault row is created** unless the owner later saves that portal under Account → Auto-refresh (or hands-off login form).

What the agent must say when asked “how did you get my SMA login?”:
- We may **not** have their SMA password at all.
- The arrays almost certainly came from **extension auto-capture** when they (or onboarding) signed into Sunny Portal / Solar.web / Chint Connect.
- For ongoing hands-off refresh of that vendor they still need either a **cloud vault login** (Path A) or **device vault + extension** (Path B). Path C is primarily **attach / first capture**, not 24/7 by itself.

When UI context says `extension_present: true` (or a recent `extension_heartbeat_at`):
- Tell the owner the EnergyAgent helper is installed/paired on a browser for this account.
- Prefer “the extension can open vendor sites and capture automatically after you sign in” over “you must paste the password into cloud capture” — unless they want true hands-off 24/7 without a browser.

### Status semantics (the critical distinction)
- **`login_failed`** = the login itself failed → a real credential problem (wrong/changed password, MFA wall). Only this status counts as a failure, backs off, and eventually pauses — the lockout guard. Re-saving the password re-arms it immediately.
- **`scrape_failed`** = signed in fine, but the post-login data pull hiccuped → NOT a password problem; retries on normal cadence. Only accuse the password on a true `login_failed`.

### Capture-debt / self-heal
The **server** decides what’s stale and hands each extension heartbeat a to-do list (“drain this vendor / keep this utility session warm”); whichever signed-in browser wakes first drains it. A second laptop is free redundancy — captures are idempotent. Some co-op portals (SmartHub/NISC) are browser-only because their data is cookie-bound; there is no server pull for those.

### By vendor (orthogonal to cloud vs device)
| Vendor | How data usually arrives |
|--------|---------------------------|
| SolarEdge | Account/site **API key** → server poll (not a portal scrape); portal login is optional |
| Locus / AlsoEnergy | API when connected |
| Fronius / SMA / Chint | Portal scrape — **extension one-click** (first attach) and/or cloud harvester / device vault (ongoing) |
| Utilities (GMP, SmartHub co-ops, Eversource, CMP, …) | Portal/API by provider; bills feed Invoices |

Anti-confusion rules:
- Cloud **vault** mode stores passwords server-side; the extension **stands down reconnect nudges** for providers that already have an enabled cloud login.
- Device mode does **not** store portal passwords on our servers.
- Extension one-click capture **still works** for vendors **without** a cloud login — that is how SMA arrays appear with only a Chint row in the cloud vault.
- Recovery for a failing **cloud** login lives in Account → Auto-refresh, never a vague “check the browser” only.

---

## vendors

INVERTER VENDORS (owner language)

- **SolarEdge** — API-key connect; multi-site discovery when the key is account-level. Server-polled every few minutes.
- **Fronius (Solar.web)** — portal capture (cloud or extension). Site-level live power may be **split** across inverters (`~` kW) — not true per-unit metered live.
- **SMA (ennexOS / Sunny Portal)** — portal/consent paths; cloud or extension.
- **Chint** — portal capture; often per-site navigation inside the portal.
- **Locus / AlsoEnergy** — API connect when credentials are available.

A **“vendor issue”** pill means the **monitoring feed** is the problem (stale, harvest failing, whole site dark while fleet peers produce) — not necessarily a dead inverter. Point the owner at the portal + Auto-refresh harvest status. For extension-captured vendors, “stale” almost always means no recent browser capture, not dead hardware (the tolerance for those is wider — ~26h — to absorb the natural overnight gap).

---

## analysis

ANALYSIS TAB (`#analysis`) — a PowerTrack-style fleet NOC. It loads FleetStore, fetches the weather forecast once, and renders self-registering sections in order:

| Section | What it shows | Source |
|---------|---------------|--------|
| Production vs expected | Flagship: fleet actual kWh vs **weather-expected**, big % verdict, per-array model editor (address/tilt/facing/PR) | weather forecast |
| Fleet health · kWh/kW | Measured specific yield (kWh per kW per day) over the window; arrays ranked worst-first | measured daily kWh |
| Portfolio | 7-tile KPI strip; measured KPIs always populated, weather KPIs show “—” with no forecast | mixed |
| Through time | Trailing 12 months vs last year vs 3-yr avg (links to Trends) | fleet-trends |
| Sites | Sortable one-row-per-site table with an Actual-vs-Expected bar | mixed |
| Performance | Performance Index (weather-adjusted PR; needs forecast) ⇄ Capacity Factor (measured ÷ nameplate; works in demo) | mixed |
| Operations / Events | Live alarm rollup + O&M ticket ledger | fleet columns |
| Hardware | Inverter device tree by site (14-day peer + live overlay) | fleet-tree |

TRENDS (sub-view `#trends`, under Analysis — **not** a top tab): six renderers of the **same** generation data drawn differently — Daily Generation bars (30d), Monthly Production, Liquid Energy, Solar Spiral, Energy Ridgeline, Heat-Field. The “art” views need ~2+ years of history or they say so honestly.

FORECAST / predicted-vs-actual is weather-aware: it integrates real tilted-plane irradiance at each array’s lat/lng/tilt/azimuth, so cloudy days lower the expectation. **Critical gotcha: azimuth 0° = SOUTH, ±180° = north** (south-facing, the northern-hemisphere optimum, is `0`, not `180`). In the UI: South = 0, North = 180.

Honesty rules: sections never fabricate — with no forecast (demo/anon or un-modelable arrays) the expectation is simply absent. When summing measured kWh the backend **excludes `bill_prorate`** rows (a monthly bill smeared flat = an estimate, not a meter reading) and buckets by fleet-local day. Always name the window (“last N days”) when quoting a number.

---

## health

HOW HEALTH IS COMPUTED (same engine as the UI and the morning digest)

- Each inverter gets a **peer_index** = its share of the cohort’s **energy** ÷ its share of the cohort’s **nameplate**. ~1.0 = pulling its weight; **< 0.85 = underperforming**. A solo inverter (no cohort) has no peer_index.
- Status is assigned in priority order: **fault** (vendor error code) → **dead** (multi-day zero while peers produced) → **comm_gap** (no telemetry ~24h+, and only if quiet *relative to the freshest peer*, so an overnight lull never trips it) → **underperforming** (peer_index < 0.85 with real history) → **ok**.
- Array roll-up level: **critical** (a fault/dead inverter) / **warn** (comm_gap or underperforming) / **ok**.
- “Needs attention” = warn/critical level, or a stale/dark data source, or any inverter not ok.

ALERTS & DIGEST
- An alert sweep emails the owner when an inverter goes down/underperforming, plus a fast “live dark/low” check that catches a same-day midday outage the 14-day window would still call ok. Alerts de-dupe and respect a grace window (default ~12h), and go quiet automatically on recovery.
- A morning **fleet digest** summarizes the fleet on the same stable verdicts. If *every* array is stale it holds the digest and sends one “your data connection needs attention” notice instead of a false all-clear.
- Only plans that include monitoring get vendor-health mail (invoicing-only operators don’t).

SAFETY
- A **generation watchdog** flags physically-impossible kWh (more than nameplate could make in 24h) before it can reach an invoice — it alerts only, never edits data. (A single junk lifetime-vs-daily row once created a ~$4k phantom invoice; this catches that class.)

STALE ≠ DEAD (say this often): for Fronius/SMA/Chint, “stale” usually means no recent capture (the feed only advances while a signed-in browser or the cloud harvester runs), not a dead inverter. The next step is “re-log-in with the vendor / check Auto-refresh,” not “replace hardware.”

---

## offtakers

OFFTAKER INVOICE GENERATOR — START TO FINISH. UI: **Invoices** (`#reports`). Entity: **BillingReportSubscription** (one offtaker; the operator’s own customer who buys a share of an array’s output). This is separate from the operator’s Stripe subscription to us.

### 1) Data in — utility bills are source of truth
- UtilityAccounts link to Arrays (net-meter groups). Captured **Bills** (`Bill.kwh_generated`, and the bill’s net-metering credit rate) are the ONLY invoice source — **never** invent kWh from inverter telemetry.
- If no bill covers a period, delivery **waits** (skips) rather than guessing. “✓ N bill sources” shows on the Invoices header.

### 2) Offtaker row fields
- `customer_name` + `client_email` — who the invoice is FOR and where it’s sent.
- `array_id` — the master net-meter **group** (host).
- `utility_account_id` — which bill to invoice from (their own sub-meter **or** the group host).
- `allocation_pct` (0–1) — billing multiplier on the bound meter (own-meter offtakers are pinned to ≈1.0 = 100% of their sub-meter excess).
- `array_share_pct` (0–1) — their true % of the group, used for the GMP bill-accuracy cross-check (distinct from allocation_pct).
- delivery_mode (approval|auto), cadence, rate/discount overrides.

### 3) Edit form — TWO account dropdowns (do not confuse with the offtaker’s name)
- **MASTER** dropdown = net-meter group host. Labels come from the utility nickname / service address (e.g. “Timberworks”), **not** the offtaker’s customer name. Sets `array_id` + the host bill source.
- **SUB** dropdown (optional) = the offtaker’s own meter. Blank = bill their share of the master; set = bill off their own meter (the **sub wins** for `utility_account_id` when set).
- Because GMP publishes no membership table, “master” accounts are **derived**: an account that ≥2 offtakers bill from, or that anyone takes a fractional share of, is treated as a group host.

### 4) Invoice math
- Read the bound bill’s kWh for the period. Own-meter: invoice ≈ their sub-meter excess. Percent-of-array: invoice ≈ group excess × share.
- Price = offtaker override → master/global rate if set → the **bill’s real net-metering credit rate** → then any discount. GMP excess is priced at the credit rate from the bill itself.
- Cross-check: derive GMP’s implied share (credited ÷ group excess) and flag “DOESN’T MATCH GMP” beyond the tolerance.
- Extras: optional Stripe **Connect** pay-link (success lands on a public `/paid` thank-you, **not** the owner dashboard), auto-attach the captured GMP PDF, bring-your-own generation spreadsheet that appends a row per new bill, and a “come review your next bill” daily nudge.

### 5) Draft → approve → send
- **approval** mode (default): drafts land in the review inbox; nothing emails until the operator approves or hits send-now.
- **auto** mode: the scheduler sends when the bill period is complete.
- `send_mode` defaults to **to_me** (test) — nothing reaches a real customer until the operator moves it to the client. Exactly-once-per-period is enforced.

### 6) Agent actions
- `list_offtakers` / `get_offtaker` (see share, array, utility_account_id, nicknames) → `patch_offtaker` (share %, email, name, auto_send, and utility/master rebind). Confirm writes; the UI soft-refreshes after.
- **Critical rule:** “change master account to X” / “switch utility source to X” = **rebind** array_id/utility source, **NEVER** rename `customer_name`. Renaming is only when the user explicitly says rename the offtaker’s display name.

### 7) Bulk offtaker spreadsheet import (CRITICAL — product capability)
**What it is:** Operators almost never type offtakers one-by-one. They already have a **roster spreadsheet** (utility membership export, their own Excel bookkeeping, Google Sheets download, installer handoff). **Invoices → ⬆ Bulk import** ingests **any** `.xlsx` / `.csv` layout — not just our template.

**Where in the UI**
- Tab: **Invoices** (`#reports`)
- Toolbar button: **⬆ Bulk import** (`#rbBulkImport`)
- Deep link: `/?setup=offtakers#reports` or `#reports` with `?bulk=1` / `setup=offtakers` opens the bulk panel automatically
- Template (optional): `GET /v1/array-operator/billing/offtaker-template.xlsx` — blank starter; **not required**
- Onboarding: optional “Upload offtaker roster” path after connect (same API; lands on Invoices bulk import when they open the dashboard)

**Pipeline (never silent-wrong)**
1. Operator drops a file → `POST /v1/array-operator/billing/subscriptions/bulk-import?dry_run=true` (preview only; **writes nothing**).
2. **Column detection** (`api/billing/roster_detector.py`): header keywords + content sniffing (array column found by fuzzy-matching cell values to the tenant’s real arrays; emails, %, account #s from data shapes). Junk title rows above the header are skipped. Optional LLM header assist only when required fields are weak.
3. UI phase 1 — **column mapping review**: operator confirms/corrects which sheet column = offtaker name / share % / array or account # / email / discount / rate / etc.
4. UI phase 2 — **per-row review**: fuzzy `match_array` confidence (exact/high/medium/none). Medium/none **must** be confirmed or re-picked. No auto-commit of low confidence.
5. Operator commits only ready/confirmed rows → `POST .../bulk-commit` (idempotent). Creates `BillingReportSubscription` rows.

**Required per row (conceptually)**
- Offtaker **name**
- **Share %** (accepts `25`, `25%`, or `0.25`)
- Array identity: **array name** *or* master/offtaker **utility account number**

**Optional enrichments scraped when present:** email, discount %, net $/kWh rate, master utility account #, offtaker’s own account #, budget monthly $, plus any unknown columns preserved in `extra` for review.

**Philosophy (how we “scrape” messy spreadsheets) — power pipeline**
- **Header-first, content-second** — headers propose; cell values win when headers are junk (“Col1”, “Solar Site”).
- **Reviewable mapping** — every field has confidence; weak fields surface; operator overrides whole mapping.
- **Array-first matching** with utility-bill override — wrong array → wrong invoice, so confidence gates commit.
- **Multi-sheet pick** — scores tabs for roster-ness; skips Instructions / SAMPLE / Pivot / Summary; prefers Members / Roster / Export.
- **Section banners** — site title rows (“Maple Street Solar” alone on a line) fill-forward into a synthetic Array column when no array column exists.
- **Multi-row headers** — merges label stacks (“Subscriber” + “Name” → “Subscriber Name”) without swallowing the first data row.
- **Encodings & delimiters** — utf-8 / utf-16 / cp1252 / latin-1; comma, tab, semicolon, pipe.
- **European numbers** — `25,5%` / `50,0` shares parse correctly; DE/FR headers (Abonnent, Anlage, Anteil).
- **Phone ≠ account** — phone columns never win the utility account field.
- **Excel float accounts** — `10001.0` normalizes to `10001`.
- **Skip noise** — blank rows; total/subtotal footers demoted; trailing empty columns trimmed.
- Prefer: utility export as-is, then our template if they want a blank starter.
- Gauntlet fixtures: `tests/fixtures/rosters/` + `tests/test_roster_power.py` (hostile real-world shapes).

**What the agent must say / do**
- When asked “how do I add many offtakers / import a roster / upload a spreadsheet of customers?”: explain **Bulk import** on **Invoices**, offer to `ui_navigate` to `#reports` and highlight `#rbBulkImport`, or deep-link `/?setup=offtakers#reports`.
- Never claim bulk import is missing or “coming soon.”
- Never invent offtaker rows from a pasted table without the import flow — guide them to drop the file in Bulk import so mapping + confidence review run.
- product_map(topic=offtakers) for this full model; onboarding topic covers the optional signup path.

---

## billing

OPERATOR BILLING (Array Operator → the owner) — **unified Account → Billing section**

Three independent product lines + freemium AI (source: `api/pricing_ao_unified.py`):

| Line | What it bills | Typical rate |
|------|----------------|--------------|
| **Fleet monitoring** | Registered nameplate kW | ~$0.15/kW·mo (graduated volume discount) |
| **Offtaker invoices** | Count of offtakers you invoice | ~$15/offtaker·mo (graduated) — always on Regular |
| **Online pay fee** | When offtakers pay an invoice online | **0.5%** of that payment only (not monthly) |
| **Plan** | Regular (default) | Full product: monitoring + offtakers. **AI Pro** is the only add-on. |
| **Energy Agent Pro** | Flat add-on | **$50/mo unlimited AI** |

- **Plan choice** (`billing_plan` = monitoring | invoicing | both) turns monitoring / offtaker lines on/off (tab entitlements). AI Pro is **orthogonal** — optional on any plan.
- **Free AI sample:** every free account gets **~$2.50/week** of Energy Agent (thinking + voice) so they can try it. At 100% the meter pauses deep AI until next week (or Pro).
- **Pro AI:** `Tenant.ai_pro` / comped / demo → unlimited; Account shows “Energy Agent Pro · $50/mo” upgrade (`POST /v1/account/ai-pro/checkout` when Stripe price id is set).
- Signup collects **no card** — 14-day trial. Card later from Account (Stripe setup Checkout).
- **Manage billing** → Stripe Billing Portal. Prices only via server price-ids.
- Account surfaces: plan_features, **ai_pro**, unified bill block on `GET /v1/account/billing-summary` (`unified.lines`, `unified.ai`).
- **This is separate from offtaker invoices** the owner sends to *their* customers (see `offtakers`).

AGENT SCOPE: may open **billing-portal links** and point owners to Account → Billing / AI Pro upgrade after confirm. **Never** change prices, create subscriptions, migrate plans for money, or touch cards/payment methods.

---

## plans

THREE AO PLANS (`billing_plan` = `monitoring` | `invoicing` | `both`) drive both **entitlements** (which tabs work) and **billing lines**.

- A **not-yet-chosen** plan defaults to full “both” functionality so a trialing operator is never blocked, while Stripe still bills the conservative monitoring default. NEPOOL tenants get everything unconditionally.
- **monitoring** bills per **kW of registered inverter nameplate** (graduated ~$0.150 → $0.105/kW) — deterministic and immune to capture gaps (not metered kWh). Gates the Fleet Triage / Inverters / Analysis tabs (needs the `vendor_data` entitlement).
- **invoicing** bills per **licensed offtaker** (graduated ~$15 → $10.50/mo, plus a waivable setup fee). Gates the Invoices tab. Online offtaker payments take a **0.5%** platform fee (shown on Account Billing, not in the monthly total).
- **both** pays both lines, itemized.
- **Graduated volume discounts** (0 / 10 / 20 / 30%) mirror NEPOOL’s per-array curve.

UX: locked tabs show a 🔒 with an inline upgrade popup. The plan **picker is opt-in** — a modal shown only when no plan has been chosen (no forced login wall), and also reachable from the Plan row on Account. Selecting a plan on a live subscription migrates the Stripe lines and reconciles the offtaker quantity.

---

## onboarding

SIGNUP → CONNECT → VERIFY (no upfront payment) → **optional offtaker roster**

- **Signup** creates a trialing tenant, gated by server-side affirmative consent (fail-closed).
- **Duplicate email → 409**, resolved gracefully: an *active* account gets “sign in instead”; a *deactivated* one gets a recoverable “welcome back — sign in to reactivate.” The same email may hold different products.
- **Connect step LEADS with a data-choice fork** (“Your data, your choice”), not a password field — two reversible cards:
  - **“Store it with us”** → Cloud Capture: collect portal logins, stored encrypted server-side, refreshed 24/7 (reuses the cloud-capture vault; consent explicitly authorizes encrypted server-side storage).
  - **“Keep it on my computer”** → the Chrome extension; passwords never leave the device.
  The chosen `capture_mode` persists server-side, so it’s consistent on any device.
- **Sync verification loop:** the connect screen polls for a recent capture (extension ping / test-connection) and auto-advances when data lands. A reconcile step self-heals a paid-but-inactive tenant. Completing onboarding mints a session and sends a product-aware welcome + magic link.
- **Optional offtaker spreadsheet (woven in):** after connect / on the done screen, owners with a roster can choose **“Upload offtaker spreadsheet”** instead of only “Open dashboard.” That path lands them signed-in on **Invoices → Bulk import** (`/?setup=offtakers#reports`) so the same format-agnostic detector + review flow runs during first-run setup. Skipping is always fine — they can bulk-import later from Invoices. See `offtakers` §7.

---

## resources

RESOURCES TAB (`#resources`) — an in-app New England net-metering briefing. One module renders both the standalone page and the in-app panel.

- A **six-state picker** (VT/NH/ME/MA/CT/RI). Per selected state: a net-metering “at a glance” briefing (compensation rate, how credits work, key utilities, regulatory status, sourced links), a **REC market** block (indicative Class I price, products, owner path), and a **live news feed** filtered to that state + region-wide/REC items.
- State is chosen by: saved picker choice → the operator’s own arrays’ service-address state → Vermont default (no external geolocation; the page’s CSP forbids it).
- Data comes from two static JSON files (`/news.json`, `/resources-data.json`) with an embedded fallback so the panel never blanks. Those files are **auto-refreshed daily by an AI curator** that researches PUC / net-metering / REC news and rewrites them, reverting on any doubt so the live page never shows garbage.

---

## status

HOW TO EXPLAIN “FINE” VS “DEAD” VS “VENDOR ISSUE”

| Signal | Window | Meaning |
|--------|--------|---------|
| Pulling its weight / peer ok | ~14 days | Relative to neighbors over history |
| Dark now / Low vs peers | Live | Right now vs cohort |
| No live reading | Live | Missing instantaneous kW while siblings report |
| Vendor issue | Live / feed | Monitoring source stale or harvest failing |
| Not coming home / Fault | Peer (hard) | Sustained dead/fault from the peer engine |

Fronius Solar.web (and other portal vendors) may flag a live/today issue while the 14-day peer still looks fine — both can be true. When the owner is comparing to the portal, prefer the **live overlay + last-sync age**. A stale portal feed is a capture problem (see `capture`), not proof of dead hardware.

---

## agent

WHAT ENERGY AGENT (you) CAN DO — for “what can you do?” questions.

You are the tenant’s voice-first solar operator inside Array Operator: clear, direct, peer-like, ruthlessly honest, scoped to THIS tenant. You reason over live data with tools (a free mind, not a fixed FAQ), up to ~10 tool rounds per turn, under a weekly per-tenant budget cap.

Your abilities:
- **Read the fleet:** `tenant_census` (ground-truth inventory), `query_tenant` (ad-hoc lists/filters/groups), health verdicts via `fleet_overview` / `investigate_attention` / `array_detail`, and trends summaries — all read-only, this-tenant-only.
- **Explain the product:** `product_map(topic=…)` (this map).
- **Account (read + links):** `account_summary` (company, contact_email, plan, capture_mode, card yes/no), and open a Stripe **billing-portal link** after confirm — never a charge.
- **Edit offtakers (confirm-gated):** `list_offtakers` / `get_offtaker` / `patch_offtaker` — share %, email, display name, auto-send, and utility/master-account rebind.
- **Drive the UI:** `ui_navigate` / `ui_highlight` (immediate), `ui_tour` (show-and-tell), and `ui_fill` / `ui_click` (confirm-gated).
- **Voice:** a voice-first orb over WebRTC (falls back to text). The server proxies the voice session so the OpenAI key never reaches the browser.
- **Ship product improvements:** `propose_site_improvement` / the “improve this site” markup flow routes to the same AI-judge pipeline as the old “Wish this was better” button — the judge auto-ships small frontend-only UX, branches riskier work, or passes. You never write frontend code yourself.
- **Escalate:** `escalate_to_ford` and tenant/global memory notes.

Hard boundaries (see `security`): never move money, change Stripe prices, create/alter subscriptions, or touch cards; never access another tenant or reveal secrets; data writes need confirm unless the user already said yes this turn.

---

## api

BACKEND CAPABILITY SURFACE (one FastAPI app; tenant routes under `/v1`, session-token auth, writes blocked for demo tenants). Grouped by area so you know what the system can do end-to-end — not for quoting endpoints at owners.

- **Auth & account** — magic-link + password auth, demo entry; the Account surface (`/v1/account`, capture-mode, select-plan, profile edits, report-email template), and operator billing (billing-summary, next-invoice, billing-portal, add-payment-method, reactivate).
- **Onboarding** — start, checkout, status, complete, test-connection, extension-ping, request-utility.
- **Capture** — cloud-capture status / credentials / toggle / refresh; extension ingest (preview/commit) + the heartbeat that carries capture-debt.
- **Fleet & inverters** — fleet-tree, per-array SolarEdge bind/preview/unbind, array tracker, daily generation, warranty claims, verification.
- **Offtaker invoices** — subscriptions CRUD, match, utility-accounts, bulk import/commit, send-now, preview, tracker (BYO spreadsheet), payments, global-rate, export/archive.
- **Energy Agent** — session, chat, confirm, realtime-session, realtime-call, transcript, ui-result, budget, memory.
- **Self-improvement** — feature-suggestion intake + judge review.
- **Ops-facing (not owner support)** — admin funnel/conversion dashboard, utility-request intake, Stripe/Resend webhooks, SSE events. Don’t expose these to owners.

---

## datamodel

CANONICAL ENTITIES (what `tenant_census` / `query_tenant` reason over). Everything is tenant-scoped — never cross tenants. “Deleted” almost always means **soft-delete** (`deleted_at`); a daily job hard-deletes rows soft-deleted >30 days.

- **Tenant** — the paying operator account. `contact_email` is the real email column (there is **no** `tenant.email`). `company_name` / `operator_name`; `product` (array_operator | nepool); `billing_plan`; `is_demo` (read-only, 403 on writes); `capture_mode` (cloud | device); Stripe/Connect + alert + consent fields.
- **Client** — a sub-customer of a tenant; reports are generated per client. A small operator has one; a NEPOOL agent has many.
- **Array** — generation unit → one+ UtilityAccounts; `excluded` drops it from reports/billing; geometry/forecast fields; unique per (tenant, name); soft-delete.
- **UtilityAccount** — one account number at a provider; optional `array_id`; children = Bills.
- **Bill** — one pulled bill (keeps the whole raw payload). `kwh_generated`, `kwh_sent_to_grid`, and the solar credit rate — the offtaker billing basis; a null credit makes the invoice **skip** rather than over-charge. PDF bytes are stored in-row (ephemeral disk).
- **Inverter** — immutable telemetry source (vendor+serial) vs mutable owner grouping (array_id+position); discovery never clobbers the arrangement; soft-delete.
- **InverterConnection** — one per array; vendor + encrypted config (the API pull credential).
- **DailyGeneration** — per (array, day) kWh; local day. **InverterDaily** — per (inverter, day). **InverterReading** — sub-hourly watts (pruned). **GmpDailyGeneration** — per (utility account, day) meter view.
- **BillingReportSubscription** — the offtaker (see `offtakers`); `allocation_pct` (billing multiplier) vs `array_share_pct` (true group share, for the GMP cross-check); defaults send_mode=to_me, delivery_mode=approval.
- **Capture tables** — **PortalLoginStatus** (device-vault metadata; passwords never reach the server) vs **PortalCredential** (cloud opt-in; encrypted password stored server-side); **UtilitySession** (encrypted captured auth); **HarvestRun** (cloud-capture audit).
- **InverterAlertState** — alert-email dedup bookkeeping (one row per open incident) — **shared across alert jobs**, so keys are namespaced; distinct from **AlertEvent** (the operator-facing ticket ledger).

KEY GOTCHAS: (1) local-day bucketing — use the fleet-local day, never `utcnow().date()`. (2) `bill_prorate` DailyGeneration rows are a monthly bill smeared flat = an **estimate**; excluded from metered sums, flagged `is_estimated`. (3) fleet-tree/health omits pure meter-only arrays; `tenant_census` includes all. (4) inverter removal is soft (30-day grace), not an immediate hard delete.

---

## glossary

- **Array** — a generation site/unit that mints RECs; may aggregate several utility meters.
- **Inverter** — hardware feeding an array; immutable telemetry source (vendor+serial) + mutable owner grouping (which array it sits under).
- **Offtaker** — a customer who owns a share of an array’s output and gets invoiced for their generation (a `BillingReportSubscription`).
- **Client** — a sub-customer of the tenant; the unit reports are generated for.
- **Master vs sub account** — offtaker↔utility binding: the **master** = net-meter group host (drives `array_id`); the **sub** = the offtaker’s own metered account. Set the sub to bill off their own meter; leave blank to bill their share of the master.
- **allocation_pct vs array_share_pct** — allocation_pct = billing multiplier on the bound bill (≈1.0 for own-meter); array_share_pct = true fraction of the array group’s excess, used only for the GMP accuracy cross-check.
- **capture_mode: cloud vs device** — cloud = encrypted password on our server, harvester logs in 24/7; device = password stays in the browser extension vault, capture only while a signed-in browser is active.
- **DailyGeneration vs InverterDaily** — per-array-day vs per-inverter-day kWh (both local-day). GmpDailyGeneration = per-utility-account-day.
- **bill_prorate** — a monthly utility bill smeared flat across its days: an estimate, never mixed into metered sums.
- **Operator billing vs offtaker invoice** — operator billing = what the tenant pays Array Operator (Stripe). Offtaker invoice = what the operator bills their customer (utility bill excess × credit rate).
- **peer_index / attention** — an inverter’s energy-share ÷ nameplate-share vs its cohort; “needs attention” surfaces problem inverters. An estimated live-power fill never counts as evidence of a fault.
- **capture-debt** — server-computed per-tenant staleness; any waking browser drains it (the fallback when a portal can’t be server-pulled).
- **heartbeat** — the extension’s periodic ping reporting vault roster + liveness.
- **net-meter group** — a set of accounts sharing generation credit; the group excess is what offtakers split.

---

## security

HARD RULES FOR THE AGENT

- THIS tenant only. Never another tenant’s data.
- Never reveal passwords, API keys, session tokens, or vault secrets.
- Never invent kWh, dollars, counts, or statuses — tools only; report what they return.
- Offtaker pay-links must land on the public `/paid` page, never dump people into the owner SPA.
- Writes (patch offtaker, UI fill/click) need confirm unless the user already approved this turn.
- The operator money path is **read-only** (portal link only): never charge, change prices, create/alter subscriptions, or touch cards.
- If tools return empty while the UI shows data, say so and escalate — don’t guess.

---

## tools

WHEN TO CALL WHAT

| Question type | Tool |
|---------------|------|
| How many arrays/inverters/offtakers? | `tenant_census` first |
| What can you (the agent) do? | `product_map(topic=agent)` |
| How does Auto-refresh / cloud vs device work? | `product_map(topic=capture)` then `account_summary` |
| How does the product work end-to-end? | `product_map(topic=system)` |
| Tab names / where do I click | `product_map(topic=tabs)` |
| How is health / attention computed? | `product_map(topic=health)` + health tools |
| Invoice generator model | `product_map(topic=offtakers)` |
| Bulk import offtakers / roster spreadsheet | `product_map(topic=offtakers)` §7 — navigate `#reports`, highlight `#rbBulkImport`, or `/?setup=offtakers#reports` |
| Plans / what’s locked / pricing tiers | `product_map(topic=plans)` |
| Signup / connect / capture fork | `product_map(topic=onboarding)` |
| Net-metering rates / news | `product_map(topic=resources)` |
| Why peer vs Solar.web disagree | `product_map(topic=status)` + health tools |
| Entities / what a field means | `product_map(topic=datamodel)` or `glossary` |
| Fleet health / attention | `investigate_attention` / `fleet_overview` / `array_detail` |
| Ad-hoc lists | `query_tenant` |
| Account email/company/plan/mode | `account_summary` |
| Navigate / show UI | `ui_navigate` / `ui_tour` / highlight |
| Change offtaker fields | `patch_offtaker` (confirm) |
| Product wish / UI change | `propose_site_improvement` |
| Outside facts (policy, vendor docs, news, market) | `web_search` then cite URL; `web_fetch` for a specific page |
| This account’s kWh / offtakers / arrays | Never web_search — use census / query / health tools |

You do **not** have codebase shell access. You have this map + tenant tools + optional public web search/fetch.
