# Array Operator Reports/billing + the multi-agent READ-CONTRACT pattern

How the Array Operator **Reports** tab (owner-site billing) is built, and the
reusable pattern for being the READ-ONLY CONSUMER half of a feature whose
data-capture half is owned by another agent. Distilled from the Jun 2026 build of
the rate model, source-agnostic kWh, quarterly reports, editable draft email, and
GMP-bill auto-attach.

## Where Reports lives (two repos, one feature)
- FRONTEND: `/root/array-operator/public/reports.js` (+ styles in
  `command-center.css`, base in `styles.css`). Vanilla JS, no build step.
  Mounts under `#reportsRoot` via `window.__aoLoadReports`. Deploy = MANUAL
  `netlify deploy --prod --dir=public` (git push only updates GitHub).
- BACKEND: `/root/solar-operator/api/billing/` — `routes.py` (under
  `/v1/array-operator/billing`), `delivery.py` (the shared send pipeline used by
  BOTH the scheduler and send-now), `matcher.py` (`compute_invoice`,
  `BillingMatch`, `Period`), `invoice.py`/`summary.py` (renderers). Models:
  `BillingReportSubscription` + `ReportDraft` in `api/models.py`. Railway
  auto-deploys on push to main (see the cron/migrate reference).
- Local dev: `python dev_proxy.py 8089` (in array-operator) reverse-proxies
  `/v1/*` to the backend on `:8788` so the static site can hit the API. Run the
  backend with a pinned `SESSION_SECRET` + a throwaway `SOLAR_DATA_DIR` sqlite,
  seed a tenant+array+DailyGeneration, and `mint_session_for_tenant(tid)` →
  drop it in `localStorage.so_session` to sign the Playwright page in. (Stale
  dev sqlite that predates a new column 500s the same way prod does — use a
  FRESH `SOLAR_DATA_DIR` per schema change so `create_all` builds it clean.)

## The billing rate model (Ford's "global default + per-customer override")
Invoice = produced kWh × effective $/kWh. Precedence: per-customer
`BillingReportSubscription.rate_per_kwh` > tenant
`Tenant.default_billing_rate_per_kwh` (global) > legacy VT fallback
(`MANUAL_TARIFF 0.18398 × MANUAL_BILLING_RATE 0.9`). Implementation:
- `delivery.resolve_rate_per_kwh(sub) -> (rate|None, source_label)` does the
  precedence + returns provenance ("customer" / "global" / "vt_default").
- When an explicit rate is set, price kWh DIRECTLY: `tariff=rate, billing_rate=1.0`
  so `compute_invoice` yields `amount_owed == kwh × rate` exactly.
- Stamp `computed["rate_per_kwh"]` + `computed["rate_source"]` so the UI shows the
  auditable "kWh × $rate = $" line and where the rate came from.
- Routes: `rate_per_kwh` on create (Form) + PATCH (use `model_fields_set` so
  explicit `null` CLEARS the override vs. omitted = unchanged); `GET/PUT
  /global-rate`; `_validate_rate` guards 0..5 $/kWh (a fat-finger can't produce a
  wild invoice). Frontend: a "Your default rate" bar (GET/PUT global-rate) + a
  per-customer rate chip + inline editor on each customer card, + a Rate field in
  the manual add form (blank = use default).

## Source-agnostic period kWh with provenance
`delivery._array_period_kwh_sourced(db, array_id) -> (kwh, start, end, label, source)`
PREFERS the GMP daily-read contract, FALLS BACK to DailyGeneration/Bill, and
returns which fed the number (`gmp_api` | `daily_csv` | None). Stamp
`computed["kwh_source"]`; surface it in `preview-math` and the UI ("source: GMP
metered data" / "your uploaded generation data"). NEVER fabricate — when no array
generation exists, `has_data=false` and kWh/amount are null.

## The MULTI-AGENT READ-CONTRACT pattern (the big reusable one)
When a feature's data-CAPTURE is another agent's lane but the CONSUME/render is
yours, do NOT wait and do NOT build their half. Build the consumer against a
read seam:
1. **Call functions, never their tables.** Put a read module in
   `api/reports/<thing>_read.py` that the other agent owns/fills (e.g.
   `gmp_daily_read.py`: `get_daily_series/get_monthly_totals/get_coverage`;
   `gmp_bill_pdf_read.py`: `get_bill_pdf_for_period/has_capturable_gmp_account`).
   The consumer imports ONLY these functions — never the `Gmp*` ORM models or the
   underlying tables. Keeps storage internal to the owner so it can evolve.
2. **Degrade defensively, never raise into the send/render path.** Wrap the seam
   call in `try/except Exception` + `logger.warning(...exc_info=True)` and fall
   back. A PROVISIONAL/missing module or empty tables must produce "nothing
   attached / fell back to other source", never a 500 on a real send.
3. **Be honest about provenance; never fabricate.** If the captured artifact
   isn't there yet, say so in the UI with a truthful status (e.g. draft card
   `gmp_auto_status`: "ready" green = a real PDF will attach / "pending" amber =
   "will attach once captured" / "no_gmp" = "connect a GMP account"). A toggle
   can be ON and still truthfully attach nothing.
