# Data hub: server-side poller + the raw_json "sponge" pattern (Jun 2026)

Two durable architecture patterns that turned the product from a stale-snapshot
viewer into a real-time data hub + a years-deep energy-record sponge. Both
shipped + proven live on prod (Bruce's fleet, a 16.4-year GMP tenant).

═══════════════════════════════════════════════════════════════════════════
## PATTERN 1 — Server-side telemetry poller (continuous live readings)
═══════════════════════════════════════════════════════════════════════════

### The bug that motivated it (Tannery Brook "producing at 9pm")
Symptom Ford caught: the sandbox showed an SMA inverter "producing 17 kW" while
SMA's OWN portal showed ~0. ROOT CAUSE (proven via prod query): the captured
power was a 2:35pm peak reading; at 9pm we still rendered it as live. The
freshness gate `_POWER_FRESH` in `api/inverter_fleet.py` had been widened 3h→24h
so capture-vendor cards wouldn't go blank between captures — which traded "blank"
for "lying." A stale afternoon reading shown at night is WORSE than showing nothing.

### The vendor-fresh vs capture-stale distinction (the core mental model)
- **SolarEdge** ships an API key → `build_fleet_tree` pulls a LIVE instantaneous
  power on every page load → always fresh.
- **SMA/Fronius/Chint** are EXTENSION-captured → no live feed → power only updates
  when the owner manually re-logs-in. Between captures we have nothing new.
This is why SolarEdge cards always have live fills and capture-vendor cards go
stale. The fix is NOT a card bug fix — it's getting continuous server-side data.

### The honesty floor (always ship this regardless of polling)
`_live_power_w(iv, m, *, daylight)` must NEVER report a stored capture as live
power when the sun is down. A genuine live telemetry value (m["last_power_w"],
freshly pulled this request) is trusted as-is; only the STORED capture fallback
is daylight-gated. The fleet build already computes `daylight = _is_daylight()`;
pass it through. Test: a 6h-old capture at night returns None.

### The poller (`api/poller.py`) — vendor-agnostic by construction
- `poll_all_sources(*, force_daylight=None)`: iterate every Array with a PULLABLE
  connection, fetch per-inverter telemetry via the EXISTING
  `inverter_fleet._telemetry_for_site` path (dispatches by vendor), write an
  `InverterReading` time-series row + refresh `Inverter.last_power_w/at`.
- "Pullable" = `_pullable_connection(db, arr)`: reuses `_resolve_connection`,
  then requires creds the telemetry path needs — `api_key`+`site_id` (SolarEdge
  today) OR `refresh_token`/`client_id`+`client_secret` (SMA/OAuth, ready). An
  extension-only array (no pullable creds) returns None and is skipped. So ADDING
  a vendor to the hub = giving an array real API creds, NOT editing the poller.
- Safety: daylight-gated (no night API spend), per-array error isolation (one bad
  vendor never aborts the run), only inverters with a real fresh reading get a row.
- `InverterReading` model (high-freq instantaneous watts) is distinct from
  `InverterDaily` (one kWh/day). Pruned on a rolling 14-day window
  (`prune_old_readings`) so it stays bounded.
- Scheduler (`api/scheduler.py` start()): `poll_all_sources_job` every 5 min
  (max_instances=1, coalesce=True) + `prune_inverter_readings_job` daily 04:10 UTC.
  Wrap the job in try/except → never crash the scheduler thread.

### SCALING TRAP the live run caught (don't skip this)
A FULL-fleet poll EXCEEDED 60s over `railway ssh` because SolarEdge needs one
call PER INVERTER and is rate-limited (~300 req/day/key). With 60+ inverters a
single 5-min tick won't finish at scale. FIXES (do before relying on it at scale):
batch SolarEdge at the SITE level (one inventory call returns all inverters'
power — the adapter already has `fetch_inventory`; use its bulk power instead of
per-serial `fetch_inverter_telemetry` in the poll path), stagger arrays across
ticks, and respect the daily API budget. The honesty fix + poller correctness are
proven; the throughput hardening is the remaining work.

### SMA is the real "always-current" unblock — but needs an external cred
SMA's adapter (`api/inverters/sma.py`) ALREADY has a real OAuth2 `fetch_live`
against ennexOS. It just needs an SMA developer APP REGISTRATION
(client_id/client_secret) — an external account step only Ford can do. The moment
those creds land on an array's connection, the poller polls SMA live exactly like
SolarEdge, no manual captures. Surface this blocker to Ford BEFORE building around
it. (Same external-cred pattern blocks Locus/AlsoEnergy — see inverter-vendor-status.md.)

═══════════════════════════════════════════════════════════════════════════
## PATTERN 2 — The raw_json "sponge" + re-derive-from-raw (THE reusable win)
═══════════════════════════════════════════════════════════════════════════

Ford's framing: "we are going to become data sponges." At onboarding, absorb the
owner's ENTIRE utility history (GMP returned 16.4 YEARS / 2,924 bills for one
tenant) as their system-of-record energy life. The switching-cost moat compounds.

