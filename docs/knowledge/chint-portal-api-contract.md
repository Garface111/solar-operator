# CHINT / CPS Portal API — Grounded Contract (from Bruce's live HARs, June 2026)

Captured by HAR-grounding Bruce Genereaux's real CHINT account (4 HARs saved to
OneDrive/Desktop: `monitor.chintpowersystems.com*.har`). This is the verified
contract that replaces the old unverified `solar.chintpower.com` CANDIDATE_*
guesses in `chint_content.js`.

## Host + port
- REAL portal: `https://monitor.chintpowersystems.com:8443` (the `:8443` port is
  part of the URL on the API calls).
- The OLD extension target `solar.chintpower.com` was wrong — that's why nothing
  ever captured. Fomware ships multiple white-label hosts; keep chintpower.com in
  host_permissions as a fallback but add chintpowersystems.com (the real one).

## Auth (custom headers — NOT cookies, NOT Bearer)
Every API request carries these request headers:
- `token: <32+hex>_<...>`  — the session token
- `loginuserid: <userId>`  — e.g. `6022f13af125887aed2efc2b`
- `platformcode: 3`
- `request-origin: web`
- `time-zone: <urlencoded tz>` (e.g. `America%2FLos_Angeles`) — best-effort
- `origin: https://monitor.chintpowersystems.com`, `referer: .../`

HAR strips cookies/auth, but the `token` header survived in the request headers.
The content script reads `token` + `loginuserid` from the page's localStorage —
the CONFIRMED key names (from the console probe `JSON.stringify(Object.keys(
localStorage))` on Bruce's tab) are **`_token`** (the session token) and
**`userIdByLogin`** (the userId). Other keys present: `app`, `layout`,
`app_version`. Read those two, set them as the `token` / `loginuserid` headers.
Tolerate JSON-string wrapping (some SPAs `JSON.stringify` the raw value, leaving
surrounding quotes) — strip quotes if `v[0]==='"'`.

**Auth varies PER ENDPOINT (verified):** `getUserInfo` carries `loginuserid` +
`platformcode` but NOT `token`; the data endpoints (`site/retrieve`,
`busTypeDevices`, `daysEnergy`) carry all three. Sending `token` on getUserInfo
anyway is harmless, so just attach all headers when present.

**CORS is fine WITHOUT a proxy** even though the API is on `:8443` and the page
on `:443` (different port = technically cross-origin): the API answers
`Access-Control-Allow-Origin: <the page origin>` (the SPECIFIC origin, not `*`)
+ `Access-Control-Allow-Credentials`. A header-authed `fetch` from the content
script with the extension's host_permissions passes cleanly — no background
proxy (unlike SMA's wildcard-ACAO uiapi). Use getUserInfo (code:"0" + data.userId)
as the signed-in probe.

## Response envelope
All endpoints return `{ "code": "0", "msg": "success", "data": <...> }`
(some also `"success": true, "timestamp": <ms>`). `code:"0"` = OK.

## Endpoint map (all GET)

### whoami
`GET /api/users/user/getUserInfo?appKey=WEB`
→ `data.userId` (string), `data.email`, `data.userName`, `data.companyName`,
`data.rateCountry` (currency). Use `userId` for the daysEnergy call below.

### site (station) list
`GET /api/asset/site/retrieve?page=1&limit=20&key=&customerAdminId=&customerId=&endUserId=&installerId=&...`
→ `data` is an ARRAY of sites. Per-site fields:
- `id` — the siteId (used by busTypeDevices)
- `siteName`
- `installedCapacity` — nameplate kW, STRING e.g. `"186.0"`
- `currentPower` — live AC power in WATTS, STRING e.g. `"189200.0"`
  (also `currentPowerWithUnit` "189.2 kW")
- `onlineCount` / `totalCount` — device counts
- `weekETrend[]` — `[{name:"20260610", value:"996.2"}, ...]` daily kWh, last 7d
- `statusName` ("Normal"), `statusColor`, `timeZone`, `address`, lat/long

### per-site devices (the inverter comb)
`GET /api/asset/site/busTypeDevices?siteId=<id>`
→ inverters are NESTED, not top-level: `data.gwDevices[]` (gateways) →
each gateway `.commDevices[]` (the connected devices). Filter commDevices to
`assetTypeName === "Inverter"` (or `assetType === 2`) — the gateway itself
(`assetType:1`, "Gateway") is also in gwDevices, skip it.

⚠️ DATA-LOGGER / "DETECTOR" LEAK (fixed 2026-07-17, commit 5cb77b86): the
FlexOM/collector can also surface among a gateway's commDevices (hex serial like
`00009e021902bb00`, no `model`, never produces). It was slipping into the inverter
list via the `assetType === 2` fallback and getting flagged as a dead/"gone quiet"
inverter — Ford called it "a detector, not an inverter." Both parsers now classify
POSITIVELY and reject non-inverter kinds: `isInverterDevice`/`_is_inverter_device`
(extension chint_content.js + harvester chint.py) reject by name
(gateway|collector|logger|detector|meter|sensor|environment|weather|combiner|…),
by `assetType === 1`, and by the gateway-serial echo (commDevice.sn == parent
gwDevice.sn). Real Chint inverter SNs are 16 DECIMAL digits with a model
(`SCA50KTL-DO/US-480`); loggers are hex + model-less — that signature is what the
one-off cleanup used to purge the 8 mis-ingested rows.

Per-inverter (commDevice) fields:
- `sn` — serial (e.g. `"0001013791738041"`) — the stable per-inverter key
- `assetAlias` — display name (often == sn)
- `model` — e.g. `"SCA50KTL-DO/US-480"`
- `currentPower` — live WATTS, e.g. `51000.0` (also `currentPowerWithUnit`)
- `eToday` — today's energy kWh, e.g. `98.6` (also `energy.eToday`,
  `eTodayWithUnit` "98.6 kWh")
