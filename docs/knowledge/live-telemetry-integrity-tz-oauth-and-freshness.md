# Live inverter telemetry integrity: timezone, OAuth rotation, freshness plumbing

Class of bug: third-party inverter feeds (SolarEdge / Fronius / SMA) produce
LIVE data that *looks* working but is subtly wrong or silently dropped. The
product's whole value is trustworthy live data, so these are high-priority. Four
distinct, recurring failure modes — all hit and fixed Jun'26.

## 1. Vendor timestamps are SITE-LOCAL naive — never assume UTC
SYMPTOM: "GMP says Londonderry out 24h, our system says 19h" (multi-hour gap).
ROOT CAUSE: SolarEdge's equipment telemetry `date` AND overview `lastUpdateTime`
are in the SITE's LOCAL time with NO tz marker. `_source_status`
(api/inverter_fleet.py) read a naive timestamp as UTC → a VT site
(America/New_York, EDT = UTC-4) looked ~4-5h MORE stale than reality (outage
clock ran fast / overstated).
FIX (api/adapters/solaredge.py): fetch the site's IANA `timeZone` from
`/site/{id}/details` (`details.location.timeZone`, e.g. "America/New_York"),
cache it per site (`_SITE_TZ_CACHE`, never changes), and convert naive→tz-aware
UTC via `_localize_to_utc_iso(naive_ts, tzname)` before returning `last_report`.
Unknown tz → leave naive (downstream treats naive as UTC = pre-fix behavior).
VERIFY: `02:39` local → `06:39 UTC`, age 11.6h not the buggy 15.6h (clean 4h
correction = the EDT offset). The corrected age only refreshes on the NEXT poll
(re-stamps last_report), not instantly.
FLEET-WIDE: this bug class affects EVERY SolarEdge site. Fix at the adapter.
Fronius/SMA timestamp parsing may have the same naive-as-UTC assumption — audit
when touched.

## 2. "Our number" vs the utility's number measure DIFFERENT feeds
Even with correct timezones, our outage clock (last INVERTER telemetry from the
vendor portal) and GMP's (last UTILITY METER reading) anchor to different events
on different cadences — they will rarely match. Don't "fix" them to be equal.
Instead LABEL the source so they aren't mistaken for the same measurement: the
sandbox source-outage banner (sandbox.js `sourceStatusHTML`) now says
"<vendor> inverter monitoring last reported Nh ago … (this is the inverter feed;
your utility meter, e.g. GMP, tracks separately and may show a different time)".

## 3. OAuth refresh-token ROTATION (SMA "worked until I reconnected")
SYMPTOM: SMA live data works right after connect, silently dies hours later,
comes back only on manual reconnect. The reconnect-fixes-it tell = a token
problem (not network/credentials — reconnect only supplies a fresh token).
ROOT CAUSE: SMA rotates the refresh_token on EVERY refresh grant and invalidates
the one just used. `api/inverters/sma.py` discarded the new token from the
response → 1st refresh (~1h after connect) worked, 2nd reused the now-dead
original → 401 → dark until reconnect wrote a fresh token.
FIX (mirrors AlsoEnergy, PLUS durable persistence):
- `_get_token` caches the rotated refresh_token (cache tuple is now
  `(access, refresh, expires_at)`), reuses the freshest one, writes it back into
  `config` in place; on 401 clears the dead token so the next call falls back to
  a client_credentials grant.
- The poller (`api/poller.py` `_persist_config_if_changed`) writes the mutated
  config back to the InverterConnection row with `flag_modified(conn,"config")`
  — JSON columns DON'T auto-detect nested mutation. This makes the rotated token
  survive access-token expiry AND a server redeploy (the part even AlsoEnergy
  lacks: it only caches in-memory).
TESTS: tests/test_sma_token_rotation.py (rotation reused+persisted; dead token
cleared). AlsoEnergy already does the in-memory half; SMA needed both.

## 4. Backend fields that never reach the card (FleetStore strips them)
SYMPTOM: a freshly-added backend field (e.g. `source_status`) is correct on the
fleet-tree endpoint but the UI behavior keyed on it never fires.
ROOT CAUSE: the Arrays tab does NOT read the backend fleet-tree directly — it
goes through FleetStore (fleet-store.js), which rebuilds each array in TWO
places: `adaptTree` (on ingest) and `toColumns` (on render). A new column field
must be added to BOTH or it's dropped before the renderer sees it. Same shape as
the GMP-backfill "built but a field got stripped mid-pipeline" class.
RULE: when adding a fleet-tree column field consumed by sandbox.js, grep
fleet-store.js for `adaptTree` + `toColumns` and thread the field through both.

## Source-staleness UX (the "SOURCE OFFLINE" treatment)
`_source_status` returns {state: ok|stale|none, last_report, age_hours};
stale = freshest inverter last_report older than `_SOURCE_STALE_HOURS` (6h).
Ford wanted it UNMISSABLE: sandbox.js array card gets THREE treatments when
stale — a "⚠ SOURCE OFFLINE" corner ribbon (.sb-array--srcout::after), an amber
glowing card frame, and the in-card banner. Copy must make OWNERSHIP explicit:
"data outage at the SOURCE — not Array Operator." Fresh/none render nothing.
Verify the banner end-to-end through the REAL FleetStore ingest (stub the
fleet-tree fetch response), not a DOM hack — a DOM hack won't catch the
adaptTree/toColumns strip bug above.
