# Array Operator Reports/billing — evolution beyond the initial build

Builds on `reports-billing-and-multiagent-read-contracts.md` and
`reports-billing-build-and-multiagent-shipping.md`. This file captures the
feature arc AFTER the first ship: GMP-bill auto-attach, the discount pricing
model, the data-derived rate schedule, the first-run setup wizard, and the
label rename. Ford drove these one at a time; each is a clean, reusable pattern.

## Ford's defining principle here: DERIVE, never fabricate rate/billing numbers
When Ford asked to "default to a pre-propagated blended state rate" that changes
by month/utility/location/array-age, the WRONG move is to invent VT rate
numbers. The RIGHT move (what he praised — "use GMP, use everything you can"):
**measure the rate from the bills we already capture.** 27k+ GMP bills carry the
real per-kWh charge. So:
- `api/rate_schedule.py`: `blended_rate_from_bill(raw_json)` = sum of positive
  `segmentLineItems` where unitOfMeasure==KWH and unitCode=="NET" (the charge $)
  / the CONSUMED kWh. Guard band 0.05–0.50 $/kWh rejects parse noise. Net-metered
  bills swing wildly on `avg_rate_cents_kwh` (net of credits) — do NOT use that;
  use the gross NET-charge line items.
- Median over a (utility × effective-window × age-bucket) cell, min_samples gated
  (≥8) — cells with too few bills are SKIPPED and fall back, never guessed.
