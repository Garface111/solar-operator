# GMP (Green Mountain Power) utility-meter production capture — contract

Grounded live Jun 2026 against api.greenmountainpower.com (Bruce's real account
HAR). SHIPPED v1.9.23. This captures SOLAR GENERATION from the UTILITY METER (a
production source for owners with no inverter portal), distinct from the existing
GMP BILL capture (NEPOOL reports) and from inverter VENDORS.

## Key concept: utility ≠ inverter vendor
- Utilities (GMP, VEC) measure at the METER: net-metered generation + consumption.
  Whole-array, NOT per-inverter. Tracked as ADAPTERS (api/adapters/), not VENDORS.
- The meter is the ONLY production signal for an owner who has no inverter
  monitoring → Ford made utility-meter data a REQUIREMENT.

## Auth (the unlock)
GMP API calls authenticate with a Bearer JWT the SPA stores in localStorage key
`gmp-vue` → `.user.apitoken` (the existing extension/content.js already reads it
for bills). The browser HAR STRIPS the Authorization header, so you won't see it
in a HAR — read it from localStorage. Headers on every call: `Authorization:
Bearer <jwt>`, `GMP-Source: web`, `Accept: application/json`.

## Endpoints (api.greenmountainpower.com/api/v2)
- GET /users/current → `customData.energyAccounts[]` = [{accountNumber, nickname,
  isPrimary}] — enumerate the owner's premises. (Also: email, fullName.)
- GET /usage/{acct}/summary → THE generation signal: `isNetMetered`,
  `totalGrossGenerated`, `totalGenerationSentToGrid`, `totalGenerationUsedByHome`,
  `totalConsumption`, `billingPeriodStartDate/EndDate` (+ `lastBillingPeriod*`).
  A no-solar account returns isNetMetered=false, totalGrossGenerated=0 — VALID,
  just no panels (record honestly, has_generation=false).
- GET /usage/{acct}/daily?startDate&endDate&temp=f → intervals[].values[] with
  `date` + `consumed`/`consumedTotal` + **`returnedGeneration`** (the solar daily
  production field — CONFIRMED Jun 2026, see "Daily-grain CONFIRMED" below).
  /monthly = same shape, monthly bins. Both accept arbitrary year ranges (see
  "History depth" below).

## The build (file map)
- `api/adapters/gmp.py`: `parse_usage_summary(summary)` → {account_number,
  is_net_metered, period_start, period_end, kwh_generated (totalGrossGenerated,
  fall back to totalGenerationSentToGrid if 0), kwh_sent_to_grid, kwh_consumed}.
- `api/array_owners.py`: POST `/v1/array-owners/utility-meter-capture`,
  `_UTILITY_CAPTURE_VENDORS={"gmp"}` (SEPARATE from inverter `_CAPTURE_VENDORS`).
  Body {provider:"gmp", accounts:[{account_number, nickname, summary, daily?}]}.
  Matches/creates an Array per account; writes DailyGeneration source="utility_meter"
  (idempotent max-kwh upsert, same pattern as inverter_capture); no-solar accounts
  recorded with has_generation=false. Returns accounts[] with has_generation flags.
- `extension/gmp_meter_content.js`: intent-gated (so_capture_intent vendor:"gmp"),
  reads the JWT, asks background to fetch, emits GMP_METER_CAPTURED.
- `extension/background.js`: GMP_FETCH_USAGE (cross-origin proxy: page is
  greenmountainpower.com, API is api.greenmountainpower.com → fetch from the SW
  like the SMA_API_GET proxy, NOT the content script which CORS-blocks),
  GMP_METER_CAPTURED→SO_CAPTURE_LANDED, arms gmp intent on SO_OPEN_PORTAL.
- Frontend (/root/array-operator/public/sandbox.js): "Log in with GMP" in
  LOGIN_VENDORS + PORTAL_URL.gmp + BRAND.gmp; handleCaptureLanded routes provider
  "gmp" with `accounts[]` to utility-meter-capture; honest toast counts
  with-solar vs no-solar accounts. (NOTE: layout-view.js no longer exists; only
  sandbox.js carries the BRAND map now.)

