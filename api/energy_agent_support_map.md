# Array Operator — Energy Agent Support Map

**Source of truth for the in-app Energy Agent** (`product_map` tool).
Support-facing: how the product works for THIS tenant, end to end. No deploy ops, no Railway SSH, no multi-tenant admin, no secrets.

Coding agents editing the product still load skill **`solar-operator-energyagent`**.
When product behavior changes, update **this file** (regenerate with the **product-map-cartographer** agent, or edit by hand).

Topics = `## heading` ids below. Call `product_map(topic=<id>)` before explaining that area.
Available topics: `tabs · system · fleet · capture · vendors · analysis · health · offtakers · generation_reports · billing · plans · onboarding · resources · status · agent · api · datamodel · glossary · security · tools · surface · product_spine · surface_* · surface_mobile · product_spine_mobile · surface_mobile_*`.

**Page-level understanding** (macro / meso / micro):
- **Desktop:** `product_map(topic=surface)` or `surface_fleet` / `surface_marketplace` /
  `surface_invoices` / … Source: `energy_agent_surface_model.md` + atlas shots in
  `array-operator/docs/surface-atlas/`.
- **Mobile (owner-web /m + React Native):** `product_map(topic=surface_mobile)` or
  `surface_mobile_fleet` / `surface_mobile_agent` / … Source:
  `energy_agent_mobile_surface_model.md`. Honor `context.client` (owner-web |
  owner-native | desktop).

---

## tabs

### Desktop top nav — EXACT labels (hashes internal)

| User-facing label | hash | What it is |
|-------------------|------|------------|
| **Fleet** | `#dashboard` (Triage) · `#arrays` (Sandbox) | **One top tab** with segments **Triage \| Table \| Sandbox**. Attention queue + vendor spreadsheet + spatial fleet canvas. (Inverters is no longer a separate top tab.) Default attention home = Triage. |
| **Analysis** | `#analysis` | Fleet NOC. Sub-views: Fleet analysis · **Trends** (`#trends`) · **Resources** (`#resources`). Trends/Resources are NOT top tabs. |
| **Invoices** | `#reports` | Offtaker solar-credit invoices (NOT “Reports”). Segments: Offtakers · Bill audit · Trends · **Generation reports** (when revealed — see `generation_reports`). |
| **Repairs** | `#ops` | Chat-first O&M automation. Staged Agent prompt; roster + cases in chat/tools. |
| **Marketplace** | `#marketplace` | Offtaker Exchange — vacancy (unallocated credits) + demand waitlist; Credit Exchange / Array Market subs. |
| **Account** | `#account` | Profile, plan/card, Auto-refresh vault. |

Never say Dashboard, Arrays, Reports, Operations, Inverters, Trends, or Resources as
**top-tab** names. Spoken Fleet attention view = “Fleet Triage” or “Fleet → Triage.”
The offtaker form field **Master account** is a different concept (net-meter group host).

### Mobile bottom nav (owner-web `/m` + owner-native)

| Label | Route (web) | Notes |
|-------|-------------|--------|
| Fleet | `/fleet` | Cards/table; default home |
| Analysis | `/analysis` | Summary + Ask Agent |
| Invoices | `/invoices` | Pipeline + offtakers |
| Repairs | `/repairs` | Cases + Agent |
| Market | `/marketplace` | Short label for Marketplace |
| Account | `/account` | Profile / plan |

Energy Agent: **bottom dock** → full-screen chat (AgentSheet / AgentModal). Attach via
composer **+** (Camera / Library / File on web). Context: `client=owner-web|owner-native`,
`surface=agent_sheet_mobile|rn_agent`, `mobile=true`.

### APP SHELL notes
- **Desktop SPA:** static `array-operator/public/*`; router in `sandbox.js` (`hashchange`).
  Auth `localStorage["so_session"]` + tab-scoped session helper. FleetStore is source of
  truth for arrays domain.
