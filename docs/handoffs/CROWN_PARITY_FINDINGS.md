# Crown/GMCS monthly-parity — VERIFIED findings (2026-07-06)

Read-only investigation on prod (`--service web`, tenant `ten_2274f94eac1050b9`).
No prod writes, no writer change. Supersedes the hypothesis in
`CROWN_PARITY_FIX.md` §2.

## Verdict

The monthly divergence from Crown is a **data-source limitation, not a writer
bug and not a fixable-in-code proration bug.** Do **not** rewrite the GMCS
writer or "improve" proration as the fix — it doesn't help (measured below).

## What the handoff got wrong (breaks its verbatim first-action)

1. **Tenant `ten_14b76982523a3b47` does not exist.** Real tenant is
   `ten_2274f94eac1050b9` ("Bruce Genereaux"). The verbatim command returns
   "0 clients" — a false "no data."
2. **`railway ssh` links to the `gotenberg` service** (no `DATABASE_URL` →
   silent empty fallback DB, all-zero output). Must pass `railway ssh --service web`.
3. **The "minting-quarter lag fix" is NOT merged to main / not deployed.**
   `default_reporting_reference_date` isn't in the deployed writer, so the diag
   script `ImportError`s on prod. Prod's window still uses `date.today()`
   (ends Q2'26). `scripts/diag_crown_parity_prod.py` is the self-contained,
   prod-working version (scores every Crown ground-truth month directly).

## What's actually happening

- The 4 Crown arrays (Chester, Tannery Brook, Timberworks, Waterford) are 100%
  "daily-sourced" in the writer — but every `DailyGeneration` row is
  `source='bill_prorate'`: a monthly bill total spread **flat** across calendar
  days (e.g. Chester = 545.3 kWh *every* day across the Feb→Mar boundary).
  Proration is real, just laundered into the daily table (via
  `api/jobs/bill_to_daily.py`), not applied at write time.
- **No real metered data exists to switch to.** `GmpDailyGeneration` (15-min
  metered daily) and `GmpUsageRaw` (the interval sponge) are **empty across the
  entire DB — 0 rows, every tenant.** No inverters / `InverterDaily` either.
  The only generation signal we possess for these arrays is monthly bill totals.
- **Quarter totals reconcile** with Crown to ±0.3–0.4%; only the monthly split
  diverges (~1.37 MWh/mo mean |Δ|, up to 4.8). Crown measures on a monthly
  revenue-meter cadence that GMP's mid-month net-metering bills can't reconstruct.

## Proof that no code-only interim fix works

Bake-off vs Crown ground truth, mean |Δ| MWh across the 4 arrays (lower=better):

| attribution rule | OVERALL | Chester | Tannery | Timber | Waterford |
|---|---|---|---|---|---|
| **flat proration (current)** | **1.37** | 1.25 | 1.00 | 1.68 | 1.55 |
| insolation-weighted proration | 1.33 | 1.19 | 1.02 | 1.63 | 1.47 |
| whole bill → read (end) month | 2.87 | 2.48 | 2.89 | 3.43 | 2.69 |
| whole bill → start month | 2.19 | 1.84 | 1.30 | 2.69 | 2.95 |
| end −15d / −20d / +10d shifts | 2.19–3.46 | — | — | — | — |

Flat proration (already deployed) is the best of the family. Insolation
weighting improves only +3% overall and **regresses** Tannery Brook — noise,
not a fix, and it would require re-materializing ~15k `bill_prorate` daily rows.
Reproduce: `scripts/diag_crown_bakeoff.py`.

## The only real fixes (both need a new data source — Ford/Bruce call)

1. **Ingest authoritative monthly generation** (the NEPOOL-GIS meter reads Crown
   already has). Exact parity by construction; a small monthly-generation import
   on our side; coordination with Bruce/Crown. Note: a *real* operator likely
   won't have Crown's file — this is a Bruce-testing convenience, not a product
   answer.
2. **Make GMP interval/daily generation capture actually work** and backfill it.
   This is the durable product answer, but the pipeline has produced **0 rows
   ever** in prod — unknown whether GMP even serves interval *generation* for
   these net-metering meters. Real R&D, not a config toggle.

Until one lands, the workbook is quarter-accurate and monthly-approximate, and
that's the honest ceiling for bill-only arrays.
