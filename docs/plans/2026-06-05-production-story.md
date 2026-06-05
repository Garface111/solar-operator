# Solar Production Story — Lift Paul's Insight Into the Dashboard

## Why
Paul (network contact) confirmed: utility APIs expose USAGE not GENERATION.
The bill is the only authoritative source for net-metered production.
Solar Operator already scrapes this (GMP bill PDF, VEC SmartHub chart).

We have the raw data nobody else can easily get. Currently it lives in
the GMCS workbook and nowhere else in the UI. Light it up so the
operator can SEE the production story in the dashboard.

## What
Per-client dashboard widget showing solar production trends — the
"holy shit you have all my data" moment.

### MVP scope
1. **Per-client monthly production chart** — bar chart, last 12 months,
   MWh on Y axis, month labels on X. One bar per month, summed across
   all arrays for that client.
2. **Big-number stats above the chart**:
   - Last 30 days: X.XX MWh (vs previous year same period: ±Y%)
   - Last 12 months: X.X MWh (vs previous TTM: ±Y%)
   - YTD: X.X MWh
3. **Drill in** — click a month bar to see per-array breakdown for
   that month (popover or expandable row).
4. **Empty / partial states** — first month of data shows "more
   coming as bills arrive". Year-over-year requires 13+ months;
   show "—" until we have it.

### Tasks
1. `GET /v1/account/clients/{client_id}/production?months=12` —
   returns array of {month: "YYYY-MM", mwh: float, by_array: [{array_id, mwh}]}
2. Use existing Bill data (kwh_generated, period_start, period_end) +
   Array.bill_offset_months to map kWh→month.
3. New component `web/app/src/components/clients/ProductionChart.tsx`
   — minimal Recharts or hand-rolled SVG bar chart (NO new heavy deps).
4. Embed in ClientCard expanded view.
5. Tests: `tests/test_production_endpoint.py` — synthetic Bills across
   18 months for two arrays, verify monthly aggregation + YoY math.
6. `./build_app.sh` + commit api/app_dist/.

### Constraints
- DO NOT add Recharts or any heavy chart lib unless absolutely needed
  — try hand-rolled SVG (~50 lines) first since we're warm/handmade.
- Respect Bruce's `bill_offset_months=0` (Starlake same-month).
- Respect `Array.excluded` (Pittsfield).
- DO NOT touch gmcs_writer.py.
