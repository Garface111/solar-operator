# EnergyAgent extension capture + auth — hard-won detail (Jun 2026)

Session-specific depth behind the compact pointers in SKILL.md. Read this when
adding a vendor scraper, debugging a capture stall, or touching login/sessions.

## ⚠️ MV3 CRITICAL: same-origin vs CROSS-ORIGIN portal APIs (the SMA stall)

The #1 non-obvious landmine for portal scrapers. Before writing
`<vendor>_content.js`, check whether the portal's DATA API is the SAME origin as
the page the content script runs on.

- **Same-origin** — SolarEdge (page+API both monitoring.solaredge.com), Fronius
  (both www.solarweb.com): the content script can
  `fetch(relativePath, {credentials:"include"})` directly. No CORS. This is WHY
  Fronius and SolarEdge "just worked" and SMA didn't.
- **CROSS-ORIGIN** — SMA: page = `ennexos.sunnyportal.com`, API =
  `uiapi.sunnyportal.com` (different subdomain ⇒ different origin). A content-script
  credentialed fetch is **CORS-BLOCKED** → the capture STALLS SILENTLY (the AO
  spinner spins forever). uiapi returns `Access-Control-Allow-Origin: *`, which the
  browser flatly refuses to combine with `credentials:"include"`. In MV3,
  host_permissions do NOT grant a CORS bypass to *content-script* fetches — only to
  the **background service worker**.

FIX (canonical MV3 pattern): add a hard-allowlisted proxy in background.js:
```js
if (msg.type === "SMA_API_GET") {
  if (!/^https:\/\/uiapi\.sunnyportal\.com\//.test(msg.url)) { sendResponse({ok:false,error:"url-not-allowed"}); return; }
  (async () => {
    try { const r = await fetch(msg.url, {credentials:"include", headers:{Accept:"application/json"}});
      if (!r.ok) { sendResponse({ok:false,status:r.status}); return; }
      sendResponse({ok:true, status:r.status, data: await r.json().catch(()=>null)}); }
    catch (e) { sendResponse({ok:false, error:String(e&&e.message||e)}); }
  })();
  return true; // async
}
```
and route the content script's getJson/isSignedIn through
`chrome.runtime.sendMessage({type:"SMA_API_GET", url})`. The SW (holding
host_permissions) makes the credentialed cross-origin call CORS-free.

HOW TO SPOT IT IN A HAR before coding: the data call's `Sec-Fetch-Site` =
`same-site`/`cross-site` (⇒ cross-origin), an `Origin` request header is present,
and the response carries `Access-Control-Allow-Origin: *`. Any of those ⇒ you MUST
proxy through the SW.

REMAINING SUSPECT if the SW proxy still returns 401: SMA's session cookie may be
`SameSite=Lax/Strict` and not attach to a SW-initiated fetch (no tab context).
Fallback then is `chrome.scripting.executeScript` with `world:"MAIN"` to run the
fetch in the page's own context, exactly like the portal's own JS. **Untested as
of v1.9.5 — the root cause was not yet live-confirmed in a browser when this was
written; the v1.9.5 diagnostic build was shipped to get the real failure reason.**

## DIAGNOSTIC-OVER-BLIND-GUESSING (discipline Ford effectively enforced)

