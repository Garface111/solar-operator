# EnergyAgent extension — no-API vendor capture (SolarEdge / Fronius / SMA / …)

The "extension-capture" vector: for vendors whose official API is high-friction
(paid, dev-app registration, owner consent), the Chrome extension reads the
owner's per-inverter readings straight from the portal they're already logged
into, and POSTs them to `/v1/array-owners/inverter-capture` (vendor must be in
`_CAPTURE_VENDORS`). The ingest endpoint + peer/fleet engine are vendor-agnostic.

## Adding a new no-API vendor (checklist)
1. Grab a REAL portal HAR (never write a scraper blind — project rule: never
   fabricate an integration). Drill into the per-device/analysis view so the HAR
   captures the per-inverter endpoint, not just system totals.
2. Write `<vendor>_content.js` grounded on that HAR.
3. manifest: add the portal host (AND the API host if different — see CORS below)
   to host_permissions + a content_scripts entry.
4. background.js: add a `<VENDOR>_CAPTURED` → `SO_CAPTURE_LANDED` broadcast, and
   arm the capture intent on `SO_OPEN_PORTAL` for the vendor's host.
5. Backend: add the vendor string to `_CAPTURE_VENDORS` (api/array_owners.py).
6. AO frontend (onboarding.html + sandbox.js): "Log in with <vendor>" button +
   route the landed readings to ingest.

## ⚠️ CRITICAL: decide server-side vs client-side capture FROM THE HAR's auth
Before building a vendor, read the HAR to see how its DATA API authenticates — it
dictates the ONLY viable capture architecture. Two cases (VEC cost a full wrong
build, v1.9.25, by guessing this):