- **Mobile web:** Vite React app `apps/owner-web` deployed under `/m`.
- **Native:** Expo app `apps/owner-native`.
- **Legacy mobile OS** on the desktop bundle (`mobile-os.js`, narrow viewports): AI-home
  + Detail mode still possible; context may include `mobile_os` + phase.

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

**NEPOOL Operator (solaroperator.org / nepooloperator.com):** Cloud Capture is the **recommended default** for new signups (onboarding fork: “Store it with us” vs “Keep it on my computer”). Operators store **client utility** logins (GMP, SmartHub co-ops, etc.) in Master account → Cloud Capture; the harvester pulls **bills** ~12h. Extension remains available as the private/device path. Existing tenants with `capture_mode` null or `device` keep the extension portal roster as primary — no forced migration.

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
- “Needs attention” = warn/critical level, or a stale/dark data source **in daylight**, or any inverter not ok. **At night** (`is_daylight=false`): zero live power and overnight source quiet are **sleep**, not attention — only multi-day health flags or multi-day data silence count.

TIME ZONES & NIGHT (Energy Agent must get this right)
- Fleet calendar day + default sun-up use **America/New_York** (US Eastern) — Vermont / NE solar. Per-array lat/long (when stored) shifts sun-up for distant sites so a West-coast array is never judged on Vermont’s sun.
- Every Energy Agent turn gets a **FLEET CLOCK** (local time + solar_state day/night). Tool rows carry `is_daylight` / `solar_state`. At night: do **not** invent outages from zero live power; multi-day 14-day flags can still be real.

ALERTS & DIGEST
- An alert sweep emails the owner when an inverter goes down/underperforming, plus a fast “live dark/low” check that catches a same-day midday outage the 14-day window would still call ok. Alerts de-dupe and respect a grace window (default ~12h), and go quiet automatically on recovery.
- A morning **fleet digest** summarizes the fleet on the same stable verdicts. If *every* array is stale it holds the digest and sends one “your data connection needs attention” notice instead of a false all-clear.
- Only plans that include monitoring get vendor-health mail (invoicing-only operators don’t).

SAFETY
- A **generation watchdog** flags physically-impossible kWh (more than nameplate could make in 24h) before it can reach an invoice — it alerts only, never edits data. (A single junk lifetime-vs-daily row once created a ~$4k phantom invoice; this catches that class.)

STALE ≠ DEAD (say this often): for Fronius/SMA/Chint, “stale” usually means no recent capture (the feed only advances while a signed-in browser or the cloud harvester runs), not a dead inverter. The next step is “re-log-in with the vendor / check Auto-refresh,” not “replace hardware.”

EXPECTED-LOW / SHADING (an inverter that’s *supposed* to run low):
- Some inverters are permanently below their peers for a **fixed physical reason** — afternoon shade from a neighbour’s tree, a chimney, a poor roof face. That’s not a fault to chase; flagging it “underperforming” forever just trains the owner to ignore the flag.
- When the owner confirms the cause, the inverter is marked **expected-low**: we record its current peer ratio as a **baseline** and judge it against *that* level instead of the cohort floor. So a steadily-shaded unit reads calm (“Holding at its expected reduced level ~42% of peers”), but it **still flags and alerts if it drops BELOW that baseline** — a genuine new problem on top of the shading. This silences the known bias without going blind.
- The Energy Agent should PROACTIVELY spot a steady, long-standing underperformer and **ask** the owner whether shading explains it; on a yes it calls `mark_inverter_expected_low`. There’s also a manual toggle on the inverter’s detail card. Removing the shading (tree cut) → `clear_inverter_expected_low` returns it to normal grading. `expected_low_breach = true` means a marked unit fell below its baseline — treat it as a real issue.

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

## generation_reports

NEPOOL / REC GENERATION REPORTING — folded in from **NEPOOL Operator** (2026-07-16).