### The pattern (generalizable to ANY upstream integration)
1. **Store the FULL raw upstream payload** in a `raw_json` (JSONB) column on the
   record, ALONGSIDE the modeled/parsed fields. The modeled columns are a
   QUERYABLE CONVENIENCE LAYER; raw_json is the AUTHORITATIVE source of truth.
2. **Parse best-effort** into the modeled columns.
3. **When the parser is wrong/incomplete, fix it and RE-DERIVE from stored
   raw_json — ZERO re-pulls.** `sponge.rederive_from_raw(tenant_id=None)` re-runs
   the extractor over every stored raw_json in place. This retroactively enriched
   16 years of history in seconds with no GMP API calls. THIS is the payoff of
   keeping raw_json — and the reason to ALWAYS keep it.

### Why this beat the VEC-trap class of failure
My first GMP extractor GUESSED field names (CONSUME/USAGE/EXPORT/amountDue) — ALL
wrong; cost+consumption came back null across 2924 bills. Because raw_json held
100% of the real data, the fix was a pure parser correction + one re-derive — NOT
a 16-year re-scrape. Never let a wrong parser cost you the data; the sponge column
makes parser bugs cheap and recoverable.

### Debug method: INTROSPECT FROM STORED raw_json (no new HAR needed)
When the parser misses fields, you already HOLD the real payloads. On prod, dump
the actual structure from a stored raw_json: top-level keys, `billSegments[0]`
keys, and a frequency Counter of every `segmentLineItems[].unitCode` /
`unitOfMeasure` / where `dollarAmount` lives. This replaces "ask Ford for another
HAR" — the sponge already captured the ground truth, just read it.

### The REAL GMP bill JSON field map (verified across 400+ live bills, Jun 2026)
GMP `/api/v2/accounts/{acct}/bills` returns energy-only line items (NO top-level
amountDue). Structure: `billSegments[].segmentLineItems[]` (energy, by unitCode)
+ `billSegments[].segmentCalcs[]` (the MONEY).
- **Generation kWh** = unitCode `GENERATE` (the ONLY code verified earlier; the
  rest below were the corrections).
