# Array Operator frontend — UX fixes, contract mismatches & sitewide-rename patterns

Owner site = `/root/array-operator` (vanilla JS, separate repo, Netlify-deployed).
Backend = `/root/solar-operator` (FastAPI on Railway). This file captures durable
patterns from building/fixing the Reports + Master Account + Trends UI. Read it
when touching `public/reports.js`, `public/sandbox.js`, the trends-view-*.js
chart system, or any owner-site form that POSTs to `/v1/account/*`.

## Deploy loop (owner site is MANUAL — git push only updates GitHub)
- `git push origin main` → updates GitHub ONLY. It does NOT deploy.
- Live deploy: `netlify deploy --prod --dir=public` from `/root/array-operator`
  (project `array-operator-ea`, live = arrayoperator.com).
- Backend: `git push origin HEAD:main` auto-deploys on Railway (~70–80s). New DB
  columns → run `railway ssh "cd /app && python -m api.migrate"` AFTER the deploy
  lands, then VERIFY the column via `inspect(engine).get_columns(...)` (the migrate
  LOG can run stale code / no-op — not proof). A route is healthy when it returns
  401 (auth-gated), not 500 (missing column) or 404 (route not deployed yet).

### Netlify secret-masker + token-expiry trap (RECURRING — cost several retries)
- `netlify` CLI sessions EXPIRE silently → `JSONHTTPError: Unauthorized` /
  `netlify status` says "Your session has expired… run netlify login".
  `netlify login` is interactive browser OAuth — CANNOT be done headlessly.
- Token lives at `~/.hermes/secrets/netlify_token` (chmod 600). Deploy with
  `NETLIFY_AUTH_TOKEN` from it. BUT the shell secret-masker MANGLES inline
  `$(cat …token)` / `"$TOK"` substitutions in a `terminal()` command — it rewrites
  them to `***` and the command breaks (`export: '--prod': not a valid identifier`).
  FIX: write a tiny `.sh` SCRIPT FILE (the masker only garbles the tool-call echo,
  not the written file bytes), then `bash that_file.sh`:
  ```sh
  cd /root/array-operator
  TOK=$(cat /root/.hermes/secrets/netlify_token)
  NETLIFY_AUTH_TOKEN="$TOK" netlify deploy --prod --dir=public 2>&1 | tail -3
  ```
  Read the file back with read_file to confirm the real content is intact before
  running. When the token itself is expired (40-char `nfp_…`), you're BLOCKED:
  commit+push to GitHub, then ask Ford to either run `netlify login` or paste a
  fresh personal access token (warn him the transcript holds it → rotate after).

## Frontend↔backend contract mismatch = HTTP 422 (silent body-key bug)
Symptom: a save button shows "Couldn't save (HTTP 422)". 422 = Pydantic rejected
the request BODY shape (wrong/missing key), NOT auth (401) or server error (500).
- Real example: Master Account "Company" field POSTed `{company_name: val}` to
  `/v1/account/company-name`, but the backend model `UpdateCompanyName` expects
  `{name}`. Mismatch → 422. Fix = send the key the model declares.
- DIAGNOSE by reading the Pydantic model in `api/account.py` (grep `class Update…`)
  and matching the JS body key exactly. `UpdateEmail{email}`, `UpdateCompanyName{name}`,
  `UpdateName{name}`.
- PROVE the fix against the running backend with a bogus token: a correct body
  shape returns 401 (auth-gated, body parsed); a wrong shape returns 422. So
  `OLD → 422, NEW → 401/200` is the signature of a fixed contract mismatch.
- GOTCHA in test probes: Pydantic `EmailStr` REJECTS reserved TLDs like
  `*.test` ("special-use or reserved name") → 422 even with the right key. Use a
  `.com` address when testing email endpoints, or you'll misread a valid fix as broken.

## CSS `[hidden]` defeated by a class display rule (always-expanded editor bug)
Symptom Ford flagged: the Master Account password editor showed a "Current
password" field even on accounts that never set one. Root cause: the editor div
had `hidden` set by JS, but a CSS rule `.acct-pw-edit{display:flex}` OVERRODE the
UA `[hidden]{display:none}` (a class selector beats the UA attribute rule). So the
editor was ALWAYS visible. FIX: add an explicit `.acct-pw-edit[hidden]{display:none}`.
General rule: any element you toggle via the `hidden` attribute needs a matching
`.your-class[hidden]{display:none}` if the class sets its own `display`. Otherwise
the toggle silently does nothing.

## Sitewide LABEL renames — Ford's hard rule (USER-FACING TEXT ONLY)
"Rename X to Y sitewide" means DISPLAY STRINGS ONLY: button text, headings,
placeholders, tooltips, error messages, AND the labels on generated PDF/xlsx
invoices the customer sees. NEVER touch internal field names, API keys, element
IDs, `data-*` attributes, or function names — renaming those breaks the contract
for zero user benefit. Confirmed renames this project:
- "Net rate" → "Solar credit rate" (kept `net_rate_per_kwh`, `net_source`, etc.)
- "Customer(s)" → "Offtaker(s)" (kept `customer_name`, `has_customers`,
  `#rbqCustomer`, `data-sub="customers"`, `renderCustomers`, `m.customer.name`).