WHAT IT IS
- Automated **NEPOOL-GIS / REC generation reports**: per-client generation **workbooks** (GMCS format — **one sheet per producing array**, columns Quarter · Generation (MWh) · Reporting Amount · **RECs**, where a REC = **floor of the MWh**), built from the fleet's **utility-measured** generation and emailed to each client on a cadence. This is the compliance/reporting side — a NEPOOL reporting consultant / stamping agent (the operator) files generation for the solar operators they serve so NEPOOL-GIS awards them RECs (1 REC per MWh).
- The reported window is the **6 rolling complete quarters ending ~2 quarters back** — NEPOOL-GIS issues RECs about two quarters AFTER generation, so the in-progress and just-finished quarter aren't reported yet (don't tell an owner this quarter's RECs are ready). V2 generalized it beyond solar to any REC fuel (wind/hydro/etc.) via `Array.fuel_type`; solar uses the byte-pinned GMCS writer.
- It ORIGINATED as its own product (nepooloperator.com) and was folded into Array Operator so it's one product. The AO surface is a **chrome-less React embed** (`/genrep/embed.js` on the AO origin — the NEPOOL Operator app's `build:embed` bundle) that reuses the same `so_session` + `/v1` API (no separate login). Its screens: **Clients** (table + canvas), **Reports** (cadence, an AI email-template studio, **Send now**, a **[SAMPLE]** test-send to yourself, send history), and **Verify accuracy** (workbook diff).

WHERE THE OWNER FINDS IT
- A **“Generation reports” sub-tab under the Invoices tab** (`#reports` → segment **Generation reports**, hash `#reports/generation`), alongside Offtakers · Bill audit · Invoice Trends. NOT a separate top-nav tab.
- The pill is always visible (AO demo philosophy: every capability shows). The embed only MOUNTS when the account's reports world is live (`GET /v1/account` → `generation_reports: true`), otherwise an honest state.

CUSTOMER MODEL — CLIENTS, not offtakers (do not confuse)
- Generation reports are sent to the operator's **CLIENTS** (the solar operators whose arrays they report on, e.g. Green Mountain Solar). Each client has arrays, a **report cadence** (weekly / monthly / **quarterly** default), and workbook history.
- **Offtakers** (see `offtakers`) are a DIFFERENT concept — they're who the operator *invoices* for solar credits. Clients receive generation reports; offtakers receive invoices. Both live under the Invoices tab; keep them distinct.

ELIGIBILITY / STATE (honest — most AO accounts are NOT enabled yet)
- Gated on the **explicit `Tenant.generation_reports` marker** (`report_eligibility.tenant_in_reports_world`, `api/report_eligibility.py`). Legacy/NEPOOL tenants: always in. AO tenants: only when the fold **migration** (`scripts/migrate_nepool_tenant.py`) flips the marker. The marker is explicit ON PURPOSE — data-presence inference (“has clients + cadence”) is unsafe because the AO capture path auto-creates a Client per utility login (47 real AO tenants already have capture-created clients), so inference would have mailed workbooks to dozens of accounts.
- **Today it is FALSE for every AO tenant until the fold migration runs (behavior-neutral).** So a signed-in AO owner sees the pill but gets: *“Generation reports aren't set up for this account yet — they automate NEPOOL/REC reporting… ask us to enable them.”* Anonymous demo shows the door + explainer, never fabricated NEPOOL data. Never tell an owner it's active when `generation_reports` isn't true — offer to have Ford enable it for their fleet.