## Proven E2E (TestClient): solar account → 642.5 kWh persisted source=utility_meter;
no-solar account → has_generation=false, 0 rows. 58 backend tests green.

## Daily-grain CONFIRMED (Jun 2026) + the field name
RESOLVED: the /usage/{acct}/daily generation field is **`returnedGeneration`**
(intervals[].values[] each {date, consumed, returnedGeneration}). Grounded against
Bruce's 1a_Chester solar account. GMP_FETCH_USAGE in background.js now pulls daily
and maps returnedGeneration>0 → DailyGeneration rows. SOLAR-ONLY filter before the
daily call (isNetMetered OR grossGen/sentGrid/usedHome > 0) so we don't fire a /daily
per non-solar account (Bruce has ~48 GMP accounts, most non-solar homes/pumps).

## ⚠️ GRANULARITY COLLAPSE ON WIDE RANGES — read this BEFORE trusting any backfill
CORRECTION (Jun 2026, later session): GMP's /daily endpoint DOWNSAMPLES to MONTHLY
when you ask for a wide date range. A request spanning 2025-06-01 → 2026-06-30
returned only **13 rows, each dated the 1st of a month** (2025-06-01, 2025-07-01,
… 2026-06-01) — NOT daily. So the "year-sized chunks are safe" claim below is
WRONG for getting DAILY data: a one-call-per-calendar-year backfill ingests ~12
monthly points/year and writes them into DailyGeneration as if they were daily —
worse than shallow, because fake-daily LOOKS real in Trends.
- The endpoint serving HTTP 200 for any year proves DATA EXISTS that far back; it
  does NOT prove DAILY granularity survives at that depth/width. Two different
  claims — never conflate them.
- The decisive metric is `rows:` PER FIXED-WIDTH (31-day) WINDOW: ~28–31 = true
  daily at that depth → walk in ~31-day chunks; ~1–2 = monthly-only that far back
  → daily backfill impossible from this endpoint, ship HONEST monthly history.
- To get real multi-year DAILY you must chunk in SMALL (~31-day) windows, not
  full years. If GMP only serves monthly beyond ~13mo, the honest ceiling is
  monthly — label it monthly, do not fake daily.

## HISTORY DEPTH — GMP serves data back to ~account-start (HAR-verified Jun 2026)
THE finding that unblocks multi-year Trends. GMP's /daily and /monthly accept
ARBITRARY startDate/endDate (any year, all return HTTP 200) and serve generation
back to when the array came online — NOT a fixed window. (DEPTH is real; GRAIN at
depth is the open question — see GRANULARITY COLLAPSE above.)
- Verified via HAR paging the GMP usage UI back year-by-year: 2019-06 daily
  response = 6218 bytes (DATA, denser than current month), 2018 daily/monthly =
  **144 bytes (EMPTY shell)** = that account started 2019. So ~7 years available.
- **The 144-byte response is the reliable "no data here" SENTINEL** — use response
  size ≤~200b / zero returnedGeneration rows to auto-detect each account's true
  start date instead of guessing a global floor.
- HAR-READING TRICK: Chrome "Save as HAR" often DROPS response bodies (textLen=0)
  but KEEPS `response.content.size`. You can read the history ceiling from SIZE
  alone (big = data, 144b = empty) without needing the body text. Don't conclude
  "HAR has no data" from empty `.text` — check `.content.size`.

## THE BOTTLENECK + multi-year backfill technique
Trends tab is shallow ONLY because the extension hardcodes a 35-day window:
`background.js` GMP_FETCH_USAGE ~L502 `start = end - 35*24*3600*1000`. Everything
downstream already handles arbitrary history (utility-meter-capture is idempotent
per (array,day) max-kWh; Trends reads all DailyGeneration). To go multi-year:
1. Walk BACKWARD year-by-year per solar account (one /daily call per calendar year;
   the 2019 full-month call returned cleanly so year-sized chunks are safe).
2. STOP walking back for an account when a year returns the 144b empty sentinel
   (its start) — no wasted calls into the pre-array void.
