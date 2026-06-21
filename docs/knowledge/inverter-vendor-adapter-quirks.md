# Inverter / vendor adapter quirks — live-verified gotchas (Jun 2026)

Class: per-vendor live-power + timestamp adapters in `api/inverters/*.py`,
`api/adapters/*.py`, and the extension content scripts. Each vendor lies in its
own way. Every fix below was confirmed against a REAL live account — do not
"fix" a units/tz/freshness assumption without a real daytime reading.

## The recurring meta-pattern
A vendor feed that returns HTTP 200 with no error but serves WRONG/STALE/zero
data → the UI faithfully renders it → reads as "our app is broken." For an
auditor/monitor product this is existential (trust). Three defenses that paid off
this session:
1. Add LOUD per-gate diagnostic logging so a silent return tells you WHERE it
   stopped (see Fronius below) — don't iterate builds blind.
2. Distinguish "no feed" vs "stale 0" vs "real 0 (night)" in the data + UI.
3. Surface freshness/last-seen and attribute the gap to the SOURCE, not us.

## SMA — OAuth refresh-token ROTATION (`api/inverters/sma.py`)
SYMPTOM: "SMA worked until I reconnected." Works right after connect, dies hours
later, comes back only on reconnect.
ROOT CAUSE: SMA rotates the refresh_token on EVERY refresh grant and invalidates
the one just used. The adapter discarded the new token → first refresh OK →
second reuses the dead original → 401 → plant dark until manual reconnect (which
writes a fresh token). The tell that it's THIS and not a blip/creds: reconnecting
fixes it (reconnect only supplies a new token).
FIX: cache `(access, refresh, expiry)`; reuse the freshest refresh_token; write
the rotated token back into `config` in place; on 401 clear the dead token so the
next call falls back to client_credentials. AlsoEnergy already did the in-memory
half (`new_refresh = body.get("refresh_token") or refresh_token`) — SMA lacked it
AND lacked DB persistence.
PERSISTENCE: the poller must write the mutated config back. `poller._persist_config_if_changed(conn, cfg)`
re-assigns `conn.config = cfg` + `flag_modified(conn, "config")` (JSON columns do
NOT auto-detect nested mutation) so the rotated token survives access-token expiry
AND a redeploy (in-memory cache alone dies on every `railway up`).

## SolarEdge — timestamps are SITE-LOCAL, not UTC (`api/adapters/solaredge.py`)
SYMPTOM: "GMP says Londonderry out 24h, our system says 19h" — a multi-hour gap
that's ~the VT UTC offset (4–5h).
ROOT CAUSE: SolarEdge equipment telemetry `date` + overview `lastUpdateTime` are
site-local time with NO tz marker; `_source_status` read naive ts as UTC →
VT site (America/New_York) looked ~4h MORE stale than reality (overstates outage).
FIX: fetch the site's IANA `timeZone` from `/site/{id}/details` (cached per site
in `_SITE_TZ_CACHE`), convert naive→UTC via `_localize_to_utc_iso`. Unknown tz →
leave naive (pre-fix behavior). This is a FLEET-WIDE bug class (every SE site),
fixed at the adapter. The corrected age only refreshes on the NEXT poll (re-stamps
last_report). NOTE: zoneinfo is stdlib 3.9+.
SECOND, SEPARATE truth: GMP times the utility METER; we time INVERTER telemetry —
different feeds, will never match exactly even with correct tz. Label the card's
outage clock as the inverter feed so the two numbers aren't conflated.

## Fronius (Solar.web / solarweb_content.js) — devwork is WATTS, freshness ~30min
UNITS: the `/Chart/GetAnalysisChart?channels=devwork` per-inverter "Total Power"
series is in WATTS, not kW. Live-verified: a Primo 12.5kW inverter read ~1699 →
1699 W = 1.7 kW (1699 kW impossible). Normalize the series ÷1000 ONCE so
integrateKwh, peak, and the live point are all kW (current_power_w then ×1000
back to W). The diagnostic LOG line "per-inverter last devwork point" is exactly
what settles units — ASK FOR THAT LIVE LINE rather than guessing.
FRESHNESS: Solar.web's chart cadence is ~30 min between points; a midday capture
read points 34–35 min old. A 30-min LIVE_FRESH_MS rejected legitimately-recent
readings → card stuck "no live feed." Use 60 min.
SELF-DIAGNOSING CAPTURE: the content script returned silently at each gate (no
intent / not signed in / capture error). Added loud `[solar-operator/fronius]`
logs: on load ("content script loaded vX … if you don't see this it's not
injected"), per tick (intent yes/NO, signed-in yes/NO, captured systems+inverters
count), capture-flow errors logged not swallowed, "✓ capture complete". This made
the daylight verification self-explaining instead of a blank console.

## Extension build + delivery (recap, see also other refs)
- NOT auto-deployed: Ford loads unpacked / uploads manually. BUMP manifest
  version on every change. Build: `bash scripts/build_extension_zip.sh` → reads
  manifest version, zips, copies to /mnt/c/Users/fordg/Desktop/ (root + Archives).
  VERIFY the fix is actually IN the zip (unzip + grep) before telling Ford.
- Sharing a build link to a real person (e.g. Bruce): the established pattern is a
  GitHub Release with the zip asset — `gh release create ext-vX.Y.Z <zip>#<name>
  --target $(git rev-parse HEAD)` (tag isn't pushed; pass --target). VERIFY the
  asset downloads (curl -sL -w '%{http_code} %{size_download}') before sending.
  Repo Garface111/solar-operator is PUBLIC so the release link is openly grabbable.
