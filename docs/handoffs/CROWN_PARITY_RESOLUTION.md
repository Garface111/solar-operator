# Crown/GMCS monthly parity — RESOLVED (root cause + one-line fix, verified)

**Status:** root cause found and fixed; verified live on prod data. The fix is a
one-line date-format change in the GMP adapter. Deploy is gated on Ford (it
changes generation numbers that feed offtaker billing).

## The real bug (not what the original handoff guessed)

The original hypothesis (writer-time bill proration) was a red herring. The chain:

1. Our GMCS monthly numbers came from `DailyGeneration` rows that were **100%
   `source='bill_prorate'`** — a monthly bill total smeared flat across calendar
   days. Flat smear preserves the quarter total (±0.3%) but flattens the monthly
   peaks Crown reports → per-month REC floors diverge (up to ~4.8 MWh off).
2. Why were we prorating instead of using real metered generation? Because the
   GMP daily-generation **sponge captured nothing** — `GmpUsageRaw` and
   `GmpDailyGeneration` were **empty on every tenant since inception**.
3. Why empty? **`api/adapters/gmp.fetch_usage_csv` sent the date as bare
   `YYYY-MM-DD`.** GMP's `/api/v2/usage/{acct}/download` rejects that with
   **HTTP 400 `INVALID_DATE`**. It requires full ISO-8601 with milliseconds + Z:
   `YYYY-MM-DDT00:00:00.000Z`. So every backfill window silently failed and we
   fell back to bill proration.

GMP has the real 15-minute generation, same login, back to 2015 (verified live).
Crown derives its monthly numbers from exactly this data. **No other data source
is needed** — we were formatting one query parameter wrong.

## The fix

`api/adapters/gmp.py`: format usage-window dates as `...T00:00:00.000Z`
(`_gmp_usage_date` helper). Regression test: `tests/test_gmp_usage_date_format.py`.

## Verified live (2026-07-06, read-only, real prod token)

Fetch with the corrected format → deployed `parse_usage_csv_to_daily` → sum by
calendar month → compare Crown:

| array | 2026-01 | 2026-02 | 2026-03 | vs Crown |
|---|---|---|---|---|
| Chester | 12.67 / 12.61 | 16.98 / 16.92 | 21.11 / 21.06 | Δ ≤ 0.06, **RECs exact** |
| Timberworks | 4.07 / 4.04 | 7.57 / 7.55 | 15.54 / 15.52 | Δ ≤ 0.03, **RECs exact** |
| Waterford | 4.55 / 4.54 | 8.81 / 8.80 | 18.43 / 18.42 | Δ ≤ 0.01, **RECs exact** |

Baseline (flat bill-proration) error was 1.37 MWh mean, up to 4.8. With the fix,
error is ~0.02–0.06 MWh and **every REC floor matches Crown exactly** (12/12,
16/16, 21/21, …). Earlier single-interval check: Chester June-2025 NGEN sum =
26,744.80 kWh vs Crown 26.745 MWh — exact.

## Two follow-ups (do NOT block the fix)

1. **Tannery Brook** (GMP acct 2778764040, "Groton Community Solar LLC 2") returns
   interval data **frozen at ~2017** for every requested window — a stale/
   replaced-meter interval feed at GMP. Its bills still flow, so it falls back to
   bill-proration (no worse than today). Needs Bruce to confirm the current
   account/meter for that array. Not a code bug.
2. **USTND allocation:** the CSV carries a second tiny "Standalone Comm" service
   agreement (~30–40 kWh/mo) summed alongside the real NGEN generation, a ~0.02–
   0.05 MWh/mo over-count. Doesn't affect REC floors. Could filter to the NGEN SA
   later; left as-is to avoid excluding valid generation on differently-named
   meters.

## Deploy note (Ford's call — money gate)

Deploying makes the scheduled sponge start filling `GmpDailyGeneration` with real
metered generation, which the GMCS writer prefers over bill-proration. That
changes generation figures that feed **offtaker invoices/billing**. Recommend:
merge → let the backfill run (or trigger it) → spot-check billing impact before
the next invoice cycle. Historical backfill will walk each meter's full history
in ≤60-day windows (90-day windows 503 — the job already pages at 60).