THE PIPELINE (once enabled)
- Scheduled sends by each client's cadence (`Tenant.report_frequency`, default **quarterly**, per-client override; ~09:00 UTC batches). **Pre-send review** email to the operator 2 days before a batch (exactly what will send, to whom) + **delivery receipt** after (Resend-confirmed delivered / bounced / awaiting-confirmation) — `api/jobs/report_digests.py`, logged in `ReportDelivery`. Plus on-demand from the dashboard: Send now (all or a picked subset), a [SAMPLE] to yourself.
- Operator **directory downloads** (all clients at once, emailed only to the operator): the **NEPOOL-GIS REC directory** (`/v1/account/directory-report.xlsx` — RECs/MWh per array per quarter, GMCS form for bulk upload) and a raw **generation directory** (`/generation-directory.xlsx` — utility kWh per project × month). Both gated on `generation_reports` + having report clients.

DATA
- Built from **utility-measured** generation — `DailyGeneration` (used exclusively for a month when present, no bill mixing) and the GMP 15-min interval overlay (`GmpDailyGeneration`), the authoritative **NEPOOL-truth series**. It deliberately EXCLUDES inverter/vendor telemetry and bill-prorate estimates from the REC basis (telemetry feeds monitoring; utility reads feed RECs). Array carries `nepool_gis_id` (e.g. "Chester (53984)"), `fuel_type`, `excluded`.
- The fold's `--carry-generation` re-points both series from the source array to its claimed AO twin so the workbook stays byte-identical (never recomputed).

