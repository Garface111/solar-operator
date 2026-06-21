# Deriving billing rates/pricing from CAPTURED data (never fabricate)

When Ford asks for a rate/price/number that drives a real invoice to a real
offtaker, the iron rule is: **measure it from data we already captured; never
invent it.** Wrong numbers = wrong invoices to Paul Bozuwa's actual customers.
This session built the Array Operator billing pricing stack end to end on that
rule. Capture the reusable patterns here.

## The single most valuable move: rates come FROM the bills
We capture 27k+ GMP bills. The real blended retail $/kWh is DERIVABLE from each
bill's line items — do NOT hardcode a guessed VT rate.
- Method (`api/rate_schedule.blended_rate_from_bill`): per bill segment, take the
  `CONSUMED` KWH line and sum the POSITIVE-dollar `NET` energy-charge KWH lines;
  blended = charges / consumed. Guard band `0.05 < rate < 0.50` rejects parse noise.
  (Net-metering EXCESS/credit lines are negative and excluded — they'd corrupt the
  gross retail rate.)
- Aggregate with the MEDIAN over a cell (robust to outliers), min-sample gated
  (≥8 bills) — too few bills → return None and FALL BACK honestly, never guess.
- Validated live: median recent GMP blended rate ≈ $0.197/kWh. The biennial
  progression is real and visible in the data (2022→24 $0.177, 24→26 $0.188,
  26+ $0.210, n=460/478/129) — you don't hardcode the trend, you MEASURE it.

## Effective-date schedule (rates reset ~every 2 yrs) — self-updating
`RateSchedule(state, utility, location_class, age_bucket, effective_start,
effective_end, blended_rate_per_kwh, sample_size, source_note, is_provisional)`.
- Resolver picks the row whose effective window contains the billing MONTH,
  matching utility + location + age, **specificity-ranked** (exact → wildcards),
  newest effective_start wins. Verified: a 2023 invoice gets the 22–24 rate, a
  2026 invoice gets the 26+ rate, automatically by date.
- Biennial update needs NO code change: add the next window row (or re-run the
  refresh) and the resolver rolls over when billing months enter it.
- Age rule = VT 10-yr net-metering adder boundary: bucket `le11` (≤11 yr since
  `Array.first_connect_date`) vs `gt11`. Don't hardcode the adder — MEASURE each
  bucket from its bills so the step falls out of real data.
- Refresh = `refresh_rate_schedule(db)` (idempotent upsert per utility×window×age),
  exposed at `POST /admin/rate-schedule/refresh`. Run after a bill pull.

## Inputs already in the model (don't add capture for these)
utility=`UtilityAccount.provider`; location=`Array.region`(+ZIP via weather_location);
age=`Array.first_connect_date`; month=billing period. The pieces existed — the
gap was the schedule structure + resolver, not new ingestion.

## Provenance is mandatory, fallback is honest
Every resolved rate carries a `source` (`schedule` | `schedule_provisional` |
`vt_default`) + human note ("VT blended · GMP · age le11 · eff 2024-01–2026-01").
No schedule cell → documented provider default in `api/rates.py` (get_energy_rate),
surfaced in the UI as a "VT default" badge, NOT silently. Cells with <8 bills are
skipped and fall back — never fabricated. The frontend shows
"Net rate $X − N% = $Y" + a source badge so the number is never an invisible default.

## Discount billing model (the layer the rate feeds)
Invoice = produced kWh × net_rate × (1 − discount). Default discount 10% off.
- `delivery.compute_invoice` already had `billing_rate`; setting it to (1−discount)
  makes `amount_owed` and `solar_savings` fall out exactly — reuse, don't reinvent.
- Net-rate precedence: customer override → operator global → AUTO schedule →
  legacy flat rate → VT default. Discount precedence: customer → global → 10% default.
- Legacy flat `rate_per_kwh` stays supported: treated as net with 0 discount so
  existing customers' dollars are byte-unchanged. Always keep the old field working.
- Validate discount ∈ [0,1) (reject ≥1 — would zero/inverse the bill).
- UI: whole-% in the box ↔ fraction in the DB (divide/multiply by 100 at the seam).

## GMP bill-PDF auto-attach (companion feature, same session)
- Durable storage: `Bill.pdf_bytes` (+`pdf_content_type`). `pdf_path` is Railway
  EPHEMERAL — persist BYTES in-row or attachment breaks after a redeploy.
- Capture on the JSON-first pull via `_capture_current_bill_pdf` (worker.py):
  `gmp.fetch_bill_pdf(currentBillUrlBinary)` → validate `%PDF` magic (an auth
  redirect returns HTML; never store HTML as a PDF) → write bytes onto newest bill.
- Read seam `api/reports/gmp_bill_pdf_read.get_bill_pdf_for_period` + per-customer
  `auto_attach_gmp` toggle; manual upload kept as fallback; honest status
  (ready/pending/no_gmp) — never implies a PDF exists when it doesn't.
- REAL pull result (verified): JSON API 401'd but the PDF redirector accepted the
  token → 59/65 accounts, 61 durable PDFs. The 6 fails were PRE-EXISTING data
  issues (MultipleResultsFound on duplicate bill rows; stale 1.1KB HTML redirector
  pages), NOT the new code.