- **Consumption kWh** = unitCode `CONSUMED` (NOT CONSUME/USAGE).
- **Excess-to-grid kWh** = unitCode `EXCESS` / `EXCESSO` (NOT EXPORT).
- **Net kWh** = `NET`; **solar credit kWh** = `SOLCRED`; **total energy** = `TOTENGY`.
- **Bill cost $** = sum of `segmentCalcs[].dollarAmount`. NEGATIVE total = a
  net-metering CREDIT the owner earned (Bruce's bills run negative). Line-item
  `dollarAmount` and segmentCalcs sum to the SAME total — use segmentCalcs ONLY,
  never both (double-count). `segmentCalcs[].rate` is a rate CODE (e.g. "GST01")
  with `billText` describing it.
- **Per-line dollar rate** is embedded in line `billText` ("@ $0.21457 per").
- GMP repeats a 0.0 placeholder line + the real total → take MAX-per-code (abs),
  mirroring the proven `_extract_kwh_generated`. NOT sum.
- **Blended ¢/kWh guard**: a near-zero-consumption solar bill ($29 fixed charges /
  2 kWh = 1456¢) explodes. Require a consumption floor (>=10 kWh) AND clamp to a
  sane 0–100¢/kWh range, else leave rate null (keep cost/consumption).

### The onboarding flow (fire-on-capture + progress bar)
- The GMP capture handler (`api/app.py` /v1/sync, end of handler) already fired a
  background `pull_bills_for_tenant`. UPGRADE that exact spot: for provider=="gmp"
  fire `sponge.absorb_history(tid, "gmp")` in a daemon Thread (never blocks the
  sync response); other providers keep the plain pull.
- `SpongeProgress` table (one row per tenant+provider) tracks accounts_done /
  bills_absorbed / years_covered → `GET /v1/account/sponge` serves it for the
  frontend progress bar ("Importing your 16.4 years…"). `GET
  /v1/account/energy-history` serves the absorbed record back.
- `absorb_history` reuses the proven `worker._pull_via_json` path (same GMP
  fetch/parse/persist), one account at a time so the bar advances visibly.
- FRONTEND HALF (shipped Jun 2026): `SpongeProgressCard` (polls /v1/account/sponge
  → live progress bar on the account page) + `EnergyHistoryView`
  (/account/energy-history) — MERGED with another agent's billing Trends tab by
  reusing its chart/stat components. See array-operator-card-ui.md §"MERGING a
  feature into ANOTHER AGENT's existing React tab" + §"DEPLOY-VERIFY the React
  bundle actually rotated" for that pattern + the bundle-staleness gotchas.

### Absorb ALL bills, not just generation (and why it's NEPOOL-safe)
The old `_pull_via_json` SKIPPED any bill with no kWh generated — throwing away
the rest of the owner's energy history. The sponge absorbs EVERY bill
(consumption-only periods too). SAFE for NEPOOL because every report consumer
already guards: `distribute_kwh_by_calendar_day` returns {} for a no-generation
bill (line ~33), and the writers skip `kwh_generated is None or <= 0`. Verified:
181 bill/writer tests stayed green after dropping the skip filter.

### Migration note
New `bills.*` sponge columns won't appear via `Base.metadata.create_all` on an
EXISTING prod table — add them explicitly in `migrate.py` (the established pattern
there). A brand-new table (InverterReading, SpongeProgress) DOES come free via
create_all. raw_json column type = JSONB.

═══════════════════════════════════════════════════════════════════════════
## PATTERN 3 — GMP DAILY-INTERVAL sponge (a DIFFERENT stream from bills)
═══════════════════════════════════════════════════════════════════════════
Verified live, read-only, against Bruce's prod tenant `ten_6522da7ac2e1d01d`
(Jun 2026). Run scripts/gmp_daily_probe.py to re-verify any time.

### ⛔ THE GROUNDING TRAP: daily depth ≠ bills depth (don't conflate them)
GMP exposes TWO unrelated history streams:
- **Monthly BILLS**: `/api/v2/accounts/{acct}/bills` → 16.4 yrs / 2924 bills
  (Pattern 2). This is the "back to ~2009/2019" depth.
- **15-min DAILY INTERVALS**: `/api/v2/usage/{acct}/download?startDate&endDate&format=csv`
  → AMI interval data, MUCH shallower. Columns: `ServiceAgreement, IntervalStart,
  IntervalEnd, Quantity, UnitOfMeasure(kWh)`; 96 rows/day/service-agreement.
A prompt/HAR claiming the DAILY endpoint "serves daily back to 2019" is almost
always the BILLS depth mis-applied. VERIFY before claiming a multi-year daily
backfill — this is the exact "HAR captured the wrong context" trap. Probed floors:
Dean meter → no data before ~Jan 2023 (~3.5 yrs); Starlake Center/North → back to
2020-12-31 (~5.5 yrs). Floor is PER-METER (its AMI/install date); NONE reached
2019 in the sample.

### ⛔ TWO hard endpoint constraints a naive backfill silently trips
1. **1-year requests 503-TIMEOUT server-side.** A 366-day window returns
   `{"httpStatus":503,"code":142,...TIMED_OUT_ERROR}` on EVERY account. 30d
   (2,880 rows) and 90d (8,640 rows) succeed. → Page in ≤90-day windows (use 60d
   for margin). A single "give me everything" call fails on day one.
2. **Below the floor GMP returns a clean HTTP 404** ("Usage data for the given
   energy period not found", code 80) — NOT an empty 200. So a backfill can
   self-discover each meter's true start: walk backward in 60-day windows until a
   404 (or two empty windows), then stop. Idempotent upsert into DailyGeneration
   (source=`gmp_api`), per-account error isolation.

### Auth + harness gotchas (proven this session)
- Token: reuse `sessions.token_for_account` then refresh on 401/403 via
  `gmp_refresh.refresh_gmp_token(refresh_token)` (sessions carry refresh tokens;
  `token_for_account` does NOT auto-refresh). GMP sessions often have
  `customer_number=NULL` → selection falls back to latest-per-provider (fine).
- **LOCAL DB is empty of prod data** (utility_sessions=0, bills=0). To verify
  against real GMP you MUST go through prod: `railway ssh` is logged in as Ford.
- `railway ssh` caps ~60s AND mangles quoting → base64-encode the probe script,
  pipe `| base64 -d > /app/_x.py`, run via `python -c 'import runpy;
  runpy.run_path("/app/_x.py", run_name="__main__")'`, then `rm` it. GMP responses
  are slow → run the ssh call as a background terminal job with notify_on_complete.
- Historical backfill does NOT need the daylight gate (settled past data, not live
  power — that gate is only for Pattern 1's live readings).

### Re-runnable probe
`scripts/gmp_daily_probe.py` — read-only depth/constraint prober (endpoint
liveness, 90-day-cap check, per-meter 404 floor). Copy to prod via the base64
pattern above. Never writes.

### IMPLEMENTATION shipped (Jun 18 2026) — dedicated tables, NOT DailyGeneration
Built the daily sponge as its OWN storage, deliberately separate from the
CSV-upload `DailyGeneration` table (mixing them risked silently changing live
report numbers, and a GMP account==one meter while an Array sums several):
- `api/models.py`: `GmpUsageRaw` (one row per account+window, VERBATIM CSV in
  `raw_csv` Text col = the sponge Ford attaches to invoices; idempotent on
  (account_id,window_start,window_end)) + `GmpDailyGeneration` (one row per
  account+day, kwh=Σ real interval Quantity, source='gmp_api'; idempotent on
  (account_id,day)). Both brand-new → come free via create_all.
- `api/adapters/gmp.py`: `fetch_usage_csv()` (raises GmpUsageNotFound on 404 =
  floor signal, GmpUsageTimeout on 503 = shrink window) + `parse_usage_csv_to_daily()`.
- `api/jobs/gmp_daily_backfill.py`: `backfill_account` / `backfill_tenant` (walk
  backward 60d windows to 404 floor, commit per window, per-account isolation,
  token-refresh + window-shrink retries) + `rederive_account` (re-derive daily
  from stored raw, ZERO re-pull — the re-derive payoff).
- `api/reports/gmp_daily_read.py` = the READ CONTRACT the Reports agent consumes
  (get_daily_series / get_monthly_totals / get_coverage / get_account_daily_series
  / get_raw_windows; all read-only, per-array reads SUM across the array's meters).
  Doc: `docs/plans/GMP_DAILY_READ_CONTRACT.md`. Tests: `tests/test_gmp_daily_sponge.py`
  (7 pass, no network). Proven end-to-end on prod with real-shaped data
  (5,760 intervals→60 day-rows/window, idempotent, raw re-derivable, then cleaned up).

### ⛔ AUTH: the refresh 403 is CLIENT-ID-WIDE (shared cred lockout)
ALL GMP sessions share ONE client_id (`C978562571FC475294191C7B94DD883E`). A burst
of `refresh_gmp_token` calls (e.g. iterating probes) trips a 403
AUTHORIZATION_FAILURE that then hits EVERY session — even untouched ones — because
the throttle is on the shared client_id, not per-token. Symptoms seen: stored
access token with a still-future JWT `exp` claim nonetheless 401s on /usage (GMP
invalidated it server-side; DB `expires_at` is NOT authoritative), and refresh
403s fleet-wide. It worked 40 min earlier → strongly a self-inflicted rate-limit
that clears with time. DON'T keep hammering the token endpoint to diagnose — that
deepens the lockout. Wait ~60 min, try ONE refresh: success ⇒ was throttle; still
403 ⇒ systemic, needs a FRESH GMP capture from the owner. Decode the JWT `exp`
offline (base64 the middle segment) instead of test-calling to check expiry.

═══════════════════════════════════════════════════════════════════════════
## PATTERN 4 — WEATHER/IRRADIANCE sponge (independent THIRD measurement leg)
═══════════════════════════════════════════════════════════════════════════
Shipped Jun 18 2026. The reconcile engine had TWO views of an array (inverter
production vs utility-settled). Irradiance is the INDEPENDENT third leg — the
physical driver of output, owned by neither vendor — so a leak becomes a 3-source
triangulation, still detectable when one source is missing. Crucially it reaches
BELOW the GMP daily-interval floor (2020/2023) back to 2021+ (archive goes to
1940) with ZERO auth fragility — exactly the gap GMP daily can't fill.

### Two FREE no-key sources (proven reachable from prod Railway egress)
1. GEOCODE: `https://api.zippopotam.us/us/{zip}` → lat/lon/place/state. We hold
   real `UtilityAccount.service_address.zip` on ~318/357 GMP accounts (real VT
   towns). NO lat/lon/tilt/azimuth stored anywhere → ZIP geocode is the only
   grounded location path; `Array.region` is NULL on ~373/405 arrays (dead end).
2. ARCHIVE: `https://archive-api.open-meteo.com/v1/archive` daily vars
   `shortwave_radiation_sum`(MJ/m²),`sunshine_duration`,`temperature_2m_max/min`.
   NO narrow-window cap (unlike GMP's 90-day 503) → whole multi-year window in ONE call.

### Implementation (same sponge shape as GMP daily)
- models: `WeatherLocation` (deduped by ZIP — a town shared by many arrays = ONE
  geocode + ONE weather pull), `WeatherFetchRaw` (verbatim Open-Meteo JSON sponge),
  `WeatherDaily` (derived per (location,day)). All brand-new → free via create_all.
- `api/adapters/weather.py`: geocode_zip / fetch_archive / parse_archive_to_daily.
- `api/jobs/weather_backfill.py`: resolve_locations(tenant) → backfill_location /
  backfill_all → rederive_location. Per-location isolation, idempotent.
- `api/reports/weather_read.py` = READ CONTRACT: get_irradiance_series /
  get_irradiance_coverage. JOIN: array → its GMP account → service_address ZIP →
  WeatherLocation. Consumers never touch weather_* tables directly.
- tests/test_weather_sponge.py (8, no network). Proven e2e on prod: 24 VT ZIPs
  geocoded (0 skipped), real Open-Meteo pull 30 days sponged+derived, idempotent,
  re-derivable, then cleaned up.

### ⛔ HONESTY BOUNDARY (Ford's never-fabricate rule)
This stores REAL measured/reanalysis weather. It does NOT produce an "expected
generation" kWh — that needs array tilt/azimuth (NOT stored) and would be a
LABELED ESTIMATE layered on top, never presented as measurement. A PVWatts
expected-gen model is the obvious next step but only honest once tilt/azimuth are
captured (or explicitly assumed + labeled).

═══════════════════════════════════════════════════════════════════════════
## DATA-AGGREGATION BACKLOG (survey done Jun 18 2026 — what's still untapped)
═══════════════════════════════════════════════════════════════════════════
Ranked cheapest+highest-leverage first. Tier 1 = mine deeper from sources we
already touch (no new auth): (a) GMP /usage may carry consumption+export channels
as separate ServiceAgreements we currently discard — probe distinct SA set; (b)
SolarEdge API serves WAY more than the site current_power_w we pull — 4 meters
(Production/Consumption/FeedIn/Purchased), per-inverter + per-OPTIMIZER
module-level telemetry, energy history, alerts, env-benefits — sponge full
payloads; (c) extension vendors (SMA/Fronius/Chint) observe richer JSON than we
persist — add a raw sponge like GMP's. Tier 2 = weather/irradiance (DONE,
Pattern 4). Tier 3 = stitch monthly bills BELOW the daily floor for continuous
multi-year depth; mine pre-JSON PDF bills; Green Button (ESPI XML) adapter. Tier
4 = breadth: VEC/BED/Stowe/WEC/Washington Electric (recon docs exist),
Enphase/Enlighten + Tigo adapters (Locus/AlsoEnergy blocked on Ford-only creds).
Tier 5 = KEEP IT FLOWING: scheduled incremental append of newest 60-day GMP
window per account (else the daily sponge freezes at last capture); backoff +
spread-out refreshes for the shared-client_id 403 trap.

═══════════════════════════════════════════════════════════════════════════
## PATTERN 5 — SolarEdge DEEP sponge (Tier 1b DONE — the richest owned source)
═══════════════════════════════════════════════════════════════════════════
Shipped Jun 18 2026. We held SolarEdge keys but kept only ONE current_power_w
per site. SolarEdge actually serves a LOT more, all real, no shared-cred lockout
(owner-provided per-array keys). Prod survey: 23 InverterConnection rows
status=ok, 1 distinct API key → 3 real sites (Cover Rooftop 4631514, Londonderry
416160, Starlake 1341613), 93 SolarEdge inverters. NOTE: InverterConnection has
NO tenant_id column and array_id is UNIQUE (one conn/array); creds live in
`config` JSON {api_key, site_id}.

### What the live probe revealed each endpoint actually returns
- `/equipment/{site}/{sn}/data`: ~10-min per-inverter telemetry with 14 FIELDS —
  we used to discard all but totalEnergy+mode. Real fields: totalActivePower,
  totalEnergy, dcVoltage, temperature (panel heat → explains output dips),
  groundFaultResistance (leading failure indicator), powerLimit, inverterMode,
  L1/L2/L3 AC current/voltage/freq/power. 7-DAY SPAN CAP per call.
- `/site/{id}/energyDetails`: the 4 meters Production/Consumption/FeedIn/Purchased
  (full net-metering picture). Unit=Wh. Non-solar meters often all-null (site
  meters Production only) — KEEP nulls, never invent.
- `/site/{id}/powerDetails`: 5-meter 15-min power (97 buckets/day).
- `/site/{id}/envBenefits`: CO2/SO2/NOx saved, trees, bulbs (marketing-ready).

### Implementation (same sponge shape)
- models: `SolarEdgeFetchRaw` (verbatim JSON sponge, keyed
  site+endpoint+serial+window), `SolarEdgeTelemetry` (rich per-reading,
  site+serial+ts), `SolarEdgeEnergyDetail` (per-meter daily Wh, site+meter+day).
- `api/adapters/solaredge.py`: + fetch_equipment_data_raw / parse_equipment_telemetry
  / fetch_energy_details_raw / parse_energy_details / fetch_env_benefits_raw.
- `api/jobs/solaredge_deep_backfill.py`: backfill_site/backfill_all/rederive_site.
  ⚠️ RATE-LIMIT GOVERNOR: SolarEdge ~300 req/day/key; equipment calls = N_inv ×
  ceil(days/7). Job takes a per-run request_budget, stops cleanly when hit,
  groups connections by api_key so a shared key's budget is split across its sites.
- `api/reports/solaredge_read.py`: get_inverter_telemetry / get_meter_daily /
  get_solaredge_coverage (READ CONTRACT; consumers never touch solaredge_* tables).
- tests/test_solaredge_deep_sponge.py (6, no network). PROVEN e2e on prod: site
  416160, 14-day window → 15,829 telemetry readings + 60 meter-days in 15 API
  calls (well under cap), idempotent, re-derivable, then cleaned up.

### Remaining untapped after this: powerDetails 15-min + per-OPTIMIZER (module-level)
  data — even deeper, same pattern. And the GMP /usage consumption+export channels
  (Tier 1a) still unprobed (GMP auth was locked out this session).
