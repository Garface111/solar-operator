# Trends/Analytics surface + multi-year data propagation (Array Operator)

Covers two linked classes of work on the AO **Trends tab**:
1. Building/extending the competitor-grade **Production Analytics** surface.
2. Diagnosing "past years / new backfill data isn't showing in the graphs."

## The Trends tab architecture (as of Jun'26)
- Backend: `GET /v1/array-owners/fleet-trends` in `solar-operator/api/array_owners.py`.
  Aggregates **DailyGeneration** (per array,day) + the **GMP daily sponge**
  (merged at READ time via `api/reports/gmp_daily_read.get_daily_series`, CSV-wins-
  on-overlap) into `years`, `monthly_by_year`, `seasonal_yoy`, `ttm_kwh`,
  `lifetime_kwh`, `daily_recent`(30d), `daily_series`(≤365d), `source_breakdown`,
  `capacity_kw`/`specific_yield_ttm_kwh_per_kwp`, `environmental`, `by_array`.
- Frontend (`/root/array-operator/public/`): a KEYSTONE core + a stacked column
  of self-registering views. `trends-core.js` (registry+canvas helper, OWNED),
  `trends.js` (orchestrator: fetch, stat band, filter, mounts every view, OWNED),
  `trends-analytics.js` (the lead Production Analytics surface — `window.AOAnalytics.mount`),
  then `trends-view-{bars,monthly,liquid,spiral,ridgeline,heatfield}.js` (each
  registers via `AOTrends.registerView`). Contract: `array-operator/TRENDS-VIEWS-CONTRACT.md`.
- To add a NEW lead surface: write `trends-<x>.js`, add `<script>` in
  `index.html` BEFORE `trends.js`, mount it in `trends.js render()` into a host
  div and push its stop-fn to `_activeStops`. Keep existing views untouched
  unless told otherwise (Ford's Jun'26 call: "keep all 6, Analytics leads").

## What the competitor portals converge on (build target)
GMP/SmartHub, SolarEdge, Fronius Solar.web, SMA Sunny Portal, CHINT all share:
Day/Month/Year/Lifetime granularity toggle + period nav, prior-period comparison
overlay+delta%, environmental impact (CO2/trees/cars/homes), specific yield
kWh/kWp. **AO's unique edge** none of them can do: a **multi-vendor source-
attribution bar** — every kWh tagged to the feed it came from (GMP/SolarEdge/
Fronius/SMA/CHINT) across one fleet, because each portal only sees its own
ecosystem. Built all of these in `trends-analytics.js`.

## HONESTY rules baked into Analytics (Ford prizes these — never regress)
- **Partial-period comparison MUST align to same N months/days.** A partial
  current year (Jan–Aug) compared against a FULL prior year shows a fake ~50%
  collapse. Fix in BOTH the Month/Day delta AND the Year-YoY branch: sum only the
  months the current period actually has, label it "(same N mos)". This bug
  recurred in the Year branch after being fixed for Month/Day — check all three.
- **Specific yield / capacity:** compute ONLY from real `Inverter.nameplate_kw`;
  null → show an "add nameplate on Arrays tab" prompt, never guess kWp.
- **Environmental factors:** use cited EPA GHG 2024 constants, show the basis
  string in the UI. Don't invent.
- **Source attribution:** map `DailyGeneration.source` via a `_SOURCE_FAMILY`
  dict (gmp_api/gmp_portal_scrape/utility_meter/smarthub→gmp; solaredge/fronius/
  sma/chint each own; extension_pull*→inverter; csv/manual/bill_prorate own). GMP
  sponge days have no DailyGeneration row → tag "gmp" at merge time.

## DIAGNOSTIC: "past years / new data isn't showing in the graphs"
**It is almost always a DATA gap, not a rendering bug.** Work the chain in order:
1. **Dump the REAL payload** for a known-good multi-year tenant and confirm
   `years` + `monthly_by_year` are full (Bruce's Green Mountain `ten_6522da7ac2e1d01d`
   has 2013–2026). If the payload has the years, the backend is fine.
2. **Render the WHOLE view stack against that real payload** (see
   scripts/trends_stack_qa.js) and screenshot each block. If every graph shows the
   years here, the rendering code is fine — the problem is the *account being viewed*.
3. **Check per-tenant year spans through the live endpoint** (loop every active
   tenant, call `ao.array_owners_fleet_trends(authorization=..., array_id=None)`).
   NOTE: call the function with `array_id=None` EXPLICITLY — calling it bare passes
   the FastAPI `Query(default=None)` object literally and trips the 404 scope check.
4. **Map the login to its tenant.** Ford's primary `ford.genereaux@gmail.com` =
   the **"Array Operator — Live Demo"** tenant `ten_a554c8e7a08f8cfa`, which is
   thinly seeded (only the current year unless backfilled). He tests on THIS, so a
   "not showing past years" report usually means the demo's data is thin, not broken.