3. Politeness delay (~350ms) between calls; solar-only filter caps the fan-out.
4. ZERO backend/schema change — idempotent ingest means a 7yr pull just fills
   history, no dupes; re-running only fills gaps (resumable).
Caveat to set with Ford: a full 7yr × all-solar-accounts pull is many calls in one
capture (couple minutes of the extension working — expected, not a hang).

SHIPPED (manifest 1.9.41, commit 5f11895). As-built differs slightly from the plan
above — match these when re-reading background.js GMP_FETCH_USAGE:
- `parseYear(yr)` fires ONE /daily call per calendar year (`{yr}-01-01T00:00:00`..
  `{yr}-12-31T23:59:59`, current year capped at fmtDate(now())), collecting
  returnedGeneration>0 rows into `daily`.
- `MAX_YEARS=12` hard cap; 250ms pacing between years (not 350).
- STOP rule = `emptyStreak`: a PRIOR year (yr < nowY) returning zero gen rows
  breaks the loop (pre-online void). The CURRENT year returning zero is NOT a stop
  (legit early-Jan), so the empty-year check is guarded by `if (yr < nowY)`.
- Per-year try/catch: one bad year logs `[so] GMP daily year N failed` and continues
  rather than sinking the whole backfill.
- Self-diagnosing: on success logs `[so] GMP backfill <acct> → N daily rows,
  <oldest>..<newest>` — the first LIVE capture is the proof the loop works end-to-end
  (HAR proved the API serves the data; this is the code that consumes it).
- Backend confirmed needs ZERO change: utility_meter_capture (array_owners.py
  ~L2289–2355) loops acct.daily with no length cap, idempotent max-kWh upsert per
  (array,day). Verified this session by reading the endpoint, not assumed.
NOTE: changing the extension does NOT auto-deploy — `git push` deploys Railway
(backend) but the extension must be reloaded/repackaged separately to take effect.

## GROUNDING-PROBE pattern (when a HAR can't reach far enough)
If the GMP UI won't page back far enough to prove API depth, ship a READ-ONLY probe
build: in GMP_FETCH_USAGE, on the first solar account only, fire /daily for several
historical 31-day windows (1/13/25/37/61 months back) and `console.log("[AO HISTORY
PROBE]" ...)` rows/withReturnedGen/served-range/sample per window. Logs to the
SERVICE-WORKER console (chrome://extensions → "service worker" link), NOT the page
console — the recurring gotcha. Bump manifest version so the loaded build is
unmistakable. In THIS session the HAR (paged to 2018/2019) answered it, so the probe
wasn't needed — prefer a HAR with old-date calls first; probe only if UI is capped.

PROBE LIFECYCLE — DO NOT strip it until a LIVE capture has proven DAILY grain at
depth. CORRECTION (Jun 2026, later session): the earlier "HAR proved depth → strip
the probe" call was premature and WRONG. The probe deliberately requests FIXED 31-day
windows at increasing ages precisely to measure whether DAILY granularity survives
at depth — which is exactly the question a wide-range HAR canNOT answer (wide ranges
downsample to monthly; see GRANULARITY COLLAPSE). A HAR showing "data exists in 2019"
does NOT prove "daily grain in 2019." So the probe is the ESSENTIAL grounding
instrument here, not disposable scaffolding.
- This session a cron auto-commit stripped the probe and kept the full-year-range
  backfill — i.e. deleted the measuring instrument and kept the thing that needed
  measuring. The fix was to RESTORE the probe (reverse-apply that commit:
  `git show <sha> -- extension/background.js | git apply -R`), bump the manifest
  version, hand it to the user for one live capture.
- Strip the probe ONLY after the live `[AO HISTORY PROBE]` lines confirm `rows:`≈28–31
  per window (true daily) AND the backfill loop is rewritten to chunk accordingly.
  If they show `rows:`≈1–2, the lesson is "monthly-only" and the probe earned its keep.
- Logs to the SERVICE-WORKER console (chrome://extensions → "service worker" link),
  NOT the page console — recurring gotcha. Bump manifest version so the loaded build
  is unmistakable. Generalizes to any read-only grounding probe: a probe that tests a
  property a HAR cannot observe is load-bearing — verify on a live console before
  removing it.