- **httpOnly SESSION COOKIE auth** (NISC SmartHub: POST /services/secured/* has NO
  Authorization header — auth rides the .smarthub.coop httpOnly cookie; Chrome even
  strips it from the HAR). A backend CANNOT replay a browser httpOnly cookie, so a
  "ship the token to the backend, pull there" design is IMPOSSIBLE by construction.
  → MUST pull CLIENT-SIDE, same-origin (the content-script fetch rides the cookie
  for free), then POST the parsed daily rows to the existing capture endpoint.
- **Durable owner API key / JWT in localStorage** (GMP: gmp-vue.user.apitoken is a
  bearer JWT). → CAN ship that token to the backend for a server-side pull (GMP
  GMP_FETCH_USAGE does exactly this). Cross-origin from the SW is fine here because
  the bearer travels in a header, not a cookie.
RULE: cookie-auth ⇒ client-side same-origin only. Header bearer/key ⇒ either works.

## ⚠️ CRITICAL: ISOLATED vs MAIN world — capturing the page's own requests
A content script in the default ISOLATED world CANNOT see the page's `window.fetch`
/ XHR or JS vars. Monkey-patching `window.fetch` from an isolated-world script to
grab a token or response is a NO-OP — it patches the isolated copy, not the page's.
Symptom (VEC v1.9.25): the token was never captured, the gated capture silently
bailed, and only the unrelated bill scrape ran. To observe the app's OWN requests
you need a SEPARATE content script with `"world": "MAIN"` in the manifest that
relays bodies to the isolated script via window.postMessage. Reference impl: Chint
(chint_inject.js MAIN-world hooks XHR+fetch → posts SO_CHINT_RESPONSE →
chint_content.js reads it). Decide world up front: if your plan says "intercept the
portal's fetch," you need a MAIN-world inject script, full stop.

## ⚠️ CRITICAL: same-origin vs cross-origin API (the CORS stall)
A content-script `fetch(..., {credentials:"include"})` obeys the PAGE's CORS
policy in MV3. host_permissions grant cross-origin+cookies ONLY to the background
service worker, NOT to content-script fetches. So:

- **Same origin** (Fronius: page+API both www.solarweb.com; SolarEdge: both
  monitoring.solaredge.com): fetch RELATIVE paths directly from the content
  script. No CORS. Simple.
- **Different origin** (SMA: page on ennexos.sunnyportal.com, API on
  uiapi.sunnyportal.com): a credentialed content-script fetch HARD-BLOCKS when the
  API answers `Access-Control-Allow-Origin: *` (browser won't pair `*` with
  credentials). **Symptom: the capture spinner STALLS forever, retries silently,
  no data, no obvious error.** This cost a full debug cycle (SMA v1.9.3→v1.9.4).
  FIX (canonical MV3): add an `<VENDOR>_API_GET` proxy handler in background.js,
  HARD-allowlisted to the API origin, that does the credentialed fetch CORS-free;
  the content script routes every API GET through
  `chrome.runtime.sendMessage({type:"<VENDOR>_API_GET", url}, cb)`.
  Reference impl: SMA `smaApiGet()` (sunnyportal_content.js) + `SMA_API_GET`
  handler (background.js).
  DIAGNOSE from the HAR: look at the API response's `Access-Control-Allow-Origin`
  header and the request's `Sec-Fetch-Site` (`same-site`/`cross-site`). Cross-origin
  + ACAO:* ⇒ you MUST proxy through the SW. (Caveat not yet confirmed live: if the
  session cookie is SameSite=Strict, even the SW fetch may not send it — check the
  extension's service-worker console for `<VENDOR>_API_GET` errors.)

## ⚠️ Capture intent only arms from the in-app button
The content script captures ONLY when `so_capture_intent` is set, which happens on
`SO_OPEN_PORTAL` (the AO "Log in with <vendor>" click). Logging into the portal
DIRECTLY captures nothing — deliberate privacy guard. Any "it's not pulling" test
MUST start from +Add array → Log in with <vendor>, not a manual portal login.

## ⚠️ Sandbox must re-fetch after capture (and show the FULL fleet)
`load()`→`renderFromStore()` only re-fetches when the FleetStore isn't loaded; on
the dashboard it's always loaded, so a successful capture re-renders STALE memory
and the new array never appears. After a connect/capture: call
`FleetStore.refetch()` (now exported) then `setFocus(<all array ids>)`.
Do NOT reset to `defaultFocusIds()` — it collapses the view to the "worst few"
flagged arrays and HIDES everything else, which looks exactly like a data wipe.
The capture backend is purely additive (match Array by name or create, never
deletes), so a user reporting "my arrays disappeared / X took over" is ALWAYS a
view-filter bug, never real loss — reassure + fix the focus, don't hunt the DB.

## ⚠️ CRITICAL: "cards appear but no live data streams" = a SILENTLY-DROPPED capture field (Chint, Jun 2026)
Symptom (exact, from a screenshot): right after a Chint/Fronius login the inverter
cards show up INSTANTLY and read "All good", but every card says "not producing
right now" with no kW and "no history yet" — even mid-day on healthy panels. The
ROWS persisted, but a per-inverter READING vanished. This is the
extension-capture sibling of the provider-keyed silent-drop bug (see SKILL.md
"misattribution bug class"): the data was captured and then thrown away on ingest.
ROOT CAUSE (verified): the content script captured `current_power_w` per inverter
(Chint `commDevice.currentPower`, grounded live on Bruce's Londonderry — real watts
like 51000.0), but the backend `CaptureInverter` Pydantic model (api/array_owners.py)
had NO `current_power_w` field. **Pydantic silently discards unknown fields**, so the
real reading was deleted the instant it arrived; the backend then fell back to
allocating a site-level total across inverters by energy share, and when that site
field was absent/missing every card got None → "not producing." FIX: add the field
to `CaptureInverter` AND make the ingest PREFER the inverter's own measured reading
(`if ci.current_power_w is not None: iv.last_power_w = …`), keeping the site-allocation
split only as the fallback for vendors that report power site-wide (Fronius gives
only a site reading; Chint/SMA give per-inverter). This was a pure BACKEND fix — no
extension rebuild needed, so it goes live for the already-installed extension on deploy.
RULES this burns in:
- When a capture vendor shows rows but a value is blank, FIRST diff the content
  script's emitted per-inverter object against the `CaptureInverter`/`CaptureSite`
  Pydantic schemas. Any field the script sends that the model doesn't declare is
  SILENTLY DROPPED — that's the #1 suspect, not a portal/auth problem.
- Real per-inverter readings BEAT derived/site-allocated estimates — prefer the
  measured value, demote the split to a fallback, and comment which vendors need which.
- "No history yet / graph appears once 2+ days are stored" is SEPARATE and EXPECTED:
  capture vendors ship only TODAY's energy, so the sparkline/min-max/peer-index fill
  in as the daily snapshot job accumulates days. Don't conflate it with the live-power
  bug. INSTANT graph backfill is now BUILT for EVERY vendor (Ford: "needs to work with
  every vendor"): each content script emits a SITE-level `site.daily[]` from history
  the portal already exposes (Chint `weekETrend`, Fronius `/Chart/GetAnalysisChart`
  per day, SMA `measurements/search` OneDay/Dif; SolarEdge already native per-inverter).
  Backend ingest + fleet-tree `daily` column + `arrayGraph` fallback are vendor-agnostic.
  Full per-vendor recipe + the SITE-level-only honesty rule + the adaptTree/toColumns
  drop trap: see extension-capture-mv3-debugging.md §6g / §6g-bis.
- Diagnosing this class is FAST with the screenshot: "the latest screenshot" in
  OneDrive/Pictures/Screenshots 1/ IS the bug report — vision-read it, the card text
  ("not producing right now" + "no history yet") pins live-drop vs history-gap exactly.
- Regression-test the field survival: POST a capture with per-inverter `current_power_w`
  and assert it lands as `Inverter.last_power_w` AND surfaces on `/fleet-tree`
  (tests/test_array_owners.py::test_inverter_capture_chint_keeps_per_inverter_live_power).
- Inverter-card DAY/NIGHT ("Sleeping") state: `/fleet-tree` exposes a server-computed
  `is_daylight` flag (per column + in `summary`). Gate the calm "Sleeping" card state on
  `is_daylight==False AND output==0`, NEVER zero-output alone (a noon fault that zeroes
  output would otherwise mask a real outage). Computed via NOAA solar elevation at a
  regional VT default — there is NO stored lat/long anywhere (verify before trusting a
  spec that claims it). Full recipe: extension-capture-mv3-debugging.md §6g-ter.

## SMA / ennexOS specifics (proven Jun2026, v1.9.4)
- Portal: ennexos.sunnyportal.com (the NEWER ennexOS portal — NOT classic
  sunnyportal.com). API: uiapi.sunnyportal.com (cookie-authed, cross-origin → SW
  proxy required, see CORS above).
- The "Log in with SMA" button lands on the PORTFOLIO ROOT (URL has no plant id).
  /navigation is a tree-walker:
  - `GET /api/v1/navigation` (bare) → owner's Plant list
    `[{componentType:"Plant", componentId, name}]`  ← enumerate ALL from root
  - `GET /api/v1/navigation/menuitems?componentId=X` → that Plant (name)
  - `GET /api/v1/navigation?parentId=X` → children (devices) of plant X
  - `GET /api/v1/overview/{plantId}/devices` → the per-inverter comb: serial,
    product (e.g. "STP 24kTL-US-10" → nameplate via /STP\s*(\d+)k/), pvPower
    (live W), totWhOutToday/totWhOutYesterday (daily kWh DIRECTLY — no curve
    integration, cleaner than Fronius), state (307=OK else fault),
    inverterComparisonState. Filter componentType=="Device" && pvPower!=null to
    drop the datamanager (EDMM-10).
  - `GET /api/v1/statuslist/plants?todayDate=…&parentId=` → portfolio status rollup.
- Plant id is also in the SPA URL path: ennexos.sunnyportal.com/<plantId>/...
- Ford's real account = TWO plants: Timberworks (8296660) + Tannery Brook
  (14993829). A single-plant assumption silently misses the second — always
  enumerate. (Tannery Brook's per-inverter data unverified as of v1.9.4 — only its
  enumeration is proven; same endpoint shape so it should work.)
