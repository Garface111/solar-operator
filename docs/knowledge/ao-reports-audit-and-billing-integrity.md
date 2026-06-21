# AO Reports/Audit build + billing data integrity (Jun 2026)

Class: the Array Operator owner-site billing/audit surfaces and the
production↔settlement data integrity behind them.

## AO ARRAYS DATA-PATH TRAP (recurring — the #1 "feature built but not showing")
The Arrays/sandbox tab does NOT read the backend `/fleet-tree` directly. It goes
through `public/fleet-store.js`, which REBUILDS each column in TWO places —
`adaptTree(t)` (on ingest) AND `toColumns(ids)` (on render) — field-by-field,
DROPPING anything not explicitly listed. A new backend column field arrives
`undefined` at sandbox.js and the render guard silently no-ops.
→ When a field "isn't showing", grep `fleet-store.js` FIRST. Thread the field
through BOTH adaptTree and toColumns. This bit us on `source_status` this session
(banner never rendered until threaded through both). Same class as the GMP
backfill / reconcile "never landed" bugs.

## Source-data freshness / "SOURCE OFFLINE" signage
- Backend `inverter_fleet._source_status(inv_rows)` → {state: ok|stale|none,
  last_report, age_hours}; stale = freshest inverter last_report older than
  `_SOURCE_STALE_HOURS` (6h). Attached to each fleet-tree column as `source_status`.
- Frontend (sandbox.js `sourceStatusHTML` + .sb-srcout CSS): when stale, show an
  amber banner + a "⚠ SOURCE OFFLINE" corner ribbon + amber card frame
  (`.sb-array--srcout`). Copy makes ownership explicit: it's the VENDOR feed that
  went dark, NOT Array Operator; labels it the INVERTER feed (vs the utility meter
  which tracks separately). Ford wanted this UNMISSABLE (three treatments).
- See inverter-vendor-adapter-quirks.md for the SolarEdge tz bug that made the age
  itself wrong.

## Fleet Audit tab (settlement reconciliation surface)
- Engine `api/reconciliation/reconcile_array()` existed but had NO route/tab/cron
  (brain with no face). Surfaced it: backend `GET /v1/array-owners/fleet-audit`
  (runs reconcile per array → coverage % + dollars flagged + status counts +
  per-array verdicts, leaks sorted first); frontend Audit tab (audit.js+audit.css)
  between Arrays and Trends — hero coverage ring + $ flagged, status filter chips,
  per-array verdict rows (status spine, variance %, production-vs-settlement,
  dollars at risk). Tab wiring = sandbox.js TABS map + tabFromHash + window.__aoLoadAudit.