Workflow: grep `[Cc]ustomer` / `[Nn]et rate` for ALL hits, change only the
displayed-string ones, leave comments + identifiers. After: `node -c file.js`,
then grep the LIVE deployed file to confirm new label present + 0 displayed old
label (code COMMENTS matching the grep are fine — verify they're `//` lines).
Also rename customer-facing invoice lines in `api/billing/invoice.py`
(PDF `_money` rows + xlsx `put("B16", …)`), not just the frontend.

## Reusable form pattern: one configurable host + after-add callback
When you need the SAME add/edit form to appear in two tabs (e.g. the manual
"Add an offtaker" form moved from the Invoice tab into the Offtakers tab),
refactor the renderer to read a module-level `MANUAL_HOST_ID` + `MANUAL_AFTER_ADD`
callback instead of a hardcoded mount id. Each caller sets those before invoking,
so one proven form serves multiple locations with no logic duplication.

## Tab removal checklist (remove a nav tab end-to-end)
To remove a tab (e.g. Claims) with no broken state: (1) nav `<a class="tab">` in
index.html, (2) the `<section class="panel" id="panelX">`, (3) its CSS `<link>` +
`<script>` includes, (4) DELETE the orphaned `x.js`/`x.css`, (5) the routing in
sandbox.js — the `TABS` map entry, the `tabFromHash()` branch, and the
`applyView()` `else if(active==="x")` block. Navigating to `#x` should then fall
back to the default tab cleanly. Verify: `node -c sandbox.js`, grep 0 live refs,
and `curl` the deleted asset expecting 404. CONFIRM SCOPE FIRST when the feature
also lives elsewhere — "remove the Claims TAB" ≠ remove the in-Arrays warranty-claim
drafting (command-center.js/app.js) or the marketing copy. Ask before over-reaching.

## Data-derived defaults — "use everything you can" = MINE captured data, never invent
Ford's hard rule restated for charts/rates: when he asks for a "default" or
"pre-propagated" value, DERIVE it from data we already hold; never hardcode-guess.
- Rate schedule: measured blended $/kWh from 27k captured GMP bills (median per
  utility×effective-window×age-bucket, ≥8 samples or skip-with-provenance). Detail
  in `references/derive-rates-and-billing-pricing-from-captured-bills.md`.
- Daily-generation bar graph: real `DailyGeneration` rows scaled by the offtaker's
  `allocation_pct`; honest `has_data:false`/empty when no rows — never synthesize
  bars from monthly data. Backend seam: `GET /subscriptions/{id}/daily-series`
  (per-offtaker, latest month with data by default) and a fleet `daily_recent`
  (last 30 contiguous days, fleet-aggregated) added to `/v1/array-owners/fleet-trends`
  for the Trends-tab version.

## Trends-view chart registry quirks (window.AOTrends)
The Trends tab loads `trends-core.js` (registry: `registerView(key, def)`,
`listViews()` sorts by `def.order`) then `trends-view-*.js` files that self-register.
- `listViews()` sorts by `(a.order || 99)` — so `order: 0` is FALSY and sorts LAST,
  not first. Use `order: 0.5` (or any positive number) to make a view appear first.
- To make a view the DEFAULT, also handle the hardcoded fallback in trends.js
  (`active = c.getView("bars") ? "bars" : …`), not just `order`.
- A report-only chart (daily bars) can be BOTH a standalone `window.AOBars.mount`
  (fed `{points}` directly in reports.js) AND a registered Trends view (reads
  `prepped.dailyRecent`). `prep()` in trends-core.js must carry the new field.
- Each view's tooltip reuses `.tr-tip` (styled in trends.css) — create the div in
  the host (which is `position:relative`).

## Shared-repo discipline (solar-operator is multi-agent + cron-auto-commit)
`/root/solar-operator` often shows ANOTHER agent's uncommitted work in shared
files (models.py WeatherLocation block, adapters, extension/). NEVER `git add -A`.
Stage ONLY your hunks: build a patch of just your `@@`-hunks, drop theirs, apply
with `git apply --cached --recount`, then VERIFY `git diff --cached --name-only`
contains zero of their files (grep -c for `adapters/|extension/|models.py`). A
sibling agent's commit may sweep up your already-saved edits in the shared tree —
that's fine as long as the committed file is clean (verify with `git show HEAD:path`).

## Local QA harness (recurring restarts)
- Backend on :8788 (`uvicorn api.app:app`, no `--reload`) + dev_proxy on :8089
  (`BACKEND=http://127.0.0.1:8788 python3 dev_proxy.py 8089`, mirrors Netlify /v1).
- Both die often (OOM exit 137 reads as "Session expired" in the UI; a stale
  uvicorn keeps an OLD code image bound to :8788 so new routes 404 — `pkill -f
  "uvicorn api.app"` + restart, then check `/openapi.json` for the new path).
- dev_proxy returns 501 on PUT/PATCH — test those methods against :8788 directly.
- Session tokens expire mid-Playwright → re-mint: `python -c "from api.account
  import mint_session_for_tenant; open('/tmp/ao_token.txt','w').write(
  mint_session_for_tenant('ten_paulbozuwa01'))"`. Set `localStorage so_session` in
  the page before navigating. seed_probe.py recreates the tenant+array+30 days of
  DailyGeneration (last full month) — re-run if the probe DB was wiped.
- Visual-QA EVERY UI change: Playwright screenshot + vision_analyze; check for
  clipping/overlap/empty-state before claiming done (Ford validates by eye).
