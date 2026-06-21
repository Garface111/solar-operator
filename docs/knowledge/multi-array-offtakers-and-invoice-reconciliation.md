# Multi-array offtakers + invoice↔GMP-bill reconciliation

Two billing-build patterns from Jun'26, both on top of the manual (workbook-less)
subscription path (`BillingReportSubscription`, delivery.build_manual_match).

## Multi-array offtakers (one offtaker owns a share of SEVERAL arrays)
ONE combined invoice summing each array's (period kWh × that array's pct), with a
per-array breakdown line. Built additively over the legacy single-array path.
- Model: NEW `BillingReportSubscription.array_allocations` JSON column =
  `[{array_id, allocation_pct}]`. NULL/empty → legacy single `array_id`/
  `allocation_pct` path runs UNCHANGED (back-compat). Migrate idempotently
  (`ALTER TABLE ... ADD COLUMN array_allocations JSON`, works sqlite+PG).
- Delivery: `build_manual_match` checks `_normalized_allocations(sub)` FIRST; if
  present, sums per-array `(period kWh × pct)` into `computed_invoice["kwh"]` and
  attaches `array_breakdown` (one dict per array). Keep the legacy single-array
  branch below it.
- Invoice PDF: a "Your share by array" table (one line per array → summed Total)
  rendered when `len(array_breakdown) > 1`. GOTCHA: the renderer rebuilds `inv`
  via `invoice_for_period`/`compute_invoice` — it does NOT carry custom keys from
  `computed_invoice`. Thread `array_breakdown` through `invoice_for_period`'s
  `inv.update({...})` (read from `match.project_totals` OR
  `match.computed_invoice`) or the table silently won't render.
- Endpoint `/subscriptions`: add `array_allocations: Optional[str] = Form()`
  (JSON string); `_create_manual_subscription` parses + validates each row,
  stores `array_allocations=(allocs or None)`, keeps `array_id`/`allocation_pct`
  = first alloc for list-view back-compat. `_sub_dict` exposes the new field.
- UI step 3 (reports.js): checkbox list of arrays, each with its own `% of array`
  input (enabled on check). Finish sends `array_allocations` JSON when >1 array,
  else legacy fields. Row display shows all arrays. Wizard QA hooks added:
  `window.__rbWizGoto(n)` + `window.__rbRenderWizard(state)` to jump straight to a
  step with stubbed setup-state (the wizard gates on `authHeaders()`, so a fake
  `so_session` won't render it — use the render hook instead).

## Invoice↔GMP-bill reconciliation (`api/billing/reconcile_bills.py`)
READ-ONLY trust check before sending: per offtaker, per array, compare OUR
invoice's produced kWh (per period) vs the captured GMP `Bill.kwh_generated` for
the same array + overlapping period.
- Verdict per array: `match` (within 1 kWh or 1%) | `mismatch` (with delta kWh +
  %) | `no_bill` (no GMP bill linked — honest, NEVER fabricated) | `no_invoice_data`.
- Compares ARRAY produced-kWh (before the offtaker's %) so it isolates "is our
  production number right vs the meter" from allocation.
- `_bill_for_array_period` matches a bill whose `period_end` is within ±20 days of
  the invoice period_end, else latest. Handle the datetime/date mix in a
  try/except (`Bill.period_end` is datetime; invoice period is date).
- Endpoint `GET /v1/array-operator/billing/reconcile-bills` (tenant-session
  gated). Mutates nothing.

PITFALL (recurring): on the AO tenants this reconcile returns all `no_bill`
because their arrays have 0 linked GMP bills (capture/link gap — see
data-unification ref). The tool is correct; the data isn't there yet. Don't read
"no_bill everywhere" as a bug.
