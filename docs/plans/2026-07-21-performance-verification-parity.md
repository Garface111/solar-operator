# Performance Verification Parity (Sunreport P0/P1)

**Date:** 2026-07-21  
**Scope:** Array Operator owners only. Monthly report on the 1st.  
**P2:** thin stubs + docs only (except auditor CSV ships with P0).

## Non-goals
- Do not change peer_analysis, offtaker invoices, NEPOOL GenReports, or capture.
- Do not fabricate expected/measured energy.
- Do not force multi-tenant O&M UI this wave.

## Package layout
```
api/verification/
  __init__.py
  standards.py       # IEC 61724-aligned method text + report footer
  boundary.py        # meter-primary measured energy selection
  series.py          # per-array daily measured/expected/PI series
  persistence.py     # sudden | persistent | seasonal + priority score
  causes.py          # soiling|shading|environmental|electrical|availability|data_quality
  availability.py    # all-in vs in-service energy, downtime hours
  intervention.py    # post-repair recovery check vs expected
  report_pack.py     # HTML+PDF monthly pack + email
  auditor.py         # CSV + assumptions JSON export
  engine.py          # orchestrate portfolio verification snapshot
  routes.py          # FastAPI endpoints under /v1/array-owners/verification/*
  p2_stubs.py        # O&M multi-tenant + SLA packaging stubs
api/jobs/verification_monthly.py  # 1st-of-month scheduler
tests/test_verification_*.py
docs/knowledge/performance-verification-method.md
```

## Measured energy boundary (P0 #4)
Priority for each (array, day):
1. **meter** — utility real sources (`gmp_api`, `gmp_portal_scrape`, `smarthub`) if present
2. **inverter** — vendor/extension/csv/manual measured sources
3. **unavailable** — no clean measured day

Never use `bill_prorate` / `utility_meter` estimates for verification PI.

Badge every PI with `boundary: meter|inverter|mixed|unavailable`.

## Expected energy
Reuse `api.forecasting.build_forecast` / POA model. Do not reimplement weather math.
Default PR = forecasting.DEFAULT_PR (0.84) unless array.performance_ratio set.
Degradation: document as 0% default unless `Array` gains a field later (do not invent a column this wave unless tests need it — use optional inputs only).

## Persistence classifier (P0 #2)
On a daily residual series `r[d] = (actual - expected) / expected` for matched days:
- **sudden**: last 1–2 days r < -threshold AND prior 7-day median near 0
- **persistent**: ≥N consecutive days (default 5) with r < -threshold
- **seasonal**: same calendar month last year (if enough history) shows similar underperformance pattern; else None
- threshold default **0.05** (5%), configurable via tenant setting JSON key `verification_deviation_threshold` (optional; default 0.05)

Priority score = `magnitude_mean_abs * duration_days * (1.5 if sudden else 1.0)` clamped 0–100.

## Cause taxonomy (P1 #5)
Heuristic labels (never claim certainty):
- `electrical` — peer_index fault/dead or vendor fault code present
- `availability` — comm_gap / multi-day zero while expected high
- `shading` — inverter marked expected_low OR afternoon-only pattern if available
- `soiling` — persistent underperformance without fault, sunny days worse residual
- `environmental` — underperformance only on extreme weather (optional weak signal)
- `data_quality` — sparse measured days, boundary mixed, low confidence

## Availability (P1 #7)
- `in_service_hours` estimate: days with actual>0 or inverter ok / window days * 24
- `all_in_energy` = sum actual (incl. zero days with expected)
- `in_service_energy` = sum actual on days without comm_gap/dead status if known
- `availability_pct` = in_service_hours / window_hours (honest null if no inverter status)

## Intervention (P1 #6)
When RepairTicket or WarrantyClaim moves to `resolved`:
- snapshot PI for 14 days **before** resolution date and 14 days **after** (if enough days)
- recovery_delta = pi_after - pi_before
- store on ticket as optional JSON column `verification_recovery` OR compute on read only (prefer on-read first; add column only if needed)

## Report pack (P0 #1)
Monthly (1st, ~13:00 UTC): for each active `array_operator` tenant with monitoring:
- Build portfolio verification for **previous calendar month**
- PDF via reportlab + `_pdf_brand` day skin
- Email HTML summary + PDF attach (Resend via notify)
- Opt-out: tenant setting `verification_reports_enabled` default **True** for AO with monitoring; respect `False`
- Hold if fleet entirely stale (mirror digest hold honesty)

Contents: portfolio PI/PR, deviation table per array, boundary badges, assumptions, IEC footer, method link text.

## Auditor export (P2 #9 ships now)
`GET .../verification/auditor-export?start=&end=` → zip or multiparty:
- `assumptions.json`
- `daily.csv` (array_id, day, measured, expected, pi, boundary, residual)
- `summary.json`

## API
- `GET /v1/array-owners/verification/summary?window_days=30`
- `GET /v1/array-owners/verification/arrays/{array_id}`
- `GET /v1/array-owners/verification/report?period=YYYY-MM` (JSON metadata)
- `GET /v1/array-owners/verification/report.pdf?period=YYYY-MM`
- `GET /v1/array-owners/verification/auditor-export?start=&end=`
- `GET /v1/array-owners/verification/method` (standards text)
- `POST /v1/array-owners/verification/settings` optional threshold/enabled
- Intervention: `GET .../repairs/{id}/verification` or include in repair detail

## Frontend (minimal, don't break Analysis)
- analysis-performance.js: show boundary badge when `ctx.verification` present
- analysis.js or new `analysis-verification.js` section: deviation classification + priority
- Optional link "Download verification pack" → PDF endpoint
- Self-register AnalysisSections pattern only

## Scheduler
`api/scheduler.py`: monthly cron day=1 hour=13 id=`verification_monthly_reports`

## Tests (required)
- boundary meter beats inverter on same day
- excludes bill_prorate
- persistence sudden vs persistent
- priority score monotonic in duration
- causes electrical when fault
- report pack builds with empty fleet (honest empty)
- auditor CSV has header + no NaN invent
- pure functions offline (no network): inject POA/actual series

## Support map
Update `api/energy_agent_support_map.md` analysis + new `verification` section.
Agent tool `production_forecast` may surface verification summary fields later (optional this wave).
