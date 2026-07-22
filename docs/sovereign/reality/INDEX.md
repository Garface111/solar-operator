# Reality INDEX — Array Operator cold truth

_Regenerated 2026-07-22T19:02:13.755960+00:00_

**Total changes recorded:** 1316

## By source
- `ford`: 903
- `sovereign`: 405
- `agent`: 5
- `sovereign_sandbox`: 1
- `bot`: 1
- `git`: 1

## By surface
- `frontend`: 727
- `backend`: 393
- `extension`: 99
- `docs`: 81
- `other`: 77
- `ops`: 11

## By repo
- `array-operator`: 801
- `solar-operator`: 515

## How Sovereign uses this

1. On every **cortex wake**, load this INDEX + the last 60 CHANGELOG lines.
2. After every **ship** (code job, feature ship, Ford-logged change), **append** one JSONL line — never rewrite history.
3. Prefer this timeline over inventing product history. Git remains the raw audit; this is the reasoned product memory.

## Latest entries (tail)

- `2026-07-19T19:39:32` [ford] (solar-operator/backend) fix(autofix): [arrayoperator] Cannot read properties of undefined (reading [Sentry PYTHON-FASTAPI-39] (#81)
- `2026-07-19T20:54:04` [sovereign] (solar-operator/backend) fix(fleet): stream spreadsheet first paint + kill 90s SolarEdge serial wait
- `2026-07-19T21:05:27` [sovereign] (solar-operator/backend) fix(email): stop the two owner alerts from reading as a contradiction
- `2026-07-20T09:06:32` [sovereign] (solar-operator/backend) fix(auth): magic-link email states its REAL lifetime instead of "15 minutes"
- `2026-07-20T10:44:21` [sovereign] (solar-operator/backend) fix(sync): stop the utility_accounts lock fight that 500s POST /v1/sync
- `2026-07-20T11:17:55` [sovereign] (solar-operator/backend) fix(vault): stop the web process decrypting portal secrets — 3 dead endpoints
- `2026-07-20T14:34:37` [sovereign] (solar-operator/backend) feat: vendor-offline production fallback from utility meter
- `2026-07-20T14:54:23` [sovereign] (solar-operator/backend) feat: per-array trailing-12-mo on fleet-trends year matrix
- `2026-07-20T15:14:08` [sovereign] (solar-operator/backend) feat: per-array Master Data Pack download (bills + daily + YoY)
- `2026-07-20T15:22:33` [sovereign] (solar-operator/backend) perf: speed master-data fleet zip (skip daily autosize, store zip)
- `2026-07-20T17:32:22` [sovereign] (solar-operator/backend) feat: Energy Agent can set offtaker/gen-report email copy + timed overrides
- `2026-07-20T17:43:23` [sovereign] (solar-operator/backend) fix: SolarEdge weather model gets nameplate without waiting on inventory
- `2026-07-20T22:29:39` [sovereign] (solar-operator/backend) feat(energy-agent): tools for mobile offtaker/marketplace/Stripe Connect
- `2026-07-20T23:07:38` [sovereign] (solar-operator/backend) feat(energy-agent): dual surface memories + access surface awareness
- `2026-07-20T23:34:33` [sovereign] (solar-operator/backend) fix(energy-agent): full tenant history for honest fleet diagnosis
- `2026-07-21T07:50:14` [agent] (solar-operator/backend) Add Command Center (fleet) read-only mode to Energy Agent
- `2026-07-21T08:53:00` [sovereign] (solar-operator/backend,docs) feat(ao): continuous performance verification (Sunreport parity)
- `2026-07-21T10:28:43` [sovereign] (solar-operator/backend,extension) fix(fronius): Waterford sparklines — unit conversion + rebalance
- `2026-07-21T12:26:37` [sovereign] (solar-operator/backend) fix(alerts): suppress live dark/low during local sunset shoulder
- `2026-07-21T19:48:45` [sovereign] (solar-operator/other) Merge main into command-center fleet mode branch
- `2026-07-21T19:54:39` [sovereign] (solar-operator/backend,extension) fix: Fronius WDC nameplates are AC kW, not DC watts
- `2026-07-21T19:56:15` [sovereign] (solar-operator/backend,extension) fix: parse Fronius 'Primo NN 12.5-1' WDC portal names correctly
- `2026-07-21T19:56:31` [sovereign] (solar-operator/backend) fix: don't parse Fronius voltage ranges as nameplate kW
- `2026-07-22T09:32:00` [sovereign] (solar-operator/backend) Colleen low-hanging: honest rates, exclude-from-fleet, verification copy
- `2026-07-22T10:00:38` [sovereign] (solar-operator/backend) Fix Analysis modeling gaps for Cover/Starlake/Waterford Fronius
