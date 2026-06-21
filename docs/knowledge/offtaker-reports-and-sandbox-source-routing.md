# Offtaker reports = UTILITY data only · Sandbox source routing · banner auto-clear

Three hard-won rules for Array Operator (the plain-JS `/root/array-operator/public`
site + shared `solar-operator` backend). Ford "needs to get this right" — these are
his explicit mental model, not nice-to-haves.

──────────────────────────────────────────────────────────────────────────────
## 1. OFFTAKER INVOICES USE UTILITY-BILL DATA EXCLUSIVELY (no vendor, no hourly)

Ford's words: "The offtakers exclusively pull from the utility bills, the PAPER
COPIES, not the hourly data. When adding an offtaker you select the GMP utility
bill that connects with them, not just the array." It is "like a separate function
for our system."

WHAT THIS MEANS PRECISELY (each distinction matters):
- Source of truth = the `Bill` row (`Bill.kwh_generated` per billing period for
  the bound GMP `UtilityAccount`). The "paper copy."
- NOT the array. NOT `DailyGeneration` (vendor/inverter telemetry). NOT the GMP
  hourly/interval read (`gmp_daily_read` / `GmpDailyGeneration`) — Ford
  specifically excludes "the hourly data" too. NOT a CSV. NO fallback to any of
  these, EVER.
- If no utility bill covers the period yet → SKIP (wait), surfaced as "waiting on
  the utility bill." Never fabricate, never substitute another source.

THE BUG THIS REPLACED: `_array_period_kwh_sourced()` tried GMP daily-read first but
FELL BACK to `DailyGeneration`/`Bill`. So an offtaker invoice could silently be
built on vendor data. The whole point of this change is killing that fallback for
offtakers.

