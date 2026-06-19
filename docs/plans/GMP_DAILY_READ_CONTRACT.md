# GMP Daily-Generation READ Contract  (data-sponge ↔ Reports)

> **STATUS: PROVISIONAL (v0).** Ford is supplying the exact table + query-function
> contract that must be **identical** in the Reports agent's prompt. This document
> + `api/reports/gmp_daily_read.py` are the single source of truth for that contract
> on the data side. When Ford's wording lands, update BOTH and notify agent-3.

## Ownership boundary
- **Owned by the data-sponge side** (this agent): the tables `gmp_usage_raw` and
  `gmp_daily_generation`, the backfill job, and the read module
  `api/reports/gmp_daily_read.py`.
- **Reports agent (agent-3) is a READ-ONLY consumer.** It calls the functions in
  `gmp_daily_read.py` and must **never** import the ORM models or query the
  `gmp_*` tables directly. This keeps storage internal and free to evolve.

## Storage model (why two tables)
| table | grain | role |
|---|---|---|
| `gmp_usage_raw` | one row per (account, fetched window) | **THE SPONGE** — verbatim CSV GMP returned. Authoritative, re-derivable, attached to invoices later. |
| `gmp_daily_generation` | one row per (account == GMP meter, calendar day) | Modeled/queryable. `kwh` = Σ that day's real 15-min interval Quantity. `source='gmp_api'`. |

A GMP **account == one meter/ServiceAgreement**. An **Array may sum several meters**
(e.g. Bruce's Starlake = 3 sub-meters). The per-array read functions therefore SUM
across the array's accounts per day. Stored per-account to avoid an `(array_id, day)`
collision and keep the raw→modeled mapping 1:1.

> This is intentionally SEPARATE from the existing `daily_generation` table (CSV
> uploads, per-array). Mixing them risked silently changing live report numbers.

## The functions (all READ-ONLY, return plain dicts/lists)

```python
from api.reports import gmp_daily_read as gdr

# Per-day kWh for an ARRAY, summed across its GMP meters. Ascending.
gdr.get_daily_series(array_id, *, start=None, end=None) -> list[
    {"day": date, "kwh": float, "meters": int, "intervals": int}
]

# Per-(year,month) totals for an ARRAY. Ascending. `days` = distinct days w/ data.
gdr.get_monthly_totals(array_id, *, start=None, end=None) -> list[
    {"year": int, "month": int, "kwh": float, "days": int}
]

# Evidence summary — trust this before rendering.
gdr.get_coverage(array_id) -> {
    "array_id": int, "meters": int, "day_count": int,
    "first_day": date|None, "last_day": date|None, "total_kwh": float
}

# Per-day kWh for ONE meter (e.g. show Starlake's 3 sub-meters separately).
gdr.get_account_daily_series(account_id, *, start=None, end=None) -> list[
    {"day": date, "kwh": float, "intervals": int}
]

# Provenance / invoice attachment — the raw sponge. raw_csv only when asked (large).
gdr.get_raw_windows(account_id, *, include_payload=False) -> list[
    {"window_start": date, "window_end": date, "http_status": int,
     "row_count": int, "interval_min": date|None, "interval_max": date|None,
     "fetched_at": datetime, ["raw_csv": str|None]}
]
```

### Guarantees the consumer can rely on
- `kwh` is never fabricated: it is the summed real interval Quantity from a stored
  raw payload. A day with no GMP data simply has no row (gap, not a zero).
- Negative interval noise is clamped to 0 in the modeled layer; the raw sponge
  keeps the true values.
- `intervals` lets the consumer detect partial days (96 = a full day per meter).
- Idempotent + re-derivable: a parser fix can re-run `rederive_account` to enrich
  history with **zero** GMP re-pulls.

## Endpoint grounding (verified live, read-only, 2026-06-18)
- `GET /api/v2/usage/{acct}/download?startDate&endDate&format=csv`, Bearer JWT.
- 15-min intervals; cols `ServiceAgreement, IntervalStart, IntervalEnd, Quantity, UnitOfMeasure(kWh)`.
- **History depth is PER-METER, NOT 16 years** (that's the *bills* endpoint). Verified
  floors: some meters reach 2020-12-31, others only ~2023. Below the floor GMP returns
  a clean HTTP 404 — the backfill self-discovers each meter's true start.
- **A ~1-year request 503-times-out server-side.** Backfill pages in ≤90-day windows
  (default 60d) walking backward to the 404 floor.

## Known operational blocker (auth)
The live backfill needs a valid GMP session token. Observed 2026-06-18: stored
access tokens can be stale (401) and the refresh endpoint returned
`403 AUTHORIZATION_FAILURE` after repeated refreshes (likely rate-limit/lockout, or
the owner's session needs re-capture). GMP does **not** rotate the refresh_token
(none returned). Resolve before a fleet backfill: confirm a fresh GMP capture for the
tenant, and avoid hammering the token endpoint.