- `statusName` — "Running" / "Normal" / etc; `status` (int), `statusColor`
- `communicationStatus`, `model`, `moduleVersion[]`

`data` (site level) also has `eTodayWithUnit`, `eTotalWithUnit`,
`installedCapacityWithUnit`, `currentPowerWithUnit`, `socialContributions`.

### daily energy series (per day, for history backfill)
`GET /openApi/v1/dashboard/daysEnergy?month=YYYYMM&userId=<userId>`
→ `data.times[]` (["20260601",...]) parallel to `data.energys[]`
(["1.167","1.541",...]) — per-day energy, unit in `data.unit` (e.g. "MWh" at
portfolio level; site-level eToday is kWh). Also `toGrids[]`, `fromGrids[]`.

Other dashboard endpoints seen (lower priority): `/openApi/v1/dashboard/monthsEnergy`,
`/energyYears`, `/monthAmount`, `/api/asset/overview/siteDataIncome` (portfolio
roll-up: `data.energys.etotal`, `data.powers.activePowerTotal`).

## Verified against Bruce
Account "GMCS Manager" / Green Mountain Community Solar LLC. Site "Londonderry 186"
= id `5e15c66df12588458ffc011a`, 186 kW, 1 gateway (FlexOM, FG4C) →
4 inverters (SCA50KTL-DO/US-480 ×3 + SC36KTL-DO/US-480 ×1), eToday
98.6/105.6/101.3/73.5 kWh = **379.0 kWh summed**, which MATCHES the dashboard's
own `daysEnergy` figure for that day — a free correctness check. This is the
real per-inverter comb the peer-analysis engine consumes — exactly the SMA/Fronius
shape.

## STATUS: ⚠️ BLOCKED at live capture — auth CANNOT be replayed (v1.9.20, Jun 2026)
Host/endpoints/field-maps are RIGHT and shipped (v1.9.12) and the mapping was
validated by Python-replay against the HARs. BUT the live click-through on Bruce's
account proved the capture does NOT work, and we burned ~8 build iterations
(1.9.12→1.9.20) finding out exactly why. Hard-won findings — DO NOT repeat these:

