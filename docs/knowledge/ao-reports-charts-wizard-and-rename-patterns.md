# AO Reports: GMP auto-attach, discount model, data-derived rates, setup wizard, daily-bar charts, sitewide renames

Session-proven patterns for the Array Operator Reports system (frontend
`/root/array-operator/public/reports.js` + backend `/root/solar-operator/api/billing/`).
All ship via: solar-operator `git push origin HEAD:main` (Railway auto-deploy) +
array-operator MANUAL `netlify deploy --prod --dir=public`. Stage ONLY your hunks
in the shared solar-operator tree (sibling agents' WeatherL

<!-- APPEND-2 (Jun'26): chart-registry bug · 422 debug · tab prune · deploy masker -->
### Trends chart registry — `order: 0` falsy-sort LANDMINE
`trends-core.js listViews()` sorts by `(a.order || 99)` so **`order: 0` is falsy →
sorts LAST**. Use `order: 0.5` (any positive) to make a view first. Default-view
fallback in `trends.js` hardcoded "liquid" — to default a new view add
`c.getView("<key>") ? "<key>" : (...)`. Views: `C.registerView(key,{label,badge,
order,describe,mount})`; add a normalized field in `C.prep()` to feed all views
(e.g. `dailyRecent`). A view can be BOTH standalone (`window.AOBars.mount(host,
{points})`, used by the report) AND registered (Trends gets it via the registry).

### Daily-bar default + 30-day x-axis
Ford wanted the daily bar graph as DEFAULT (report + Trends) and ~30 days on x
("much more to look at"). Data never fabricated: per-offtaker
`GET /billing/subscriptions/{id}/daily-series?period=YYYY-MM|YYYY-Qn` (array
DailyGeneration × allocation_pct, latest-month default, has_data:false empty);
fleet `/fleet-trends` gained `daily_recent` (30 contiguous days ending at last day
with data, 0-bars for gaps). Renderer trends-view-bars.js.

### 422 "Couldn't save" = frontend↔backend body-KEY mismatch (recurring)
JS POSTs a key the Pydantic model rejects. This session: Master Account "Company"
sent `{company_name}` but `UpdateCompanyName` (api/account.py) wants `{name}` →
422; email `{email}` matched `UpdateEmail` so it worked. DEBUG: read the
endpoint's BaseModel, match the JS body key. VERIFY both shapes (422 vs 200);
a `.test` email TLD also 422s (reserved) — test with `.com`.

### Removing a tab / tab headers
Remove a nav tab end-to-end: `<a id="tab<Name>">` in `.tabbar`, `#panel<Name>`,
its script/link includes, the JS/CSS files, AND sandbox.js routing (`TABS` map,
`tabFromHash()` branch, `applyView()` else-if). Did Claims; `#claims` → falls back
to Arrays. SCOPE-CHECK: "remove the claims tab" = the nav tab only (warranty-claim
drafting also lives in Arrays triage + marketing copy — Ford said leave those).
Tab HEADERS: Ford finds per-tab title+`.hint` blocks redundant — removed
`.section-h` from Master Account/Trends/Reports; content starts under the nav
(page-top marketing hero is separate, kept).

### Netlify deploy + secret-masker (reconfirmed)
Token ~/.hermes/secrets/netlify_token (chmod 600). Masker mangles `$(cat token)`
inline → bash "syntax error"/"export: not a valid identifier". FIX: write a `.sh`
FILE (masker garbles the tool echo, not written bytes) and `bash` it. CLI session
expires (interactive login can't run headless) → ask Ford for a PAT, store chmod
600, warn transcript holds it. Verify: `curl -s ".../<file>?cb=$(date +%s)" | grep -c <marker>`.
<!-- end APPEND-2 -->ocation/sponge/adapter
work coexists in models.py/migrate.py — never `git add -A`).

## GMP bill-PDF auto-attach (consumer + ingestion both built here)
- Toggle is per-customer `BillingReportSubscription.auto_attach_gmp` (bool col + migration).
- CONSUMER seam: `api/reports/gmp_bill_pdf_read.py::get_bill_pdf_for_period(array_id, period_start, period_end)`
  resolves the matching `Bill` by account+period and returns durable bytes.
- DURABLE STORAGE: `Bill.pdf_bytes` (LargeBinary) + `Bill.pdf_content_type`. `Bill.pdf_path`
  is Railway-EPHEMERAL — never rely on it for attach weeks later. Persist bytes in-row.
- CAPTURE/PERSIST (worker.py): `_capture_current_bill_pdf` runs on the JSON pull, calls
  `gmp.fetch_bill_pdf(url)`, validates the first bytes are `%PDF` magic (a GMP auth
  redirect returns an HTML login page — reject it, never store HTML as a fake PDF),
  upserts onto the newest Bill row. Best-effort: never break the pull; surface
  `{"saved": false, "reason": "..."}` honestly instead of fabricating.
- delivery.generate_files: manual upload (`sub.gmp_invoice_pdf`) takes precedence; else
  auto-attach when toggle on + a captured PDF exists. UI shows honest status:
  "✓ GMP bill found" (green) only when bytes exist, else "will attach once captured".
- GMP PULL GOTCHA (live, verified): the JSON metrics API can 401 while the PDF
  redirector (`fetch_bill_pdf`) still accepts the same session token — so bill PDFs
  capture even when JSON pulls fail. Probe ONE account first (and roll back) before
  hammering all 65; failures seen: `MultipleResultsFound` (duplicate Bill rows per
  period_end → newest-bill lookup hits >1) and a 1.1KB HTML redirector page
  ("No form fields") for stale bill URLs.

## Discount billing model (Ford's mental model, replaced flat $/kWh)
- invoice = produced kWh × net_rate × (1 − discount). Default discount 10% (= bill at 90%).
  This maps onto the engine's existing `compute_invoice(billing_rate=1−discount)`; the
  savings = kWh × net_rate × discount is the customer-facing "solar savings".
- Schema: `Tenant.default_discount_pct` + `Tenant.default_net_rate_per_kwh`;
  `BillingReportSubscription.discount_pct` + `net_rate_per_kwh`. KEEP legacy flat
  `rate_per_kwh` cols as back-compat (treated as net-rate with 0 discount).
- Precedence (delivery.resolve_discount_pricing): customer override → operator global
  → AUTO schedule (see below) → legacy flat → documented VT default. Stamp provenance
  (`net_rate_source`, `net_rate_note`) into computed_invoice + preview-math + _sub_dict.
- Patch tests when the unified default shifts (two old "VT defaults" 0.18398 vs 0.21
  collapsed to 0.21 = `get_energy_rate(provider)`).

## Data-derived rate schedule (Ford: "use GMP, use everything you can" = MEASURE, never invent)
- Ford's HARD rule: never fabricate/hardcode a VT rate. "Default" = DERIVE from data we
  hold. Here: measure the blended retail rate from the 27k+ captured GMP bills.
- `models.RateSchedule` (state, utility, location_class, age_bucket, effective_start/end,
  blended_rate_per_kwh, sample_size, source_note, provisional) — created by create_all,
  verify via migrate log.
- `api/rate_schedule.py`: `blended_rate_from_bill` (positive NET energy charges / consumed
  kWh — NOT the net-metered avg_rate which swings wildly with credits),
  `array_age_bucket` (≤11 vs >11 yr net-metering adder boundary), `derive_blended_rate_from_bills`
  (MEDIAN per utility×window×age cell, min-sample gated — skip cells with <8 samples and
  fall back with provenance rather than guess), `resolve_net_rate` (specificity-ranked
  lookup, auto-rolls over by billing month), `refresh_rate_schedule` (idempotent upsert
  from bills; admin `POST /admin/rate-schedule/refresh`).
- Biennial auto-update: effective-window rows; resolver picks the window containing the
  billing month. Verified live: GMP le11 2022→0.177, 2024→0.1878, 2026→0.2097 (real, measured).
- Inputs already in the model: utility=UtilityAccount.provider, location=Array.region,
  age=Array.first_connect_date, month=billing period.

## First-run setup wizard (gating + aggregator pattern)
- `GET /billing/setup-state` = ONE aggregator call powering the whole wizard: arrays (with
  age/provider/region + auto-resolved net rate + what's missing), has_customers flag,
  global rate/discount defaults. UI shows wizard when has_customers is false.
- `PATCH /billing/arrays/{id}` sets install_year→first_connect_date (feeds rate buckets) +
  region; tenant-scoped, validates year range, 404 on foreign array.
- Frontend `FORCE_TAB` guard: wizard auto-shows on zero customers; "⚙ Setup" link reopens;
  "Skip for now" escapes. 4 steps: arrays+age → rate → offtakers → review→finish.
- Refactor the manual-add form to a configurable host (`MANUAL_HOST_ID` + `MANUAL_AFTER_ADD`
  callback) so the same form serves both the wizard and the Offtakers subtab without dupe.

## Daily-generation bar graph (replaced the ridgeline as default chart)
- People want DAILY bars in monthly reports. Source REAL data: `DailyGeneration` rows, never
  monthly-derived fakes. Report: `GET /subscriptions/{id}/daily-series?period=YYYY-MM|YYYY-Qn`
  → array daily rows scaled by allocation_pct, honest empty (has_data:false, points:[]) when none.
- Trends tab (fleet): added `daily_recent` to `/fleet-trends` = 30 contiguous days ending at the
  most recent day WITH data (full chart even if today's pull hasn't landed; 0-bars for gaps).
- `trends-view-bars.js` = standalone canvas renderer (`window.AOBars.mount`) AND registers as a
  Trends view via `C.registerView("bars", {...})`. `prep()` carries `dailyRecent`.
- TRENDS VIEW-ORDER BUG: `listViews()` sorts by `(a.order||99)` — `order:0` is FALSY → sorts
  LAST. Use `order: 0.5` to be first. Make it default in trends.js: `c.getView("bars") ? "bars" : ...`.
- Removing a Trends view = drop its `<script>` from index.html (keep the file parked).

## Sitewide label renames (Ford means USER-FACING LABELS only)
- "rename X to Y sitewide" = displayed strings ONLY. NEVER touch internal field names, API
  keys, element IDs, data-attrs, function names (e.g. `net_rate_per_kwh`, `customer_name`,
  `has_customers`, `#rbqCustomer`, `data-sub="customers"`, `renderCustomers`). Renaming those
  breaks the API contract + seeded data for zero user benefit.
- Done this session: "Net rate"→"Solar credit rate", "Customer(s)"→"Offtaker(s)" across UI +
  the customer-facing PDF/xlsx invoice line items (those ARE user-facing). Verify live with
  grep for the new label + 0 displayed old label (code comments are false positives).
- Subtab rename example: label "Invoice generator"→"Offtaker Invoice Generator" but keep
  `data-sub="invoice"` value.

## Local QA + deploy traps (recurring this session)
- STALE UVICORN: an old `uvicorn api.app` keeps :8788 bound; the new one silently fails to
  bind and exits → routes 404 / `/openapi.json` shows old paths. `pkill -f "uvicorn api.app"`,
  confirm port free (`ss -ltnp | grep 8788`), restart, verify the NEW route in openapi.
- OOM exit 137: local dev uvicorn gets OOM-killed mid-Playwright → 502/"Session expired" in UI.
  Check /health; restart.
- dev_proxy :8089 returns 501 on PUT/PATCH (only GET/POST). Test PUT/PATCH against :8788 directly;
  the 501 in a Playwright run is a proxy limitation, not a code bug. Prod Netlify proxies all methods.
- Token expires mid-Playwright → re-mint (`mint_session_for_tenant`) before each shot run.
- seed_probe.py gets swept (untracked + cron) — recreate it; it seeds tenant + array +
  last-full-month DailyGeneration so the bar chart + wizard have real data.
- RAILWAY DEPLOY TIMING: after push, the migrate/route check runs OLD code for ~60-90s. WAIT,
  then verify the route returns 401 (healthy) not 404 (not deployed) / 500 (column missing).
- NETLIFY AUTH EXPIRES mid-session: `netlify status` → "session expired", `netlify login` is
  interactive (can't do headless). FIX: store a personal access token chmod600 at
  `~/.hermes/secrets/netlify_token`, deploy with `NETLIFY_AUTH_TOKEN=<token> netlify deploy ...`.
  TRAP: the shell secret-masker mangles `$(cat ~/.hermes/secrets/netlify_token)` inside a
  command → "syntax error near unexpected token `)`". Run `export`/token-read as its OWN step,
  or pass the token inline without `$(...)`.

## Test isolation in the shared sqlite suite
- Tests share one SOLAR_DATA_DIR sqlite with NO per-test DB reset. A new test that seeds
  tenants/subs and DOESN'T clean up will break OTHER files' tests that do
  `select(BillingReportSubscription).scalar_one()` (global single-row assumption). Symptom:
  passes in isolation, fails when run with the rest; order-dependent.
  FIX: add an `autouse` fixture that deletes the rows your test seeded (track tenant ids;
  delete sub→daily→array→client children, then `db.get(Tenant, tid)` separately since Tenant
  keys on `id` not `tenant_id`). BillingReportSubscription uses `enabled=` not `active=`, and
  has NO `provider` field (that's UtilityAccount).
