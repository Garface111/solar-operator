# Performance verification method

Owner- and operator-facing description of how EnergyAgent Array Operator
evaluates fleet performance. Source of truth for labels: `api/perf_verification/standards.py`
(`METHOD_SUMMARY`, `REPORT_FOOTER`).

## Standards alignment

EnergyAgent Performance Verification evaluates PV systems using methods
**consistent with** IEC 61724-1 (monitoring) and IEC 61724-3 (energy evaluation).

This is an **operational verification layer**, not a third-party bankability
certification. We never claim “IEC certified.”

## Measurement boundary

Preferred measured energy is **utility / revenue-grade meter generation** at the
point of interconnection (POI) when available for the calendar day.

When meter-day energy is absent, **inverter / telemetry AC energy** is used.

**Never used for Performance Index:**

- monthly bill prorations (`bill_prorate`)
- utility-meter smears that are estimates rather than day-true meter readings

Every PI is labeled with a boundary badge: `meter` | `inverter` | `mixed` |
`unavailable`.

## Expected energy

Expected AC energy is computed from:

- plane-of-array (POA) irradiance (satellite/reanalysis via Open-Meteo
  `global_tilted_irradiance`)
- array nameplate kW
- plane geometry (tilt / azimuth)
- a labeled performance ratio (PR)

Default PR = **0.84** unless the owner sets a site PR.

```
expected_kwh = nameplate_kw × (POA_kWh/m² / 1.0) × PR
```

Degradation default is **0%** unless a site-level field is provided later — we
do not invent degradation.

## KPIs

### Performance Index (PI)

```
PI = measured_energy / expected_energy
```

over **matched days** (days with both measured and expected). PI ≈ 1 means
production aligned with the weather-and-PR model.

### Performance ratio (model)

The model PR is an input derate (inverter, wiring, temperature, soiling
allowance, mismatch). It is always labeled **assumed** vs **owner-set**.

### Deviation

Daily residual:

```
r = (measured − expected) / expected
```

Portfolio and array summaries report mean residual and classify persistence:

| Label | Meaning (default threshold 5%) |
|-------|--------------------------------|
| **sudden** | Last 1–2 days below threshold after a near-zero prior week |
| **persistent** | ≥N consecutive underperforming days (default N = 5) |
| **seasonal** | Same calendar month last year shows a similar pattern (when history allows) |

Priority score scales with magnitude and duration (sudden weighted higher),
clamped 0–100.

### Availability

Where inverter status exists, availability distinguishes all-in energy from
in-service periods (excluding multi-day `comm_gap` / dead windows). **Null when
status history is insufficient** — never fabricated.

## Windows

Consistent calendar windows:

- daily
- rolling N-day (default 14 / 30)
- prior full calendar month for scheduled reports

Partial current day is excluded from matched comparisons.

## Monthly report pack

On the **1st of each month** (~13:00 UTC), Array Operator tenants with
`verification_reports_enabled` (default **on**) receive a pack for the previous
calendar month: portfolio PI/PR, per-array deviation table, boundary badges,
assumptions, and IEC footer.

Opt out via verification settings (`PUT /v1/array-owners/verification/settings`).

## Auditor export

`GET /v1/array-owners/verification/auditor-export?start=&end=` returns a ZIP:

- `assumptions.json`
- `daily.csv` (array_id, day, measured, expected, pi, boundary, residual)
- `summary.json`

No invented NaNs; empty measured days stay empty cells.

## Honesty

No fabricated irradiance, measured energy, or PI. Unavailable inputs yield
structured nulls with reasons.

## Report footer (every pack)

> Performance Verification · methods consistent with IEC 61724-1 / 61724-3 ·
> EnergyAgent Array Operator · not a third-party certification · Measured energy
> uses the utility meter when available, otherwise inverter AC · Expected energy
> is weather irradiance (POA) × nameplate × performance ratio

## API surface (Array Operator)

| Method | Path |
|--------|------|
| GET | `/v1/array-owners/verification/summary?window_days=30` |
| GET | `/v1/array-owners/verification/arrays/{array_id}` |
| GET | `/v1/array-owners/verification/report?period=YYYY-MM` |
| GET | `/v1/array-owners/verification/report.pdf?period=YYYY-MM` |
| GET | `/v1/array-owners/verification/auditor-export?start=&end=` |
| GET | `/v1/array-owners/verification/method` |
| GET/PUT | `/v1/array-owners/verification/settings` |
| GET | `/v1/array-owners/verification/interventions/{repair_ticket_id}` |

Auth: dashboard session (`Bearer so_session`) or tenant key, same as other
array-owner routes.

## Related (not this product surface)

- **Peer analysis** — relative inverter health within a site (different engine)
- **Workbook verification** (`api/verification.py`) — operator upload vs NEPOOL
  workbook accuracy (unrelated to PI)
- **P2 planned:** O&M multi-tenant view, SLA packaging stubs in
  `api/perf_verification/p2_stubs.py`
