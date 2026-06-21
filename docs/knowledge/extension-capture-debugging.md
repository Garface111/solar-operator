# Browser-Extension Vendor Capture — Debugging Recipe & Auth Patterns

How the EnergyAgent Chrome extension pulls per-inverter data from vendor portals
(SolarEdge, Fronius/Solar.web, SMA/ennexOS) into Array Operator, and how to debug
it when a vendor "stalls" or returns nothing. Distilled from the SMA fix saga
(v1.9.2 → v1.9.8, June 2026).

## The capture architecture (so you know where to look)

1. AO page (arrayoperator.com) injects `so_bridge.js` (document_start) which
   auto-announces `SO_EXTENSION_PRESENT` — THIS is the AO "handshake". It is NOT
   the popup's "Paired" status. **"Not paired" in the popup is the NEPOOL
   tenant_key mechanism and is IRRELEVANT to Array Operator capture.** Do not
   chase it when AO capture fails.
2. User clicks "Log in with <vendor>" → background.js arms
   `so_capture_intent {vendor, ts}` in chrome.storage.local, opens the portal tab.
3. The vendor content script (`<vendor>_content.js`) polls: checks intent →
   checks signed-in → runs captureFlow → sends `<VENDOR>_CAPTURED {payload}`.
4. background relays as `SO_CAPTURE_LANDED`; so_bridge forwards to the AO page;
   sandbox.js `handleCaptureLanded` posts to the backend and refetches the canvas.

## THE most important auth fact: same-origin vs cross-origin

- **SolarEdge & Fronius** content scripts fetch RELATIVE paths on their OWN
  origin (monitoring.solaredge.com, www.solarweb.com) → same-origin, cookies ride
  automatically, no CORS. `credentials:"include"` works.
- **SMA is the odd one out**: the portal is `ennexos.sunnyportal.com` but the API
  is `uiapi.sunnyportal.com` → CROSS-ORIGIN. And SMA does NOT use cookies at all.

## SMA / ennexOS auth — the canonical pattern (THE fix)

ennexOS authenticates to uiapi with a **Keycloak OAuth Bearer token**, NOT a
cookie. The token is in the PAGE's `localStorage` under the key `access_token`
(alongside `id_token`, `refresh_token`, `expires_at`). Content scripts share the
host page's localStorage, so:

```js
const tok = localStorage.getItem("access_token");
const r = await fetch(url, {
  headers: { "Authorization": "Bearer " + tok, "Accept": "application/json" },
  // NO credentials:"include" — that triggers the CORS wall below
});
```