- Real measured result (Jun'26): GMP ≤11yr 2022→$0.177, 2024→$0.1878,
  2026→$0.2097 — the real biennial climb, straight from bills.

## RateSchedule table + resolver (the auto-applied rate)
- `models.RateSchedule(state, utility, location_class, age_bucket, effective_start,
  effective_end, blended_rate_per_kwh, sample_size, source_note, is_provisional)`.
  Biennial reset = ADD A ROW with the next effective window; resolver auto-picks
  by billing month. No code change to roll over.
- `resolve_net_rate(db, provider, region, first_connect_date, period_end)`:
  specificity-ranked candidate keys (utility,loc,age) → (utility,loc,*) → … →
  (*,*,*), newest effective_start wins, falls back to `rates.get_energy_rate(provider)`
  (documented VT default) — ALWAYS returns a provenance `note`. source ∈
  {schedule, schedule_provisional, vt_default}.
- Age rule = VT 10-yr net-metering adder boundary: `array_age_bucket()` →
  'le11' (≤11 yrs since first_connect_date) vs 'gt11'. AGE_THRESHOLD_YEARS=11.
  Unknown install date → 'le11' (conservative/common).
- `refresh_rate_schedule(db)` upserts all cells from bills; idempotent. Exposed
  as `POST /admin/rate-schedule/refresh` (ADMIN_API_KEY) AND runnable directly
  via `railway ssh` (avoids needing the key over HTTP). Seed it on prod after a
  bill pull. NOT yet wired into the scheduler — manual refresh for now.

## Discount pricing model (Ford's preferred mental model)
Ford reframed flat $/kWh → "solar credit rate minus a discount" (default 10%
off). invoice = kWh × net_rate × (1 − discount). The engine already supported it:
`compute_invoice(kwh, tariff=net_rate, adder=0, billing_rate=(1−discount))` →
amount AND `solar_savings` (= the discount $) fall out exactly.
- `delivery.resolve_discount_pricing(sub, period_end=, region=, first_connect_date=)`
  net-rate precedence: customer override → tenant global → **AUTO RateSchedule**
  → legacy flat rate (treated as net w/ 0 discount, dollars unchanged) → VT
  default. discount precedence: customer → global → DEFAULT_DISCOUNT(0.10).
  Returns net_source/discount_source/net_rate_note for UI provenance.
- Routes: `GET/PUT /billing/global-rate` carries net rate + discount (+ echoes
  effective_*); create/patch accept discount_pct + net_rate_per_kwh;
  `_validate_discount` rejects ≥1. `_sub_dict` calls resolve_discount_pricing to
  expose `resolved_net_rate/discount/effective/source` so the card shows the
  applied rate (no blank box).
- PITFALL: consolidating two different "VT defaults" (delivery's 0.18398 vs
  rates.py 0.21) shifts test expectations — the resolver's fallback is
  get_energy_rate(provider) = 0.21. Update tests to the unified default.

## First-run setup wizard (the guided Reports onboarding)
- `GET /billing/setup-state` = ONE call powering the wizard: arrays (with age +
  provider + region + auto-resolved rate + what's missing), has_customers flag,
  global rate/discount. UI shows wizard when `has_customers` is False.
- `PATCH /billing/arrays/{id}` sets install_year → first_connect_date (feeds the
  age buckets) + optional region. Validates year 1990..thisyear; tenant-scoped 404.
- Frontend `reports.js`: 4-step flow (arrays+age → rate → customers → review),
  in-memory `WIZ` state, reuses existing PATCH/PUT/POST endpoints, `FORCE_TAB`
  guard so finish/skip shows the normal tab. "⚙ Setup" link in subtab row
  reopens it. Gating chosen: first-visit/zero-customers (clean testable slate).

## Label rename discipline (Ford: "rename X to Y sitewide")
Rename ONLY user-facing display strings; NEVER internal field names / API keys /
ORM columns / variable names (`net_rate_per_kwh`, `resolved_net_source`, …) —
those are contract-stable and renaming them breaks the API + seeded data for zero
benefit. "Net rate" → "Solar credit rate" touched: reports.js UI labels +
previews + error msgs + manual form + per-customer row, AND the customer-facing
invoice line in BOTH `invoice.py` render_invoice_pdf (`Net rate —`) and
render_invoice_xlsx (`B16 Net Rate:`). Leave JS comments alone. Grep both repos
for the display string; verify live curl shows new term, 0 old.

## GMP bill-PDF auto-attach (durable capture, finished end-to-end)
- `Bill.pdf_bytes` (LargeBinary) + `pdf_content_type` — DURABLE in-row storage
  (pdf_path is Railway-ephemeral, wiped on redeploy).
- `worker._capture_current_bill_pdf` on the JSON-pull path fetches the current
  bill PDF via `gmp.fetch_bill_pdf(currentBillUrlBinary)` and persists bytes onto
  the newest bill row. Validates `%PDF` magic so an auth-redirect HTML page is
  never stored as a PDF. Best-effort — never fails the pull.
- VERIFIED LIVE: a real GMP pull (tenant ten_6522da7ac2e1d01d, 65 accounts) →
  59/65 ok, 61 bills with durable pdf_bytes. JSON API 401'd but the PDF
  redirector path accepted the token (that's the path auto-attach needs).
  6 failures were PRE-EXISTING data issues (MultipleResultsFound = duplicate bill
  rows same period_end; "No form fields in redirector HTML" = stale bill URL) —
  not the new code. Probe ONE account first (db.rollback()) before pulling 65.

## Local-QA gotchas that bit repeatedly this arc (fix patterns)
- STALE UVICORN on :8788: a previous background uvicorn often still holds the
  port, so a freshly-started one silently fails to bind and serves OLD code
  (routes 404, openapi missing your endpoints). ALWAYS check
  `ss -ltnp | grep 8788`, kill the old PID, confirm "free", THEN start. Verify
  new routes via `/openapi.json` paths, not assumption.
- OOM kill (exit 137): the local probe uvicorn gets OOM-killed mid-Playwright on
  the memory-tight box → list fetches 502 / "Session expired" in screenshots.
  Restart it; it's local-only, prod is fine.
- dev_proxy (:8089) returns **501 on PUT** — the local proxy doesn't implement
  PUT/PATCH passthrough fully; global-rate save 501s locally but works on prod
  Netlify. Verify PUT/PATCH against the backend :8788 DIRECTLY, don't trust the
  proxy for non-GET. dev_proxy also dies independently — restart with
  `BACKEND=http://127.0.0.1:8788 python3 dev_proxy.py 8089`.
- Session tokens expire mid-Playwright run → re-mint
  `mint_session_for_tenant(tid)` to /tmp/ao_token.txt before each shot batch.
- seed_probe.py is UNTRACKED and gets swept by the cron/another agent — recreate
  it when missing (tenant ten_paulbozuwa01, array "Bozuwa Field A", DailyGen for
  last full month). Bump SOLAR_DATA_DIR (ao_probe_dbN) for a clean schema each build.

## Netlify deploy auth expires mid-session (AO frontend SHIP blocker)
The AO frontend deploy is MANUAL (`netlify deploy --prod --dir=public` from
/root/array-operator). Mid-session the Netlify CLI session can expire →
`JSONHTTPError: Unauthorized` on deploy, and `netlify status` says "Your session
has expired… run `netlify logout` and `netlify login`". The stored token lives in
`~/.config/netlify/config.json` (users.<id>.auth.token) but when expired the API
rejects it. `netlify login` is INTERACTIVE browser OAuth — CANNOT complete it
headlessly. So: git push still succeeds (code lands on GitHub), but PROD STAYS
STALE until re-auth. Don't claim "deployed" — say it's committed+pushed but the
deploy is blocked, and give Ford the two unblock paths:
  1. He runs `netlify login` in his terminal, then you re-run the deploy; OR
  2. He creates a Personal Access Token (app.netlify.com → User settings →
     Applications → New access token) and you deploy with
     `NETLIFY_AUTH_TOKEN=<tok> netlify deploy --prod --dir=public`.
NOTE: the shell secret-masker mangles a pasted token inline ("...") — have him
store it to a file and read it, or export it once, rather than inlining.
This is a CREDENTIAL/SETUP state (Ford fixes it), not a code bug — never treat it
as "netlify is broken."
WORKING DEPLOY-WITH-TOKEN PATTERN (the masker also breaks `NETLIFY_AUTH_TOKEN=$(cat
file) netlify …` inline AND breaks it inside a terminal command — it rewrites the
`$(...)`/`$VAR` to `***`): WRITE a tiny `/tmp/deploy_ao.sh` via write_file with
the cat/export inside it, then `bash /tmp/deploy_ao.sh`. The masker garbles the
tool-call ECHO but the FILE CONTENT is written correctly, so the script runs with
the real token. Store the durable token at `~/.hermes/secrets/netlify_token`
(chmod 600) so future sessions skip the whole dance. Script body:
  #!/bin/bash
  cd /root/array-operator
  TOK=$(tr -d '\n' < /root/.hermes/secrets/netlify_token)
  export NETLIFY_AUTH_TOKEN="$TOK"
  netlify deploy --prod --dir=public 2>&1 | tail -4

## Daily-generation BAR GRAPH (replaced the report's ridgeline as default)
Ford: "people want daily generation in a bar graph for their monthly reports" →
replaced the Energy Ridgeline in the Quarterly/monthly report with a daily bar
chart as the PRIMARY/default visual (ridgeline stays available in the Trends tab).
- DATA = REAL daily rows, never fabricated from monthly: new
  `GET /billing/subscriptions/{id}/daily-series?period=YYYY-MM|YYYY-Qn` →
  the sub's array `DailyGeneration` rows over the window, each point carries
  `array_kwh` AND `kwh` (= array_kwh × allocation_pct, the offtaker's share).
  Defaults to the latest month with data. No rows → `has_data:false, points:[]`
  (honest empty state, not invented bars). period parser handles both YYYY-MM
  and YYYY-Qn; bad period → 400.
- CHART = standalone `public/trends-view-bars.js` exposing `window.AOBars.mount(
  container, {points,period_label,total_kwh})`. NOT a registered AOTrends view —
  it takes a daily-points payload directly (the AOTrends P-shape is multi-year
  monthly, wrong for daily). It REUSES AOTrends tokens + createCanvas when present
  but falls back to a local hi-DPI canvas so it renders standalone. Design that
  read as "cool": vertical gradient bars (brighter at top) + bright cap line,
  value-axis nice-ticks + gridlines, day-of-month labels thinned to fit, weekend
  tinting, peak-day glow, grow-in animation, hover tooltip via the shared
  `.tr-tip` class (defined in trends.css — ensure trends.css is loaded).
- Wiring: `reports.js mountQTrends(subId)` fetches daily-series → AOBars.mount as
  hero (in a green `.rb-q-chart-wide` card, caption "Daily Generation · <month>"),
  THEN mounts Solar Spiral as the secondary flourish. Removed the `#rbqRidge`
  host + the ridgeline mount from the report only. `index.html` must `<script
  src="trends-view-bars.js">` BEFORE trends.js. Verified live: 31 real May bars,
  8,273 kWh, scaled to the offtaker's 50%.
- PATTERN takeaway: when a report needs a PER-ENTITY daily chart, add a focused
  read endpoint (real rows, scaled, honest-empty) + a payload-driven standalone
  renderer — don't force it through the multi-year fleet Trends framework.

### Bar graph ALSO became the Trends-tab default (follow-up ask)
Ford then asked to put the same daily bars in the Trends tab with a 30-day x-axis.
The Trends tab IS the multi-year fleet framework, so here you DO register it:
- Backend: add `daily_recent` to `GET /v1/array-owners/fleet-trends` — fleet-wide
  DailyGeneration aggregated into a CONTIGUOUS 30-day window ending at the most
  recent day WITH data (so the chart is full even if today's pull hasn't landed;
  days with no gen render as 0 bars). `prep()` in trends-core.js carries it as
  `dailyRecent`.
- `trends-view-bars.js` ALSO `C.registerView("bars", {... mount(container,prepped)
  → AOBars.mount(container,{points:prepped.dailyRecent})})`. So the same renderer
  serves both the report (payload-driven) and the Trends switcher (registry).
- Make it DEFAULT: trends.js `active = c.getView("bars") ? "bars" : views[0].key`.
- Removed the ridgeline from the Trends tab too (dropped its `<script>` in
  index.html). The ridgeline view file stays in-repo but is loaded nowhere now.
- PITFALL — `order: 0` is FALSY: trends-core `listViews()` sorts by `(a.order||99)`,
  so `order:0` collapses to 99 and the view sorts LAST despite "wanting first."
  Use a positive fractional order (`0.5`) to land it first. (General JS trap:
  `x||default` eats a legitimate 0.) Also badges A/B/C/D were taken — give the
  new view a distinct badge ("30d") to avoid a duplicate-letter collision.

## Removing a nav TAB end-to-end (AO index.html + sandbox.js)
A whole tab spans 5 places — miss one and you get a broken/half-removed state:
1. `index.html` nav button (`<a id="tab<Name>">`),
2. `index.html` panel section (`<section id="panel<Name>">`),
3. its CSS `<link>` + JS `<script>` includes (and delete the now-orphan
   `<name>.js`/`<name>.css` files),
4. `sandbox.js` routing: the `TABS` map entry, the `tabFromHash()` `if(h==="#x")`
   branch, AND the `applyView()` `else if(active==="x"){…}` block,
5. verify nav `#x` now FALLS BACK cleanly (sandbox.js default → arrays).
Did this for the Claims tab. Verify live: nav shows the remaining tabs, the
removed panel id is absent, navigating to the dead hash redirects, 0 console
errors, and the deleted .js returns 404. PITFALL: a sloppy CSS-link removal can
duplicate a sibling `<link>` — check the head after.
SCOPE-CONFIRM FIRST: "remove the X tab" ≠ "rip out feature X." Warranty-claim
DRAFTING also lived in the Arrays triage queue (command-center.js/app.js) + in
marketing copy — those are NOT the tab. Ford confirmed "just the tab," kept the
rest. Always clarify tab-vs-feature before deleting deep logic.

## Removing redundant per-tab explanatory headers (legibility ask)
Ford: the header explaining what a tab does is "all self-explanatory" → remove.
The AO panels carry a `<div class="section-h"><h2>…</h2><span class="hint">…
</span></div>` block at the top of Master Account / Trends / Reports. Remove the
whole `.section-h` block so content starts right under the nav; the page's TOP
marketing hero banner is NOT a tab header — leave it. Verify `grep -c section-h`
== 0 and live curl shows the old hint copy gone. (Ford prefers lean, self-evident
UI — strip explanatory chrome when he flags it.)
When Ford says "move the Add-X button from tab A to tab B," do NOT duplicate the
form. Refactor the existing `renderManual()`-style fn to be host-configurable:
module-level `MANUAL_HOST_ID` (mount element id) + `MANUAL_AFTER_ADD` (refresh
callback). The destination tab sets both before calling, drops a `<div id=...>`
mount + a trigger button, and the same proven form + save path serves both. Keeps
one code path, one validation, one POST. Used to move Add-offtaker out of the
invoice tab into the Offtakers subtab. Also: when removing the form's old mount,
delete BOTH the `<div id="rbManual">` AND the `renderManual()` call in load().

## Shared-tree commit (reinforced — another agent's work constantly present)
solar-operator was MULTI-AGENT all arc: models.py/migrate.py/routes.py routinely
hold a sibling's uncommitted WeatherLocation/sponge/solaredge/extension work +
untracked adapter files. NEVER git add -A. Stage only your hunks; for a file you
SHARE (e.g. models.py with their WeatherLocation block + your new table), build a
hunk-filtered patch and `git apply --cached --recount`, then assert the other
agent's class is NOT staged (grep -c == 0) before commit. Verified pattern used
for every commit this arc (Bill.pdf_bytes, discount cols, RateSchedule).
