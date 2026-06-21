# Utility-meter data is a PRODUCT REQUIREMENT (Ford, Jun 2026)

Ford's directive: "I need utility-meter data for the system to work." This is a
first-class requirement, NOT optional. Array Operator must gather production data
from the UTILITY METER (GMP, VEC, and other utilities), not only from the inverter
/ monitoring vendor.

## Why this matters (the conceptual split — don't conflate the two)

- **Inverter/monitoring vendors** (SolarEdge, Fronius, SMA, Chint, AlsoEnergy,
  Locus) report PER-INVERTER production. This is what peer analysis ("which
  inverter is underperforming") runs on.
- **Utilities** (GMP, VEC) measure at the METER: net-metered kWh exported,
  consumption. They do NOT see per-inverter breakdown. But the meter is the
  source of truth for what the array ACTUALLY delivered to the grid (and the $
  value / credits) — and it's the ONLY production signal for an owner who has NO
  inverter monitoring. Ford wants this as required coverage so the product works
  for every owner regardless of whether they have an inverter portal.

## What EXISTS today (bills, not interval)

- Utilities are already wired as ADAPTERS (separate from inverter VENDORS):
  `api/adapters/__init__.py` ADAPTERS = {gmp, + every SmartHub utility incl vec}.
  `get_adapter(provider)` routes; new SmartHub co-ops auto-work via `sh_<sub>` prefix.
- `api/adapters/gmp.py`: pulls BILLS — `bill_json_to_metrics` / `extract_bill_metrics`
  yield ONE `kwh_generated` number per BILLING PERIOD (monthly), from the bills
  JSON (`fetch_bills_json` with the GMP JWT) or the bill PDF. This is bill-grain,
  NOT daily/interval.
- `api/adapters/vec.py` + `smarthub.py`: VEC routes through the universal SmartHub
  adapter; the extension captures the SmartHub portal (see smarthub-capture.md).
- GMP has a bespoke live path (BESPOKE_LIVE_CODES={"gmp"} in providers.py;
  gmp_refresh.py mints/refreshes the GMP JWT).

## The GAP to close (the actual work for this requirement)

Bill-grain kWh is too coarse for the array-owner production model (which wants
daily generation like the inverter `fetch_daily`). To satisfy "utility-meter data
for the system to work":
1. Pull INTERVAL / daily net-metered production (not just the monthly bill total)
   from GMP and VEC. GMP's customer portal exposes interval/usage data behind the
   same JWT used for bills — HAR-capture the portal's interval endpoint (same
   method as the Chint/SmartHub recon: open the usage/graph page, capture the
   XHR that returns the time-series). VEC/SmartHub similarly has a usage-data
   endpoint behind the SmartHub session.