**Why `credentials:"include"` is fatal here:** uiapi returns
`Access-Control-Allow-Origin: *`, and the browser HARD-REFUSES to pair a wildcard
`*` with credentials mode `include` (console: *"The value of the
'Access-Control-Allow-Origin' header in the response must not be the wildcard '*'
when the request's credentials mode is 'include'"*). Non-credentialed + Bearer
header passes CORS cleanly. (A background-service-worker proxy was tried and is
ALSO wrong — the worker isn't same-site so the cookie still wouldn't ride, and
cookies aren't the auth anyway.)

### ennexOS API map (all GET, Bearer-authed, on uiapi.sunnyportal.com)
- `/api/v1/navigation` (bare, no params) → **portfolio root: array of Plants**
  `[{componentType:"Plant", componentId, name}]`. Use this to ENUMERATE all
  plants. The "Log in with SMA" button lands on the portfolio root (no plant id
  in URL), so you MUST enumerate — a single-plant assumption misses plants.
- `/api/v1/navigation/menuitems` → Portfolio (componentId null) at root;
  `?componentId=X` → that Plant (carries display name).
- `/api/v1/navigation?parentId=X` → children (devices) of plant X.
- `/api/v1/overview/{plantId}/devices?todayDate=YYYY-MM-DD` → **per-inverter
  comb**. ⚠️ `todayDate` is REQUIRED — without it the server returns **500**.
  Use the browser's LOCAL date. Filter: keep `componentType==="Device" &&
  pvPower != null` (drops the non-producing datamanager EDMM-10; keeps the STP
  inverters). Fields: `serial`, `product`, `pvPower` (W), `totWhOutToday` (Wh→kWh).

Ford's real SMA account = TWO plants: Timberworks (8296660, 7 inverters) and
Tannery Brook (14993829). Enumeration is mandatory to get both.

## The debugging recipe (how the fix was actually found)

Symptom: "stalled" / "spinning circle of light forever" = the content script is
silently retrying and never broadcasting success OR failure.

1. **Stop shipping blind fixes after ~2 failed attempts.** Ford's rule: switch
   from speculative fixes to INSTRUMENTING from real evidence. Each blind guess
   burns a full user test cycle.
2. **Make silent stalls VISIBLE.** Two instrumentation moves that cracked it:
   - On retry-budget exhaustion, broadcast a `*_CAPTURE_FAILED {reason}` message
     (relayed → SO_CAPTURE_FAILED → shown in the AO modal) instead of hanging.
   - Add a loud, prefixed console trace (`console.log("[EnergyAgent SMA]", ...)`)
     at EVERY step: load, each tick, hasIntent, signedIn, every GET + status,
     captureFlow site count, final CAPTURED. **"Nothing in the console" is
     meaningless if the script logs nothing** — instrument first, then ask.
3. **Read the symptom precisely:** `status=401` ≠ CORS. 401 = reached server,
   not authed (→ wrong/absent credential). `net::ERR_FAILED` + CORS text =
   blocked before send. `status=500` = authed but bad request (→ missing
   required param). Each points to a different fix.
4. **Confirm the auth mechanism from the browser, not the HAR.** Chrome STRIPS
   `Authorization` headers from exported HARs, so "no auth header in HAR" does
   NOT mean cookie auth. Have the user paste in the portal-tab console:
   ```js
   console.log('LS:', JSON.stringify(Object.keys(localStorage)));
   console.log('SS:', JSON.stringify(Object.keys(sessionStorage)));
   console.log('COOKIE:', document.cookie);
   ```
   A key literally named `access_token` / `id_token` = OAuth/Bearer, not cookie.
5. **Diff the working URL against yours.** For the 500, comparing the HAR's
   working call (`.../devices?todayDate=2026-06-15`) to ours (`.../devices`)
   revealed the missing required param instantly.

## HAR-grounding rule (never fabricate an integration)

Build scrapers against REAL captured HARs, not assumptions. Pitfall hit this
session: an early HAR was captured while already INSIDE a plant, so it never
showed the portfolio-root plant-list call — leading to a single-plant assumption.
When you need a behavior the HAR doesn't show, ASK the user to capture the right
HAR (e.g. "log in, STAY on the portfolio root, don't click into a plant, save
HAR") rather than guessing the endpoint. Save new HARs under a distinct name so
they don't overwrite existing ones.

## Data ceiling: per-INVERTER, never per-PANEL (gates "copy vendor feature X" asks)
What the extension captures is **per-inverter** ("the comb") — one row per
inverter with `pvPower` + today's kWh. It does NOT capture per-PANEL / per-optimizer
telemetry. This gates product features: when asked to "copy SolarEdge's site
layout" (the photo-real roof map where each panel is drawn and colored), the honest
answer is that a true panel-level map is NOT buildable from our data — SolarEdge's
view works because every panel has an optimizer reporting individually, and even
the captured SolarEdge API key exposes inventory/production but NOT saved layout
coordinates. The buildable + recommended version is a per-INVERTER spatial layout
on the EXISTING AO fleet canvas (`fleet-store.js` + sandbox): draggable inverter/
array tiles, color-coded by live status, saved to the account — and it's
vendor-AGNOSTIC (SolarEdge + Fronius + SMA in one view), a differentiator rather
than a copy. General rule for "copy feature X from vendor portal Y": first check
whether the feature needs data below the per-inverter granularity we capture; if
so, say so plainly and pivot to the inverter-level equivalent rather than promising
a 1:1 copy.

## Diagnostic logging convention (gate, don't delete)
The `console.log("[EnergyAgent SMA]", ...)` traces that cracked the saga are kept
in `sunnyportal_content.js` behind a single `const SMA_DEBUG = false;` gate (the
`LOG()` helper early-returns when false; the two inline `getJson` logs check
`if (SMA_DEBUG)`). Declare the flag at the TOP of the IIFE with the other consts —
`getJson` runs before later code, so a flag declared mid-file throws "Identifier
already declared" / use-before-declare. Re-enabling for future debugging is a
one-line flip to `true`, rebuild, reload. Apply the same gate pattern to any other
vendor content script you instrument — ship prod quiet, keep the instrumentation.

## FIRST STEP of any extension TEST/DEBUG session: rebuild, don't assume
Source in `extension/` can be SEVERAL versions ahead of the newest BUILT zip on
the Desktop — a vendor's code (content script + manifest entry + background
handlers) can be fully committed yet absent from every loadable artifact. So
nothing is testable in a browser until you build. **Before doing anything else
in a "make vendor X work" / "test the extension" session:**
1. Compare source vs newest build:
   `grep '"version"' extension/manifest.json` vs the highest
   `Archives - Extension Builds/solar-operator-extension-v*/` folder, and confirm
   the vendor file is actually in that built folder
   (`ls "<build>/" | grep <vendor>` — e.g. chint_content.js was in v1.9.11 SOURCE
   but the newest BUILD was v1.9.10 with no chint file → nothing to load).
2. If stale, `bash scripts/build_extension_zip.sh` immediately, then VERIFY the
   built artifact (vendor file present, manifest version, host_permissions +
   background handler count) before telling the user it's ready.
This is a recurring trap: the groundwork "exists" in the repo but the testable
thing does not. Build first, verify the artifact, then test.

## Inverter capture HAS a "Log in with <vendor>" UI — in a DIFFERENT repo (don't repeat this mistake)
CORRECTION (June 2026): the "Log in with Fronius/SMA/Chint" buttons DO exist and
are LIVE — but NOT in `solar-operator/web`. They live in a SEPARATE repo,
`/root/array-operator` (github Garface111/array-operator, static `public/*.html+js`,
deployed to Netlify `array-operator-ea` = arrayoperator.com). The buttons are in
`public/onboarding.html` (loginWithChint/Fronius/SMA → openPortalLogin) and
`public/sandbox.js` (the Add-array modal: LOGIN_VENDORS + PORTAL_URL + capture
ingest). The earlier "no button exists anywhere" conclusion was WRONG — it came
from grepping only solar-operator/web (the NEPOOL Operator dashboard), which
genuinely is SolarEdge-key-only. **Lesson: before concluding a UI feature is
missing/mangled, confirm WHICH repo serves the live site.** The capture chain
spans both repos: array-operator UI (opens portal tab) → solar-operator/extension
content script scrapes → background.js → SO_CAPTURE_LANDED → array-operator page
POSTs `/v1/array-owners/inverter-capture` → solar-operator/api persists. To change
the portal URL a vendor button opens, edit BOTH `onboarding.html` and `sandbox.js`
in array-operator, then `netlify deploy --prod --dir=public --site=<id>` (this
site is NOT git-auto-deploy: `netlify api getSite` shows `repo_url:null`, so a
push does nothing — you MUST run the CLI deploy). Manual intent-arming via the
console is still valid for testing without going through the UI.

(historical note retained:) The `so_capture_intent {vendor}` flag is armed by
background.js purely off the opened tab's HOST, so capture can be exercised
without the UI by manually arming intent in an extension-context console:
```js
chrome.storage.local.set({ so_capture_intent: { vendor: "chint", ts: Date.now() } });
```

## CHINT / CPS grounding state (GROUNDED June 2026 from Bruce's live HARs)
CHINT connects like Fronius/SMA (no owner API key — Fomware white-label cloud).
Everything is wired (chint in VENDORS + `_CAPTURE_VENDORS`, `chint_content.js`,
manifest, background CHINT refs, tests green). The HARs cracked the two unknowns:

**WRONG HOST was the whole problem.** The extension targeted
`solar.chintpower.com` — but the REAL portal Bruce uses is
**`monitor.chintpowersystems.com:8443`** (note the `:8443` port). That alone is
why every CANDIDATE_* endpoint missed. Update manifest host_permissions +
content_scripts match + background intent-arming host check to
`monitor.chintpowersystems.com` (keep chintpower.com too — Fomware ships several
white-label hosts).

**Auth = custom headers `token`+`loginuserid` PLUS a session cookie (dual-auth).**
Every data call carries `token: <65-char hex>` + `loginuserid: <userId>` +
`platformcode: 3` + `request-origin: web` + `time-zone`. CRITICAL corrections the
LIVE run forced (the HAR alone got this WRONG twice — see the two new sections at
the end of this file): (1) the `_token` in localStorage is **AES-ENCRYPTED**
(CryptoJS `Salted__`/`U2FsdGVkX1` prefix, 128 chars) — you CANNOT send it raw;
you must OBSERVE the real decrypted 65-char token off the page's own requests via
a MAIN-world inject script. (2) the API ALSO needs the **session cookie**, so the
content-script fetch MUST use `credentials:"include"` (the API answers ACAO=
specific-origin + allow-credentials:true; it is cross-origin because the API is on
`:8443` vs the page `:443`). So "no CORS proxy needed" holds, but "same-origin /
just read localStorage" was WRONG. See "ENCRYPTED token → OBSERVE" and "token +
session COOKIE dual-auth → credentials:include" below.

**Grounded endpoint contract (all GET, on `monitor.chintpowersystems.com:8443`,
envelope `{code:"0", msg:"success", data:...}`):**
- whoami → `/api/users/user/getUserInfo?appKey=WEB` → `data.userId`, `data.email`
- site list → `/api/asset/site/retrieve?page=1&limit=20` → `data[]` of sites:
  `id`, `siteName`, `installedCapacity` (kW string), `currentPower` (W string),
  `onlineCount`/`totalCount`, `weekETrend[]`
- per-site devices → `/api/asset/site/busTypeDevices?siteId=<id>` → inverters are
  NESTED at `data.gwDevices[].commDevices[]` (filter `assetTypeName==="Inverter"`
  / `assetType===2`). Per-inverter fields: `sn`, `model`, `currentPower` (W),
  `eToday` (kWh today), `statusName` ("Running"/etc).
- daily energy → `/openApi/v1/dashboard/daysEnergy?month=YYYYMM&userId=<id>` →
  `data.times[]` + `data.energys[]` (per-day kWh, unit in `data.unit`).

Verified shape against Bruce: e.g. site "Londonderry 186" = 186 kW, 4×
SCA50KTL-DO/US-480 inverters, ~98.6 kWh/inverter today, live power. **STATUS:
grounded from HAR + shipped v1.9.12; live click-through debugging via instrumented
builds peeled SIX layers the HAR couldn't show — brittle getUserInfo probe gate
(v1.9.13/14), mis-wrapped token value (v1.9.15), AES-ENCRYPTED localStorage token
requiring MAIN-world observation (v1.9.16), missing session-cookie/credentials:
include (v1.9.17), and finally — credentials:include STILL returned 4010 even from
page context — the realization that Chint's auth is UN-REPLAYABLE (encrypted +
per-request-bound). v1.9.18→19 abandoned auth-replay entirely and switched to
PASSIVE RESPONSE OBSERVATION: the MAIN-world `chint_inject.js` hooks the app's OWN
XHR/fetch responses for `/api/asset/site/retrieve` + `/api/asset/site/busTypeDevices`
and relays the bodies; `chint_content.js` parses them and assembles the payload.
No token, no fetch of our own. ✅ CONFIRMED WORKING LIVE (v1.9.20 diagnostic →
v1.9.21 prod) — Bruce's console showed `observed SITE LIST: 1 site(s)` then the
inverters captured on site-open. The passive-observation approach is the proven
winner; auth-replay was a dead end. Two final live gotchas the click-through
exposed: (1) the SPA fetches its data ONCE on initial page load — if the inject
hooks attach after that (e.g. user was already on the dashboard), zero responses
are seen; a hard RELOAD (F5) of the portal tab makes the app re-fetch with hooks
in place. (2) it's a hash-routed SPA, so navigating dashboard↔sites does NOT
re-fetch; the owner must RELOAD or genuinely open a not-yet-loaded site for
`busTypeDevices` to fire. Onboarding copy now says "click into each of your
sites." See "When auth-replay is IMPOSSIBLE → PASSIVE RESPONSE OBSERVATION" above
for the full pattern + the 3-deep fetch-mode decision tree.**
localStorage keys confirmed: `_token` + `userIdByLogin`. Auth varies per endpoint (getUserInfo has no token;
data endpoints do); CORS is specific-origin so a direct content-script fetch
works, no proxy. Repointed host/manifest/background + array-operator UI portal
URL, rebuilt, deployed. Full HAR dump, field maps, and the DONE checklist:
references/chint-portal-api-contract.md.

## Two live auth bugs the HAR hid (CHINT v1.9.13→v1.9.15) — both class-level
The instrumented build's console screenshots exposed two distinct auth failures
that NO HAR-replay could have caught, because both are about runtime VALUES, not
endpoint shapes. Watch for both on any new vendor:

1. **Brittle auth-PROBE gate blocking real capture.** v1.9.13 added an
   `isSignedIn()` that probed `getUserInfo` and gated captureFlow on it. Console
   showed `isSignedIn: false` every tick while token + userId were BOTH present —
   the probe failed (in the HAR, `getUserInfo` was the ONE call that omitted the
   `token` header, so adding it server-rejected the probe) and that false blocked
   everything, including the data calls that would have worked. **Lesson: don't
   gate capture on a separate "am I logged in" probe. Token + userId present in
   localStorage IS the login signal. Go straight to the real data calls and let
   THEM be the auth check — if the session is stale they 401/redirect and
   captureFlow throws with the true reason. A probe that uses a different
   header/param shape than the data calls will give false negatives.**

2. **HTTP 200 + API error-code = wrong token VALUE (not 401, not CORS).** The
   data call returned `fetch /api/asset/site/retrieve -> 200 application/json`
   but the JSON body was `{code:"4010", msg:"Please login"}`. This is a THIRD
   failure class beyond the status-code triage (401/CORS/500): the request
   reached the server, CORS passed, HTTP was 200 — but the app-layer envelope
   reported auth failure. Cause: the token VALUE was wrong-shaped. `_token` in
   localStorage was a JSON-wrapped object, and the code sent `[object Object]`
   (or a quoted string) instead of the real 65-char token. **Fix pattern: a
   robust `getToken()` that parses `_token` whether it's a raw string OR a JSON
   object (try `.token`/`.access_token`/`.accessToken`/`.value`/`.authToken`/etc),
   AND logs the extracted shape (`typeof`, length, prefix, obj keys) so the next
   console run confirms you're sending the real value. Diff the length against the
   HAR's working token (e.g. 65 chars) to confirm.** Always have `getJson` throw
   on `code != "0"` with the code+msg in the message, so a body-level auth reject
   surfaces as a clear `captureFlow threw: ... api code 4010 (Please login)` line
   rather than a silent 200 that looks like success.

GENERAL RULE: extend the status triage (recipe step 3) with a fourth branch —
**200 + non-zero envelope `code`/`success:false` = authed transport, rejected
payload → almost always a wrong/short/mis-wrapped credential value, or a missing
required param. Log the parsed token shape and compare to the HAR.**

## ENCRYPTED token in localStorage → OBSERVE the page's request, don't decrypt (CHINT v1.9.16, class-level)
A FIFTH auth class, found after the v1.9.15 getToken() rewrite STILL got 4010.
The logged token prefix was `U2FsdGVkX1` — that is the base64 of `Salted__`,
the signature of a **CryptoJS AES-encrypted** value. So the 128-char `_token`
blob in localStorage is ENCRYPTED; the SPA decrypts it client-side at runtime to
the real header value (the 65-char `81f5f6a7…` seen in the HAR). Sending the
encrypted blob as the `token` header → server says "Please login". No amount of
`.token`/`.value` plucking helps — the stored value is ciphertext.
- **Do NOT reverse-engineer their crypto key.** It needs their secret (often not
  even in the HAR — JS bundles frequently aren't saved by "Save HAR with content"),
  and it shatters the moment they rotate the key. Fragile, wrong tool.
- **Instead OBSERVE the decrypted token the app already sends.** The SPA itself
  fires authed API calls constantly with the correct decrypted `token` header.
  Add a MAIN-world content script (`<vendor>_inject.js`, manifest entry with
  `"world": "MAIN"`, `run_at: document_start`) that monkey-patches
  `XMLHttpRequest.prototype.setRequestHeader` AND `window.fetch` to read the
  outgoing `token`/`loginuserid` headers, then relays them to the isolated content
  script via `window.postMessage({type:"SO_<VENDOR>_AUTH", token, loginuserid},
  location.origin)`. The content script listens (guard `e.source===window &&
  e.origin===location.origin`), prefers the observed token, and uses it. Key,
  decryption, and rotation all sidestepped — you ride exactly what the app uses.
- **Handle the listener race:** the page may fire its authed calls before the
  isolated content-script listener attaches. So the inject script must (a) cache
  the last token, (b) re-broadcast on an interval (~3s) AND on a
  `SO_<VENDOR>_AUTH_REQUEST` ping, and the content script pings that request each
  tick. The log line `observed token from page (len 65, prefix 81f5f6a7…)` — note
  length dropping from 128 (encrypted) to 65 (real) — is the proof you flipped it.
- **MAIN vs ISOLATED world:** content scripts default to the ISOLATED world and
  CANNOT see the page's patched fetch/XHR or page-scope JS. Only a `"world":"MAIN"`
  script shares the page's JS context to intercept its requests. The two worlds
  talk only via `window.postMessage`. This is the general bridge for "read a value
  the page computes at runtime."

## Token + session COOKIE dual-auth → credentials:"include" (CHINT v1.9.17, class-level)
Even sending the CORRECT observed 65-char token, `/api/asset/site/retrieve` still
returned 200 + `{code:"4010","Please login"}`. Header-diffing our request vs the
HAR's working one showed the token matched — but the API's RESPONSE carried
`access-control-allow-credentials: true` with a SPECIFIC `access-control-allow-
origin` (the page origin, not `*`). A server only sends those when it expects a
**session cookie alongside the token**. HARs do NOT record HttpOnly cookies, so
"no cookie in the HAR" never meant cookieless — the working request rode a cookie
I couldn't see. Fetch defaults to `credentials:"same-origin"`, and the API is
cross-origin (`:8443` vs the page's `:443`), so the cookie wasn't sent → token
alone rejected. **First attempt: `fetch(url, { headers, credentials: "include" })`.**
This is the EXACT OPPOSITE of the SMA rule above (SMA's uiapi returns ACAO:`*`,
which HARD-FORBIDS credentials:include; SMA is pure Bearer, no cookie). The
discriminator is the response's ACAO + allow-credentials pair:
- ACAO `*` (no allow-credentials) → Bearer/header-only, NEVER credentials:include.
- ACAO = specific origin + `allow-credentials:true` → token+cookie dual-auth,
  needs the cookie to ride with the token header.
Read those two response headers from the HAR before choosing the fetch mode.

## When auth-replay is IMPOSSIBLE → PASSIVE RESPONSE OBSERVATION (CHINT v1.9.18→19, the winning pattern, class-level)
credentials:"include" did NOT fix CHINT either — `4010` persisted even when the
fetch ran from the MAIN-world (page context, where the app's own identical calls
succeed). That is the terminal signal: **the auth cannot be replayed from outside
the app's own call site.** Chint's token is CryptoJS-encrypted AND bound
per-request (nonce/signature the SPA computes inline), so even byte-identical
headers + cookies from a separate fetch get rejected. Chasing the exact credential
combination is a dead end once a page-context replay with the observed token still
fails.
- **STOP replaying. Start OBSERVING the app's own RESPONSES.** The portal already
  fetches every datum successfully (it's on the owner's screen). So don't call the
  API at all — hook the app's OWN `XMLHttpRequest`/`fetch` RESPONSES in a
  `"world":"MAIN"` inject script and copy the response BODIES for the endpoints you
  want, relaying them via `window.postMessage({type:"SO_<VENDOR>_RESPONSE", path,
  body}, location.origin)`. The isolated content script collects those bodies,
  parses them, and assembles the capture payload. Zero auth, zero token, zero
  fetch of our own → `4010`/CORS/credential bugs ALL become impossible because we
  never make a request. This is strictly more robust than auth-replay and should
  be the DEFAULT for any vendor whose token is encrypted or per-request-bound.
- **Hook responses, not just request headers.** Patch `XHR.prototype.open` (stash
  url) + `send` (addEventListener("load") → read `responseText`) AND `window.fetch`
  (`p.then(r => r.clone().text())` — clone so the app still consumes its body).
  Match on `new URL(url).pathname` against your wanted-endpoint list.
- **Tradeoff the UX must cover: the owner has to NAVIGATE to load the data.** A
  response only exists once the app fetches it — the site-list loads on the
  dashboard, but per-site inverters (`busTypeDevices`) only load when the owner
  CLICKS INTO that site. So the capture is passive + progressive: grab the site
  list immediately, then enrich each site as the owner opens it. The content
  script should (a) emit progressively (backend upserts idempotently), (b) hold a
  few ticks for at least one site's devices before shipping site-level-only data,
  (c) tell the user plainly "open your dashboard, then click into each site." Don't
  assume one page load yields everything.
- **General rule:** the fetch-mode decision tree is now three-deep:
  1. ACAO `*` + Bearer in localStorage → header-only fetch, NO credentials.
  2. ACAO specific-origin + allow-credentials:true → try token + credentials:include.
  3. Still rejected from page context (encrypted/per-request-bound token) →
     ABANDON replay, switch to passive response observation (read the app's own
     responses). Recognize this early: a MAIN-world page-context fetch with the
     observed token still failing = signal #3, don't keep tuning headers.

## Live-debug discipline that worked here (Ford's anti-blind-guess rule, reinforced)
CHINT took ~7 instrumented builds (v1.9.12→1.9.19) and EVERY step forward came
from reading a real console screenshot, never from a speculative fix. The cadence
that worked, repeat it: ship instrumented build → bump version (proves reload) →
user sends ONE portal-tab console screenshot → the branched LOG lines name the
exact failure → make the ONE change that addresses it → repeat. Each screenshot
peeled exactly one layer (wrong host → probe gate → mis-wrapped token → encrypted
token → missing cookie → un-replayable auth → passive observation). Resist
bundling multiple speculative fixes per build — one change per evidence cycle keeps
the signal clean and is what Ford expects after ~2 failed blind attempts.

## Multi-SITE capture must NOT stop after the first site (CHINT v1.9.22, class-level)
A passive-observation (or any progressive) capture that sets a terminal `done`
flag the moment ONE site yields inverters will SILENTLY DROP every other site.
Bug hit live: Bruce/GMCS has many Chint sites; capture marked `done=true` after
the first site he opened, so every site opened afterward was ignored → "doesn't
take all the inverters." This is a CLASS bug for any owner with >1 site on any
vendor. Rules:
- NEVER stop capture on "got at least one." Multi-site owners open sites one at a
  time; each open fires a fresh `busTypeDevices` (or equivalent) the observer must
  catch. Keep listening until the intent WINDOW ends (MAX_POLLS), not until first
  data.
- Make the change-detection signature span EVERY site's inverters
  (`sites.map(s => s.site_id + "|" + inverters...)`), so opening a new site changes
  the hash and triggers a fresh progressive emit. The backend upserts idempotently
  (`inverter-capture` matches Array by name + Inverter by tenant+vendor+serial), so
  re-emitting the full snapshot each time is safe and additive — never duplicates.
- Consequence to handle: with no `done`, the poll loop reaches MAX_POLLS normally.
  Track an `emittedAny` flag and SUPPRESS the timeout `*_CAPTURE_FAILED` toast when
  you already shipped real inverters — otherwise a successful multi-site capture
  ends with a spurious "failed" message overwriting the good result.

## "Owner's connected arrays keep getting FORGOTTEN" = session-secret rotation + demo fallback masking (class-level, June 2026)
Symptom: an owner connects inverters (any vendor), sees them on the canvas, then
LATER the account shows no arrays / demo data — "it forgot my arrays." The data is
almost never actually deleted. Root cause found for Array Operator: a two-part trap
that generalizes to any stateless-session SPA with a demo mode.
1. **Stateless session secret derived from a mutable env var.** `api/account.py`
   signs the dashboard session as an HMAC blob; when `SESSION_SECRET` is unset it
   derives the secret from `DATABASE_URL` (account.py ~124-126). Railway rotates
   `DATABASE_URL` on DB re-provision / credential rotation / restore, which silently
   rotates the signing secret → every previously-issued `so_session` fails
   `_verify_session` → `/v1/array-owners/overview` returns 401. The 30-day TTL is a
   red herring; SECRET ROTATION is the real "keeps forgetting" trigger. **FIX (needs
   the operator): pin a fixed `SESSION_SECRET` (e.g. `openssl rand -hex 32`) in the
   Railway env so it no longer tracks DATABASE_URL. Flag to the user that applying it
   logs everyone out ONCE (old tokens were signed with the derived secret), then
   sessions survive every redeploy. Do NOT set a prod secret yourself — generate it,
   let the operator paste it.**
2. **Client demo-fallback MASKS the logout.** `array-operator/public/app.js`
   `loadDashboard()` fell back to rendering `inverter-truth.json` (DEMO data) on ANY
   overview failure — including a 401. So an expired session painted DEMO numbers
   over the owner's real (still-persisted) arrays, which reads exactly as "my arrays
   vanished and got replaced with junk." **FIX (shipped): branch on status — 401/403
   → clear the dead `so_session` + show an honest "session expired, sign back in,
   your data is safe" prompt; reserve demo fallback for transient 5xx/network AND the
   anonymous marketing branch only. Also: a signed-in owner with zero arrays should
   see the real empty state, NEVER demo (demo on a real account reads as fake data).**
GENERAL RULE: never let a demo/marketing fallback render over an AUTHENTICATED
failure. Demo is for anonymous visitors and true outages, not for logouts — masking
auth failures with demo data turns a re-login into apparent data loss. When a user
reports "X keeps getting forgotten," check session-secret stability and the
client's auth-failure branch BEFORE suspecting deletion (overview returns all
non-deleted arrays unconditionally, so server-side persistence is rarely the bug).

## HAR-replay validation (prove field maps BEFORE shipping)
Once you've grounded a vendor's endpoints from a HAR, don't ship the rewritten
content script on faith — REPLAY its mapping logic against the same HAR in Python
first. The HAR already contains the real JSON responses, so you can reconstruct
exactly what `captureFlow` would emit without a browser:
1. Load each HAR, index responses by `urlparse(url).path` (filter `code:"0"`).
2. Re-implement the JS field plucks in Python (site list → per-site
   `busTypeDevices` → `gwDevices[].commDevices[]` filter → serial/model/eToday/
   power/status) and print the resulting capture payload.
3. Sanity-check the numbers against the portal's own roll-up — e.g. the per-
   inverter `eToday` sum equalling the dashboard's `daysEnergy` figure for the day
   is a free correctness proof that the nesting + filter are right.
This caught nothing wrong for CHINT (mapping was correct first try) but the
30-second replay is what let me say "verified" honestly instead of "should work."
Pair it with `node --check` on each edited JS for syntax. Do this for every vendor
rewrite — it converts "I think the fields map" into "the real data produced the
right shape."

## GROUNDED-FROM-HAR ≠ CONFIRMED-WORKING (re-instrument when a shipped build stalls)
HAR-replay proves the FIELD MAP, not the live runtime. CHINT shipped v1.9.12 fully
grounded + replay-verified, and the FIRST live click-through still stalled: the AO
modal sat on the plain "Opening <vendor>… sign in there" spinner with NO error and
NO data. A bare spinner with no `*_CAPTURE_FAILED` means the content script never
broadcast ANYTHING — it's the same silent-stall class as the SMA saga, and a HAR
captured at one moment can't tell you whether the script even loaded, whether the
intent flag reached the tab, or whether the live token key matches what the HAR
showed. The right move (Ford's anti-blind-guess rule) is NOT another speculative
fix — it's to ship an INSTRUMENTED build and read real console evidence:
- Add the gated `const <VENDOR>_DEBUG = true; const LOG = (...a)=>{...}` trace at
  the TOP of the IIFE (same pattern as SMA), logging at: script LOADED, each tick,
  hasIntent, localStorage token presence + `Object.keys(localStorage)` (so a
  renamed key is visible), isSignedIn, every GET+status, captureFlow site count,
  final CAPTURED. Ship it `true` for the debug build, flip to `false` once green.
- BUMP THE VERSION on every debug rebuild (e.g. 1.9.12→1.9.13). The version number
  in chrome://extensions is your proof the user's remove+reload actually took —
  "did it reload?" is itself one of the failure hypotheses, so make it observable.
- Then ask for ONE console screenshot from the portal tab. The branched log lines
  map 1:1 to the fix: no `[VENDOR]` lines = script didn't load (reload/host match);
  `hasIntent:false` = intent not reaching tab; token-absent + printed keys = real
  key name differs from HAR; `isSignedIn:false`/auth threw = header shape wrong;
  `captureFlow threw …403/CORS` = needs background proxy; `CAPTURED` = downstream.
Lesson: never tell the user "grounded and done" — say "grounded + shipped, run the
live click-through to confirm," because the browser is the only real test.
Bump `manifest.json` version → `node --check` each edited JS → commit →
`bash scripts/build_extension_zip.sh`. ✅ As of Jun 2026 the build script does
the copy-to-both-Desktop-roots step ITSELF (outputs `energyagent-extension-vX.Y.Z`
to `Desktop/Energy Agent/Archives - Extension Builds/` AND auto-copies the zip to
`/mnt/c/Users/fordg/Desktop/` + `/mnt/c/Users/fordg/OneDrive/Desktop/`). Product
naming moved Solar Operator → ENERGY AGENT: the Desktop folder is now
`Energy Agent/` and builds are `energyagent-extension-*` (was
`Solar Operator/` + `solar-operator-extension-*`); the git repo dir is still
`/root/solar-operator` and the live domains are unchanged. Then optional
`gh release create ext-vX.Y.Z` → verify the SHIPPED zip contains the fix
(`unzip -p <zip> <file> | grep <token>`). The historical archived zips keep their
old `solar-operator-extension-*` names (left as-is — harmless history).
User side-loads via Load unpacked, so a GitHub release is optional when they pull
from Desktop. ALWAYS tell the user the new version number and that they must
remove+reload (not just refresh) the extension — a content-script change needs a
full reload.