IMPLEMENTATION (landed Jun'26, all in `solar-operator`):
- Model: `BillingReportSubscription.utility_account_id` (nullable FK →
  `utility_accounts`). NULL = legacy array-based sub (untouched, back-compat).
  Migration in `api/migrate.py` (idempotent `column_exists` guard).
- `api/billing/delivery.py`:
  - `_utility_bill_period_kwh(db, utility_account_id)` → most-recent `Bill` with
    `kwh_generated` + `period_end`, for THAT account only. Returns
    (kwh,start,end,label) or all-None.
  - `build_manual_match`: a TOP-PRIORITY branch `if sub.utility_account_id is not
    None:` — reads only the utility bill, sets `computed_invoice["kwh_source"] =
    "utility_bill"` and `["has_utility_bill"] = bool(kwh is not None)`. array_kwh
    stays None when no bill (signals skip).
  - `deliver_subscription`: after the match, guard — if `utility_account_id` set
    AND `has_utility_bill is False` → return `{"ok":False,"skipped":True,...}`
    (don't email a $0 invoice).
- `api/billing/routes.py`:
  - NEW `GET /v1/array-operator/billing/utility-accounts` → this tenant's GMP
    accounts with bill summary (bill_count, has_bill, latest_period_label,
    latest_kwh_generated) for the picker.
  - `create_subscription` gains a `utility_account_id` Form param, threaded into
    `_create_manual_subscription` which has a NEW highest-priority branch:
    validates the account is this tenant's + provider=="gmp", stores
    `utility_account_id` (and `array_id=acct.array_id` for list views only —
    delivery ignores it because utility_account_id is set).
  - `_sub_dict` exposes `utility_account_id`.
- Frontend `/root/array-operator/public/reports.js`: the "New offtaker" manual
  form's "Which array?" → "Which GMP utility bill?" (`#rbmUtility`), populated by
  `fetchUtilityAccounts()` → `/utility-accounts`; `saveManual` sends
  `utility_account_id` instead of `array_id`.

TEST: `tests/test_offtaker_utility_bill.py` — seeds a 1800 kWh utility Bill AND a
conflicting 9999 kWh vendor DailyGeneration on the same array; asserts invoice
uses 1800 (the bill), kwh_source=="utility_bill", and skip when no bill. This
"conflicting vendor data must be ignored" pattern is the right way to PROVE no
vendor leakage — copy it for any "source X only" change.

BACK-COMPAT: legacy array/workbook/multi-array subs (no utility_account_id) keep
their existing path verbatim. Existing offtakers are NOT auto-migrated — offer a
dry-run migration if Ford wants Paul's offtakers moved onto their GMP bills.

### 1b. EMPTY "Which GMP utility bill?" dropdown = NO GMP CONNECTED on the AO tenant
Ford reported "it's not pulling the GMP bills for the offtakers so I can't link
the offtaker to the utility bill," with a screenshot showing the picker stuck on
"No GMP utility bills yet." DIAGNOSE WITH PROD DATA FIRST — do not assume the
endpoint is broken. It almost certainly is NOT:
- `/utility-accounts` correctly returns `[]` because the **Array Operator tenant
  has ZERO GMP UtilityAccounts/Bills.** AO tenants connect inverter VENDORS
  (SolarEdge/SMA/Chint) — they have NO utility accounts at all by default. GMP
  bills exist in huge volume but ONLY on the **NEPOOL** tenants (one Ford NEPOOL
  tenant: 47 GMP accts / 11,475 bills). Same backend, different product tenant.
- Read-only probe (railway ssh, allowed) per tenant:
  `SELECT provider, COUNT(*) FROM utility_accounts WHERE tenant_id=:t AND
  deleted_at IS NULL GROUP BY provider;` and join `bills b ON b.account_id=u.id
  WHERE u.provider='gmp' AND b.kwh_generated IS NOT NULL`. On all AO tenants this
  returns NONE → the dropdown is honestly empty, not buggy.
- ARCHITECTURE: GMP bills are persisted per-tenant by the shared `/v1/sync`
  ingest (`api/app.py` ~L1156 `db.add(Bill(...))`) when the EXTENSION captures
  GMP. So once GMP is connected on the AO tenant, `UtilityAccount` + `Bill` rows
  appear under it and the picker populates. The fix is a CONNECT path in Reports,
  not a backend pull change.

THE FIX (frontend-only, `reports.js` + `command-center.css`): a dedicated
"🔗 Link GMP utility bills" button in the Reports-tab list header that launches
the EXISTING real GMP connect flow `window.__aoConnectGmp()` (defined in
sandbox.js — opens the Add-array modal + `openPortalLogin("gmp")`; the extension
captures + lands the bills via /v1/sync). Reuse this flow; do NOT build a new
connect. Plus:
- a GMP-bills STATUS line under the offtakers header (`#rbGmpBillsStatus`,
  `refreshGmpBillsStatus()`) with three honest states: none connected (amber +
  inline link) / accounts-but-no-bills-yet ("open GMP once more") / ✓ connected.
- the add-offtaker form's empty dropdown also drops an inline Link-GMP button.
- AUTO-REFRESH the picker + status when a capture lands: WRAP the existing
  `window.__aoRefreshGmpGate` (chain the previous fn, then bust `UTIL_ACCTS`
  cache + re-run status). sandbox.js already fires that hook after a GMP capture.
LESSON: "X isn't pulling for the AO tenant" where X is GMP/utility data is almost
always "GMP was never connected on the AO tenant" (AO = inverter-vendor product),
NOT a broken endpoint. Verify the data state per exact product tenant before
touching code; the answer is usually a connect button, not a pull fix.

### 1c. "Connected but STILL empty dropdown" — two more bugs found the hard way (Jun'26)
Ford connected GMP (link worked, toast OK) but the "Which GMP utility bill?"
dropdown stayed empty across THREE frustrated turns. Two distinct causes, found
ONLY by checking prod data + the live endpoint with a real session — not by
re-reading code:

(A) BACKEND GAP: `_persist_meter_accounts` (the utility-meter-capture path) wrote
ONLY `DailyGeneration` — it NEVER created a `UtilityAccount` or `Bill`. But the
picker lists `UtilityAccount`s and offtakers bill from `Bill.kwh_generated`. So a
"connected" GMP account had nothing for the picker to list and no bill to invoice.
FIX (shipped, supersedes the older "only /v1/sync writes Bill" note in
inverter-array-grouping-persistence.md): `_persist_meter_accounts` now ALSO
upserts a `UtilityAccount` (idempotent on tenant+provider+account_number, linked
to the array) AND, when the GMP `parse_usage_summary` carries a billing period +
generation, upserts a `Bill` (kwh_generated, period dates; idempotent per
period_end, climbs-only). So the meter-capture path is now self-sufficient for the
offtaker picker — you no longer need /v1/sync to have run. Tests:
`test_gmp_capture_creates_linkable_utility_account_and_bill` +
`test_gmp_capture_bill_is_idempotent_no_dupe_bill` in
tests/test_utility_meter_capture_match.py. (If a capture sends daily[] but no
summary, you get a linkable ACCOUNT but has_bill=false until a summary lands.)

(B) THE ACTUAL DROPDOWN BUG — stale truthy-`[]` cache (classic JS gotcha):
`fetchUtilityAccounts()` in reports.js cached its result in `let UTIL_ACCTS=null;
if (UTIL_ACCTS) return UTIL_ACCTS;`. An empty array `[]` is TRUTHY in JS, so the
FIRST call (made before GMP was connected) cached `[]`, and every reopen after
returned that stale empty list FOREVER — never re-fetching even after the 16 GMP
accounts existed. Symptom is exactly "link works, data is in the DB and the
endpoint returns it, but the dropdown is empty." FIX: removed the cache entirely
(the list is tiny, fetch fresh each open). Guard rule: NEVER gate a cache on a
bare collection truthiness — `if (cache)` is wrong for arrays/objects; either
don't cache, or use a separate `loaded` flag (`if (cache !== null)`).

VERIFY-LIVE DISCIPLINE (the lesson Ford's frustration taught): when a fix "still
doesn't work" and the user is hot, STOP shipping blind frontend guesses. Prove the
backend with the REAL request first: mint a session server-side
(`railway ssh ... python -c "from api.account import _sign_session;
print(_sign_session('<tenant_id>'))"`), then curl the EXACT endpoint the UI calls
THROUGH the live domain with that bearer
(`https://arrayoperator.com/v1/array-operator/billing/utility-accounts`). If it
returns the data (it did — all 16 accounts), the bug is 100% frontend → inspect
the fetch/cache/render, not the API. This one curl would have saved two of the
three turns. (Masking can mangle a token inside a bash heredoc/`eval`; write the
token to a tmp file or run the curl via execute_code reading the file, rather than
interpolating it inline.)

──────────────────────────────────────────────────────────────────────────────
## 2. SANDBOX SOURCE ROUTING: STRICT, MUTUALLY EXCLUSIVE (Ford corrected twice)

The Vendor/Utility data toggle in the sandbox (`/root/array-operator/public/
sandbox.js`) must ROUTE arrays, not just swap each array's graph:
- vendor-sourced array → shows ONLY in the Vendor section.
- utility-sourced array → shows ONLY in the Utility section.

Ford's correction (verbatim, after my first attempt let dual-feed arrays show in
both + had a "show all" fallback): "When we get the array data from a vendor IT
ONLY SHOWS in the vendor section. When we get the array data from a utility, IT
ONLY SHOWS in the utility section." → exactly ONE bucket per array, NO dual-show,
NO "show everything" fallback.

CORRECT IMPLEMENTATION: `arrayStream(col)` returns exactly one of "vendor" |
"utility" (vendor wins if it has inverters/vendor/vendor daily stream; else
utility; default utility for meter-only arrays). `filterColsByStream(cols)` =
`cols.filter(c => arrayStream(c) === getStream())` — NO `kept.length ? kept :
cols` escape hatch. Applied in BOTH render paths (canvas `render()` + `renderGrid`).
Empty-section state: distinguish "no arrays at all" from "this section empty but
the other has N" and keep the toggle visible so they can switch — never the
misleading "Nothing connected yet."

LESSON: when Ford says "X data goes in X section," he means strict partition. Don't
hedge with "appears in both because data is integrated" — that's me overthinking;
he wants the clean either/or. classification keys off the same fleet-tree fields
as any source debug tag (daily_split.has_vendor/has_utility, vendor/vendors,
inverters) so debug view and routing always agree.

──────────────────────────────────────────────────────────────────────────────
## 3. SOURCE-OFFLINE BANNER MUST CLEAR WHEN THE SOURCE RECOVERS

Symptom Ford reported: "the source offline card update for the arrays needs to go
away when the source comes back online (the signage)."

ROOT CAUSE was NOT the banner logic (backend `_source_status` correctly flips
stale→ok at <6h fresh; frontend `sourceStatusHTML` already hides when state!=
"stale"). The real cause: the sandbox NEVER re-pulled the fleet tree once open.
`FleetStore.load()` ran ONE fetch at page open; after that the tree only refreshed
on user actions. So a recovered source kept showing the stale snapshot until a
hard reload.

FIX (in `/root/array-operator/public/fleet-store.js`): `startAutoRefresh()` —
`setInterval` every 5 min (well under the 6h stale window) calling `refetch()`,
PLUS a `visibilitychange` refetch when the tab returns to foreground. Started
after the first successful live ingest in `load()`. Guards so it never disrupts
the user: skip when `!isLive()`, when `document.hidden`, and when `_userBusy()`
(checks `.dragging-active, .inv-dragging-active, .sb-editing,
[contenteditable='true']:focus`). The sandbox already re-renders on store
`notify()`, so refetch → ingest → notify → banner clears on its own.

GENERAL LESSON: "this status/badge won't go away" on the AO sandbox is usually a
STALE-SNAPSHOT problem (no periodic refetch), not a render-condition bug. Check
whether the surface ever re-pulls before touching the badge's show/hide logic.

⚠️ SIDE EFFECT THIS FIX CAUSED (must keep both fixes together): this auto-refresh
created a write/refetch RACE — a background `refetch()` could clobber an optimistic
inverter drag, making inverters "jump back" to the wrong array. The store needed an
in-flight write guard (`_pendingWrites` wrapping apiPost/apiDelete; `refetch()`
bails + re-queues while writes are pending). See
inverter-array-grouping-persistence.md (CAUSE 1). RULE: when you add a periodic/
auto refetch to a store that has optimistic writes, add the in-flight-write guard
in the SAME change.

──────────────────────────────────────────────────────────────────────────────
## MISC this session
- "remove the audit tab": AO tab system in sandbox.js = `TABS` map + `tabFromHash`
  + `applyView` load-branch, plus the nav `<a id="tabX">` and `<section
  id="panelX">` in index.html. Remove all four; leave orphan js/css inert rather
  than touch script-load order. Old `#audit` bookmark falls through to Arrays.
- Per-array debug tag pattern: a compact client-side chip built purely from
  fleet-tree fields (vendor(s), V✓/V✗ + U✓/U✗ stream presence, live, source
  state, N inv), color-coded red=dead/amber=stale. Ford asked for it then asked
  to remove it once routing existed — keep the recipe here in case he wants it
  back; gate behind a flag if it returns.