## ROOT CAUSE class: backfill window caps
The nightly inverter pull (`api/jobs/inverter_pull.pull_all_inverters`, 3:00 UTC)
uses `days_back=90`. So a freshly-connected SolarEdge/vendor array shows only
~current year in Trends until a DEEP backfill runs. There is NO scheduled deep-
history job wired (the shipped `solaredge_deep_backfill` sponges telemetry side-
tables, not DailyGeneration). The Bill→daily transformer (`api/jobs/bill_to_daily.py`,
05:30 UTC) DOES backfill years of `bill_prorate` rows from captured GMP bills —
that's why bill-heavy tenants (Green Mountain) show 2013→ but SolarEdge-only demo
tenants don't.

## FIX pattern: real multi-year SolarEdge backfill (not fabrication)
SolarEdge `/site/{id}/energy?timeUnit=DAY` serves ~1yr/request and the connected
sites genuinely hold years of history. Chunk year-by-year from a start year→today
and upsert into DailyGeneration (`source='solaredge'`), reading creds from each
array's `InverterConnection.config` (api_key+site_id), falling back to legacy
`Array.solaredge_*`. NEVER clobber a non-solaredge real source. Idempotent by
(array_id, day). Script: scripts/backfill_solaredge_history.py (committed to the
repo so `railway ssh "python -m scripts.backfill_solaredge_history --tenant <tid>
--since 2017"` works on the prod image). Probe a site's earliest data first
(loop a few years) before a full run. This pulled the demo from 2026-only to
2017→2026 (5,718 real days). DEMO-tenant seeding/backfill is legitimate; it is
NOT fabrication into a real customer account.

## SELF-HEALING deep-history backfill (BUILT + LIVE Jun'26)
The 90-day cap is now self-healing — past years populate automatically, no manual
backfill. Architecture:
- **Marker col:** `InverterConnection.history_backfilled_at` (migration in
  `api/migrate.py`; NULL = pending). Verify on prod via `inspect(engine).get_columns`.
- **Job:** `api/jobs/inverter_history.py` — VENDOR-AGNOSTIC (uses
  `inverters.fetch_daily(vendor,...)` for any `SUPPORTS_DAILY` vendor), chunks
  year-by-year from `HISTORY_START_YEAR=2010`→today, upserts DailyGeneration,
  NEVER clobbers `_PROTECT_SOURCES` (csv/manual/utility_meter/gmp_api/...).
  `backfill_connection_history(conn_id)` STAMPS only on a fully error-free pass
  (a vendor error in any year leaves it NULL → retried). `heal_missing_history(limit=50)`
  scans NULL connections, capped per-run for vendor rate limits.
- **Trigger 1 (immediate):** `array_owners._trigger_history_backfill(db, array_id)`
  fires `backfill_connection_history_async` (daemon thread) after every connect —
  wired into `_connect_inverter`, `solaredge_connect_account`, `locus_connect_account`.
- **Trigger 2 (safety net):** scheduler `_run_history_heal` at 04:15 UTC.
- **Admin:** `POST /admin/inverter-history/heal?limit=N` and
  `/admin/inverter-history/connection/{id}?since_year=YYYY` (503 in prod unless
  ADMIN_API_KEY set → run via `railway ssh "python -c ...heal_missing_history()"`).
- **GOTCHA (fixed):** a connection whose Array is soft-deleted MUST be stamped done
  (not returned as error) or the healer retries it forever — 9 orphans stayed
  pending until this fix. Same for no-daily vendors (chint): stamp immediately.
- **Deploy:** push → wait deploy → `railway ssh "python -m api.migrate"` → VERIFY
  column via get_columns (migrate log can run old code) → run heal once to make it
  live on existing connections. First live heal: 23 conns, 19,870 real days
  backfilled, converged to 0 pending after the orphan fix.
- Tests: `tests/test_inverter_history.py` (multiyear, protect-source, error→unstamped,
  orphan-stamped, no-daily-vendor, heal idempotent). Run with DATABASE_URL=sqlite.

## QA harness (proven this session)
- Standalone HTML harness loading core+analytics with a demo payload, OR
- Full-stack harness that stubs `localStorage.so_session` + intercepts the
  `/fleet-trends` fetch to return a real dumped payload, loads the real
  trends*.js, and screenshots each `.tr-chartblock`/`.an-wrap` block.
Run via Playwright (chromium) against a `python3 -m http.server` in public/.
Always vision-check at 1180px AND 390px. Zero console errors = pass.
See scripts/trends_stack_qa.js. Clean up `_*.html`/`_*.js`/`_gmcs.json` after.

## Deploy
- Backend: `git push origin HEAD:main` → Railway auto-deploy (~80s). No migration
  here (only reads existing `source`/`nameplate_kw`). Verify endpoint 401/403 not 500.
- Frontend: `python3 scripts/netlify_api_deploy.py` (REST file-digest deploy;
  CLI session is broken — see ao-deploy-and-frontend-debugging.md). Verify the new
  asset is 200 + grep for a marker string on arrayoperator.com.
- Shared tree: stage ONLY your files (other agents have untracked work in flight).
