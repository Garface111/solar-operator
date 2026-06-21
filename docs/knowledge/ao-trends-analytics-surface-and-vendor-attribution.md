# AO Trends — Production Analytics surface + multi-vendor attribution

How to add a competitor-grade analytics/visualization surface to the Array
Operator **Trends** tab, grounded in real data (no fabrication). Built Jun'26 as
the keystone "Production Analytics" lead block. Class: adding a new chart/analytics
surface to Trends, or any feature that attributes fleet kWh by data source.

## Trends tab architecture (read FIRST, before touching anything)
- `public/trends-core.js`  — KEYSTONE. shared helpers + view REGISTRY + responsive
  hi-DPI canvas (`window.AOTrends`). Tokens: COLORS, yearColor, fmt0, kCompact,
  esc, hexA, smoothPath, createCanvas, prep, registerView/listViews/getView.
- `public/trends.js`       — orchestrator: fetch `/v1/array-owners/fleet-trends`,
  stat band, freshness line + CSV export, per-array filter dropdown, by-array
  table, and it MOUNTS each registered view into its own host (stacked column).
- `public/trends-view-*.js`— the 6 stacked views (bars, monthly + 4 decorative
  multi-year art: liquid/spiral/ridgeline/heatfield). Self-register via
  `C.registerView(key, def)`. Contract: `TRENDS-VIEWS-CONTRACT.md`.
- `public/trends.css`      — scoped styles. APPEND-only, prefix your rules.
- `public/index.html`      — loads the scripts in order; nav tab `#tabTrends`,
  panel `#panelTrends`, root `#trendsRoot`. sandbox.js `__aoLoadTrends` mounts it.

## The keystone-mount pattern (how the Analytics surface plugs in)
A big NEW surface that isn't just-another-stacked-view should be a SELF-CONTAINED
module, NOT a registered view, and mounted as the LEAD block by trends.js:
1. New file `public/trends-analytics.js` exposing `window.AOAnalytics.mount(host, payload, core) -> stopFn`.
2. `index.html`: add `<script src="trends-analytics.js">` right AFTER trends-core.js
   (it needs the core) and BEFORE trends.js.
3. `trends.js render(d)`: inject `<div id="anHost">` into the innerHTML template
   ABOVE `${blocks}`, then after wiring, mount it and push the stop fn:
   `const stop = window.AOAnalytics.mount(anHost, d, c); if (stop) _activeStops.push(stop);`
   (`_activeStops` is the existing cleanup array — teardown() calls every stop fn.)
4. CSS: append `.an-*` rules to trends.css. Reuse brand vars (--card, --card2,
   --line, --ink, --muted, --faint, --good, --good2, --gold, --bad). Set a
   per-surface `--an-accent` and switch it per granularity.
