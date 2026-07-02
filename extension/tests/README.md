# Extension capture-parser regression harness

Automated tests for the **pure parse/derivation logic** in the vendor content
scripts, so changes to capture parsing no longer depend on Ford manually clicking
through a live portal. No browser, no network, no new dependencies.

## Run

    bash extension/tests/run.sh        # exit 0 = all green
    # or directly:
    node --test extension/tests/*.test.js

Uses Node's built-in test runner (node:test + node:assert/strict). Requires
Node >= 18 (developed against v22).

## How it works

Each content script is an IIFE that runs only in the browser. A **browser-inert**
test hook was added at the end of each IIFE:

    if (typeof module !== "undefined" && module.exports) {
      module.exports = { /* pure helpers */ };
    }

In a browser `module` is undefined, so this is a no-op and the runtime is
unchanged. Under Node, require()-ing the file returns the pure helpers. The
IIFE's browser bootstrap (tick() / setInterval / window.addEventListener) and its
load-time location.* reads are gated behind a `_SO_BROWSER` guard so importing
the file for tests never touches a browser global.

## Coverage (36 assertions across 4 vendors)

- **fronius.test.js** (solarweb_content.js): parseAspNetDate (/Date(ms)/),
  nameplateFromModel (first-decimal-as-kW incl. the \b word-boundary edge:
  "Gen24" -> null), deriveNameplateKw (energy/yield), deriveStatus,
  integrateKwh (TRAPEZOIDAL 7-day history integration incl. >1h data-gap skip,
  null-point filtering, unordered-point sort), findLocation / applyLocation /
  _soValidLatLng (GeoJSON [lng,lat] vs [lat,lng], null-island + range rejection,
  address fallback).
- **sma.test.js** (sunnyportal_content.js): nameplateKw (STP product + NkW name,
  incl. 33.3), deriveStatus (ennexOS state===307=OK vs fault), findLocation deep
  scan, _soValidLatLng guards. Covers SMA multi-plant nameplate parsing.
- **chint.test.js** (chint_content.js): parsePowerToW (W/KW/MW), num/kwFromStr,
  mapStatus, invertersFrom (inverter vs meter/gateway filter; TRANSIENT-0 HOLD
  over a known-good reading vs honest off-state 0), countInverters,
  weekTrendDaily (YYYYMMDD), siteIdFromSearch, dailyFromChart (30-min PV curve ->
  daily kWh, URL-driven interval), mergeDaily (max-wins union).
- **solaredge.test.js** (solaredge_content.js): _parseApiKey (bare token / JSON
  string / JSON object scan / .data nesting), _mapSites (searchSites page -> site).

Fixtures are inline and mirror the exact JSON shapes documented in each content
script's header comment (all HAR-grounded against live accounts).

## What this does NOT cover (still needs real-Chrome e2e)

- The async capture orchestration: captureFlow / captureInverters /
  captureOnePlant / the Chint passive-observation walk. They wrap fetch /
  chrome.runtime / window.postMessage and are exercised only by a live portal.
- **Per-inverter power allocation by energy share.** The content scripts emit raw
  per-inverter + site-level readings; the BACKEND splits/allocates site power
  across inverters (see the note in sunnyportal_content.js ~L419). Test that in
  the backend suite, not here.
- **SMA freshness / epoch-timestamp sanitation.** SMA source-freshness is derived
  inside the async fetchSiteLivePowerW from the gauge timestamp (not a pure
  export). There is currently NO _TS_SANITY_FLOOR_YEAR / 1970-epoch floor in these
  content scripts; if that guard is added later, extract a pure helper + test it.
- Auth / intent gating, CORS/Bearer handling, the SPA route-nudge + auto-walk,
  live-power freshness windows: browser-runtime behavior, validate with a real
  logged-in portal (Ford's manual click, or a future Puppeteer harness).
