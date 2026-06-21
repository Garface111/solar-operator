# Array Operator frontend: charts, sitewide renames, wizard, and the local-QA/deploy traps

Scope: `/root/array-operator/public/` (vanilla-JS owner site, Netlify). Companion to
the SKILL.md body. Everything here was learned shipping Reports + Trends features
end-to-end (discount billing, GMP auto-attach, rate schedule, setup wizard, daily
bar graph, label renames, removing a nav tab).

═══════════════════════════════════════════════════════════════════════
SITEWIDE LABEL RENAMES — change DISPLAY text, never identifiers
═══════════════════════════════════════════════════════════════════════
Ford's rule (confirmed twice this session: "net rate"→"solar credit rate",
"customers"→"offtakers"): rename USER-FACING LABELS only. Leave internal field
names, API keys, element IDs, function names, and `data-*` routing values alone —
renaming those breaks the API contract / seeded data for zero user benefit.

KEEP UNTOUCHED (these are NOT labels even though they read like the word):
  - API/JSON keys + Pydantic/ORM fields: `net_rate_per_kwh`, `customer_name`,
    `has_customers`, `default_net_rate_per_kwh`, `resolved_net_source`, `allocation_pct`
  - element IDs: `#rbqCustomer`, `#rbSubCustomers`, `#rbmName`
  - routing values + classes: `data-sub="customers"`, `.rb-cust`, `.rb-wiz-custs`
  - function names: `renderCustomers`, `drawStepCustomers`
  - code comments (harmless; don't bother)
CHANGE (these ARE labels): `<h3>`/`<p>` copy, `<span class="rl">` field labels,
  `placeholder="…"`, button text, `.textContent`/`.innerHTML` status strings,
  tooltip `title="…"`, error messages, `<option>` empty-state text, and the
  customer-facing PDF/xlsx invoice lines in `api/billing/invoice.py`.
PROCESS: `search_files` for the word case-insensitively across BOTH
  `array-operator/public/*` (UI) AND `solar-operator/api/billing/*` (invoices).
  Patch each display hit; after, grep the live deploy to prove 0 displayed hits
  remain (a code-comment hit is fine). Run `node -c public/reports.js` after — the
  file uses unescaped double-quotes in template literals, easy to break.
PITFALL: a patch that drops a stray `</p>` or `${subId?"":""}` no-op slips through
  `node -c` only if balanced — eyeball the diff.

═══════════════════════════════════════════════════════════════════════
ADDING A NEW TRENDS-TAB CHART (the AOTrends view registry)
═══════════════════════════════════════════════════════════════════════
Trends framework files (loaded in this order in index.html):
  trends-core.js   → window.AOTrends: COLORS tokens, yearColor, hexA, fmt0,
                     prep(data)→{years,monthly,peak,seasonal,dailyRecent,byArray,raw},
                     smoothPath, createCanvas(container,{aspect,maxHeight,minHeight}),
                     registerView(key,def), listViews(), getView(key)
  trends-view-*.js → each calls C.registerView(key,{label,badge,order,describe,
                     mount(container,prepped,core)->stopFn})
  trends.js        → orchestrator: stat band + switcher + crossfade + by-array table.
                     ACCENT map (per-view hue) lives here — add your key.
Data source: GET /v1/array-owners/fleet-trends (fleet-aggregated). prep() normalizes
it. To add a daily series, append a field to the endpoint payload AND to prep()
(e.g. `dailyRecent: data.daily_recent || []`), then read it in your view's mount.

TO MAKE A VIEW THE DEFAULT:
  - `order` controls switcher position BUT `listViews()` sorts by `(a.order||99)`,
    so **order:0 is FALSY and sorts LAST**. Use `order: 0.5` to land first.
  - trends.js default pick is `savedView() || views[0].key`; if you want a hard
    default regardless of registration, patch it to
    `c.getView("yourkey") ? "yourkey" : (views[0]&&views[0].key) || "liquid"`.
  - badges A/B/C/D are per-view; pick a non-colliding one (used "30d").
Removing a view from the tab = drop its `<script>` from index.html (the file can
stay in the repo). The registry only has what's loaded.

STANDALONE chart reuse (e.g. in a Report, not the Trends tab): expose a plain
`window.AOBars = { mount(container, {points:[{day,kwh}], period_label}, core?) }`
that reuses AOTrends tokens/createCanvas when present but falls back to a local
hi-DPI canvas so it renders even if AOTrends isn't loaded. Then ALSO register it
as a Trends view via a thin adapter that reads prepped.dailyRecent. One renderer,
two mount points (report + Trends tab).

DAILY BAR GRAPH is now the DEFAULT production chart in BOTH the Quarterly report
and the Trends tab (replaced the Energy Ridgeline). Owners want daily bars on
monthly reports. trends-view-bars.js + window.AOBars. 30-day window in Trends.

═══════════════════════════════════════════════════════════════════════
BACKEND READ ENDPOINTS for charts/wizard (never fabricate)
═══════════════════════════════════════════════════════════════════════
- Daily series: GET /v1/array-operator/billing/subscriptions/{id}/daily-series
  ?period=YYYY-MM|YYYY-Qn → reads DailyGeneration for the sub's array over the
  window, scales each day by allocation_pct (returns array_kwh + offtaker kwh).
  Defaults to the latest month WITH data. No rows → has_data:false, points:[].
- Fleet daily (Trends): /v1/array-owners/fleet-trends gained `daily_recent` — a
  contiguous 30-day window ending at the most recent day with data, summed across
  arrays; missing days render as 0 bars.
- First-run wizard: GET /billing/setup-state (one aggregator: arrays w/ age +
  provider + region + auto-resolved rate + what's missing, has_customers flag,
  global rate/discount) drives the whole flow; PATCH /billing/arrays/{id} sets
  install_year→first_connect_date (feeds the rate age buckets) + region.
HONESTY: every chart/math endpoint returns has_data:false + nulls/[] on thin data
so the UI shows a muted "no data yet" instead of invented numbers. Ford prizes this.

FIRST-RUN WIZARD pattern (reports.js): show a guided 4-step flow on first visit
(setup-state.has_customers===false): ① arrays+age → ② rate+discount → ③ add
offtakers → ④ review&finish. A FORCE_TAB guard + a "⚙ Setup" link reopen it. The
"Add an offtaker" manual form is host-configurable (MANUAL_HOST_ID +
MANUAL_AFTER_ADD) so the same form serves the wizard AND the Offtakers subtab.

═══════════════════════════════════════════════════════════════════════
LOCAL QA HARNESS + DEPLOY TRAPS (these bit repeatedly this session)
═══════════════════════════════════════════════════════════════════════
LOCAL STACK: uvicorn :8788 (solar-operator venv + SOLAR_DATA_DIR sqlite) +
dev_proxy.py :8089 (mirrors Netlify /v1 proxy). Browser hits :8089, set
localStorage `so_session` to a minted token. seed_probe.py seeds tenant
ten_paulbozuwa01 + array + DailyGeneration; it gets SWEPT periodically — recreate it.

- STALE UVICORN BOUND TO :8788 → silently serves OLD code. Symptom: your new
  route 404s / new `_sub_dict` field missing even after "restart". An old pid is
  still bound; your new bg uvicorn failed to bind and exited. FIX: `pkill -f
  "uvicorn api.app"; sleep 2; ss -ltnp | grep 8788` (expect free) → start fresh →
  verify the route via /openapi.json or a curl, NOT by assuming.
- dev_proxy :8089 RETURNS 501 ON PUT (and PATCH-ish). Wizard rate-save (PUT
  /global-rate) shows a console 501 locally but works on prod Netlify. To verify
  PUT/PATCH locally, hit :8788 DIRECTLY, not through the proxy.
- LOCAL UVICORN OOM-KILL (exit 137) → 502s through the proxy, reads as "Session
  expired" / "Couldn't load" in the UI. `process(poll)` shows exit 137. Just
  restart it; not a code bug.
- TOKEN EXPIRES mid-Playwright (long multi-step shots) → a sub-panel shows
  "Session expired". Re-mint right before each shot run:
  `python -c "from api.account import mint_session_for_tenant; open('/tmp/ao_token.txt','w').write(mint_session_for_tenant('ten_paulbozuwa01'))"`
- VISUAL-QA is mandatory for every UI change (Ford's rule): Playwright screenshot
  over :8089 (NEVER file://) + vision_analyze every state. For canvas charts,
  also print computed values (tooltip text, bar count) from page.evaluate so you
  confirm real data, not just pixels.

DEPLOY (manual for AO):
  - AO frontend: `git push` updates GitHub ONLY. Prod = `netlify deploy --prod
    --dir=public` from /root/array-operator.
  - NETLIFY SESSION EXPIRES ("Your session has expired… netlify login"). `netlify
    login` is interactive browser OAuth — can't do headless. FIX: get a personal
    access token (app.netlify.com → User settings → Applications → New token),
    store chmod600 at `~/.hermes/secrets/netlify_token`, deploy with
    `NETLIFY_AUTH_TOKEN=<token>`. Warn Ford the chat transcript holds a pasted
    token; suggest rotating.
  - SECRET-MASKER MANGLES `$(cat ~/.hermes/secrets/netlify_token)` and `$TOK` in
    terminal — it rewrites them to `***` and the command breaks (syntax error /
    empty token). FIX: write a tiny `.sh` file (write_file content is NOT masked
    the same way) that does `TOK=$(tr -d '\n' < …/netlify_token); export
    NETLIFY_AUTH_TOKEN="$TOK"; netlify deploy …` and `bash /tmp/deploy_ao.sh`.
  - solar-operator backend: `git push origin HEAD:main` auto-deploys on Railway.
    New DB column → run `railway ssh "cd /app && python -m api.migrate"` AFTER the
    deploy lands, then VERIFY the column via railway-ssh inspect (the migrate LOG
    often runs OLD code mid-deploy and no-ops — not proof). New-route check: the
    route returns 401 (auth-gated, healthy), not 404 (not deployed) / 500 (ORM
    mismatch). GET on a PATCH-only route → 405 = registered.
  - Timing: after pushing backend, WAIT ~75s for the Railway build before
    migrate/verify, or you'll see 404 and re-check.

═══════════════════════════════════════════════════════════════════════
SHARED-TREE COMMIT HYGIENE (solar-operator is multi-agent)
═══════════════════════════════════════════════════════════════════════
Sibling agents have uncommitted work in the same tree (api/models.py with their
WeatherLocation/sponge models, api/adapters/*, extension/*). NEVER `git add -A`.
- Commit ONLY your files. For a file you share (e.g. api/models.py with both your
  field + their model block): build a patch of JUST your @@-hunks, drop theirs,
  `git apply --cached --recount` (recount fixes stale line numbers from their
  edits), then verify `git diff --cached | grep -c <their-marker>` == 0.
- A sibling's commit may sweep up your already-saved edits to a shared file (e.g.
  sandbox.js) — that's fine; verify `git show HEAD:<file> | grep -c <marker>`==0.
- TEST ISOLATION: the suite shares one SOLAR_DATA_DIR sqlite with NO per-test
  reset. A new test that seeds tenants/subs LEAKS rows into other files' global
  `scalar_one()` count assertions → spurious failures only when run together. FIX:
  give your test module an autouse cleanup fixture that deletes its seeded rows
  (Tenant by id via db.get; children by tenant_id) on teardown. Run the suite in
  multiple orders to confirm order-independence.

═══════════════════════════════════════════════════════════════════════
GMP AUTO-ATTACH (bill PDF) — consumer + ingestion both shipped
═══════════════════════════════════════════════════════════════════════
Per-customer toggle (BillingReportSubscription.auto_attach_gmp) auto-attaches the
captured GMP bill PDF. Durable storage = Bill.pdf_bytes + pdf_content_type (NOT
pdf_path — Railway disk is ephemeral). worker._capture_current_bill_pdf fetches
via gmp.fetch_bill_pdf on the JSON pull and persists bytes (validates %PDF magic
so an auth-redirect HTML page is never stored as a fake PDF). Read seam
api/reports/gmp_bill_pdf_read.py matches by account+period. Lights up only once a
live GMP token is captured + a pull runs; honest "not captured yet" UI until then.
NOTE: GMP JSON API may 401 while the PDF redirector still accepts the token — the
PDF path is what auto-attach needs, so it can work despite a JSON 401.
