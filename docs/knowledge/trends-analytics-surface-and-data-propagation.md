# Trends "Production Analytics" surface + data propagation (AO)

Covers the keystone Production Analytics surface on the Array Operator Trends tab
and how to VERIFY that new backfilled data actually reaches it. Body is >100k so
this ref is discovered by listing `references/`, not from a SKILL.md pointer.

## The Trends tab architecture (array-operator/public/)
- `trends.js` ORCHESTRATOR (keystone): fetch `/v1/array-owners/fleet-trends`,
  stat band, freshness line, CSV export, array-filter dropdown, then renders a
  STACKED COLUMN of views. It mounts `window.AOAnalytics` into `#anHost` as the
  LEAD block ABOVE the six legacy views, pushing the stop fn onto `_activeStops`.
- `trends-core.js` shared helpers + view REGISTRY (`AOTrends.registerView`).
- `trends-analytics.js` = the keystone analytics surface (`window.AOAnalytics.mount(host, payload, core) -> stopFn`). Self-contained vanilla JS, own hi-DPI canvas.
- `trends-view-{bars,monthly,liquid,spiral,ridgeline,heatfield}.js` = the legacy
  six views. Ford's standing call (Jun'26): KEEP them, Analytics just LEADS.
- `trends.css` — append `.an-*` rules for analytics (scoped); brand vars come from
  styles.css `:root` (--card/--card2/--line/--ink/--muted/--good/--gold/--sky/--vio/--bad).
- Loaded in index.html in order: trends-core → trends-analytics → views → trends.js.

## What Production Analytics ships (competitor parity + AO edge)
Researched against GMP/SolarEdge/Fronius/SMA/CHINT. The common denominator they
ALL converge on: a Day/Month/Year/Lifetime granularity toggle + period nav +
prior-period comparison + environmental impact + specific yield. Built all of it:
- Granularity toggle Day·Month·Year·Lifetime, ‹prev/next› period nav (month/day only).
- Prior-period comparison overlay (ghost bars) + delta%.
- **Multi-vendor data-attribution bar** = AO's UNIQUE edge. Each kWh tagged to the
  feed it came from (GMP/SolarEdge/Fronius/SMA/CHINT). Single-ecosystem portals
  CANNOT show a mixed fleet attributed by source. This is the "and more".
- Environmental impact (CO2/trees/cars/homes) via cited EPA 2024 factors (provenance string shown).
- Specific yield kWh/kWp + system size kWp; honest "add nameplate" prompt when null.
- Records (best day/month/year), blended rate (measured from billing data).

## Backend `/v1/array-owners/fleet-trends` (api/array_owners.py)
Added fields the surface needs (no new DB columns — reads existing data):
- `source_breakdown`: per canonical family `{key,label,lifetime_kwh,share_pct,monthly_by_year}`.
  Mapped via `_SOURCE_FAMILY`/`_source_family()`: gmp_api/gmp_portal_scrape/utility_meter/smarthub→gmp;
  solaredge/fronius/sma/chint→themselves; extension_pull(_corrected)→inverter; csv/manual/bill_prorate→csv/manual/bill.
  The GMP daily sponge (no DailyGeneration row) is attributed "gmp" at merge time.
- `capacity_kw` + `capacity_known_arrays` = summed live `Inverter.nameplate_kw` over scoped arrays; null when unknown.
- `specific_yield_ttm_kwh_per_kwp` = ttm_kwh / capacity_kw (null when no nameplate).
- `environmental` = EPA GHG equivalencies on lifetime kWh (factors: 1.5634 lb CO2/kWh, 48 lb/tree-yr, 11015 lb/car-yr, 10500 kWh/home-yr).
- `daily_series` (≤365d) for Day/Week nav alongside the legacy 30d `daily_recent`.
- `blended_rate_usd_per_kwh` (the rate behind EST. VALUE).
Tests live in `tests/test_gmp_trends_integration.py` (source breakdown + specific yield).
**Cleanup gotcha:** that test file's autouse `_cleanup()` must delete `Inverter`
rows too or teardown 500s on a FK constraint when a test seeds inverters.

## ★ PITFALL: partial-period comparison honesty (bit me TWICE)
A PARTIAL current period (e.g. Jan–Jun of this year) compared against a FULL prior
period reads as a fake ~50% collapse ("▼51.8% YoY"). Ford prizes not-fabricating —
this is a credibility bug, not cosmetic. The rule, in EVERY comparison branch:
**compare only the months/days the current period actually has data, against the
SAME months/days of the prior period, and LABEL it "(same N mos)".**
- Month/Day: align cmp by `lastWithData` index of the current series.
- **Year YoY has the SAME trap** and I missed it the first pass — the year branch
  in `renderDelta()` must sum the latest year's PRESENT months and the prior year's
  SAME months when the latest year is partial (`nMo < 12`), not full-year totals.
- The backend stat band already did "same N months" correctly — match it in the UI.
Day-granularity comparison label unit must be "day(s)" not "mo".

## ★ Verifying new backfill PROPAGATES to Trends (the real ask)
New data only shows in Trends if it lands in `DailyGeneration` (fleet-trends reads
that table + merges the GMP 15-min sponge at read time). Side-stores
(SolarEdgeTelemetry/EnergyDetail, ext-capture sponge, weather) do NOT show unless a
job writes daily rows. The Bill→daily transformer (`api/jobs/bill_to_daily.py`,
source=`bill_prorate`, nightly 05:30 UTC after GMP backfill) was THE missing link:
47k parsed GMP bills existed but never surfaced because the frontend only read daily
streams. bill_prorate is COARSEST — `(array,day)` unique + source check make real
meter readings always win; it only fills gaps. Idempotent.

VERIFY chain end-to-end, don't assume (Ford clamped down on prod endpoint curling —
use in-process function calls / railway-ssh python probes, NOT HTTP to prod):
1. DB landed? `railway ssh "python -c ..."` count DailyGeneration grouped by source
   per tenant; confirm bill_prorate days exist where bills exist. (scripts/probe_bill_propagation.py)
2. Endpoint surfaces it? Call `ao.array_owners_fleet_trends(authorization='Bearer '+mint_session_for_tenant(tid), array_id=None)`
   in-process. **Gotcha:** pass `array_id=None` explicitly — calling the route fn
   directly passes the FastAPI `Query(default=None)` object (truthy) → spurious 404.
   Check years span, monthly_by_year fullness, source_breakdown shares.
3. Frontend renders the REAL shape? Build a QA harness with a payload SHAPED like the
   real tenant (e.g. GMCS = 14 years 2013–2026, M-scale kWh, 99.5% bill-dominant) —
   M-scale numbers + 14-year spans are exactly what break layouts. Playwright
   screenshot desktop(1100)+mobile(390) across all granularities, vision-check, 0 console errors.

## Deploy
- Backend: stage ONLY your hunks (`git add <files>`, never `-A` — shared hot tree),
  push origin HEAD:main, Railway auto-deploys ~70s. No migration here (read-only fields).
  Verify route 401/403 (not 500) = live + importing.
- Frontend (AO Netlify): `python3 scripts/netlify_api_deploy.py` (site array-operator-ea
  966cb1f5-944e-41fd-855b-10053edc5d18). CLI is unreliable; the REST file-digest
  script is the reliable path. Verify the asset is 200 + grep a unique new token on
  arrayoperator.com.