This keeps the existing 6 views byte-identical (Ford chose "Analytics leads, keep
all existing views"). Non-destructive = lowest risk.

## What competitor portals show (the common denominator to match)
GMP/SmartHub, SolarEdge, Fronius Solar.web, SMA Sunny Portal, CHINT all converge on:
- **Granularity toggle** Day / Month / Year / Lifetime + period nav (‹ prev/next ›)
- **Prior-period comparison** overlay + delta%
- **Environmental impact** (CO₂ / trees / cars / homes)
- **Specific yield** kWh/kWp (SolarEdge/SMA) + system size kWp
- **Records** (best day/month/year)
AO's UNIQUE edge ("and more"): **multi-vendor source attribution** — one fleet, every
vendor side-by-side, attributed to the feed each kWh came from. No single-ecosystem
portal can do this. Lead with it.

## Backend (`api/array_owners.py` /v1/array-owners/fleet-trends) — what's real
The endpoint merges DailyGeneration (CSV/meter/inverter) + GMP daily sponge
(`reports.gmp_daily_read.get_daily_series`), CSV-wins-on-overlap. Fields ADDED for
Analytics (all derived from real telemetry; null when unknown — never guessed):
- `source_breakdown`: per canonical family `{key,label,lifetime_kwh,share_pct,monthly_by_year}`.
  Map raw `DailyGeneration.source` via `_source_family()` (`_SOURCE_FAMILY` dict near
  `_MONTH_LABELS`). The GMP sponge has NO DailyGeneration row → tag its days `"gmp"`.
  Families: gmp (gmp_api/gmp_portal_scrape/utility_meter/smarthub), solaredge, fronius,
  sma, chint, inverter (extension_pull[_corrected]), csv, manual, bill, other.
- `capacity_kw` + `capacity_known_arrays`: sum `Inverter.nameplate_kw` over SCOPED
  arrays (deleted_at IS NULL, nameplate NOT NULL). Null if none → frontend prompts
  "add nameplate on Arrays tab", does NOT fabricate.
- `specific_yield_ttm_kwh_per_kwp` = ttm_kwh / capacity_kw (null if no capacity).
- `environmental`: EPA GHG 2024 factors (CO2_LB_PER_KWH=1.5634, TREE=48 lb/yr,
  CAR=11015 lb/yr, HOME=10500 kWh/yr) on LIFETIME kWh, with a cited `basis` string
  shown in the UI. Provenance-backed, not invented (Ford's hard rule on numbers).
- `daily_series`: extended ≤365-day contiguous window (kept `daily_recent`=30d for
  the existing bars view). Frontend prefers daily_series, falls back to daily_recent.
- `blended_rate_usd_per_kwh`: surfaced for the rate KPI.
No new DB columns → **no migration needed** (only reads existing source/nameplate).

## Frontend technique notes (trends-analytics.js)
- Granularity: Day only offered when `daily_series` has data. Persist choice in
  `localStorage["ao_analytics_gran"]`. Accent per gran: day=sky, month=green,
  year=gold, lifetime=light-green.
- Grouped/comparison bar chart on a small hi-DPI canvas (own `makeCanvas`, redraw
  on demand + on resize via ResizeObserver). Hover tooltip = single abs-positioned
  div in a `position:relative` host; remove in stop fn.
- Vendor attribution = horizontal share bar (segments width=share_pct) + legend,
  stable color per family.

## PITFALL (caught in QA — honest comparison)
Comparing a PARTIAL current period (e.g. Jan–Aug this year) against a FULL prior
year UNDERSTATES the current period (showed a false "-23%"). FIX: align the
comparison to only the months/days the current series actually has data — find
`lastWithData = hasData.lastIndexOf(true)`, sum cmp only for `i <= lastWithData`,
and label it "vs 2025 (same N mo)" / "(same N days)" for day-gran. This mirrors the
stat band's existing `yoyMonths` same-N-months logic in trends.js.

## QA recipe (vanilla JS, no build step)
Standalone harness `public/_analytics-qa.html`: inline `:root` brand vars + load
trends-core.js + trends-analytics.js + a synthetic 3-year / 5-vendor / daily payload,
call `AOAnalytics.mount(...)`, set `window.__ready=true`. Serve via
`terminal(background=true)` python http.server (NOT nohup/&). Playwright script:
screenshot at 1100px AND 390px, click each granularity, collect console errors
(must be none), then `vision_analyze` each PNG for clipping/overflow. DELETE the
harness + qa.js after (don't ship test files). Mobile: `.an-grid` collapses to 1col,
`.an-gran` scrolls — verify "(same N mos)" label and no right-edge overflow.

## Deploy (both ends)
- Backend: shared tree → `git add` ONLY your files (verify `git diff --stat` shows
  just yours), commit, `git push origin HEAD:main` (Railway auto-deploys ~70s).
  Run Mc/SO tests with explicit safe DB. Verify live endpoint returns 401/403 (not
  500) at `https://web-production-49c83.up.railway.app/v1/array-owners/fleet-trends`.
- Frontend (AO/Netlify): Netlify CLI unreliable → use the REST file-digest deploy
  (skill scripts/netlify_api_deploy.py, site_id 966cb1f5-944e-41fd-855b-10053edc5d18,
  token ~/.hermes/secrets/netlify_token). Verify the new asset is live:
  `curl arrayoperator.com/trends-analytics.js` → 200 + grep AOAnalytics, and
  index.html loads the script tag.

## Test seeding gotcha
tests/test_gmp_trends_integration.py `_cleanup()` deletes per-tenant rows in FK
order — when seeding `Inverter` rows (for capacity/specific-yield tests) you MUST
add `Inverter` to the cleanup Model tuple BEFORE Array, or teardown 500s on a
FOREIGN KEY constraint.
