# Inverter portal access — login automation, API-key path, scraper spike

How to get live inverter data into the Array Operator engine. Covers SolarEdge (proven
this session) and the general rule for every vendor.

## The strategic rule (state this to Ford every time)

For any inverter brand, there are two ways in, and **the API key beats driving the owner's
login** wherever an official API exists:

- **API-key path (gold):** owner/you grabs the key from the portal → paste it → backend
  pulls all sites. Stable, structured, historical, zero lockout risk. SolarEdge account-level
  key + the built `/v1/array-owners/solaredge/discover` ("1 key → all arrays") is the gold
  path for Bruce. The login PASSWORD is NOT what we want — only the API key flips the
  peer-analysis engine from demo to live. The portal login is merely the means to reach the
  key (Admin → Site Access → API Access).
- **Extension-scraper path:** only earns its place where NO usable API exists — **Fronius**
  (cloud API paid + not-US; only free path is the LAN Solar API the Railway backend can't
  reach but the owner's browser CAN) and **Chint/CPS** (no public API at all). SMA = zero-setup
  fallback to its OAuth friction. Don't build a scraper where an API key works.

**Always offer Ford the API-key path FIRST** when he hands you a portal password for an
API-capable brand — it sidesteps bot-detection, security emails, and lockout on a live
customer account (Bruce's is the live pilot). Driving the login is the riskier second choice.

## Driving the SolarEdge monitoring login via playwright (proven Jun 2026)

Playwright + chromium are NOT in solar-operator; they're installed in **`~/array-operator`**
(`~/array-operator/node_modules/.bin/playwright`, chromium cached at
`/root/.cache/ms-playwright`). Run login scripts with `cd ~/array-operator` +
`export NODE_PATH="$(pwd)/node_modules"` so `require('playwright')` resolves. (General Node
rule: the script's resolution walks up from its own dir; running `node /tmp/x.js` fails to
find modules — set NODE_PATH or place the script in the repo.)

**The flow is AWS Cognito, two screens:**
1. `https://monitoring.solaredge.com/solaredge-web/p/login` → redirects to a micro-frontend
   landing (`/mfe/auth/`) that shows only a **"Log in" button**, no fields. Click it.
2. It opens `login.solaredge.com` (Cognito). Real form fields:
   - email: `input[type=email]` / `name="username"` (placeholder `name@host.com`)
   - password: `input[type=password]` / `name="password"` (placeholder `Enter password`)
   - submit: the **"Sign in"** button (there are TWO "Sign in" buttons — the first is the
     personal-account one; the second is "Corporate email". Use the first / role-based
     `getByRole('button',{name:/sign in/i}).first()`). There's also a hidden `csrf` and
     `cognitoAsfData` — don't touch.
3. No CAPTCHA / no 2FA observed for this account — a wrong password yields a clean inline
   error **"Invalid input: Incorrect username or password."** (visible in the screenshot).

**Verify success by SESSION PROBE, not the screenshot.** After submit the URL staying on
`login.solaredge.com` OR `/mfe/auth/` = NOT logged in. The reliable check: save
`ctx.storageState({path})`, then in a fresh context load that state and `goto` the dashboard
(`/solaredge-web/p/home`); if it bounces back to `/mfe/auth/` you're not authenticated. A
JSESSIONID cookie alone does NOT prove auth — it's set pre-login too.

**Stop at TWO failed attempts.** Repeated Cognito failures can lock the account or fire a
security email to the (live-customer) owner. One clean rejection is safe; hammering is not.
If both attempts fail it's almost always (a) wrong password (check case — Ford sent
`778300Aa++` then `778300aa++`, both rejected → likely a different/changed password) or
(b) wrong portal login EMAIL (owner's monitoring login may differ from contact email, or the
arrays live under the INSTALLER's account). Escalate to the API-key path rather than guessing.

## Scraper spike (only for no-API brands)

Cannot write scraper selectors blind — without a real portal login or a saved HTML/JSON
sample, any selectors are fabricated guesses that break on contact (violates the project's
"never fabricate an integration" rule). The gating dependency for ANY inverter-scraper build
is: a real portal session to inspect, or saved portal HTML/JSON. The capture normalizes to a
"unit" `{nameplate_kw, daily kWh, error_code, last_report}` and drops into
`api/inverters/peer_analysis.py analyze_cohort()` with zero engine rework. Full plan:
`docs/plans/2026-06-13-extension-inverter-capture.md`.

## Building an extension scraper for a NEW vendor — the HAR-grounding workflow (proven Fronius, Jun 2026)

The repeatable recipe for adding a no-API vendor (Fronius proved it; reuse for SMA/Chint).
The whole thing is gated on one artifact: a **HAR file of the owner's logged-in portal**.
Ford captures it; you parse it; you NEVER guess endpoints.

**Step 0 — get the HAR (Ford does this, walk him through it carefully).** He found "Save all
as HAR" the hardest step — the exact instructions that worked: F12 → Network tab → check
"Preserve log" → clear (🚫) → filter to **Fetch/XHR** → click around the dashboard → right-click
the request list → **"Save all as HAR with content"** (or the ⬇ download-arrow in the Network
toolbar) → save to Desktop. He saves to `OneDrive/Desktop` sometimes, not `Desktop` — search
BOTH (`/mnt/c/Users/fordg/Desktop` and `/mnt/c/Users/fordg/OneDrive/Desktop`), and pick the
NEWEST by mtime (`ls -la --time-style=...`), since old HARs linger and get deleted between
captures.

**Step 1 — parse the HAR in execute_code, never dump it into context.** It's large JSON with
secrets. Read URLs + response *shapes*, scrub cookies/Authorization. Pattern: load
`har["log"]["entries"]`, filter to the portal host (skip static assets + analytics/dynatrace
`/rb_` noise), print each `(method, path, response-size)` then the small JSON bodies' key
structure. The fat (>30KB) responses are usually the per-device data.

**Step 2 — the DRILL-DOWN is a separate capture.** A portfolio/list HAR shows only SYSTEM
totals + an inverter COUNT — NOT per-inverter data. To get the real per-inverter comb you need
a SECOND HAR where Ford opens ONE system's **Analysis** view and drills into devices. (Fronius:
the portfolio view fired `/PvSystems/GetPvSystemsForListView` + `/ActualData/GetActualValues`
(system totals, WATTS); the analysis drill-down fired `/Chart/GetAnalysisChart` which returns
`deviceChannels.devices[]` = every inverter + a per-device power-curve series. `isPremiumFeature:true`
at the top is a RED HERRING — the channel we use, `devwork`/Total Power, is `isPremiumChannel:false`
and reads on a normal account.) Ask "what level of detail does drilling in show?" before assuming.

**Step 3 — PROVE the transform against the real HAR before trusting it.** Run the scraper's
pure functions (integrate-power-curve→kWh, parse-nameplate-from-model-string) against the actual
response in execute_code and sanity-check: e.g. the 12 Fronius inverters' integrated kWh summed
to 99.2% of the portal's system total (gap = mid-day capture, ongoing). A self-reported scraper
is not enough — show the numbers reconcile.

**Step 4 — two capture architectures, pick by whether a usable key exists:**
- SolarEdge-style: scrape an **API key** → broadcast it → page runs the existing preview +
  connect-account flow → backend PULLS via the API. No readings stored.
- Fronius-style (no usable US key): scrape the **READINGS themselves** → POST to
  `/v1/array-owners/inverter-capture` → backend STORES them (DailyGeneration per-array +
  InverterDaily per-inverter) and `build_fleet_tree` falls back to the stored rows when there's
  no live connection. This is the pattern for any no-key vendor.

**Step 5 — wire ALL THREE layers or it's an untestable stub (see the pitfall below).**

## PITFALL — "wire it all up" means the FRONT-END BUTTON too, not just the capture engine (cost a round-trip, Jun 2026)

When Ford says "wire it all up / build the full thing," a portal-capture feature has FOUR layers
and the easy mistake is building the capture engine end-to-end, verifying it by POSTing data
directly to the endpoint, and declaring victory — while the USER-FACING TRIGGER is missing.
Built Fronius capture (scraper + `FRONIUS_CAPTURED` broadcast + `/inverter-capture` ingest +
per-inverter persistence + sandbox-comb rendering), proved every layer against real data... then
Ford asked "if my dad logs into Fronius will it auto-capture?" and the honest answer was NO —
the Array Operator onboarding page had a "Log in with SolarEdge" button but NO "Log in with
Fronius" button, and its message listener filtered `d.provider === "solaredge"` so it IGNORED
the Fronius broadcast entirely. The feature was un-triggerable by a real user.

The FULL chain for an extension capture, all four links required:
1. **Scraper** content-script reads the portal (`<vendor>_content.js`).
2. **Background** broadcasts the capture (`<VENDOR>_CAPTURED` → `broadcastToSoTabs` →
   `SO_CAPTURE_LANDED`) + arms the capture-intent on `OPEN_UTILITY_PORTAL`.
3. **Backend** ingests (`/v1/array-owners/inverter-capture`) + persists + renders.
4. **FRONT-END (the one that's easy to forget):** the AO site (`array-operator/public/
   onboarding.html`, and the sandbox add-array flow) needs a **"Log in with <Vendor>"** button
   that fires `extSend("SO_OPEN_PORTAL", {url, active:true})`, AND a message-listener branch
   `if (d.type==="SO_CAPTURE_LANDED" && d.provider==="<vendor>") on<Vendor>Capture(d)`, AND an
   `on<Vendor>Capture()` handler, AND a post-signup attach branch in `finish()`. Mirror the
   SolarEdge functions (`loginWithSolarEdge`/`onSolarEdgeCapture`) — but note the SHAPE DIFFERS:
   SolarEdge's capture carries an apiKey (runs the preview/connect path); a readings-vendor
   (Fronius) carries `sites[].inverters[]` directly, so `on<Vendor>Capture` renders them without
   a `/public/preview` call and `finish()` POSTs the raw capture to `/inverter-capture`.
   `so_bridge.js` forwards the WHOLE `SO_CAPTURE_LANDED` message verbatim, so any payload (sites,
   inverters) reaches the page intact — no bridge change needed per vendor.

LESSON: for any capture feature, before saying "done," trace the path a REAL USER clicks from
the button to the data landing. "I verified it by curl/POST to the endpoint" proves layers 1–3,
NOT that anyone can trigger it. The button is the deliverable; the engine is plumbing.

## SMA — GROUNDED + SHIPPED (ennexOS, Jun 2026)
SMA has TWO portals with different internal APIs — ASK Ford which URL his address bar shows:
`sunnyportal.com` (classic) vs `ennexos.sunnyportal.com` (newer ennexOS). Bruce is on **ennexOS**
and that's what shipped (v1.9.2, `extension/sunnyportal_content.js`). The classic portal is NOT
covered — different API, would need its own HAR.

**ennexOS GROUNDED CONTRACT (live, plant 8296660 "Timberworks", 7 STP inverters + 1 datamanager):**
- Content script runs on `ennexos.sunnyportal.com` but the API lives on a DIFFERENT host
  `uiapi.sunnyportal.com` (cross-origin → BOTH need manifest host_permissions). **Session-cookie
  auth** — no Authorization/bearer header in the HAR at all (httpOnly cookie scoped to
  .sunnyportal.com), so `fetch(..., credentials:"include")` just works.
- **The killer endpoint — ONE call gets the whole comb:** `GET /api/v1/overview/{plantId}/devices`
  → array of devices, each `{serial, product ("STP 24kTL-US-10" → nameplate via /STP\s*(\d+)k/),
  pvPower (live W), totWhOutToday (daily kWh DIRECTLY in Wh — no curve integration, cleaner than
  Fronius), state (307=OK else fault), inverterComparisonState, componentType:"Device"}`. Filter
  `componentType==="Device" && pvPower!=null` to drop the datamanager (EDMM-10).
- **Plant id discovery:** the SPA URL path `ennexos.sunnyportal.com/<plantId>/monitoring/...`, or
  `GET /api/v1/navigation/menuitems` → `{componentType:"Plant", componentId, name}`.
- Other endpoints (didn't need them, but documented): `GET /api/v1/components/{plantId}/livestatus`
  → `{devicesOnline, devicesCount}`; `POST /api/v1/measurements/search` with `{queryItems:
  [{componentId, channelId:"Measurement.Metering.TotWhOut.Pv", resolution:"OneDay", aggregate:"Dif"}],
  dateTimeBegin, dateTimeEnd}` → full historical daily/monthly/yearly energy (Wh). SMA CAN do
  multi-day history via this — a clean backfill follow-up if ever wanted (the extension currently
  captures today only, like the others).
- There's an UNVERIFIED SMA *API* module (`api/inverters/sma.py`, smaapis.de OAuth) — needs
  developer-app registration + owner consent (high friction). The extension-scrape sidesteps it.
- Backend reused with ZERO new logic: just added `"sma"` to `_CAPTURE_VENDORS` in array_owners.py;
  it flows through the same inverter-capture ingest + InverterDaily + fleet-tree comb built for
  Fronius. Proved live on prod: posted Bruce's 7 real inverters → /fleet-tree returned the 7-prong
  comb, peer-analyzed (15kW + 24kW units correctly nameplate-normalized).

## Lead-with-login UX + the per-PAGE bridge-wiring pitfall (Jun 2026)
Ford's directive: the "Add array" / connect surfaces should **lead with one-click portal login**
("Log in with SolarEdge / Fronius / SMA"), and keep manual API-key entry available but **demoted
behind a button** ("Enter keys manually instead"). When the helper isn't installed, lead with an
"Add the 1-click helper — free" install CTA + a "Re-check" button (`extSend("SO_STATUS_REQUEST")`)
that re-renders the modal the moment `SO_EXTENSION_PRESENT` arrives.

**PITFALL — the extension bridge is wired PER PAGE; a second connect surface has NONE of it.**
The onboarding wizard (`onboarding.html`) had the full bridge wiring (SO_EXTENSION_PRESENT detect,
extSend SO_OPEN_PORTAL, SO_CAPTURE_LANDED listener). The signed-in dashboard's sandbox add-array
modal (`sandbox.js`) had ZERO of it — different page, the wiring doesn't carry over. To add
one-click login to a NEW surface you must re-add: the `let EXT_PRESENT` + portal-URL map, an
`extSend()` helper, a `window.addEventListener("message", ...)` that handles SO_EXTENSION_PRESENT +
SO_CAPTURE_LANDED, and an `extSend("SO_STATUS_REQUEST")` on load (the bridge announces at
document_start, before your listener attaches, so ask explicitly). The signed-in surface is SIMPLER
than onboarding: the session already exists, so a landed capture attaches straight to the account
(SolarEdge → connect-account with apiKey; Fronius/SMA → POST the readings to /inverter-capture) and
re-renders the tree — no signup/preview detour. Verify the deployed bundle actually carries the new
code (`curl .../sandbox.js | grep <new-symbol>`) — the committed-dist / Netlify deploy can serve a
stale bundle. NOTE: a Netlify deploy proving the code SHIPS+PARSES is NOT the same as the live
in-browser click-through working; that needs a real browser with the matching extension version.