2. Normalize utility production into the SAME DailyGeneration shape the inverter
   path writes (source tag e.g. "utility_meter" / "gmp_interval"), so the owner
   overview + value model consume it identically. A utility-sourced array has
   whole-array daily kWh but NO per-inverter rows (peer analysis degrades to the
   array level — already supported; see overview's array-level cohort path).
3. Surface it: an owner can connect a UTILITY (meter production) as an alternative
   to / alongside an inverter vendor. Decide UX — likely a "Connect your utility"
   path distinct from the inverter "Log in with <vendor>" buttons.

## Recon method (proven this project)
Same as every other integration here: get the owner logged into the portal,
HAR-capture the real interval/usage XHR (host + path + response shape), then
write the adapter to the OBSERVED contract — never guess endpoints. GMP and VEC
both need a live login to ground the interval endpoint. Test fixtures: Bruce
(GMP + SmartHub) and Paul Bozuwa (GMP/VEC across his VT sites).

## STATUS: GMP meter capture SHIPPED + DAILY-GROUNDED (v1.9.24, Jun 2026)
GMP is built end-to-end at TRUE daily granularity — see
references/gmp-meter-api-contract.md for the full contract. Gist: extension reads
the GMP JWT from localStorage gmp-vue.user.apitoken; background GMP_FETCH_USAGE
pulls /users/current (48 accounts; solar ones are nicknamed 1a_*/1b_*) then, for
SOLAR accounts only, /usage/{acct}/summary + /usage/{acct}/daily; backend POST
/v1/array-owners/utility-meter-capture writes per-day generation to DailyGeneration
source="utility_meter", creating an Array ONLY for accounts with real generation.

### The two confirmed field facts (grounded on 1a_Chester acct 4392604400, Jun 2026)
1. **kwh_generated = `totalGrossGenerated`** (summary), NOT totalGenerationSentToGrid.
   Chester generates 3560 kWh/period but sends 0 to grid (uses it all on-site) —
   sentToGrid alone would wrongly read 0.
2. **Daily/monthly generation field = `returnedGeneration`** in
   /usage/{acct}/daily|monthly intervals[].values[] (each {date, consumed,
   returnedGeneration}). Confirmed: Chester returnedGeneration 12,608 kWh in Jan.

## VEC + WEC (SmartHub) — VEC CONFIRMED WORKING (v1.9.28); WEC needs its own HAR
VEC (vermontelectric.smarthub.coop) + WEC (washingtonelectric.smarthub.coop) UI is
wired (buttons in sandbox.js: BRAND/PORTAL_URL/LOGIN_VENDORS/handleCaptureLanded;
so_bridge.js forwards the provider hint through SO_OPEN_PORTAL so a shared
smarthub.coop host disambiguates vec/wec). But the v1.9.25 CAPTURE design was
architecturally WRONG and did not work in a live test — see the two-part root cause
below. The fix is a client-side pull (the Chint/Fronius/SMA pattern). VEC is now
CONFIRMED WORKING end-to-end (v1.9.28 — see the working-path section below); WEC
shares the identical code path but is unverified on a real WEC solar meter.

### Why the server-side-pull design failed (BOTH proven from West Glover HAR + a live test, Jun 2026)
The original idea: extension grabs the SmartHub auth token, ships it to a backend
`/v1/array-owners/smarthub-meter-capture` endpoint, backend pulls generation via
adapters/smarthub.py. TWO independent killers, each fatal:
1. **The SmartHub data API authenticates with httpOnly SESSION COOKIES, not a
   bearer token.** The live HAR of POST /services/secured/utility-usage has NO
   Authorization header — auth rides the .smarthub.coop httpOnly cookie (Chrome
   even strips it from the HAR). A backend CANNOT replay a browser's httpOnly
   cookie, so "ship the token, pull server-side" is impossible by construction.
2. **smarthub_content.js runs in the ISOLATED world** (manifest has no
   `"world": "MAIN"` for it). Its window.fetch monkey-patch therefore never sees
   the page's own fetch calls, so the auth token was never captured →
   maybeSendMeterCapture() bailed at `if(!capturedAuthToken) return` → only the
   old bill scrape ran ("Synced → local-only", no meter message). Same trap Chint
   hit, which is exactly why Chint uses a separate MAIN-world chint_inject.js.

### The CORRECT design (do this — mirrors Chint/Fronius/SMA, all of which WORK)
Pull the usage data INSIDE the extension, CLIENT-SIDE, same-origin (the content
script's fetch to vermontelectric.smarthub.coop rides the owner's cookie session
for free — same-origin, no CORS, no token replay). Then ship the parsed daily[]
generation rows to the EXISTING /v1/array-owners/utility-meter-capture endpoint
(the daily[] path already proven + grounded for GMP). The generation PARSER logic
is already grounded and correct (see next section + smarthub.py fetch_daily_generation)
— only the LOCATION of the fetch moves from backend → extension. The data contract
to the backend (daily[] of {date, generated_kwh}) does not change.

### CONFIRMED WORKING end-to-end (v1.9.28, Jun 2026) — VEC live on Paul's West Glover
The client-side pull connected and landed real production on the canvas. The path
that finally worked (4 builds in; the first 3 each fixed one HAR-grounded detail):
- ACCOUNT DISCOVERY: do NOT call GET /services/secured/user-data — it returns HTTP
  401 even though the SAME session cookie 200s on other calls. Instead reuse the
  WORKING bill path: GET /services/secured/billing/history/overview?acctNbr=NNN
  (cookie-only, NO x-nisc header). Its response carries acctNbr + custNbr +
  servLocs[0].id.srvLocNbr (the serviceLocationNumber the usage POST needs) +
  address. acctNbrs come from decodeHashCreds().acctNbr + acctsFromDom() (home-page
  DOM headings) — same sources the bill capture already uses.
- USAGE POST /services/secured/utility-usage: try COOKIE-ONLY first (matches the
  working billing call); add the x-nisc-smarthub-username header only as a fallback.
- TRIGGER: call maybeSendMeterCapture() at the TOP of tryScrape() (fires every
  scrape pass), NOT only from the auth-fetch hook — an already-signed-in owner
  never re-hits /oauth/auth/v2, so a hook-only trigger silently never runs.
- DEBUG LESSON (the one that cracked it): the live [EnergyAgent] console LOG lines —
  not the HAR — showed "user-data failed HTTP 401" right next to "Synced: 36 bill
  rows" on the SAME cookie. When two same-origin calls disagree (one 200, one 401),
  STOP guessing the failing call's auth and REUSE the working call's path. A HAR
  shows a SUCCESSFUL browser request; it can NOT show why the same request fails
  from inside the extension — only the live console can. Ask Ford for the console
  line early next time instead of iterating builds blind.

### The original design rationale (kept for context)
server-side vs client-side capture, check the HAR for how the data API authenticates.
httpOnly session cookie → MUST be client-side same-origin (backend can't replay it).
A durable owner-facing API key/JWT in localStorage (e.g. GMP) → can be server-side.
And ALWAYS verify the content script's world: an isolated-world script can't read
the page's window.fetch/JS; capturing the app's own requests needs a MAIN-world
inject script (Chint pattern). 50+ other SmartHub co-ops in api/data/providers/*.csv
all share this same cookie-auth + client-side requirement.

### VEC generation NOW LIVE-GROUNDED + CLIENT-SIDE SHIPPED (v1.9.26, Jun 2026)
(extension, the live path) were both grounded against Paul's real West Glover
solar account (West Glover Roaring Brook Solar LLC, vermontelectric.smarthub.coop
acct 6578300). The prior HA-integration assumptions were ALL wrong. The REAL contract:
- Endpoint: POST /services/secured/utility-usage (NOT .../poll). Body: timeFrame
  DAILY, screen USAGE_COMPARISON, serviceLocationNumber, accountNumber, industries
  [ELECTRIC], startDateTime/endDateTime as epoch-ms INTEGERS, userId=email.
- Response is the data object DIRECTLY: {"ELECTRIC":[{ "series":[{"name","data":
  [{"x":epoch_ms,"y":kWh}]}], "meters":[{seriesId, flowDirection, isNetMeter}],
  "hasDaily" }]}. NO {status:COMPLETE,data:...} envelope, NO type==USAGE marker.
- GENERATION SIGNAL = NEGATIVE daily y (net export), regardless of meter flags.
  West Glover's meter is tagged flowDirection=FORWARD + isNetMeter=FALSE yet every
  daily y is negative (it net-exports on rate 10NET:COOP). So daily generation =
  abs(min(y,0)) per day; positive y = net consumption. An explicit RETURN
  flowDirection (if a deployment exposes one) is taken directly as generation.
  DON'T trust isNetMeter/flowDirection to decide generation — trust the SIGN.
- Verified: real 31-day HAR → 7,257.6 kWh generated (exact). Account list via
  GET /services/secured/user-data (a LIST; customerName e.g. "WEST GLOVER ...",
  account, address, rate "10NET:COOP"). WEC may differ — still needs its own HAR.
