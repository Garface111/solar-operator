# Data unification: the per-array daily timeline + Bill→daily transformer

The "ultimate data transformer" architecture. How the four production data
sources unify into ONE per-array daily stream the frontend reads, and the
recurring trap where data is captured + parsed but never TRANSFORMED into the
form the UI integrates.

## The TWO GMP streams (do not confuse them)
1. **`Bill`** — monthly utility STATEMENTS. Rich: `kwh_generated`, `total_cost`,
   `kwh_consumed`, `net_credit`, `avg_rate_cents_kwh`, full `raw_json` sponge.
   This is what the bill-pull worker (`worker.pull_bills_for_tenant`) captures;
   ~47k rows in prod (Jun'26), ~34k with cost, ~27k with rate.
2. **`GmpDailyGeneration`** — the GMP API 15-min-interval series rolled to daily.
   Populated by the 05:00 `gmp_daily_backfill` job. **Was 0 rows everywhere** for
   AO — only fills when a GMP session/account is captured + linked to an array.

## The frontend reads the DAILY streams, NOT the Bill table
Trends / fleet totals / 30-day bars / month×year (`array_owners.py` trends path)
merge **`DailyGeneration` + `GmpDailyGeneration`** per day. They never SELECT the
`Bill` table for production. So 47k parsed bills produced ZERO frontend value —
classic "captured + parsed but not transformed into the integrated form." Same
class as the GMP-backfill "never happened" and the `source_status` fleet-store
strip: the pipe is built up to storage then stops short of the unification layer.
When data "isn't showing", ask WHICH table the read path consumes vs. which table
the capture WROTE — the gap is usually between them.

## The per-array daily timeline + source priority (the merge)
All sources flow into one `DailyGeneration`-keyed per-day timeline (the
`(array_id, day)` UNIQUE constraint = one row per array per day). Priority when
multiple sources cover a day — REAL METERED DATA ALWAYS WINS, coarsest fills gaps:
  inverter telemetry (solaredge/fronius/sma/chint/extension_pull) > CSV/manual >
  GMP-API 15-min (gmp_api) > **bill_prorate** (coarsest gap-filler).
`_source_family` (array_owners.py) maps raw `source` → display family; the
frontend already renders/legends a `bill` family as "Bill (prorated)".

## Bill→daily transformer (`api/jobs/bill_to_daily.py`)
Fills the missing link: prorates each bill's `kwh_generated` EVENLY across its
service days into `DailyGeneration` rows with `source="bill_prorate"`.
- ONLY fills days no real source covers; never overwrites a real reading (checks
  existing `source` against `_REAL_SOURCES`; an older `bill_prorate` row CAN be
  refreshed by a newer bill). The unique constraint + source check = real-wins.
- Multi-meter arrays: sum each meter's per-day prorate for the day.
- Idempotent; multi-year (a 13-yr bill history reconstructs to ~5k daily rows/array).
- Entry points: `transform_array_bills(db, array_id)`,
  `transform_tenant_bills(tenant_id)`, `transform_all_tenants()`.
- Wired: nightly **05:30 UTC** (AFTER the 05:00 gmp_daily_backfill so granular
  GMP-API days land first) + admin triggers `POST /admin/bill-to-daily/tenant/{id}`
  and `/admin/bill-to-daily/all` (both `_require_admin`).
- PROVEN on prod Jun'26: 16,285 bills → 333,791 bill_prorate days across 242
  arrays, 104 days correctly SKIPPED (real readings present). Verify rows landed
  via `DailyGeneration.source=='bill_prorate'` count + that `_source_family`
  returns `'bill'`.

PITFALL — prorating is even-spread (monthly truth shown daily), NOT real daily
shape. Where granular GMP-API or inverter data exists it wins and you get true
shape; bill-prorate just makes years of otherwise-blank history visible. Say this
honestly; don't claim daily granularity it doesn't have.

## Why AO arrays still show nothing (the real remaining gap)
Transforms make bills SURFACE the moment they're captured — but AO tenants have
0 GMP utility accounts linked to their arrays (Starlake/Timberworks/Tannery Brook),
so 0 bills, so nothing to transform. The 6-hourly `enqueue_pull_for_all_tenants`
+ 05:00 backfill RUN for them but have no captured GMP session to pull. The
missing human step: log into GMP via the extension WHILE signed in as the AO
tenant → `/v1/sync` stores session+accounts → autopop links account→array →
bills pull + extract → transforms light up Trends. It is a CAPTURE/LINK gap, not
a pipeline gap (the pipeline is fully built and proven by the 47k NEPOOL bills).