## Native <select>/dropdown dark-theme fix (AO site-wide)
Option popups render white on dark themes because nothing sets `color-scheme`.
Global fix in styles.css: `select,option,optgroup,input,textarea{color-scheme:dark}`
+ explicit `select option{background:#0e131c;color:#eaf0f7}`; day-theme override
flips to `color-scheme:light`. Verify via computed `getComputedStyle(el).colorScheme`.

## Ship checklist that worked repeatedly this session
1. AST + `from api.app import app` import check, then pytest the billing suites.
2. When the pricing MODEL changes, EXPECT old rate tests to fail on the new
   default — update their expected numbers (e.g. unified VT default shifted
   0.18398→0.21 once the resolver routed through get_energy_rate). That's a real
   behavior change to encode, not a regression to suppress.
3. Visual-QA every UI change over localhost http (dev_proxy :8089 → backend :8788),
   Playwright screenshot + vision_analyze. Re-mint the token if a shot shows
   "Session expired"; if the backend exited 137 (OOM on the probe box) just restart
   it — that's a local-box limit, not a prod issue.
4. Commit ONLY your hunks in the shared solar-operator tree (sibling agents + the
   cron auto-commit). Stage clean files with `git add <file>`; for a file you SHARE
   with another agent (models.py with their WeatherLocation block), build a
   single-hunk patch and `git apply --cached --recount`, then assert their class is
   NOT staged (grep -c == 0) before committing.
5. Push → wait ~75s for Railway → run `python -m api.migrate` → the migrate LOG
   often doesn't print the new column/table (deploy still building), so VERIFY the
   column/table directly via railway-ssh inspect, re-run migrate if missing →
   confirm route is 401 not 500. AO frontend = manual `netlify deploy --prod --dir=public`.
6. After a schema feature lands, SEED/refresh derived tables from prod data
   (`refresh_rate_schedule`) and verify the resolver returns measured (not default)
   values before declaring it live.

## First-run setup WIZARD (guided sequential onboarding)
When Ford wants the first click into a tab to be a guided data-collection flow
instead of a bare screen (e.g. "redo what happens when you click Reports"):
- Drive the WHOLE multi-step flow from ONE aggregator endpoint
  (`GET /billing/setup-state`: arrays + age/utility/region + auto-resolved rate +
  what's MISSING, `has_customers` flag, global rate/discount defaults). One call,
  no per-step round-trips for read.
- Each step PERSISTS via the SAME endpoints the normal tab uses (PATCH arrays for
  install year→`first_connect_date`→rate buckets, PUT global-rate, POST
  subscriptions) so wizard and tab never drift.
- Gate on `has_customers` (zero → show wizard); add a JS `FORCE_TAB` guard so
  finishing/skipping shows the normal tab, plus a "⚙ Setup" link to reopen.
- Don't make them re-enter auto-discovered data — CONFIRM arrays, only ASK for the
  genuinely-missing piece (array age). Step order that worked: ① arrays+age →
  ② accept rate+discount (auto-defaults shown, live effective preview) →
  ③ add customers (repeatable) → ④ review & finish. Preserve "nothing emails
  automatically — you review every draft."
- New endpoints (no schema change) verify live as 401 (exists+auth) or 405 (route
  registered, wrong method) — NOT 404 (= deploy still building, wait & recheck).

## Sitewide LABEL rename (Ford asks "rename X to Y sitewide")
Recurring request ("rename net rate → solar credit rate", "change customers →
offtakers"). The iron rule: rename ONLY user-facing DISPLAYED strings; NEVER
touch identifiers — doing so breaks the API contract / DOM wiring for zero
user benefit.
- CHANGE: HTML/text labels, headings, placeholders, button text, option text,
  status/error messages, tooltips (`title=`), confirm() prompts, and the strings
  baked into generated invoices (PDF + xlsx in `api/billing/invoice.py` — those
  are customer-facing too, not just the UI).
- DO NOT CHANGE: field/API keys (`customer_name`, `net_rate_per_kwh`,
  `has_customers`), element IDs (`#rbqCustomer`, `#rbSubCustomers`), routing
  attrs (`data-sub="customers"`), JS function/var names (`renderCustomers`,
  `WIZ.customers`), and code comments (harmless, leave them).
- Workflow: `search_files` the whole term, then read each hit and classify it
  display-vs-identifier before editing. Many lines mix both
  (`<b>${esc(c.customer_name)}</b>` in a chip whose label changes) — edit the
  label, keep the field. A fallback literal IS displayed:
  `m.customer.name || "a customer"` → keep `.customer.name`, change `"a customer"`.
- Verify: `node -c public/reports.js` (catches a stray `</p>` etc. from a
  fat-fingered patch), then `curl` the live JS and grep — expect the new term
  present and 0 displayed old-term; a remaining grep hit that's a `//` comment is
  fine. Visual-QA one screen to confirm rendering.
- These are usually frontend-only (reports.js + invoice.py if invoices touched) —
  no schema/migration; AO = `netlify deploy --prod --dir=public`.

## Extra local-QA traps (beyond OOM-137 / Session-expired)
- STALE uvicorn still bound to :8788 → your new bg uvicorn silently fails to bind
  and the OLD code serves (tell: openapi.json missing your new routes / 404 on a
  route you just added). Fix: `ss -ltnp | grep 8788` → `kill <pid>` → confirm
  "free" → restart with `exec uvicorn …`.
- dev_proxy.py 501s on PUT/PATCH (local only) — a wizard's PUT/PATCH 501s through
  :8089 but works against the backend directly and on prod Netlify. Verify
  mutating endpoints by calling the backend directly, not via the proxy.