1. **The `token` header value is CryptoJS-ENCRYPTED in localStorage.** `_token`
   holds `{"token":"U2FsdGVkX1...(128 chars)"}` — the `U2FsdGVkX1`/`Salted__`
   prefix = CryptoJS AES. The app decrypts it client-side to a 65-char value
   (`81f5f6a7...`) which is what actually goes in the `token` request header.
   So you can't just read `_token` and send it.
2. **Observing the real decrypted token works, but doesn't help.** A MAIN-world
   inject hooking `XHR.setRequestHeader`/`fetch` captured the exact 65-char token
   (verified: same prefix as HAR). Relaying it to a content-script fetch →
   **still `api code 4010 "Please login"`** (HTTP 200 body). Same token, from
   the PAGE's own context too, WITH `credentials:"include"`. ⇒ the token is bound
   per-request (HMAC/nonce); replay is DEAD. The earlier note that getUserInfo is
   a clean signed-in probe is MOOT — every data call 4010s on replay.
3. **CORS note above is misleading.** ACAO is the specific origin + allow-creds,
   but the content-script (isolated-world) fetch never attaches Chint's session
   cookie, so credentials:include changed nothing. Page-context fetch also 4010'd
   — so it's not even a cookie problem, it's the per-request token binding.

### Current approach (still unsolved as of v1.9.20): PASSIVE response observation
Stop replaying auth entirely. `chint_inject.js` (world:MAIN, run_at:document_start)
hooks `XMLHttpRequest.prototype.open/send` + `window.fetch` RESPONSES, copies the
bodies for `/api/asset/site/retrieve` + `/api/asset/site/busTypeDevices`, and
postMessages them to `chint_content.js` (isolated), which assembles the payload
from data the app ALREADY fetched. Zero auth replay ⇒ cannot 4010.
- Verified: inject DOES load in MAIN world, both hooks install (console shows
  "LOADED (MAIN world)" + "XHR hook installed" + "fetch hook installed").
- BLOCKER: hooks catch ZERO `API response seen:` lines. The SPA is hash-routed
  (`/#/dashboard/overview`) and fetched its data on initial load; navigating
  doesn't re-fetch, and a reload still showed nothing in the first few ticks.
- NEXT DIAGNOSTIC (v1.9.20 already logs every API URL): leave tab open, navigate
  to the **Sites/Plants list** (site/retrieve fired there in the HAR, NOT on
  dashboard/overview) + open a site, watch for ANY `API response seen:` line.
  - If ANY appears → hooks work, just point at the right page/endpoints. Easy.
  - If NONE after navigating → app fetches via Web/Service Worker (main-page
    hooks blind to it). Interception is exhausted; remaining options are heavier:
    `chrome.debugger` reader (shows a browser banner) or ship CHINT as a
    manual/best-effort connector. STOP grinding and give Ford the real tradeoffs.

### Debug method that worked here (reuse for any vendor capture)
Loud `console.log("[EnergyAgent CHINT] ...")` at EVERY decision point + bump the
version each build so the version number itself confirms the reload took. Ford
runs it, screenshots (or pastes) the Chint-tab console; each screenshot pins the
exact failure. This is the SMA-debugging playbook — far faster than reasoning
blind. Honest spinner + honest failure reasons surfaced to the AO modal.

## Implementation checklist to finish CHINT  (historical — all DONE in v1.9.12)
1. manifest: add `https://monitor.chintpowersystems.com/*` (+ keep chintpower.com)
   to host_permissions + content_scripts match.
2. background.js: arm `so_capture_intent {vendor:"chint"}` on host
   `chintpowersystems.com` too (not just chintpower.com).
3. chint_content.js: read `token` + `loginuserid` from localStorage (confirm key
   names via the console probe), set them as headers, hit the 4 endpoints above,
   map `gwDevices[].commDevices[]` → the capture payload (`sn`, `eToday`→kWh,
   `currentPower`→W, `statusName`→status). Note the `:8443` port in URLs.
4. Rebuild + ship per the "Ship loop for extension fixes" section.
