# Capacity Analysis: 50 and 100 Client Onboarding

_Generated 2026-06-04 via `scripts/multi_client_stress_test.py --dry-run --client-count N`_

## Short answer

**Yes, we can handle both.** 50 clients produces reports in ~1.4 seconds total; 100 clients in ~2.7 seconds. At 27 ms per workbook, report generation is not the bottleneck.

---

## Test results

| Metric | 50 clients | 100 clients |
|---|---|---|
| Arrays | 175 | 350 |
| Utility accounts | 217 | 433 |
| Bills seeded (18 months) | 3,906 | 7,794 |
| DB seed time | 0.5 s | 0.9 s |
| Workbook generation total | 1.4 s | 2.7 s |
| **Per-client workbook** | **~27 ms** | **~27 ms** |
| Total file size (all xlsx) | ~400 KB | ~800 KB |

Scaling is linear: doubling clients doubles arrays and bills, but report generation time doubles cleanly too (~27 ms/client is constant). No combinatorial blowup.

---

## What the test actually exercises

- Seeding: creates Tenant → N Clients → ~3.5 arrays/client → ~1.2 accounts/array → 18 months of bills each
- Report generation: runs the real `gmcs_writer.build_workbook()` for every client — the same code path that runs on the quarterly scheduler tick
- Verified: each workbook contains only its client's arrays (no cross-client sheet leakage), correct sheet count matches array count

Test ran on local SQLite. Railway Postgres will have higher per-query latency (~2–5 ms vs <1 ms) but also better read parallelism, so real-world numbers should be in the same order of magnitude.

---

## Where it would break

1. **Email delivery, not report generation.** Resend rate-limits outbound emails. At 100 clients sent quarterly, that's ~400 emails/year — well within Resend's limits. But if we fan out all 100 at once (one scheduler tick), 100 simultaneous Resend API calls could hit rate limiting. The current `deliver_for_client` loop is sequential; at 100 clients × ~200 ms/Resend call = ~20 seconds per quarterly run. Acceptable, but worth watching.

2. **Scheduler tick duration.** APScheduler fires the quarterly tick on a single thread. At 100 clients, sequential delivery takes ~20 s (Resend latency dominates). At 500+ clients this would need a thread pool or Celery worker.

3. **Bill storage.** 100 clients × 3.5 arrays × 1.2 accounts × 18 months = ~7,600 bill rows. After 5 years that's ~25,000 rows per 100-client cohort. PostgreSQL handles millions of rows trivially; no concern here.

4. **Excel file size.** Each GMCS workbook is ~8 KB. 100 workbooks = ~800 KB in memory at once. Fine.

---

## Recommendation

We are comfortable at 50 and 100 clients with the current architecture. The practical limit before needing concurrency changes is roughly **300–400 clients** where the sequential email fan-out approaches the scheduler tick interval. Flag this if/when we hit 200 active clients.
