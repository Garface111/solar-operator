# Live telemetry data hub — server-side poller + the stale-vs-live honesty gate

> This is the LIVE-READINGS half of Ford's "data hub / data sponge" vision. The
> HISTORY-ABSORB half (replay the GMP session at onboarding → suck in years of
> bills as the owner's system-of-record, with a progress bar) is in
> `references/data-hub-and-sponge.md`. Read both together for the full picture.


The single biggest data-integrity trap in Array Operator: showing a STALE captured
reading as if it were LIVE. Ford caught it directly — "why is Tannery Brook saying
it is producing on our sandbox but not producing on SMA's site? We are collecting
the data FROM sma." That is the failure mode this doc prevents, and the data-hub
poller is the fix.

## The two telemetry classes (know which a vendor is BEFORE promising "live")
1. **API-pullable** (SolarEdge today; SMA/Fronius/Locus/AlsoEnergy once creds exist):
   stored creds (`api_key`+`site_id`, or OAuth `refresh_token`/`client_id`+`secret`)
   let the BACKEND fetch a fresh instantaneous value on demand / on a schedule.
   `InverterConnection.config` (JSON blob) or legacy `Array.solaredge_api_key/site_id`
   hold these. `_resolve_connection()` (in BOTH `array_owners.py` and `inverter_fleet.py`)
   returns the real row or a virtual SolarEdge namespace from the legacy columns.
2. **Extension-capture only** (Chint, and SMA/Fronius until their API creds are wired):
   the backend has NO pullable feed. The browser ships a ONE-SHOT reading at capture
   time → stored on `Inverter.last_power_w` + `last_power_at`. Between captures we have
   nothing new. There is NO replayable token from these captures (this is the VEC trap
   restated: SmartHub httpOnly cookies / extension scrapes can't be replayed server-side).

## THE BUG CLASS: stale capture rendered as live
`Inverter.last_power_w` = the afternoon peak captured hours ago. With a wide freshness
window (`_POWER_FRESH` had been bumped 3h→24h so capture cards wouldn't go blank), a
2pm 17 kW reading was still shown as "producing now" at 9pm — the EXACT opposite of
what SMA's own portal showed. Widening the window traded "blank" for "lying"; lying is
worse. Proven on prod: queried Tannery Brook at ~9pm VT, cards read 14-17 kW
`SHOWN_LIVE=True` off a `18:35 UTC` (2:35pm) capture.

### The honesty gate (live, `inverter_fleet._live_power_w`)
```
def _live_power_w(iv, m, *, daylight=True):
    pw = m.get("last_power_w")        # genuine live telemetry pulled THIS request
    if pw is not None: return pw      # trusted as-is (real instant)
    if not daylight: return None      # never show a STORED capture as live at night
    if iv.last_power_w is not None and (now()-iv.last_power_at) <= _POWER_FRESH:
        return iv.last_power_w
    return None
```
Call site passes `daylight=daylight` (build_fleet_tree already computes `_is_daylight()`
once per build). Rule: a freshly-pulled value is the real instant → trust it; only the
STORED capture fallback is daylight-gated. This kills the "17 kW at night" bug at the
read layer regardless of how stale the capture is.

## The data-hub poller (`api/poller.py`) — the real "constantly updating" fix
Ford's directive: "we need to be updating constantly … make it a beast of a data
processing hub." The poller is the spine.

- `poll_all_sources(*, force_daylight=None)` iterates every non-deleted Array, calls
  `_pullable_connection(db, arr)` (reuses `_resolve_connection`, then REQUIRES real
  creds: `api_key`+`site_id` OR oauth creds — extension-only arrays return None and are
  skipped), fetches via the existing `inverter_fleet._telemetry_for_site(vendor, …)`,
  and for each inverter with a real `last_power_w`: refreshes `Inverter.last_power_w/at`
  AND appends an `InverterReading` row.
- **Vendor-agnostic by construction**: `_telemetry_for_site` dispatches on vendor, so
  adding a vendor to the hub = giving an array real API creds, NOT editing the poller.
- **Daylight-gated**: skips the whole run when the sun is down (no night API spend; night
  readings are ~0 anyway). `force_daylight=True` for tests / manual prod kicks.
- **Per-array error isolation**: one vendor erroring sets `conn.last_error`/`status` and
  `continue`s — never aborts the run.
- Scheduler (`api/scheduler.py` `start()`): `poll_all_sources_job` every 5 min
  (`max_instances=1, coalesce=True`), `prune_inverter_readings_job` daily 04:10 UTC.
  Both wrap in try/except → log via `logger` (NOT `log` — scheduler's logger is named
  `logger`). The poller is its own module to avoid editing `array_owners.py` (autofix
  cron domain).

## InverterReading — the time-series table (the hub's high-freq memory)
`api/models.py class InverterReading`: `(tenant_id, inverter_id, ts, power_w,
energy_today_kwh, status, source)` + index `(inverter_id, ts)`. Distinct from
`InverterDaily` (one kWh total per day) — this is sub-hourly instantaneous watts; the
intraday power curve is the series of these. Rolling `READINGS_KEEP_DAYS=14` prune keeps
it bounded. **No migrate.py SQL needed for a brand-new TABLE** — `Base.metadata.create_all`
in `migrate.py` creates it on deploy automatically (migrate.py only ADDs columns to
EXISTING tables).

## PROVING it live (the rule: prove on a real run, not green tests)
Deploy, then `railway ssh "cd /app && echo <b64> | base64 -d | python"` running
`poll_all_sources(force_daylight=True)` and read back `InverterReading` rows. The proof
that mattered: Bruce's Cover Rooftop polled `power_w=0.0` at `01:05 UTC` (9pm VT) — the
TRUE value (sun down), opposite of the old stale-17kW bug. Wait for the new container
first: loop `railway ssh "... python -c 'from api.models import InverterReading; print(\"NEW\")'"`
until it prints NEW (Railway build lags the `git push` by minutes; the old container
still runs old code and ImportErrors on new symbols).

## SCALING TRAP surfaced by the live run (not by tests)
A FULL-fleet poll exceeded 60s over `railway ssh`: SolarEdge needs one telemetry call
PER inverter and is rate-limited (~300 req/day/key). With 60+ inverters a single 5-min
tick won't finish at scale. Fixes (next): (1) batch at the SITE level — `fetch_inventory`
returns all inverters' power in one call; use it in the poll path instead of per-serial
`fetch_inverter_telemetry`; (2) stagger arrays across ticks + respect the daily API
budget. This is the difference between "works on Bruce" and "holds at scale."

## SMA activation (the external blocker to name EARLY, before building around it)
`api/inverters/sma.py` already has a REAL OAuth2 `fetch_live` against ennexOS/smaapis.de
— it just needs an SMA developer-app registration (`client_id`/`client_secret`, optional
`refresh_token`) placed on the array's `InverterConnection.config`. That's an external
account step only Ford can do. Once those creds land, the poller polls SMA live exactly
like SolarEdge — zero poller change. Surface this as a blocker up front; do not discover
it after building.
