# Vendor-feed integrity + FleetStore data-path threading (AO)

Recurring bug CLASSES on Array Operator's inverter/live-data path. All verified
live Jun 2026. Read this when "a field isn't showing on the card", an outage
clock looks wrong, a vendor connection silently dies, or live data reads 0.

## 1. FleetStore strips backend fields (THE recurring "field isn't showing" trap)
The Arrays tab does NOT read backend `/fleet-tree` directly. `public/fleet-store.js`
has TWO reshape points that REBUILD each array/column field-by-field and DROP
anything not explicitly listed:
  - `adaptTree(t)` — ingests the live tree → canonical `state.arrays`
  - `toColumns(ids)` — canonical arrays → sandbox columns the renderer reads
A new backend column field (e.g. `source_status`, `is_daylight`, `daily`) MUST
be threaded through BOTH or it arrives `undefined` at sandbox.js and the render
guard silently no-ops. **When a field "isn't showing", grep fleet-store.js FIRST**
(adaptTree + toColumns), before touching the renderer. Same class as the
GMP-backfill/reconcile "never landed" bugs. This has bitten ≥2x (source_status,
earlier source fields) — it is the default suspect, not an edge case.

## 2. Vendor timestamps are SITE-LOCAL, not UTC (outage clock runs hours fast)
SolarEdge equipment telemetry `date` + overview `lastUpdateTime` come back as
naive `YYYY-MM-DD HH:MM:SS` in the **site's local time**, NO tz marker. Reading
them as UTC made a VT site (America/New_York, UTC−4/5) look ~4-5h MORE stale than
reality — surfaced as "GMP says out 24h, we say 19h".
FIX (api/adapters/solaredge.py): fetch the site's IANA `location.timeZone` from
`/site/{id}/details` (cache per site_id: `_SITE_TZ_CACHE`), then convert naive →
tz-aware UTC via `_localize_to_utc_iso(naive_ts, tzname)` using `zoneinfo`.
Unknown tz → leave naive (downstream treats naive as UTC = pre-fix behavior).
The age math lives in `inverter_fleet._source_status` (`datetime.now(utc) - freshest`).
ALSO: GMP and we measure DIFFERENT events — GMP times the utility METER, we time
INVERTER telemetry. They never match exactly even with correct tz. Label the
card/banner as the inverter feed so the two clocks aren't conflated.

## 3. OAuth refresh-token ROTATION (vendor "works until I reconnect")
Symptom: an OAuth inverter vendor (SMA) works right after connecting, silently
goes dark hours later, and only a manual RECONNECT revives it (briefly). Tell:
reconnect fixes it = a token problem, not network/credentials.
Root cause: SMA rotates the refresh_token on EVERY refresh grant and invalidates
the one just used. The adapter discarded the new token → first refresh worked,
second reused the dead original → 401 → dark.
FIX (api/inverters/sma.py + poller.py): cache+reuse the rotated refresh_token
(`new_refresh = body.get("refresh_token") or refresh_token`), AND persist it back
to the connection config so it survives access-token expiry + redeploys. The
poller writes the in-place-mutated cfg back with `flag_modified(conn, "config")`
(JSON cols don't auto-detect nested mutation) — see `_persist_config_if_changed`.
On 401, CLEAR the dead token from config so the next call falls back to a fresh
client_credentials grant. AlsoEnergy already did the in-memory half; SMA + the
DB-persist half were the gap. Audit Fronius/SMA/AlsoEnergy for the same pattern.

## 4. "Live data is 0" is often the SOURCE, not us — surface it
SolarEdge `currentPower` is laggy and frequently returns the last overnight 0 or
a stale reading; a site that stopped reporting to its own vendor portal hands us
0 with an old `lastUpdateTime`. That's a SOURCE-side outage, NOT our bug. Don't
"fix" 0 — surface it: `_source_status` returns ok|stale|none (stale = freshest
inverter last_report older than `_SOURCE_STALE_HOURS`=6h). The card shows a loud
amber "⚠ SOURCE OFFLINE" ribbon + frame (`.sb-array--srcout`) + banner naming the
vendor, explicitly "data outage at the source — not Array Operator." Energy-today
fallback does NOT help when the site is truly dark (today_Wh is also 0). Diagnose
live via railway-ssh probe of fetch_overview per site; one fresh site + dark
peers = genuine per-site outage, confirming pipeline is fine.

## Diagnostic discipline (Ford)
Ford BLOCKS curl against prod endpoints — verify backend logic via railway-ssh
python probes + unit tests + direct fn calls, never interactive prod HTTP. After
a deploy that adds a column the ORM SELECTs, VERIFY via `inspect(engine).get_columns(...)`
(the migrate LOG can run stale code / no-op), and confirm routes are 401 not 500.
