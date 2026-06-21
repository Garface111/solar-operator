# Survey the existing code BEFORE building Paul's / any "build all these" feature ask (Jun 2026)

When Ford forwards a customer's feature wishlist ("get started on making all these",
"use CC agents") for the billing / reporting side of Array Operator, the single
highest-leverage first move is **survey what already exists** — DO NOT launch agents
to "build all these" off the bare ask. This codebase already has a deep
billing/reporting module; firing N parallel agents to build from the wishlist
re-implements LIVE code and produces duplicate/conflicting PRs and wasted agent $.

## What is ALREADY BUILT (as of Jun 2026) — do not rebuild
`api/billing/` is a full module (`matcher.py`, `invoice.py`, `delivery.py`,
`summary.py`, `routes.py`) + the `BillingReportSubscription` model (`api/models.py`,
~line 822+, holds the uploaded workbook bytes as the per-cycle source of truth):
- **Percentage-allocation invoicing** — `matcher.py` AI-parses an uploaded customer
  .xlsx → `allocation_pct` + `billing_model` (`percent_of_array` / `fixed_budget` /
  `flat_rate`). Arbitrary per-customer % (the 95%/5% customer-vs-landowner split is
  already supported). `compute_invoice` does the dollar math.
- **Template reproduction (auditable)** — `invoice.py` renders the customer invoice
  as PDF **and** an .xlsx that mirrors the workbook's own Template sheet.
- **Drafted-email-for-approval flow** — `delivery.py` builds attachments, resolves
  recipients via the `send_mode` slider (`to_me` / `to_client` / `to_both`), sends a
  branded Array Operator email via Resend, schedules by cadence (monthly/quarterly).
- **YoY + trailing-12-month NUMBERS** — `summary.build_summary` already computes
  `yoy_delta_kwh`, `yoy_delta_pct`, `ttm_kwh`, and `ttm_points`.
- **Endpoints** under `/v1/array-operator/billing`: `/match`, `/subscriptions` CRUD,
  `/subscriptions/{id}/send-now`, `/subscriptions/{id}/preview`.
- Customers map onto the existing **`Client`** model (tenant → many clients → arrays).

## The GENUINE gaps (what's actually worth building from a typical reporting ask)
1. **Macro multi-year trend-line dashboard VIEW** — the numbers exist (YoY/TTM in
   summary) but there is NO front-end view with overlaid per-year trend lines. This
   is the real net-new UI. Backend: add `summary.build_trends(match) -> dict` +
   `GET /subscriptions/{id}/trends` (monthly_by_year, seasonal_yoy w/ latest_delta_pct,
   ttm/lifetime). Frontend: a trends panel under the existing reports surface
   (`web/app/src/screens/ReportsTab.tsx`, `components/reports/`, `ReportsCard.tsx`).
2. **Seasonal multi-year YoY** — `summary.py` does single-period (latest vs same-month-
   prior-year) only; extend to per-month-across-all-years.
3. **GMP invoice PDF as an attachment** — `delivery` attaches the customer invoice +
   summary but NOT the utility's GMP invoice. PARTLY BLOCKED: the customer (Paul) is
   building the GMP-DETECTION backend himself, so we don't HAVE the GMP PDF yet. Build
   only the DORMANT hook: one additive `BillingReportSubscription.gmp_invoice_pdf`
   (LargeBinary, nullable) column (+ `api/migrate.py` ADD COLUMN) and an attach-if-
   present branch in `delivery.generate_files`. Null → unchanged (the norm today).

## Orchestration recipe that fit this (CC agents, "think through UX at each step")
- Confirm `claude` CLI + auth (`claude auth status --text` → Ford's Max account).
- Author `CONTRACTS.md` YOURSELF first (the data shapes + the UX spec, esp. the
  empty/loading/single-year states — those ARE the deliverable, not afterthoughts).
- TWO agents on DISJOINT file sets to avoid collision: BACKEND owns `api/billing/**`
  + `api/models.py` + `api/migrate.py` + `tests/`; FRONTEND owns `web/app/src/**`
  only and CONSUMES the endpoint, reading the response DEFENSIVELY (backend PR not
  merged when it builds). Neither touches `api/app_dist` / `web/app/dist` — the
  orchestrator rebuilds bundles at integration.
- Isolated worktree per agent off `origin/main`; symlink the shared `.venv`
  (backend, for pytest) and `web/app/node_modules` (frontend) so they don't re-install.
- No chart lib in `web/app` deps → tell the frontend agent to use a dependency-free
  inline SVG chart, NOT add a heavy dep.
- Launch backgrounded `claude -p … --model opus --fallback-model sonnet
  --max-turns 120 --output-format json` with notify_on_complete; ONE liveness check
  at ~60s (etime alive + clean stderr) to catch a dead-on-auth agent early.
- VERIFY each PR yourself (run their tests/build — never trust the self-report),
  integrate, rebuild bundles, deploy. Standard wave discipline.

## The transferable lesson
For ANY "build these features" ask on a mature part of this product: grep/read the
relevant module FIRST and report "X and Y already exist; the real gap is Z" BEFORE
spending agent time. Ford values the honest scope-down — the ask is usually much
smaller than it sounds because the platform already does most of it.
