# Reports Backend — 3 Endpoints to Light Up Quarter Cards

## Goal
Reports tab now shows 6 quarter cards (Q1–Q6 from current date) but the
3 backend endpoints don't exist yet, so all cards show the same data
and only the most-recent timestamp is real. Build the 3 endpoints so
each card shows accurate per-quarter status, count, and time.

## Endpoints to add

### 1. GET /v1/account/reports?quarters=6
Returns the last N (default 6) quarter snapshots for the authed tenant:
```json
{
  "reports": [
    {
      "quarter": "Q1-2026",
      "year": 2026,
      "quarter_num": 1,
      "status": "sent" | "ready" | "draft" | "empty",
      "array_count": 7,
      "last_generated_at": "2026-04-10T14:22:00Z",
      "last_delivered_at": "2026-04-10T14:25:00Z",
      "mwh_total": 84.2
    },
    ...
  ]
}
```

`status` derivation:
- `sent` — there's a Delivery row for this tenant + quarter
- `ready` — generated workbook exists but not delivered
- `draft` — arrays exist but no workbook generated yet
- `empty` — no arrays had any data for this quarter

Look for existing models: Delivery, ReportRun, or similar. If no
report-history table exists, derive from Delivery + Bill rows.

### 2. GET /v1/account/clients/{client_id}/report.xlsx?quarter=Q1-2026
Returns the historical xlsx for a specific client + quarter. Builds
on demand using existing `build_workbook(client_id=..., quarter=...)`.
If quarter parameter omitted, returns the current rolling-6-quarter
workbook (existing behavior).

If client_id is omitted (i.e. `/v1/account/reports.xlsx?quarter=...`),
return a multi-sheet workbook covering all clients for that quarter.

### 3. POST /v1/account/regenerate
Body: `{"quarter": "Q1-2026", "client_id": "cli_abc"?}`
Triggers a fresh workbook build for the requested scope. If
`client_id` provided, regen just that client; else regen all.
Returns `{"status": "regenerated", "generated_at": "..."}`.

## Tasks

### Task 1 — Data audit
- Read `api/models.py` to find Delivery, Bill, Array, Client schema.
- Read `api/writers/gmcs_writer.py` build_workbook signature.
- Read `api/account.py` existing endpoints in this namespace.
- Identify if `build_workbook` already accepts a quarter parameter; if
  not, what would it take to add one (it must respect Bruce's
  bill_offset_months=0 special case for Starlake).

### Task 2 — Implement /v1/account/reports
- Add to `api/account.py` (or wherever the namespace lives).
- Logic: derive quarters from current date going back N. For each,
  count arrays that had Bill rows in that quarter, sum MWh, check
  Delivery table for sent/ready.

### Task 3 — Extend /v1/account/clients/{id}/report.xlsx
- If endpoint already exists, add the `quarter` query param.
- If it doesn't, add it. Use `build_workbook` with quarter scope.

### Task 4 — POST /v1/account/regenerate
- Calls into the existing regen path. Updates account state so
  frontend can re-fetch.

### Task 5 — Wire frontend
- Edit `web/app/src/lib/api.ts` — add typed wrappers for all 3.
- Edit `web/app/src/components/reports/QuarterCard.tsx` to use the
  new data shape (per-card status/count/time/mwh).
- Edit `web/app/src/screens/ReportsTab.tsx` to call
  `GET /v1/account/reports` on mount and pass each report obj to its
  card.

### Task 6 — Tests
- `tests/test_reports_endpoint.py` — happy path, empty state, mixed
  states across 6 quarters.
- Use synthetic non-Bruce data.

### Task 7 — Build
- `./build_app.sh` (CRITICAL: this copies web/app/dist → api/app_dist
  so Railway serves the new bundle).
- Commit api/app_dist/ as part of the same change.

### Task 8 — Verify
- `pytest tests/test_reports_endpoint.py tests/test_gmcs_writer.py -v`
- `pytest tests/ -x --tb=no` full sweep green.
- `tsc --noEmit` clean in web/app.

Commit per task. Do NOT push. 5-line summary at the end.

## Constraints
- DO NOT touch `api/writers/gmcs_writer.py` body — sacred file (footnote
  text verbatim, Bruce's pixel format). May ADD a quarter param to
  build_workbook signature if needed, but format invariants stay.
- DO NOT touch billing, extension, onboarding screens.
- TS strict, no `any`.
- Python type hints on public functions.
- Reuse existing helpers (Delivery, build_workbook, etc.) — don't
  reimplement quarter math from scratch.
