# Array Operator Reports / billing build + rate model

How the Reports tab (owner invoicing for Paul Bozuwa-style array owners) is built.
Frontend: `/root/array-operator/public/reports.js` + `command-center.css` (Netlify).
Backend: `/root/solar-operator/api/billing/` (routes.py, delivery.py, matcher.py,
invoice.py, summary.py) + models in `api/models.py`, migration in `api/migrate.py`.

## PROBE BEFORE BUILDING (this system is bigger than it looks)
When told a system "may be partly built / lost / untested", DO NOT assume it's gone.
This Reports system was ~90% built and committed already. Probe first:
- `search_files` for the feature across BOTH repos (frontend lives in array-operator,
  backend in solar-operator — they're separate repos).
- Stand it up locally and exercise it end-to-end before claiming what works.
- Report capabilities with EVIDENCE (real API output + screenshots), then build.

## Local stand-up recipe (verified working)
1. `cd /root/solar-operator && python3 -m venv venv && pip install -r requirements.txt`
2. Use a FRESH sqlite DB dir so `create_all` picks up new columns:
   `export SOLAR_DATA_DIR=/tmp/ao_probe_db SESSION_SECRET=probe-dev-secret-stable`
   (PITFALL: an OLD sqlite DB won't gain new ORM columns from create_all — you get
   `sqlite3.OperationalError: no such column: ...`. New DB dir or run migrate.)
3. Seed a tenant+array+DailyGeneration and MINT a token with `api.account.mint_session_for_tenant(tid)`
   using the SAME SESSION_SECRET the server runs with (else 401 "Session expired").
   Put env in a sourced file (`/tmp/ao_env.sh`) so server + token mint share it exactly.
4. Backend: `uvicorn api.app:app --host 127.0.0.1 --port 8788`
5. Frontend over the Netlify-mirroring proxy (NEVER file://):
   `cd /root/array-operator && BACKEND=http://127.0.0.1:8788 python3 dev_proxy.py 8089`
   The proxy reverse-proxies /v1, /accounts, /onboarding to the backend.
6. In Playwright: visit `/index.html` first, `localStorage.setItem('so_session', token)`,
   then `/index.html#reports`. The UI reads `so_session` from localStorage.

## The pricing model — DISCOUNT off the net rate (current model, Jun 2026)
Ford reframed pricing from a flat $/kWh to a DISCOUNT off the net rate (default 10% off):
  `invoice = produced kWh × net_rate × (1 − discount)`
This is the customer's solar SAVINGS story, and it maps cleanly onto the existing engine:
`compute_invoice(kwh, tariff=net_rate, adder=0, billing_rate=(1−discount))` already yields
`amount_owed = kwh × net_rate × (1−discount)` AND `solar_savings = kwh × net_rate × discount`.
So you change resolution + plumbing, NOT the math core.

Resolver: `delivery.resolve_discount_pricing(sub)` → dict
`{net_rate, discount_pct, effective_rate, net_source, discount_source}`. Precedence PER FIELD
(customer override → operator global → built-in default):
  net_rate: `sub.net_rate_per_kwh` → `Tenant.default_net_rate_per_kwh` → legacy flat
            (`sub.rate_per_kwh`/`Tenant.default_billing_rate_per_kwh`, treated as net w/ 0
            discount) → `MANUAL_TARIFF 0.18398` (VT default).
  discount: `sub.discount_pct` → `Tenant.default_discount_pct` → `DEFAULT_DISCOUNT = 0.10`.
BACK-COMPAT (do not skip): a legacy flat rate must bill at EXACTLY that rate — treat it as
net_rate with discount=0 (`discount_source="legacy_flat"`), so existing customers' dollars
never silently change when the model flips. `resolve_rate_per_kwh` is kept as a deprecated
shim that returns `effective_rate`.
Stamp on `computed`: `net_rate_per_kwh`, `discount_pct`, `effective_rate_per_kwh`,
`net_rate_source`, `discount_source`, plus `solar_savings` — UI shows the savings + provenance.
NEVER fabricate; $0 when no generation.

Schema: 4 nullable Float cols — `Tenant.default_discount_pct` + `default_net_rate_per_kwh`,
`BillingReportSubscription.discount_pct` + `net_rate_per_kwh` (keep old `rate_per_kwh` /
`default_billing_rate_per_kwh` for back-compat). models.py + idempotent ALTERs in migrate.py
(`column_exists()` guard — Postgres prod has no `IF NOT EXISTS` on ADD COLUMN).
Routes: discount_pct + net_rate_per_kwh on POST create (Form) + PATCH (`body.model_fields_set`
so `null` CLEARS vs "omitted"); `GET/PUT /v1/array-operator/billing/global-rate` now carries
net rate + discount AND echoes `effective_net_rate_per_kwh`/`effective_discount_pct` (the
defaults actually applied) so the UI can render a live "Customers pay $X/kWh" preview.
`_validate_discount` rejects ≥1 (would zero/inverse the bill); `_validate_rate` guards 0..5 $/kWh.
UI: whole-% in the field (10) ↔ fraction in the API (0.10); chip shows "10% off" / "default
discount"; inline "% off" editor PATCHes discount_pct.

### GMP bill-PDF auto-attach (per-customer toggle) — full ingestion+consumer build
`BillingReportSubscription.auto_attach_gmp` (bool). When on, `delivery.generate_files`
auto-attaches the captured GMP bill PDF for the array+period (manual upload takes precedence).
Read-only consumer seam `api/reports/gmp_bill_pdf_read.get_bill_pdf_for_period(array_id, ps, pe)`
reads DURABLE `Bill.pdf_bytes` (NOT `Bill.pdf_path` — that's Railway-ephemeral, wiped on
redeploy). Ingestion (built this session when the other agent stopped): `worker._pull_via_pdf`
+ `_capture_current_bill_pdf` fetch the current bill PDF via `gmp.fetch_bill_pdf` and persist
the BYTES in-row; validate `data[:4]==b"%PDF"` so an auth-redirect HTML page is never stored
as a PDF. Honest UI status `gmp_auto_status` = ready|pending|no_gmp — never implies a PDF
exists when it doesn't. NOTE: GMP JSON bills API may 401 while the PDF-redirector path still
works with the same session token — so the PDF capture succeeds even when JSON history pull fails.

## OFFTAKER reports = UTILITY-BILL data ONLY (Ford's hard rule, Jun 2026) ⭐
THE most important billing rule, stated emphatically by Ford: an OFFTAKER's invoice
pulls EXCLUSIVELY from the utility's PAPER BILLS — "the paper copies, not the hourly
data" — NEVER vendor/inverter telemetry, NEVER the GMP hourly-interval data, NEVER a
daily CSV, and with NO FALLBACK. He framed it as "a separate function for our system."
This SUPERSEDES the source-agnostic adapter below FOR OFFTAKERS (that adapter's vendor
fallback is wrong for offtaker billing — it's only for array/workbook subs).

Data model + flow (all in solar-operator, shipped + tested this session):
- `BillingReportSubscription.utility_account_id` (nullable INT FK → utility_accounts.id):
  binds the offtaker to ONE GMP account. NULL = legacy array-based sub (unchanged).
- `delivery._utility_bill_period_kwh(db, utility_account_id)` → `(kwh, start, end, label)`
  reads `Bill.kwh_generated` for that account's most-recent billing period. Returns
  `(None,…)` when no bill → caller WAITS, never substitutes another source.
- `build_manual_match`: a TOP-PRIORITY `if sub.utility_account_id is not None:` branch
  (before multi-array + single-array). Sets `kwh_source="utility_bill"` ALWAYS and stamps
  `computed_invoice["has_utility_bill"]` (False when no bill yet).
- `deliver_subscription` GUARD: a utility-bound sub with `has_utility_bill is False`
  returns `{"ok":False,"skipped":True}` — NO $0 invoice goes out (skip-and-wait).
- Selector endpoint `GET /v1/array-operator/billing/utility-accounts` lists this tenant's
  GMP accounts + per-account bill summary (count, latest period, latest kWh, has_bill) so
  the UI can show whether a bill is on file. `_create_manual_subscription` gained the
  utility-account branch (validates the account is this tenant's + provider=='gmp').
- FRONTEND (reports.js): the "New offtaker" manual form's "Which array?" select became
  "Which GMP utility bill?" populated from `fetchUtilityAccounts()` → `/utility-accounts`;
  `saveManual` posts `utility_account_id` (not array_id). The wizard offtaker step still
  exists; this rule applies to the typed-in manual path.
- TEST: `tests/test_offtaker_utility_bill.py` proves the bound offtaker uses the 1800-kWh
  BILL even when a conflicting 9999-kWh vendor DailyGeneration exists, and that delivery
  SKIPS when no bill. Pattern for proving "no vendor leakage": seed a conflicting vendor
  row and assert the invoice ignores it.
- BACK-COMPAT: existing array-based offtakers (no utility_account_id) keep the old path.
  Migrating them onto their GMP bills is a separate, opt-in follow-up.

## Source-agnostic produced-kWh adapter (GMP contract + fallback) — ARRAY/workbook subs ONLY
NOTE: this adapter's vendor FALLBACK is for array-based / workbook subs. Do NOT use it for
OFFTAKERS bound to a utility account (see the utility-bill-ONLY section above).
Ford's call: prefer the authoritative metered source, fall back, SHOW which source fed
each number. `delivery._array_period_kwh_sourced(db, array_id)` returns
`(kwh, start, end, label, kwh_source)` where kwh_source ∈ `gmp_api|daily_csv|None`:
  1. GMP daily-read CONTRACT — `api/reports/gmp_daily_read.py`
     (`get_monthly_totals`/`get_daily_series`/`get_coverage`). CALL THESE FUNCTIONS ONLY;
     never import the `Gmp*` ORM or query gmp_* tables directly (storage stays the
     data-sponge agent's). That module + `docs/plans/GMP_DAILY_READ_CONTRACT.md` are the
     contract; they were marked PROVISIONAL v0 — degrade safely if a key/name changes.
  2. fallback to existing `_array_period_kwh` (DailyGeneration → Bill).
Defensive: wrap the GMP call in try/except — a provisional/missing contract or empty
tables must degrade to fallback, never raise into invoice math.

## Reports subtabs + Paul's editable-draft-email workflow
- Subtabs = in-tab pills sharing the customer surface (Ford confirmed this over swapping
  whole panels): "Invoice generator" + "Quarterly reports". `.rb-subtab` toggles
  `#rbSubInvoice`/`#rbSubQuarterly` display.
- Quarterly report REUSES the Trends visuals via `window.AOTrends` (do NOT rebuild charts):
  `AOTrends.getView('spiral'|'ridgeline').mount(container, AOTrends.prep(data), AOTrends)`,
  data from `GET /v1/array-owners/fleet-trends`. Ford likes spiral + ridgeline. Track the
  returned stop-fns and call them on teardown. trends scripts already load in index.html.
- Editable email: the approval inbox draft card has a pre-filled `<textarea>` (a sensible
  default note). Save via existing `PATCH /drafts/{id}` (note field). The note must RIDE
  the real send: `_email_html(note=...)` renders it (HTML-escaped, \n→<br>) above the
  figures; `deliver_subscription(note=...)`; `approve_draft` passes `d.note`. Nothing
  auto-sends — every path ends at a human "Approve & send".

## fleet-tree shape gotcha (real bug fixed this session)
The manual "Add a customer" array dropdown reads `GET /v1/array-owners/fleet-tree`, which
returns `{columns:[{array_id, array_name, ...}]}` — NOT `{arrays:[{id,name}]}`. Reading the
wrong shape => empty dropdown ("No arrays yet"). Map from `t.columns` / `a.array_name`.

## Deploy + migrate ORDER (critical — adding DB columns)
AO frontend deploy is MANUAL: `netlify deploy --prod --dir=public` from /root/array-operator
(git push updates GitHub only; live stays stale). Backend: pushing solar-operator to main
AUTO-DEPLOYS Railway. When the push adds NEW ORM columns, the live app 500s on any query
that SELECTs them UNTIL the migration runs. So after pushing backend:
  `railway ssh "cd /app && python -m api.migrate"`  (idempotent, adds nullable cols only)
Verify: columns present via `inspect(engine).get_columns(...)`, and the billing route returns
401 (auth), NOT 500. This is a PROD write — get Ford's OK first (he said "do it").

### Deploy-timing + verification hardening (learned the hard way, repeatedly)
- The migrate LOG is NOT proof. Running `railway ssh "python -m api.migrate"` right after a
  push often runs the OLD deployed code (the new migrate block hasn't shipped yet) and prints
  no "+ column" line — yet the column may still land via create_all, or land on a later tick.
  ORDER: push → WAIT for the Railway build (~60-75s) → migrate → VERIFY THE COLUMN DIRECTLY via
  `railway ssh "...inspect(engine).get_columns('table')..."` (the authoritative check) → confirm
  the route is 401 not 500. Re-run migrate if the column is missing; don't trust the log.
- NOT-NULL columns: add with `DEFAULT false NOT NULL` (or a default) so the ALTER succeeds on a
  populated prod table.

### Multi-agent shared-tree: stage only YOUR hunks of a co-edited file
solar-operator is a cron-trap AND multi-agent repo. `git status` routinely shows ANOTHER
agent's uncommitted work in files you also edited (e.g. models.py carries their WeatherLocation
/ GmpDailyGeneration blocks alongside your Bill/Tenant fields). NEVER `git add` the whole file.
Pattern that works (verified this session, multiple times):
  1. `git add` the files that are 100% yours (delivery.py, routes.py, migrate.py, tests).
  2. For a co-edited file, build a patch of ONLY your hunks programmatically: `git diff <file>`,
     split on `@@`, keep hunks whose text contains YOUR identifiers (e.g. `discount_pct`,
     `pdf_bytes`) and drop ones containing the other agent's (`WeatherLocation`), then
     `git apply --cached --recount /tmp/mine.patch`. `--recount` fixes stale hunk line numbers
     (the other agent's inserts shift everything; without it the apply rejects).
  3. VERIFY before commit: `git diff --cached <file> | grep -c "<their marker>"` == 0 and
     `grep -c "<your marker>"` > 0. Then commit only your staged set; `git push origin HEAD:main`.

### Local QA gotchas (cost real iterations this session)
- uvicorn started WITHOUT `--reload` runs STALE code — if a route edit doesn't show, the server
  loaded before your edit. Kill (`pkill -f "uvicorn api.app"`, confirm port free with `ss -ltnp`)
  and restart. Background-process `poll` shows exit_code.
- The local dev backend can get OOM-KILLED (exit 137) mid-QA on a memory-tight box. Symptom in
  the UI: the customer-list fetch 502s through dev_proxy and the SPA shows "Session expired —
  please sign in again" even though the token is valid (global-rate card may still show cached
  data). DIAGNOSE with a direct backend `/health` (000/non-200 = backend down) before chasing a
  fake auth bug. Restart the backend (`exec uvicorn ...`), re-mint the token, re-shoot.
- A long Playwright run can outlive the token; re-mint right before the shot.