AGENT GUIDANCE
- Route here for: NEPOOL / NEPOOL-GIS / REC reporting, “generation reports / workbooks to my clients”, quarterly generation reporting, report cadence, “did my clients' reports go out”.
- To open: `ui_navigate #reports`, then the **Generation reports** sub-tab (or deep-link `#reports/generation`). Say **Invoices → Generation reports**, never “NEPOOL Operator” as a place in the app (it's folded in).
- Distinct from **offtaker invoices** (billing customers for solar credits) and from **vendor/inverter monitoring** (Fleet Triage / Inverters). If unsure whether it's enabled, check `account_summary` / `GET /v1/account.generation_reports` and give the honest state.

EDITING THE CLIENT ROSTER (you have direct tools — don't just describe, DO it)
- **Read:** `list_gen_clients` (whole roster) and `get_gen_client(client_name|client_id)` — a client's email, cc, cadence, the arrays under it **with their NEPOOL-GIS ids + fuel**, utility accounts, and the GMP/VEC login bindings. Use these to answer "how many arrays does Bruce have", "what are his NEPOOL ids", "how are his logins organized". Never say "I don't have a tool to look up the client roster" — you do.
- **Create:** `create_gen_client(name, contact_email?, report_frequency?, gmp_email|gmp_username?, vec_email|vec_username?)`. Setting the GMP/VEC login makes future captures auto-file arrays onto that client. Starts with no arrays; no charge.
- **Edit a client:** `patch_gen_client(client_name|client_id, …)` — rename, contact/cc email, cadence (monthly|quarterly), GMP/VEC login binding, active, notes.
- **Edit / organize an array:** `patch_gen_array(array_name|array_id, …)` — set its `nepool_gis_id` / `fuel_type` / `region`, and/or **move it to another client** (`reassign_to_client_name|reassign_to_client_id`) to fix how arrays sit under clients.
- **MONEY (hard stop):** `auto_send=true` on `patch_gen_client` ENROLLS the client in **$15/array/quarter** automatic reports — it returns a confirm first; only re-call with `confirm_auto_send=true` after the owner's explicit yes. There is no tool to create/delete arrays or exclude them from billing (those change the Stripe count) — direct that to the UI.
- Clients here are `Client` rows — the SAME table is NOT offtakers (`BillingReportSubscription`, edited via `patch_offtaker`). Clients get generation workbooks; offtakers get invoices. Keep them distinct.

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

**NEPOOL wizard path (live):** Welcome → Info → ClientSetup → **Connect fork** → either **Cloud** (complete early → save utility logins → Done) or **Extension** (device path, unchanged) → Done. GetStarted marketing leads with Cloud Capture, not Chrome install. Cloud path sets `capture_mode=cloud` and must not double-call `/complete` (duplicate welcome email guard).

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

You are the tenant’s voice-first solar operator inside Array Operator: clear, direct, peer-like, ruthlessly honest, scoped to THIS tenant. You reason over live data with tools (a free mind, not a fixed FAQ), up to ~6 tool rounds per turn, under a weekly per-tenant budget cap.

Your abilities:
- **Read the fleet:** `tenant_census` (ground-truth inventory), `query_tenant` (ad-hoc lists/filters/groups), health verdicts via `fleet_overview` / `investigate_attention` / `array_detail`, and trends summaries — all read-only, this-tenant-only.
- **Money senses:** `production_forecast` (weather-expected vs actual — cloudy week vs real problem), `investigate_attention.recoverable_usd_month` (the Fleet Triage “Recoverable $/mo” math), `list_recent_invoices` (drafted/sent offtaker dollars + totals). Use these to advise on earnings, not vibes.
- **O&M healing:** `repair_ops_overview` / `list_service_contacts` / `list_repair_tickets` — know the installer/O&M team, open repair tickets when sites are down, draft and (with confirm) email tech check-ins. Distinct from manufacturer **warranty claims**.
- **Explain the product:** `product_map(topic=…)` (this map).
- **Account (read + links):** `account_summary` (company, contact_email, plan, capture_mode, card yes/no), and open a Stripe **billing-portal link** after confirm — never a charge.
- **Edit offtakers (confirm-gated):** `list_offtakers` / `get_offtaker` / `patch_offtaker` — share %, email, display name, auto-send, and utility/master-account rebind.
- **Drive the UI:** `ui_navigate` / `ui_highlight` (immediate), `ui_tour` (show-and-tell), and `ui_fill` / `ui_click` (confirm-gated).
- **Voice (mouth-only — the live default):** a voice-first orb over WebRTC (falls back to text). The server proxies the session so the OpenAI key never reaches the browser. **You are the only mind. The voice model is a MOUTH: it reads your `[SPOKEN]` line verbatim and has no tools, no fleet data, and no opinions of its own.** There is no second agent consulting you — on a voice turn the panel text AND the spoken line are both authored by **you**, in one turn. If the voice ever says something you did not write, that is a bug (an undriven Realtime response), not another agent's view. **Never explain a voice answer as "the voice layer's own understanding" — there isn't one; if the spoken line was vague, that was your line.** (An `Option D weave`, where Realtime converses and calls `consult_deep_brain`, exists in the code but is **OFF**; it is live only if the runtime context says `voice_weave: true`. Do not describe it as how voice works.)
- **Ship product improvements:** `propose_site_improvement` / the “improve this site” markup flow routes to the same AI-judge pipeline as the old “Wish this was better” button — the judge auto-ships small frontend-only UX, branches riskier work, or passes. You never write frontend code yourself.
- **Standing objective (get them fully set up + keep them operational):** `setup_status` = the completeness model (arrays, auto-refresh, DATA FRESHNESS, utility bills, offtakers, repair contact, online pay) + the single highest-value `top_gap`. It's injected into your context every turn, so you always know the gap — lead with the SPECIFIC gap and offer to act, never ask "is everything set up?". Stale capture = a money leak; go silent when fully operational. `refresh_capture` (confirm-gated) actually fixes it — re-arms cloud logins (~1 min) + re-pulls bills; be honest that device/extension + SmartHub/VEC only refresh from an open browser and SolarEdge auto-polls. The mind proactively nudges the top gap with restraint (in-app when they're around, the weekly check-in by email, a direct email only when data's been stale long enough to cost money) and stays quiet when green.
- **Reminders & watches (you contact them):** `create_reminder` / `list_reminders` / `cancel_reminder`. When the owner says “remind me…”, “tell/notify/email me if/when…”, “let me know when…”, “watch for…” — set a reminder YOU keep and deliver. **time** reminder (fire_at / delay) or **watch**: `inverter_down` (an inverter goes dead/fault — optionally scoped to one site), `array_recovered` (a down site comes back), `array_attention` (a site starts needing attention), `data_stale` (capture goes stale), or `custom` (anything else — put the exact condition in condition_text). It fires ONCE, edge-triggered, and you EMAIL them (from your mailbox) + mirror it into chat. This is DIFFERENT from **Fleet Alerts** (the robotic, rule-based Alerts tab / FAB — sensitivity + frequency, always-on): those are automatic; YOUR reminders are the specific things THEY asked you to watch. If they want the standard down/underperform alerting, point them at Fleet Alerts; if they want “tell ME if THIS happens”, use create_reminder.
- **Escalate:** `escalate_to_ford` and tenant/global memory notes.
- **Weekly check-in (email):** every Monday you email the owner a first-person note — what you handled (repair outreach/replies), what you noticed (attention arrays + recoverable $/mo, weather-adjusted ratio, pending invoice totals). The owner can REPLY to that email and you act on it (same session, same tools; UI-driving is described in words on the email channel). Opt-out link lives in the email footer; if asked "stop the Monday emails," point them at it.

Hard boundaries (see `security`): never move money, change Stripe prices, create/alter subscriptions, or touch cards; never access another tenant or reveal secrets; data writes need confirm unless the user already said yes this turn.

---

## api

BACKEND CAPABILITY SURFACE (one FastAPI app; tenant routes under `/v1`, session-token auth, writes blocked for demo tenants). Grouped by area so you know what the system can do end-to-end — not for quoting endpoints at owners.

- **Auth & account** — magic-link + password auth, demo entry; the Account surface (`/v1/account`, capture-mode, select-plan, profile edits, report-email template), and operator billing (billing-summary, next-invoice, billing-portal, add-payment-method, reactivate).
- **Onboarding** — start, checkout, status, complete, test-connection, extension-ping, request-utility.
- **Capture** — cloud-capture status / credentials / toggle / refresh; extension ingest (preview/commit) + the heartbeat that carries capture-debt.
- **Fleet & inverters** — fleet-tree, per-array SolarEdge bind/preview/unbind, array tracker, daily generation, warranty claims, **repair ops** (service contacts + repair tickets + check-ins), verification.
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
- **ServiceContact** — a person/company on the operator’s O&M/repair team (installer, electrician, tech). Optional tenant default; array assignments via **ArrayServiceAssignment**.
- **RepairTicket** — field-repair work item for a down/faulted site (status open → waiting_reply → scheduled → in_progress → resolved). Auto-opened when fleet shows dead/fault **and** a contact is known. **RepairCheckIn** = outbound email or status note log.
- **WarrantyClaim** — manufacturer paperwork (different from RepairTicket). Claim = vendor warranty; ticket = human O&M follow-up.

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
| NEPOOL/REC generation reports · workbooks to clients · report cadence | `product_map(topic=generation_reports)` — Invoices → Generation reports (`#reports/generation`) |
| “remind me…” / “notify/tell/email me if/when…” / “watch for…” / “let me know when…” | `create_reminder` (you email + chat when it fires). NOT the same as Fleet Alerts (robotic rule-based). `list_reminders` / `cancel_reminder` to manage. |
| “what can you do / what can you turn on?” · a tool comes back `status=skill_locked` | `list_skills`. Some capabilities ship **dormant** per account (e.g. **Text your repair techs by SMS** `sms_alerts`, **free-form custom watches** `custom_watches`). If the owner asks for one, **offer to enable it and call `enable_skill`** — instant, no code, within your envelope. You are giving yourself a tool you already have but that was switched off. Never say “I can’t” for a dormant skill. |
| “can you add / build a NEW ability you don’t have?” (not any known skill) | `request_capability` — files a build request to Ford + the gated builder (a coding agent may build it on a branch; **it only reaches the owner after human approval**). Be honest: it’s a request, **not instant**, and you never merge it yourself. |
| “text / SMS my repair tech” | Needs the **`sms_alerts`** skill (off by default). Offer to enable it (`enable_skill sms_alerts`), then `send_repair_sms`. Email crew outreach (`send_repair_checkin`) is always on. |
| “this didn’t work” · “the button’s missing / it looks broken” · “what am I looking at?” · you need to verify the rendered UI before guiding or proposing a change | **`see_screen`** — capture and SEE the owner’s real screen (screenshot → your vision). Needs the **`screen_vision`** skill (off by default); if locked, offer to enable it (`enable_skill screen_vision`). The owner grants screen access once (browser prompt / the 👁 button). Flow: call `see_screen`, say in ONE line you’re taking a look, then STOP — the screenshot arrives on the NEXT turn as an image you can actually read. Don’t guess at the UI before the image lands. This is request 60 — the capability you asked for. |
| “did that email send / arrive?” · “did Rex get it?” · you’re about to claim mail did or didn’t go out | **`check_email_delivery`** — Resend’s own receipts (delivered / bounced / complained) for this account’s contacts. **Check, never guess**: you *can* and *do* send repair email (`send_repair_checkin`), so never say “I can’t send emails.” Read the result honestly — **no receipt ≠ not delivered** (receipts only exist for recent mail); say “I have no receipt for that window.” A **bounce is real and actionable**: name the bad address and the reason. |
| Bulk import offtakers / roster spreadsheet | `product_map(topic=offtakers)` §7 — navigate `#reports`, highlight `#rbBulkImport`, or `/?setup=offtakers#reports` |
| Plans / what’s locked / pricing tiers | `product_map(topic=plans)` |
| Signup / connect / capture fork | `product_map(topic=onboarding)` |
| Net-metering rates / news | `product_map(topic=resources)` |
| Why peer vs Solar.web disagree | `product_map(topic=status)` + health tools |
| Entities / what a field means | `product_map(topic=datamodel)` or `glossary` |
| Fleet health / attention | `investigate_attention` / `fleet_overview` / `array_detail` |
| Who repairs my arrays / O&M team | **In chat on Repairs** (`#ops`): `list_service_contacts` first. Agent is **hungry** for a full roster — on any name/email scrap call `upsert_service_contact` immediately (`needs_confirm=false`), confirm what was saved, then ask phone → arrays → “anyone else?”. Never close with “Done.” while the sheet is incomplete. |
| Down site — contact the tech | Agent drafts outreach; owner may **Approve & send** on the case card or say “send it” in chat (`send_repair_checkin`). First outreach is never auto. **TRUSTED contacts:** after the owner approves ONE send to a contact, follow-ups to that contact go out automatically on the check-in cadence (interval + spam guards). Owner says “stop auto follow-ups” → `upsert_service_contact(trusted=false)`; re-arm with `trusted=true`. Tenant mode `off` disables everything. |
| Repair pipeline status | Repairs panel “what I’m working on” + agent log; tools: `repair_ops_overview` / `list_repair_tickets` |
| Old ticket / history (“Chester in May?”, keyword, date range) | `search_repair_tickets(array_name, date_from, date_to, keyword)` — includes closed cases |
| Check in every N days until resolved | `update_repair_ticket(ticket_id, checkin_interval_hours=72)` (per-ticket cadence; auto-sends need trusted contact or tenant auto mode) |
| Save warranty claim / diagnostic / service request as a file | `save_document(content, type, ticket_id?)` → durable row + optional PDF; `list_documents` to retrieve |
| Fleet-wide $/mo leaking right now | `fleet_financial_health` (total burn + per-array cumulative est. + recoverable if fixed) |
| Steady underperformer vs sudden drop (shading?) | `underperformer_history` → `steady_underperformer_candidate`; then ask owner → `mark_inverter_expected_low` |
| Why is capture stale / login dead? | `capture_health_detail` (per-vendor last success, last error, login_failed vs scrape_failed vs MFA) |
| What am I looking at on screen right now? | UI context `live_ui_digest` (visible cards/chips/table rows) — trust over generic tab memory |
| Voice barge-in mid-answer | Context `user_interrupted` — abandon old path; answer only the new question |
| Repair status update from tech | Inbound email to `repairs@agent.arrayoperator.com` (`[AO-TICKET-#]`) → logged + owner chat update. **Energy Agent then continues the email conversation** with whoever replied (purposeful, open-ended): schedule / parts / done / owner action — until the case is coordinated. Auto-replies (OOO) are ignored. **Chat ⇄ email is one continuous surface:** every mail turn is mirrored into the open chat session and injected as ground-truth into agent context so “did they reply?” never invents silence. |
| Site down a WEEK with no fix | **Autonomous escalation ladder.** Every down inverter opens a ticket immediately (contact-less if no O&M contact — visible in Repairs, duration tracked), and the immediate in-app nudge fires day one. When a fault has been active ≥7 days (`EA_ESCALATE_DAYS`) and hasn't been owner-escalated, the mind emails the OWNER from `agent@agent.arrayoperator.com` (`[AO-TICKET-#]`, opt-out-aware): what's down, how long, ~$/mo, and the ask — no contact → "reply with your repair person's name + email and I'll reach out"; contact already engaged → "push them harder or bring in someone else?". **The owner's reply is parsed** (`handle_owner_escalation_reply`): a repair contact → created + assigned + marked trusted (owner authorized), the ticket adopts it, and Energy Agent **AUTO-sends the first crew outreach right then** (they said go), then the normal crew conversation + trusted follow-ups run, all mirrored into chat. Decline → case closed. Owner stays in the loop the whole way. |
| Rates / news / REC | **Analysis → Resources** (`#resources`) — not under Repairs |
| Why is production low / weather or broken? | `production_forecast` (fleet ratio vs one array’s ratio) |
| What is downtime costing / recoverable $ | `fleet_financial_health` (fleet totals) or `investigate_attention` → `recoverable_usd_month` + per-array `recoverable` |
| What did we invoice / how much is drafted? | `list_recent_invoices` (never sends — approve lives on Invoices) |
| Ad-hoc lists | `query_tenant` |
| Account email/company/plan/mode | `account_summary` |
| Navigate / show UI | `ui_navigate` / `ui_tour` / highlight |
| Change offtaker fields | `patch_offtaker` (confirm) |
| Product wish / UI change | `propose_site_improvement` |
| Outside facts (policy, vendor docs, news, market) | `web_search` then cite URL; `web_fetch` for a specific page |
| This account’s kWh / offtakers / arrays | Never web_search — use census / query / health tools |

You do **not** have codebase shell access. You have this map + tenant tools + optional public web search/fetch.


### Mobile / agent write tools (2026-07)

When the owner is on the phone app and asks to **add offtakers**, **marketplace vacancy**,
**capture demand**, or **Stripe Connect for offtaker pay**, the agent MUST use tools (not
“go to desktop only”):

| Tool | Use when |
|------|----------|
| `create_offtaker` | Add a subscriber (name required; email + share preferred) |
| `list_offtakers` / `patch_offtaker` | List or edit existing |
| `marketplace_vacancy` | Unallocated kWh/$ excess on THIS fleet |
| `list_exchange_demand` / `create_exchange_demand` | Waitlist leads who want credits |
| `payments_connect_status` / `start_payments_connect` | Offtaker online pay (Stripe Express) |
| `setup_status` / `capture_health_detail` / `account_summary` | Connect feeds / auto-refresh |
| `send_pipeline` / `list_recent_invoices` | Invoice pipeline pulse |

Demo tenants: create/write tools return `demo_blocked` — explain and invite real signup.