Ford burned multiple test cycles on SMA because fixes were shipped from HAR theory
without a live browser run (the agent can't run the in-browser flow itself). RULE:
after ~2 fixes that don't land for an in-browser flow you can't execute, STOP
shipping guesses. Ship a build that makes the failure VISIBLE instead:
- content script tracks WHY each attempt failed (not-signed-in / auth-check error /
  API non-2xx / no-plants) in a `lastErr` var;
- on retry-budget timeout it sends `SMA_CAPTURE_FAILED{reason}`;
- background relays `SO_CAPTURE_FAILED`;
- so_bridge forwards it to the page;
- the AO modal renders the real reason instead of spinning forever.

An infinite spinner with no failure path is itself a bug — every poll-loop capture
MUST surface a terminal error. NOTE: `so_bridge.js` only forwards the SO_* message
types it EXPLICITLY lists in its onMessage relay — add new SO_* types there or the
page never receives them.

## CAPTURE-INTENT GOTCHA (privacy guard that looks like a bug)

Content scripts capture only when `so_capture_intent` (chrome.storage.local, shape
`{vendor, ts}`, 10-min TTL) is armed — and it is armed ONLY by the AO site sending
`SO_OPEN_PORTAL` (the in-app "Log in with <vendor>" button →
background `OPEN_UTILITY_PORTAL` opens the portal tab AND arms the intent). If the
user logs into the portal DIRECTLY (not via the button), NOTHING captures — by
design (privacy: don't scrape the user's portals unprompted). Always test via the
button; tell users to start from it, not by pre-logging-into the portal.

## ennexOS (SMA Sunny Portal) API map — tree-walker (grounded on real HAR)

- `GET /api/v1/navigation` (bare, NO parentId) → the owner's PLANT LIST:
  `[{componentType:"Plant", componentId, name}]`. THIS is how you enumerate all
  plants from the portfolio root. Ford had TWO (Timberworks 8296660 + Tannery Brook
  14993829) — a single-plant assumption silently misses the rest.
- `GET /api/v1/navigation?parentId=<plantId>` → that plant's child devices.
- `GET /api/v1/navigation/menuitems` → Portfolio (componentId null) at root;
  `?componentId=<id>` → that Plant (carries the plant display name).
- `GET /api/v1/overview/<plantId>/devices` → the per-inverter comb: each device has
  `serial`, `product` (e.g. "STP 24kTL-US-10" → nameplate via `/STP\s*(\d+)k/`),
  `pvPower` (live W), `totWhOutToday` (daily kWh DIRECTLY, no curve integration),
  `state` (307 = ok). Filter `componentType==="Device" && pvPower!=null` to drop the
  datamanager (EDMM-10).
- Plant id is also in the SPA URL path `ennexos.sunnyportal.com/<plantId>/...`. The
  "Log in with SMA" button opens the ROOT (`/`), so there is NO id in the URL — you
  MUST enumerate via `/navigation`, not rely on the URL.
- Auth is session-cookie (no bearer/Authorization header in the HAR), cross-origin
  to uiapi — see the MV3 section above.

THE "HAR CAPTURED INSIDE ONE ENTITY" PITFALL: the v1.9.2 SMA build worked in tests
but pulled NOTHING for a real owner, because the test HAR was recorded while already
INSIDE the Timberworks plant (URL had the id). A fresh "Log in" lands on the
portfolio root where there is no id. When grounding a scraper, capture a HAR from the
TRUE entry point (portfolio/list view), not from deep inside one entity.

## AUTH: multi-tenant-per-email (the recurring "login glitch" root cause)

An email can legitimately own >1 Tenant — one per product (a NEPOOL account AND an
Array Operator account both on ford.genereaux@gmail.com). The auth paths didn't
handle that, producing "wrong account" / "invalid password" glitches.

Fixes shipped (api/account.py + api/onboarding.py + array-operator login.html):
1. **password-login** (`/v1/auth/password-login`): was `select(Tenant).where(email).first()`
   — one ARBITRARY tenant. If it grabbed the NEPOOL tenant when the user typed their
   AO password → 401 "invalid password" despite a correct password. NOW: verify the
   password against EVERY tenant for the email, then pick by rank
   (requested `product` > active > newest). Body gained optional `product`.
2. **magic-link** (`issue_magic_link` / `/v1/auth/request`): accepts `product` and
   prefers the tenant in that product (was `active DESC, created_at DESC` guess).
3. **signup** (`/v1/onboarding/start`): the duplicate guard only blocked ACTIVE
   duplicates, so an inactive tenant on an email could spawn a SECOND same-product
   tenant — the source of the duplicate accounts. NOW blocks per-PRODUCT whether
   active OR inactive; still ALLOWS the same email across DIFFERENT products.
4. AO `login.html` sends `product:"array_operator"` on both password + magic-link.
5. **SESSION_SECRET**: was unset → derived as `sha256(DATABASE_URL)`. If Railway ever
   rotated the DB password, every session would die at once (mass logout). FIX: set an
   explicit Railway var EQUAL TO the current derived value
   (`eccfe2f6…98e` = sha256(current DATABASE_URL)) so existing sessions survive AND a
   future DB rotation can't invalidate them. GOTCHA: `railway variables` table display
   TRUNCATES long values — after setting, verify the full 64-char length via
   `railway ssh ... python -c "len(os.getenv('SESSION_SECRET'))"` before trusting it.

`tenant_from_session` ALLOWS inactive tenants (read-only dashboard); mutating
endpoints gate on active/subscription and return 402. So an inactive AO account can
log in but report-sending 402s — expected, not a glitch.

Tests: tests/test_password_auth.py (multi-tenant disambiguation x3) +
tests/test_onboarding.py (inactive-dup 409, cross-product allowed). Full suite 912.
