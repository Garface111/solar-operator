# Data unification, capture→array linking, and "launch the real flow" UX

The recurring meta-bug on this product: **data is captured + parsed but never
TRANSFORMED into the stream the frontend actually reads**, so it looks like
"nothing shows up" when storage is full. Plus the capture→link plumbing and the
"buttons must do the real thing" UX rule. (Jun'26 session.)

## 1. TWO GMP streams — the frontend reads the DAILY one, not Bill
There are THREE production stores; the AO frontend (Trends, fleet totals, 30-day
bars, month×year) reads only the **daily** streams, never the `Bill` table:
- `Bill` — monthly statements. 47k rows captured + richly parsed (kwh_generated,
  total_cost, kwh_consumed, avg_rate_cents_kwh, net_credit, raw_json). **NOT read
  by the UI for production.**
- `GmpDailyGeneration` — 15-min-interval daily sponge (`gmp_daily_read.py`
  contract: get_daily_series / get_monthly_totals / get_coverage). Filled by the
  05:00 `gmp_daily_backfill` job — **needs a captured GMP session+account linked
  to an array**. Was EMPTY everywhere on prod.
- `DailyGeneration` — inverter/CSV/portal daily (`extension_pull`, `csv`,
  `gmp_api`, etc.). The Trends read merges this with GmpDailyGeneration,
  CSV/inverter-wins-on-overlap.

DIAGNOSE "bills don't show up" by counting rows per store on prod (railway ssh
python probe) and checking whether the offtakers' arrays have linked
UtilityAccounts — almost always they DON'T (capture gap), so every store the UI
reads is empty even though Bill is full.

## 2. The Bill→daily transformer (the missing link) — `api/jobs/bill_to_daily.py`
Converts each bill's `kwh_generated` evenly across its service days into
`DailyGeneration` rows with `source="bill_prorate"` — a source family the UI was
ALREADY wired to render ("Bill (prorated)", in `_SOURCE_FAMILY`/`_SOURCE_ORDER`
in array_owners.py) but nothing ever populated.
- PRIORITY is everything: bill-prorate is the COARSEST source. It ONLY fills days
  no real metered reading covers. The `(array_id, day)` unique constraint + an
  explicit source check guarantee inverter/CSV/GMP-API readings are NEVER
  overwritten (`days_skipped_real` counts those). Multi-meter days sum.
- Idempotent: re-run only writes/updates `bill_prorate` days (0 dupes on rerun).
- Wired: nightly **05:30 UTC** (after the 05:00 GMP-daily backfill, so granular
  GMP days land first) + admin triggers `POST /admin/bill-to-daily/{tenant_id}`
  and `/admin/bill-to-daily/all` (admin-gated).
- Prod result first run: 16,285 bills → 333,791 daily rows across 242 arrays;
  one array gained 13 YEARS of history (2013→2026). Verified the array gains
  daily rows AND `_source_family('bill_prorate')=='bill'`.
- HONEST framing for Ford: prorate is a faithful MONTHLY truth shown daily, not
  true daily shape — real daily shape comes only where inverter/GMP-15min exists
  (and those win). Say this; don't imply it's granular.

## 3. Capture→array LINK gap — autopop only CREATES, never LINKS to existing
`/v1/sync` autopop creates ONE new Array per GMP account; it had no path to
attach a captured account to an EXISTING same-named array → a GMP login under an
AO tenant would spawn "Starlake-2" duplicates (or collide on uq_array_per_tenant)
instead of lighting up the real Starlake.
- FIX 1 (in `/v1/sync` autopop): before creating, PREFER linking to an existing
  active same-named array of the owner that has no GMP account yet
  (case-insensitive name match, deterministic, no-guess).
- FIX 2 (the multi-meter bridge, name-match can't solve Starlake=3 meters):
  `GET /v1/array-owners/utility-accounts` (accounts + link state + bill_count)
  and `POST /v1/array-owners/utility-accounts/link` {account_id, array_id|null}.
  Tenant-scoped. Once linked, bills flow via the bill→daily transform.
- ROOT TRUTH: even with all code wired, the AO arrays stay dark until a GMP login
  actually happens through the extension under the AO tenant (0 sessions, 0
  accounts, 0 nepool ids on prod). Code can't fabricate the trigger — name the
  gap and don't fake it.

## 4. Onboarding gate — "you're not done until GMP is connected"
`GET /v1/array-owners/onboarding-status` → {gmp_connected, linked_arrays,
unlinked_accounts, complete, next_step ∈ connect_gmp|link_accounts|done}.
Complete = GMP connected AND ≥1 array linked (no settlement bills = nothing to
audit/reconcile/bill). Drives an unmissable banner below the AO tab bar
(`#gmpGate` in index.html, `updateGmpGate(session)` in app.js): amber "FINISH
SETUP / Connect GMP" → green "ALMOST DONE / Link your GMP accounts" → hidden when
complete. Signed-out visitors never see it.

## 5. UX RULE (Ford, firm): a CTA must launch the REAL in-app flow, never a detour
Ford reacted hard ("the fuck is this … it should just open the GMP login in
another tab and grab the data") to a "Connect GMP" button that linked back to
`/onboarding`. The fix reused the EXISTING proven connect plumbing, not a
reinvention:
- `window.__aoConnectGmp()` (exposed from sandbox.js) opens the Add-array modal
  and, when `EXT_PRESENT`, immediately calls `openPortalLogin("gmp")` →
  `extSend("SO_OPEN_PORTAL", {url:greenmountainpower.com, provider:"gmp"})` → the
  extension opens GMP in a NEW TAB and captures → `SO_CAPTURE_LANDED` →
  `handleCaptureLanded` POSTs `/v1/array-owners/utility-meter-capture`.
- When the extension isn't installed, the modal shows the "add the 1-click
  helper" step (correct next action), NOT a redirect.
- LESSON: when adding any "connect/do X" button, find the existing flow that
  already does X (here the "+ Add array → Log in with <vendor>" picker) and fire
  IT; don't bounce the user to a setup page. Verify with Playwright that the URL
  stays put and the expected postMessage (`SO_OPEN_PORTAL`) fires.

## 6. SolarEdge timestamps are site-LOCAL, not UTC (fleet-wide outage-clock bug)
SolarEdge equipment/overview timestamps (`lastUpdateTime`, telemetry last point)
are in the site's local tz with no marker; code that stamped them UTC made the
"source offline / last reported Nh ago" clock run ~4h fast for VT
(America/New_York). FIX: read the site's `location.timeZone` from
`/site/{id}/details` (cache it) and convert naive→UTC in the adapter
(`_localize_to_utc_iso`); fall back to leaving naive when tz unknown. This was
fleet-wide (every SE site), not one array. Also: "GMP says out 24h, we say 19h"
is partly this tz bug AND partly that GMP times the utility METER while we time
inverter TELEMETRY — two different clocks; label which feed the number is.

## 7. Invoice-vs-GMP-bill reconciliation (trust check) — `reconcile_bills.py`
`GET /v1/array-operator/billing/reconcile-bills`: per offtaker per array compares
our invoice produced-kWh vs the GMP bill's kwh_generated for the matching period
→ match | mismatch(Δ kWh + %) | no_bill (honest "awaiting GMP data", never
fabricated) | no_invoice_data. Read-only.

## 8. Multi-array offtaker billing (one combined invoice)
`BillingReportSubscription.array_allocations` JSON [{array_id, allocation_pct}].
When set, `delivery.build_manual_match` SUMS each array's (period kWh × pct) into
one invoice with a per-array breakdown; the invoice PDF renders a "Your share by
array" table (invoice.py — note `invoice_for_period` must pass `array_breakdown`
through from match.project_totals/computed_invoice or the table silently won't
render). Legacy single array_id/allocation_pct path untouched. Step-3 wizard UI =
checkboxes + per-array % (each % input enables on check); finish sends
`array_allocations` JSON. Migration adds the JSON column (idempotent sqlite+PG).