- Coverage reality (live, Jun'26): 405 arrays, 232 w/ settlement, ~2 w/ production
  → 0 auditable. Bottleneck is DATA PLUMBING (production leg), not the engine. The
  tab honestly shows "needs data" until GMP daily lands. INTEGRITY: a variance
  backed only by utility-sourced production = `leak_unconfirmed` (shows $, never an
  asserted leak); a real `leak` requires an INDEPENDENT inverter feed.
- Weekly client digest: scheduler `deliver_weekly_audit_digest` Mon 13:00 UTC →
  per active AO tenant, audits the fleet, emails Tenant.contact_email via
  `_send_via_resend(product="array_operator")` + email_skin. Sends "all clear" when
  clean; skips owners with no bills; never invents a leak.

## Multi-array offtaker billing (one offtaker → share of several arrays)
- Model: `BillingReportSubscription.array_allocations` JSON = [{array_id,
  allocation_pct}]. NULL/empty → legacy single array_id/allocation_pct (back-compat).
  Migration adds the column (JSON, idempotent sqlite+PG).
- Math: `delivery.build_manual_match` — when allocations present, SUM each array's
  (period kWh × pct) into ONE combined invoice; `_normalized_allocations(sub)`
  coerces/validates. Per-array breakdown carried on computed_invoice +
  project_totals as `array_breakdown`.
- Invoice PDF: `invoice.py` renders a "Your share by array" table (one line per
  array → summed Total) ABOVE the line items when >1 array. NOTE: the renderer
  reads `inv = invoice_for_period(...)` which REBUILDS via compute_invoice — must
  pass `array_breakdown` through `invoice_for_period`'s `inv.update({...})` or the
  table silently won't render (same field-stripping class as fleet-store).
- UI step 3 (reports.js): checkbox list of arrays, each with its own % input
  (enabled on check); finish sends `array_allocations` JSON. Single-array path kept.
  QA hooks added: `window.__rbWizGoto(n)` + `window.__rbRenderWizard(state)` to
  drive the wizard in Playwright without a real session.
- Step-2 copy (Ford's framing): the rate used = the rate on the current bill
  (auto-read solar credit rate); the owner enters the DISCOUNT from their offtaker
  CONTRACT. Labels only, internals untouched.

## Invoice ↔ GMP-bill reconciliation (verify invoice values vs the meter)
- `api/billing/reconcile_bills.py` + `GET /v1/array-operator/billing/reconcile-bills`:
  per offtaker, per array, compares our invoice's produced-kWh vs the captured GMP
  Bill.kwh_generated for the same array+period. Verdict: match | mismatch (kWh
  delta + %) | no_bill (no GMP bill linked — honest, never fabricated) |
  no_invoice_data. Compares PRODUCED kWh per array (before the offtaker %) to
  isolate "is production right vs the meter" from allocation. Read-only.
- DIAGNOSIS that matters: the GMP auto-scrape pipeline is FULLY BUILT and running
  (extension capture → /v1/sync stores sessions+accounts → autopop links arrays →
  scheduler enqueues `pull_bills` every 6h for ALL active tenants → worker pulls +
  `adapters/gmp._extract_full_record` extracts kWh/cost/credit/rate/raw_json).
  Proof: 47k+ GMP bills system-wide. BUT every AO tenant has 0 utility accounts /
  0 bills — they NEVER had a GMP login captured under the AO tenant; the arrays
  (Starlake/Timberworks/Tannery Brook) were created via spreadsheet/autopop with no
  GMP session behind them. The pull job runs but has nothing to pull. This is a
  CAPTURE/LINKING gap, not a missing pipeline — same "data plumbing not engine"
  class. To close: log into GMP via the extension while signed in as the AO tenant.
- Current AO subs are all "(sample)"/demo rows, not real billing relationships.

## THE DATA-UNIFICATION LAYER — "ultimate data transformer" (the deeper gap)
Ford's framing: "we need to be the ultimate data transformer." Two streams + a
missing bridge surfaced this session:
- THERE ARE TWO GMP STREAMS, and the frontend reads the WRONG one for bills:
  (1) `Bill` = monthly statements (47k rows, RICH: kwh_generated, total_cost,
  kwh_consumed, avg_rate_cents_kwh, net_credit, raw_json) — what the bill-pull
  captures. (2) `GmpDailyGeneration` = 15-min-interval daily series (0 rows in
  all of prod). The Trends/Arrays surfaces read the DAILY streams
  (`DailyGeneration` + `GmpDailyGeneration`) and NEVER the `Bill` table for
  production → 47k parsed bills were a dead-end, invisible in the UI. Extraction
  was great; the TRANSFORM into the form the frontend integrates was missing.
- FIX — Bill→daily transformer (`api/jobs/bill_to_daily.py`): prorate each bill's
  kwh_generated evenly across its service days into `DailyGeneration` rows with
  source=`bill_prorate` — a source family the UI was ALREADY wired for ("Bill
  (prorated)" in `_SOURCE_FAMILY`/`_SOURCE_ORDER`/`_SOURCE_LABELS` in
  array_owners.py; the slot existed, nothing ever filled it). PRIORITY: the
  (array_id, day) UniqueConstraint + an explicit source check mean bill-prorate
  ONLY fills days no real metered reading covers; real sources (inverter/CSV/
  gmp_api) always win the slot. Idempotent (only writes/refreshes its own
  bill_prorate days); multi-meter days SUM. Wired: nightly 05:30 UTC (AFTER the
  05:00 GMP daily backfill) + admin triggers `/admin/bill-to-daily/{tenant_id}`
  and `/admin/bill-to-daily/all`. PROVEN on prod: 16,285 bills → 333,791
  bill_prorate days across 242 arrays, 104 days correctly skipped (real data won);
  array "BMU" gained 13 years (2013→2026) of daily production from PDFs.
- THE UNIFIED TIMELINE (the answer to "data transformer"): all sources merge into
  one per-array daily series with priority inverter telemetry > CSV > GMP-API
  15-min > bill-prorate, no double-count (CSV/inverter wins on overlap). The merge
  machinery already existed in the Trends endpoint + `reconciliation/reconcile._production_over_window`;
  it just had nothing to merge because the daily streams were empty for AO.
- HONEST LIMIT: prorating is even-spread (bill total ÷ days) = a faithful MONTHLY
  truth shown daily, not true daily shape; real daily data wins where present.
- BILL-ONLY signals (consumption, $ cost, net credit, blended rate) still have NO
  frontend home — they can't come from inverters. Candidate next surface
  ("Energy & cost" view). Don't conflate with production.

## CAPTURE→ARRAY LINK path (so AO arrays light up on a real GMP login)
The autopop in `/v1/sync` historically only CREATED a new array per GMP account —
no path to ATTACH a captured account to an EXISTING array → a GMP login under the
AO tenant would spawn duplicates (collide with existing "Starlake"). Built both
halves:
- AUTO link-by-name in `/v1/sync` autopop: when a captured account isn't linked,
  PREFER an existing active same-named array of the owner that has no GMP account
  yet (case-insensitive name match, deterministic, no-guess) before creating a new
  array. Existing autopop tests still pass.
- MANUAL bridge (multi-meter case — Starlake = 3 GMP accounts → 1 array — which
  name-matching can't auto-resolve): `GET /v1/array-owners/utility-accounts`
  (captured accounts + link state + per-account bill_count + the tenant's arrays)
  and `POST /v1/array-owners/utility-accounts/link` {account_id, array_id|null}
  (null = unlink). Tenant-scoped. Once linked, the account's captured bills flow
  into the array's daily stream via the bill→daily transform; trends/audit/
  reconcile light up. Proven end-to-end: link account-with-bills → transform →
  existing array gains 30 days/3000 kWh, NO duplicate array.
- Code can't trigger the CAPTURE itself — the GMP login via the extension under
  the AO tenant has never happened; that's still the one human step. Both link
  endpoints are API-only (no UI panel yet).

## ★ Dual-GMP-script RACE → false "couldn't read your inverters" on a GOOD connect
TWO content scripts run on greenmountainpower.com at once (both in the manifest):
- `content.js` (BILL capture) → POSTs `/v1/sync` server-side (creates accounts +
  pulls bills), then broadcasts `SO_CAPTURE_LANDED {ok:true, accountCount:N}` —
  **with NO `accounts[]` array and no `kind`.**
- `gmp_meter_content.js` (LIVE-USAGE capture) → broadcasts
  `SO_CAPTURE_LANDED {kind:"utility_meter", accounts:[...]}`.
The AO `handleCaptureLanded` (sandbox.js) only accepted the message if `d.accounts`
was a non-empty array. The bill broadcast (no array) usually WINS the race → fell
through to the `else` → showed red "We reached GMP but couldn't read your
inverters" on a SUCCESSFUL connect (doubly wrong — GMP is a METER, not inverters).
FIX: add a FIRST branch that treats the bill-sync broadcast as SUCCESS —
`isMeterProvider && !hasAccounts && d.ok!==false && d.kind!=="utility_meter"` →
refresh fleet + onboarding gate, toast "Connected N accounts — bills syncing in",
close modal. Keep the live-usage `accounts[]` branch. Make the genuine-failure
copy meter-appropriate for gmp/vec/wec ("couldn't read your account", not
"inverters"). Verify the branch logic deterministically in node (dev proxy can't
establish a Playwright session): bill-sync→success, live-usage→ingest,
true-empty→honest error.

## Onboarding gate + "Connect GMP must LAUNCH the real flow" (Ford correction)
`GET /v1/array-owners/onboarding-status` → {gmp_connected, has_gmp_accounts,
linked_arrays, unlinked_accounts, arrays_total, complete, next_step
∈ connect_gmp|link_accounts|done}. complete = GMP connected AND ≥1 array linked.
Drives an unmissable banner BELOW the tab bar (`#gmpGate` index.html,
`updateGmpGate(session)` app.js): amber "FINISH SETUP/Connect GMP" → green
"ALMOST DONE/Link your GMP accounts" → hidden when complete. Hidden for signed-out
visitors.
★ The CTA must LAUNCH THE REAL CONNECT FLOW, never href back to /onboarding (Ford:
"the fuck is this connect GMP link that leads back to the onboarding ... it should
just open the GMP login in another tab and grab the data"). The real flow already
lives in sandbox.js: `openPortalLogin("gmp")` → `extSend("SO_OPEN_PORTAL",
{url:greenmountainpower.com, provider:"gmp"})` → extension opens portal in a new
tab + captures. Expose `window.__aoConnectGmp()` (opens add-array modal; if
`EXT_PRESENT` fires `openPortalLogin("gmp")`, else the modal already shows the
"add the free helper" step) and wire the banner CTA to it via onclick (clear the
href). Also expose `window.updateGmpGate` / `window.__aoRefreshGmpGate` so a
landed capture re-checks the gate.

## SolarEdge timezone bug — outage clock ran ~4h fast ("GMP 24h vs we 19h")
SolarEdge's equipment API returns `lastUpdateTime` in the SITE's LOCAL time (IANA
from `/site/{id}/details` `location.timeZone`, e.g. America/New_York) with NO tz
marker, but `inverter_fleet._source_status` stamped naive timestamps as UTC →
a VT site looked ~4h MORE stale than reality. FIX in `adapters/solaredge.py`:
cache the site tz (zoneinfo), convert naive local → tz-aware UTC ISO in
`fetch_inverter_telemetry`'s `last_report` (safe fallback leaves naive when tz
unknown). SEPARATELY, GMP and we measure DIFFERENT events (GMP = last utility
meter reading; us = last inverter telemetry) — they never match exactly even with
correct tz, so LABEL the source on the card ("inverter monitoring last reported
Nh ago … your utility meter tracks separately"). Corrected age only refreshes on
the next poll that re-stamps last_report.

## Cross-agent git hygiene on the SHARED solar-operator tree (recurring this session)
Sibling agents run `git add` that sweeps YOUR files AND theirs into the index
(seen repeatedly: a co-agent rebuilding the React web bundle staged app_dist
deletions + web/app edits + its own new test/screen files under MY commit). RIGHT
BEFORE every commit: `git reset -q` then `git add <your exact files>`, and
`git diff --cached --name-only` to CONFIRM the staged set is EXACTLY yours before
`git commit`. The array-operator repo is separate but a co-agent may still touch
trends.css/trends.js — stage only your files there too.

## Verify-before-claim discipline (held throughout)
Never fabricate a comparison/leak when data is thin — report "awaiting GMP data"
/ "no_bill" / "$0 until production lands" with the real reason. Ford prizes the
honest gap-named-explicitly over a faked win. Render every PDF/UI change and
vision_analyze it over localhost http; verify the served file/route post-deploy
(401 not 500 = alive+migrated; grep the live JS for the change).