4. **Write the contract doc for the owner to fill.** `docs/plans/<THING>_READ_CONTRACT.md`
   (mirror the existing `GMP_DAILY_READ_CONTRACT.md` shape): mark STATUS
   PROVISIONAL/v0, state the ownership boundary, the exact function signatures +
   return dict shapes, and NAME THE GAP the ingestion agent must close (e.g.
   "persist `Bill.pdf_bytes` durably in-row — `Bill.pdf_path` points at Railway's
   EPHEMERAL disk and can't be attached weeks later"). When it lands, the consumer
   lights up with zero further change. Note known blockers (e.g. GMP backfill auth
   was blocked: stale token 401 / refresh 403 → no PDFs/usage until re-captured).
5. **Test the seam with a monkeypatch.** You can't rely on real captured data, so
   prove BOTH branches: monkeypatch the read fn to return bytes → assert attached;
   leave it returning None → assert nothing attached.
6. **If the owner agent stops, Ford may tell you to build their half too.** Then
   implement the persist side against your OWN contract; the seam already reads
   the real column once it exists. DONE for GMP-bill auto-attach (Jun 2026, other
   agent abandoned): added durable `Bill.pdf_bytes` + `pdf_content_type` columns
   (pdf_path was Railway-ephemeral) + migration; `worker._capture_current_bill_pdf`
   fetches the current bill PDF via `gmp.fetch_bill_pdf(currentBillUrlBinary)` and
   persists bytes on the newest bill row — runs best-effort on the JSON-first pull,
   NEVER fails the pull, and VALIDATES `%PDF` magic so an auth-redirect HTML page
   is never stored as a fake PDF. `get_bill_pdf_for_period` now reads the real
   column; `delivery.generate_files` attaches when toggle on + bytes present,
   manual upload as fallback. STILL blocked on a fresh GMP session token (nothing
   pulls until re-captured) — surfaced honestly, never fabricated. Scope = current
   bill only (historical back-capture needs a per-bill PDF URL confirmed in
   raw_json — deferred, GMP auth blocked). Tests: `tests/test_gmp_bill_pdf_capture.py`.

## Paul Bozuwa's invoice workflow (the target the Reports tab serves)
Owner invoices N offtakers for produced power. The flow that's built: draft lands
in an approval inbox (`ReportDraft`, status pending) → operator edits a
PRE-WRITTEN email (`draft.note`, editable textarea; `defaultDraftNote()` seeds it
with period+kWh+amount) → optionally attaches/auto-attaches the GMP bill PDF →
Approve & send delivers via Resend, and the edited note RIDES the email
(`_email_html(note=...)` leads the body, escaped+nl2br). NOTHING auto-sends —
every path ends at a human Approve & send; the UI says so. Quarterly subtab reuses
the Trends charts (Solar Spiral + Energy Ridgeline) via `window.AOTrends`
(`getView(key).mount(host, AOTrends.prep(fleetTrendsData), AOTrends)`) so report
visuals can't drift from the Trends tab. The two subtabs (Invoice generator /
Quarterly reports) are pills sharing the same customer surface, not separate views.

## Verify like the feature demands
Run `pytest tests/test_billing_delivery.py tests/test_report_drafts.py
tests/test_billing_trends.py -q` after backend edits (add a regression test for
each new knob — rate tiers, kwh_source, note-rides-send, auto-attach both branches).
Visual-QA every UI state over the dev_proxy with Playwright + vision_analyze
(Ford's hard rule): screenshot, confirm no clipping/overflow, count chart canvases,
assert 0 console errors. Then commit ONLY your files (see the cron/multi-author
reference for selective staging), push, run prod migrate AFTER the deploy lands,
and MANUAL `netlify deploy` for the frontend.

## SHIP-LOOP TRAPS that bit me repeatedly (cross-cutting, reuse everywhere)
- **Deploy → migrate ORDERING (bit me 3x).** `git push origin HEAD:main`
  auto-deploys the backend on Railway and the new code SELECTs any new ORM column
  IMMEDIATELY → endpoints 500 until migrate runs. But the deploy takes ~60–90s to
  build; if you migrate right after push it runs against OLD code and your ALTER
  block isn't there. The migrate LOG is NOT proof (it no-ops silently if old code
  ran or the col exists). Correct: push → wait ~70s → `railway ssh "python -m
  api.migrate"` → VERIFY the column directly:
  `railway ssh "cd /app && python -c \"from api.db import engine; from sqlalchemy import inspect; print('<col>' in [c['name'] for c in inspect(engine).get_columns('<table>')])\""`
  → confirm the route is 401 (healthy) not 500. NOTE: a new NOT-NULL-with-DEFAULT
  column may get added by `create_all` on deploy before migrate even runs — verify,
  don't assume.
- **Selective-hunk staging when another agent shares your file.** `git add <file>`
  bundles their uncommitted work (e.g. a `WeatherLocation` model in the same
  models.py as your `Bill.pdf_bytes`). For a MIXED file, extract only your hunk(s)
  to a patch and `git apply --cached --recount /tmp/mine.patch` (recount because
  their additions shifted source-side line numbers). Then verify:
  `git diff --cached --name-only` is exactly your files, `git diff --cached | grep
  -c "<their-marker>"` == 0. NEVER `git add -A` in /root/solar-operator.
- **Terminal secret-masker mangles `$(...)`, quotes, and inline `python -c`.**
  Write probes to a `.py`/`.sh` file (or pure-Python urllib) and run them; for
  auth, build a header file once (`printf 'Authorization: Bearer *** > h.txt;
  cat tok >> h.txt`) and `curl -H @h.txt`. `write_file` content occasionally gets
  dropped — re-issue and read back to confirm.
- **Native `<select>` dropdowns render WHITE on the dark theme.** Fix site-wide in
  `styles.css`: `select,option,optgroup,input,textarea{color-scheme:dark}` +
  explicit `option{background;color}`; add the `html[data-theme="day"]` inverse in
  `theme-day.css`. Global base rule, not per-component.
