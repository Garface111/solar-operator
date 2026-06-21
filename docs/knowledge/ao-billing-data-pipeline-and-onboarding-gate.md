# AO billing data-pipeline, multi-array offtakers, invoice↔bill reconcile, capture→link, onboarding gate

Session-proven (Jun'26). All LIVE + tested on prod. Backend = /root/solar-operator,
frontend = /root/array-operator (Netlify). Covers: the TWO GMP data streams, the
Bill→daily transformer, multi-array offtaker billing, invoice-vs-GMP reconcile,
the capture→array-link path, and the "you're not done until GMP" onboarding gate.

## THE BIG ARCHITECTURE TRUTH — two GMP streams; the frontend reads neither bills
Ford's "ultimate data transformer" thesis exposed a real gap. There are **two
separate GMP data shapes**, and the owner frontend (Trends, fleet totals, 30-day
bars, month×year) reads ONLY the *daily* streams — NOT the Bill table:
1. **`Bill`** = monthly statements. 47k+ rows on prod, richly parsed (kwh_generated,
   total_cost, kwh_consumed, net_credit, avg_rate_cents_kwh, raw_json sponge). This
   is what the bill-pull worker captures.
2. **`GmpDailyGeneration`** = 15-min-interval API pull → per-day. Was **0 rows
   everywhere** — the granular pull needs a captured GMP session+account-linked-to-array.
3. **`DailyGeneration`** = inverter/CSV/portal daily (what Trends merges).

So 47k parsed bills NEVER surfaced: they sat in a table the UI doesn't read.
**When Ford says data "doesn't show up," trace capture→parse→store→TRANSFORM→
frontend-read→merge.** The gap is almost always the transform/read seam, not storage.
Read paths: `api/reports/gmp_daily_read.py` (GMP daily seam, READ-ONLY — never import
Gmp* ORM directly), and `array_owners.py` Trends builder merges DailyGeneration +
gmp_daily_read per day (CSV/real wins on overlap). `_SOURCE_FAMILY`/`_SOURCE_ORDER`
in array_owners.py is the attribution legend — a `bill_prorate`→`bill` ("Bill
(prorated)") family was ALREADY wired but nothing ever wrote those rows.

## Bill→daily TRANSFORMER (the missing link) — api/jobs/bill_to_daily.py
Converts each Bill's kwh_generated, prorated evenly across its service days, into
`DailyGeneration(source="bill_prorate")` — the exact stream + source-family the UI
already renders. PRIORITY RULE (critical): bill-prorate is the COARSEST source; it
only fills days NO real metered reading covers. The `UniqueConstraint(array_id, day)`
+ a `_REAL_SOURCES` set guarantee inverter/CSV/GMP-API readings always WIN the slot;
bill-prorate is the gap-filler. Multi-meter days SUM. Idempotent (re-run updates only
its own bill_prorate rows). Wired: nightly 05:30 UTC (`_run_bill_to_daily` in
scheduler.py, AFTER the 05:00 GMP daily backfill so granular days land first) +
admin triggers `/admin/bill-to-daily/{tenant_id}` and `/admin/bill-to-daily/all`.
PROVEN on prod: 16,285 bills → 333,791 daily rows across 242 arrays, 104 real-data
days correctly skipped; one array gained 13 YEARS of history (2013→2026).
CAVEAT to state to Ford: proration is even-spread = faithful MONTHLY truth shown
daily, NOT true daily shape; real daily shape comes from inverter/GMP-15min where present.

## Multi-array offtaker billing (one offtaker owns a share of SEVERAL arrays)
- Model: `BillingReportSubscription.array_allocations` JSON = `[{array_id, allocation_pct}]`
  (migration in api/migrate.py, JSON works sqlite+PG). Legacy single `array_id`/
  `allocation_pct` kept for back-compat — delivery PREFERS array_allocations when present.
- Math: `delivery.build_manual_match` sums each array's (period kWh × pct) into ONE
  combined invoice; `_normalized_allocations(sub)` coerces/validates the list.
- Invoice PDF (api/billing/invoice.py): a "Your share by array" table = one line per
  array → summed total. The breakdown rides on `computed_invoice["array_breakdown"]`
  AND `project_totals["array_breakdown"]`; **`invoice_for_period` rebuilds `inv` via
  compute_invoice and does NOT carry computed_invoice's extra keys — you MUST thread
  `array_breakdown` through invoice_for_period explicitly or the table won't render**
  (this exact bug bit once: PDF math right, breakdown table missing).
- Endpoint `/subscriptions` accepts `array_allocations` as a JSON-string form field.
- Step-3 wizard UI (reports.js): single-array dropdown → CHECKBOX list of arrays, each
  with its own % input (enabled when checked); finish sends array_allocations when >1.
  Confirmed with Ford: ONE combined invoice summing per-array shares.

## Invoice ↔ GMP-bill reconciliation — api/billing/reconcile_bills.py
READ-ONLY trust check. `GET /v1/array-operator/billing/reconcile-bills`. Per offtaker
per array: our invoice's produced-kWh vs the GMP Bill's kwh_generated for the matching
period (±20-day overlap). Verdict: match | mismatch (with kWh delta+%) | no_bill
(no GMP bill linked — HONEST, never fabricated) | no_invoice_data. Never mutates.

## Capture→array LINK path (why AO arrays show nothing)
Diagnosis pattern: AO tenants had 0 GMP sessions, 0 UtilityAccounts, arrays bare
(no nepool_gis_id, no account#). The pipeline (extension→/v1/sync→autopop→bill-pull,
scheduler enqueues pull_bills every 6h for ALL active tenants) is FULLY BUILT — the
gap is that no GMP login ever happened under the AO tenant. Two fixes shipped:
1. **Autopop link-by-name** (app.py /v1/sync): when a captured GMP account isn't
   linked, PREFER attaching to the owner's existing active same-named array that has
   no GMP account yet, instead of creating a DUPLICATE. (Autopop historically only
   CREATED arrays — would've spawned "Starlake-2".)
2. **Manual link endpoints** (array_owners.py) for the multi-meter case (Starlake =
   3 GMP accounts → 1 array) that name-matching can't resolve:
   `GET /v1/array-owners/utility-accounts` (accounts + link state + bill_count) and
   `POST /v1/array-owners/utility-accounts/link` (account_id + array_id|null). Tenant-scoped.
Once linked → bill→daily transform → array lights up in Trends/Audit/reconcile.
The remaining gap is the HUMAN GMP login via the extension under the AO tenant —
code can't trigger it; never fabricate an account/bill to fake it.

## Onboarding GATE — "you're not done until you connect GMP"
`GET /v1/array-owners/onboarding-status` returns gmp_connected, linked_arrays,
unlinked_accounts, complete, next_step (connect_gmp|link_accounts|done). Complete =
GMP connected AND ≥1 array linked (no settlement bills = nothing to audit/reconcile/bill).
Frontend (index.html `#gmpGate` + app.js `updateGmpGate(session)`): unmissable banner
below the tab bar on every tab — amber "FINISH SETUP / Connect GMP" or green "ALMOST
DONE / Link your GMP accounts", hidden when complete, never shown to signed-out
visitors. Drove it off a DEDICATED lightweight endpoint, NOT threaded through the heavy
fleet-tree (avoids the fleet-store.js adaptTree/toColumns field-dropping pitfall).

## Vendor adapter fixes proven live this session
- **SolarEdge timestamps are SITE-LOCAL, not UTC.** `lastUpdateTime`/equipment-telem
  `last_report` come in the site's tz (VT = America/New_York). Code stamped naive→UTC,
  inflating the "source offline" age by ~4h. FIX in adapters/solaredge.py: fetch site
  `location.timeZone` (cached) via `/site/{id}/details`, convert naive local→UTC ISO
  (`_localize_to_utc_iso`). This was a FLEET-WIDE bug (every SolarEdge site). The age
  only corrects on the NEXT poll (re-stamps last_report). "GMP says 24h, we say 19h"
  is ALSO partly definitional: GMP times the utility METER, we time inverter TELEMETRY
  — two feeds; label the card's outage time as the inverter feed vs the utility meter.
- **Fronius Solar.web devwork series is WATTS, not kW** (verified live: Primo 12.5kW
  read ~1699 = 1.7kW). Normalize ÷1000 once in solarweb_content.js. Solar.web cadence
  ~30min → the live-fresh window must be ≥60min (was 30 → rejected legit 34-min-old points).
- **SMA OAuth rotates refresh tokens** (returns a NEW one, invalidates the old on every
  refresh). Adapter discarded it → worked ~1h then 401 until reconnect ("SMA wasn't
  working until I reconnected"). FIX api/inverters/sma.py: capture rotated token, reuse
  freshest, PERSIST back to connection config (poller flag_modified — JSON cols don't
  auto-dirty); on 401 clear the dead token. AlsoEnergy already handled rotation in-mem
  but didn't persist (same redeploy weakness). Tell = reconnecting fixes it.

## Extension self-diagnosing logs (debug a blank capture console fast)
Fronius capture (solarweb_content.js) returned SILENTLY at each gate. Added loud
per-gate `LOG()`: "content script loaded vX on <host>" (absence = not injected),
"tick #N — intent: yes/NO", "signed in: yes/NO", "captured systems/inverters",
"✓ capture complete". BUMP manifest version each change; build via
scripts/build_extension_zip.sh → Ford's Desktop (he loads unpacked MANUALLY).
Distribute a build to a non-dev (Bruce) via a GitHub Release asset (gh release create
ext-vX <zip>) + verify the download link 200s; load-unpacked needs the FOLDER not zip.

## Shared-tree git hygiene (sibling agents active in BOTH repos this session)
Sibling agents were mid-rebuild of solar-operator web/ AND touched array-operator
trends.* / command-center.css. They ran `git add` that swept THEIR files into the
index under my name. ALWAYS: `git reset -q` then `git add <only-my-exact-files>`,
then `git diff --cached --name-only` to confirm ONLY your files, before commit.
On array-operator a sibling's same-file edits (command-center.css) interleave — verify
your cached diff is only your hunks. AO deploy still MANUAL via python3
/tmp/netlify_api_deploy.py; backend auto-deploys on push, then VERIFY route is
401 (alive) not 404 (stale) — sibling pushes can delay/race your deploy.
