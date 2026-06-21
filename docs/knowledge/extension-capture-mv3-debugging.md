# MV3 extension-capture debugging playbook (learned on SMA, Jun 2026)

When a vendor's "Log in with X" capture STALLS on Array Operator (the AO spinner
spins forever, never resolves into success OR error), work this checklist in order.

## 0. Capture only fires when ARMED from the AO button
The content script's poll loop only captures while `so_capture_intent` (chrome.storage.local,
10-min TTL) is fresh — and that flag is set by the background `OPEN_UTILITY_PORTAL` handler,
which only runs when the user clicks **"Log in with X"** inside Array Operator. Logging into the
portal directly does NOTHING. Always confirm the user started from the AO button.

## 1. "Not paired" in the popup is a RED HERRING for AO
Pairing / `tenant_key` is the **NEPOOL** utility-upload path only. Array Operator capture uses the
so_bridge auto-handshake (`SO_EXTENSION_PRESENT` announced on arrayoperator.com) + the logged-in AO
session. Do NOT chase pairing status for an AO capture bug.

## 2. SAME-ORIGIN vs CROSS-ORIGIN — the #1 structural gotcha
- **SolarEdge, Fronius**: content script fetches its OWN origin via RELATIVE paths
  (`monitoring.solaredge.com`, `www.solarweb.com`). Same-origin → no CORS, session cookie rides free.
  These "just work."
- **SMA**: the ONLY cross-origin vendor. Content script runs on `ennexos.sunnyportal.com` but the API
  is `uiapi.sunnyportal.com`. In MV3, a **content-script** cross-origin fetch is bound by the PAGE's
  CORS policy — `host_permissions` grant cross-origin + cookies only to the **service worker**.
  uiapi returns `Access-Control-Allow-Origin: *`, which the browser REFUSES to combine with
  `credentials:"include"` from a content script → hard CORS block → silent retry → stall.
- **Fix attempt #1 (necessary, not always sufficient):** route the GET through a background
  `SMA_API_GET` proxy (hard-allowlisted to the API host) so the SW makes the credentialed fetch
  CORS-free. Content script's `getJson`/`isSignedIn` call the proxy via `chrome.runtime.sendMessage`.

## 3. The SW proxy can STILL 401 (the real SMA failure)
A service-worker fetch has no tab/site context, so a portal's `SameSite=Lax/Strict` session cookie is
**not sent** → 401. Console proof from the live session:
```
[EnergyAgent SMA] GET https://uiapi.sunnyportal.com/api/v1/navigation/menuitems -> FAIL status=401
[EnergyAgent SMA] signedIn: false
(looping every tick)
```
**status=401 (not status=0) PROVES it's AUTH, not CORS** — the request reached the server
unauthenticated. Do not keep chasing CORS once you see 401.

## 4. Diagnose with a LOUD console trace, not silent messages
"Nothing in the console" is meaningless when the script logs nothing. Ship a debug build that
`console.log("[EnergyAgent SMA]", …)` at every step: content-script load, each tick, `hasIntent`,
`signedIn`, every GET (ok / `FAIL status=` / err), captureFlow site count, and the final send.
ALSO turn the infinite spinner into a real error: on give-up, broadcast `SMA_CAPTURE_FAILED{reason}`
→ background relays as `SO_CAPTURE_FAILED` → so_bridge forwards to the page → AO modal shows the
reason. An infinite spinner with no failure branch is itself a bug.

## 5. Cookie-vs-token decider — run in the PORTAL tab console
```js
(async()=>{const r=await fetch('https://<api-host>/api/v1/navigation',{credentials:'include'});
console.log('STATUS',r.status,'LS',Object.keys(localStorage),'SS',Object.keys(sessionStorage))})()
```
- **page-context fetch = 200** → the cookie works IN-PAGE; the SW just can't carry it. Fix: run the
  capture in the page's MAIN world (`chrome.scripting.executeScript({world:"MAIN"})`) so it executes
  exactly like the site's own JS.
- **page-context fetch = 401** → it's a Bearer token (SMA = Keycloak/OIDC at `login.sma.energy`) held
  in storage. Grab it from the listed keys (containing `token`/`kc`/`keycloak`/`oidc`) and attach
  `Authorization: Bearer …`.
- NOTE: HARs frequently show NEITHER an Authorization header NOR a Cookie header on the API calls yet
  still return 200 — that ambiguity is exactly why you must run the in-page probe rather than infer
  the auth scheme from the HAR.

## 6. ennexOS plant discovery (grounded against real HARs)
- `GET /api/v1/navigation` (bare, no parentId) → root **Plant list**
  `[{componentType:"Plant", componentId, name}]`.
- `GET /api/v1/navigation?parentId=<plantId>` → that plant's **devices**.
- `GET /api/v1/navigation/menuitems?componentId=<plantId>` → that plant's menu (carries display name).
- `GET /api/v1/overview/<plantId>/devices` → per-inverter rows: serial, product (→ nameplate via
  `/STP\s*(\d+)k/`), pvPower (live W), totWhOutToday (daily kWh DIRECT, no integration), state
  (307=OK). Filter `componentType==="Device" && pvPower!=null` to drop the datamanager.
- The "Log in with SMA" button opens the PORTFOLIO ROOT (`ennexos.sunnyportal.com/`, no plant id in
  URL). You MUST enumerate plants from bare `/navigation` — do NOT assume a single plant from the URL
  path. Ford has two: Timberworks (8296660) + Tannery Brook (14993829).

## 6b. SERVER-SIDE pull is impossible for a cookie-auth portal (VEC/WEC, Jun 2026)
Before choosing WHERE the data fetch runs, decide server-side vs client-side from the auth scheme
(section 5's probe answers this):
- **httpOnly SESSION COOKIE** (NISC SmartHub: VEC/WEC `/services/secured/*`) → the fetch MUST run
  CLIENT-SIDE in the content script, same-origin (`credentials:"include"` rides the cookie for
  free). A BACKEND CANNOT replay a browser's httpOnly cookie — so any "extension ships the token,
  backend pulls the data" design is impossible by construction. I built exactly that wrong design
  for VEC first (a `/v1/array-owners/smarthub-meter-capture` endpoint taking host+email+auth_token)
  and it could never have worked: the HAR of `POST /services/secured/utility-usage` has NO
  Authorization header at all — auth is the `.smarthub.coop` httpOnly cookie Chrome even strips
  from the HAR. The correct shape mirrors the working vendors: pull client-side, then POST the
  PARSED daily rows to the existing additive `/v1/array-owners/utility-meter-capture` (the same
  `daily[]` path GMP uses). Only the LOCATION of the fetch moved; the backend contract didn't.
- **Durable owner-facing API key / JWT in localStorage** (e.g. GMP's `gmp-vue.user.apitoken`) →
  CAN go server-side (ship the token, backend pulls) OR client-side. GMP works server-side because
  that JWT is a real bearer the backend can replay; SmartHub's cookie is not.
RULE: run section 5's in-portal probe and check the HAR for the auth mechanism BEFORE designing the
capture; never assume a vendor can be pulled server-side just because another one could.

## 6c. Trigger the capture from the POLL LOOP, not only the login-fetch hook (VEC, Jun 2026)
A capture wired to fire only inside the `window.fetch` interception of the login endpoint
(`/services/oauth/auth/v2`) NEVER RUNS for an already-signed-in owner — they don't re-hit login, so
the hook never fires. Symptom: the bill scrape runs ("Synced → local-only") but the meter step is
silent, no error. FIX: call the (idempotent, intent-gated, once-guarded) capture function from the
main scrape poll loop (`tryScrape`) so it fires on initial load AND every SPA nav, independent of a
fresh login. Keep the login-hook call too as an extra trigger. If the capture needs a username it
can't get yet, un-set the once-guard and let the next poll retry rather than failing.

## 6d. SmartHub secured data calls require the username in TWO places (VEC HAR, Jun 2026)
`/services/secured/user-data` and `/services/secured/utility-usage` 401 ("couldn't read your
accounts") without the owner's email on BOTH: `user-data?userId=<email>` as a query param AND an
`x-nisc-smarthub-username: <email>` header on both calls (the session cookie alone is NOT enough).
Resolve the email client-side from (in order): the auth-intercept `primaryUsername`, the home-page
URL hash `#/home?<base64 ...userId=...>` (`decodeHashCreds`), or a localStorage/sessionStorage scan
for an email-looking value under a user/name/primary/nisc key. SmartHub usage data contract +
the negative-y=generation signal: see utility-meter-data-requirement.md.

## 6e. Capture SUCCEEDS but "no live data" — the silent-drop / display-gate class (Chint, Jun 2026)
A DIFFERENT failure shape from the stall: the inverter cards APPEAR immediately (capture worked,
rows persisted) but every card reads "not producing right now" with no kW, across multiple retries.
This is NOT a capture bug — the data is flowing — it's getting silently dropped or refused downstream.
Two real bugs stacked here; check BOTH:
1. **Pydantic silently drops fields the schema doesn't declare.** `chint_content.js` shipped
   `current_power_w` per inverter (grounded: `commDevice.currentPower`), but `CaptureInverter` had
   NO `current_power_w` field → FastAPI/Pydantic discarded it on ingest with no error. The backend
   then fell back to splitting a site-level total by energy share, and when that site field was
   absent every inverter got null power. FIX: add the field to the capture schema and PREFER the
   inverter's OWN reading; keep site-allocation only as a fallback for vendors that report power
   site-wide (Fronius). LESSON: whenever the content script sends a field, GREP the Pydantic body
   model for it — an undeclared field vanishes silently, looks like a capture failure but isn't.
2. **Front-end refused to show real output without a nameplate.** `sandbox.js outputState()`
   required `nameplate_kw` to render ANY live output (it computed % of max). Chint reports no
   nameplate → `meaningful=false` → "not producing right now" even on a live 51 kW reading. It
   conflated "I don't know the rated max" with "it's not producing." FIX: "producing" needs only a
   real live reading — with nameplate show "% of max", without show absolute "X.X kW · producing
   now" + a full calm bar. The 1s live ticker already no-ops when `data-maxw` is empty, so no
   re-render regression. LESSON: never gate displaying a real measured value on a DERIVED quantity
   (a ratio) whose denominator may be unknown.

## 6e-bis. "Cards show no live data / no liquid fill" can be STALE data, not a bug (Jun 2026)

Symptom: SolarEdge inverter cards show live kW + the liquid-fill visual, but Fronius
& SMA cards all read "not producing right now" with no fill — looks like the new
card visual is broken for those vendors. IT ISN'T. Diagnose with the §6f prod probe:
print each inverter's `last_power_w` AND `last_power_at`, then compare `last_power_at`
to `now()` against `_POWER_FRESH` (3h, in inverter_fleet.py `_live_power_w`).
ROOT CAUSE: capture-only vendors (Fronius/SMA/Chint) only get fresh power when the
owner MANUALLY re-captures; SolarEdge pulls live from its API on every page load so
it's always fresh. So 3h after a capture, `_live_power_w` correctly NULLs the stale
reading (showing a 20h-old watt value as "live" would be a lie) → card shows "not
producing" → no fill (fill height needs live output). The DB still HAS the real
watts (e.g. Tannery #1=3857W from yesterday 22:17) — they're just gated as stale.
FIX = re-capture (not a code change); the card is behaving correctly.
DESIGN FOLLOW-UP Ford flagged but hadn't decided: the 3h gate makes capture-vendor
cards go blank 3h after every capture (recurs forever). Options: widen `_POWER_FRESH`
for capture vendors to ~24h + show an "as of Xh ago" label (live-ish, honest), or a
daily scheduled re-capture. Prefer the labeled-wider-window — don't silently show
old data as current.
RELATED single-inverter outlier: a lone inverter at power_w=0.0 with 0 history while
its peers produce (e.g. SMA Tannery Brook #7, "30 MW"/"20 kW" mangled name) is EITHER
a real dead/faulted unit (the peer engine's "Below its neighbors" flag is then
correct) OR a bad capture — a stale 0 can't tell you which. Re-capture to disambiguate
before telling Ford it's an outage.

## 6f. PROVE it server-side with a prod DB + in-process /fleet-tree probe (don't guess from the UI)
Once the console shows the extension emitting good values (section 4's loud trace), the bug is
downstream — settle WHERE definitively instead of iterating UI builds. Two read-only prod probes,
run via `railway ssh "cd /app && echo <base64-of-script> | base64 -d | python"` (base64 because
inline python f-strings/parens trip the shell-quote redactor — see the secret-handling note):
- **DB probe**: `select(Inverter).where(Inverter.vendor=="chint")` → print serial/last_power_w/
  last_power_at/tenant. If `last_power_w` matches the console values → ingest+persist are CORRECT,
  bug is purely render/session. If null → the drop is in ingest (see 6e #1).
- **Endpoint probe**: build `inverter_fleet.build_fleet_tree(db, tenant)` in-process and print each
  inverter's `current_power_w` — proves what the API actually returns to the page, no browser
  needed. When both show the real watts, the remaining bug is 100% front-end (see 6e #2).
This probe also surfaced a SECOND issue for free: every "Log in with Chint" RETRY created a NEW
array_operator tenant (the re-capture-into-soft-deleted-array UniqueViolation, fixed separately) —
so the dashboard session may be pointed at a tenant that lacks the latest capture. When a user
"keeps retrying and it won't show," check for duplicate tenants holding the same serials.

## 6g. Instant graph history backfill from a per-SITE daily series

> ⚠️ **CORRECTION (Jun 2026) — `weekETrend` DOES NOT EXIST in Chint's responses. The whole
> "instant Chint backfill" below was built on an ASSUMED field, shipped, and claimed "done"
> TWICE before the user caught it ("why aren't the graphs showing up? I thought we fixed
> this?").** The grounded Chint contract (verified from Bruce's 2026-06-16 HAR, documented at
> the top of `chint_content.js` and in `chint-portal-api-contract.md`) is ONLY:
>   - `site/retrieve`: `{id, siteName, installedCapacity, currentPower}` — **NO history series**
>   - `busTypeDevices`: per-inverter `{sn, model, currentPower, eToday, statusName}` — only TODAY
> There is no `weekETrend`, no daily-history array, in either endpoint we observe. So
> `st.weekETrend` is always `undefined`, the backfill writes nothing, each array keeps ONLY
> today's row, and the ≥2-day graph never appears. **A later HAR of the Chint production-chart
> view (`monitor.chintpowersystems.com...arrayproduction.har`) confirmed the chart endpoint is
> `/openApi/v1/siteMertics/getSiteTimeSharingChart2?...&type=power&interval=30` — INTRADAY 30-min
> POWER, not daily energy — and its response body wasn't even captured.**
>
> **RESOLVED (Jun 2026, v1.9.39) — the chart endpoint IS the source; integrate it.** A follow-up
> HAR (`...arrayproductionfaily.har`) finally captured the chart RESPONSE BODY:
> `/openApi/v1/siteMertics/getSiteTimeSharingChart2` returns `data.times[]`
> (`"YYYY-MM-DD HH:MM"`, 30-min slots) + `data.pv[]` (the dedicated PV-production series, kW per
> slot; there's also `metrics`/`load`/`meter`). There is NO daily-energy endpoint — so DON'T keep
> waiting for one: INTEGRATE the 30-min power curve into daily kWh yourself, exactly like the
> Fronius path: `kWh_per_day = Σ(pv_kW × interval_h)`, interval from the URL `&interval=30` (→0.5h).
> Verified against the real HAR body: 6/15=1498.8, 6/16=1671.3, 6/17=941.6 kWh (plausible for the
> 186 kW site). Wiring that shipped: `chint_inject.js` ALREADY relays `/openApi/` responses;
> `chint_content.js` parses the chart in the SO_CHINT_RESPONSE listener → `dailyFromChart(json,
> search)` (siteId from the query string) → `dailyBySite` map → `assemble()` sets `site.daily` from
> it (the dead `dailyFromTrend`/`weekETrend` path was DELETED). DEDUP GOTCHA: the chart response
> often arrives AFTER the inverters, so the emit-dedup signature MUST include the daily day-count
> (`"|d"+s.daily.length`) or the later history never re-emits. The site.daily → DailyGeneration →
> fleet-tree column → arrayGraph plumbing below is unchanged (it was always correct).
> The same HAR also handed us the real per-site **lat/long** from `api/asset/site/info`
> (`data.latitudeLongitude`, e.g. Londonderry 43.208448/-72.780698) — wire that into the §6g-ter
> sun calc.
>
> **THE HARD LESSON (this is the capture-grounding trap my own MEMORY warns about):** I invented
> a field name, built a 4-layer feature on it, and reported success without ever confirming the
> field appears in a real captured response. The non-negotiable rule for THIS class of work:
> before building ANY capture/backfill on a field, GREP a real HAR/fixture for that exact field —
> if there's no fixture in the repo and you can't point at the bytes, you are GUESSING; say so and
> ask for the HAR, do NOT ship + claim "done." Two different endpoints were guessed across the
> same session (`/openApi/v1/dashboard/daysEnergy` then `weekETrend`) — neither verified.
>
> **HOW IT GOT UNSTUCK (the recovery recipe):** (1) ask for a HAR of the SPECIFIC chart view you
> need, and tell the user to enable \"capture response bodies\" / preserve-log — the first chart HAR
> had the right URL but EMPTY bodies (0 bytes), useless; the second (bodies on) cracked it.
> (2) When NO daily-ENERGY endpoint exists but an intraday POWER curve does, don't keep hunting —
> INTEGRATE power×interval into daily kWh (Σ kW×0.5h), the same trick the Fronius path uses.
> (3) UNIT-TEST the extraction fn against the real HAR body before shipping (`node` eval the fn out
> of the content script, feed it the captured JSON, assert sane kWh) — that's what turned \"claimed
> done\" into actually-verified this time.
>
> **THE OTHER ROOT CAUSE of "Chint graphs not showing" — DUPLICATE TENANTS fragment the history.**
> Even with backfill aside, day-by-day accumulation should eventually reach 2 days — it can't,
> because every "Log in with Chint" RETRY minted a NEW array_operator tenant (the re-capture-into-
> soft-deleted-array UniqueViolation, §6f). Prod had FIVE separate tenants each named "Londonderry
> 186", each holding ONE day (6/16 on one, 6/17 on others). The system HAS ≥2 days of data — it's
> just scattered across accounts, and the dashboard session sees only its tenant's single row. So
> the fastest REAL fix for "graphs not showing" needs NO history endpoint: CONSOLIDATE the
> duplicate tenants (move the DailyGeneration rows onto one surviving "Londonderry 186") and the
> graph appears from data already collected. Probe it: `select(Inverter).where(vendor=="chint")`
> → group by `array_id`/`tenant_id`, print each array's DailyGeneration row count + day. Per
> Ford's deletion-safety rule, do the consolidation NON-destructively and confirm the surviving
> tenant with him before anything irreversible.
>
> **CONSOLIDATION EXECUTED (Jun 2026) — the recipe that worked.** Ford said keep
> `ford.genereaux@gmail.com` + `bruce.genereaux@gmail.com`, soft-delete the rest. DRY-RUN FIRST
> revealed: (a) there were NO unowned throwaway tenants — every dup was a typo/plus variant of
> Ford's own email (`ford.genereaux1@`, `ford.generea44ux@`, …); (b) one dup (`ten_d688`) held the
> ONLY 7-day dataset; (c) Ford's real AO tenant (`ten_a554`, matched via `tenants.contact_email`,
> NOT a `Tenant.deleted_at` column — that doesn't exist) already owned a `Londonderry 186` array
> but SOFT-DELETED with 0 data. So mass-delete would have nuked the only good history. SAFE MOVE:
> in ONE transaction — reactivate Ford's deleted array (`deleted_at=None`), repoint the 7 DG rows +
> 4 Chint inverters onto it (no collision: `Inverter` uq=`(tenant_id,vendor,serial)` unique to
> source; `Array` uq=`(tenant_id,name)` satisfied by reusing the existing deleted array, not making
> a new one; `DailyGeneration` uq=`(array_id,day)` target empty), then soft-delete the 4 typo
> tenants via `t.active=False; t.subscription_status="cancelled"` (Tenant soft-delete is the
> `active` flag, per onboarding.py — REVERSIBLE) + `deleted_at` on their arrays. Verified by
> building `build_fleet_tree` for Ford's tenant in-process: column `daily` = 7 days → graph renders.
> The whole consolidation is read-model-safe and reversible; the atomic txn meant the first failed
> attempt (wrong `Tenant.deleted_at` attr) committed NOTHING.

(Historical write-up of the intended pipeline, kept for the mechanism — but see the correction
above: the Chint `weekETrend` source is FICTIONAL; the persistence/render plumbing below is real
and vendor-agnostic, it just has no Chint history to feed it yet.)
Chint's `site/retrieve` response was ASSUMED to carry
`weekETrend[]` (`[{name:"20260610", value:"996.2"}]` = ~7 days of SITE daily kWh) — IT DOES NOT.
Pipeline (the plumbing IS correct, the source is not): extension maps a `site.daily[]` →
backend persists each day as `DailyGeneration` (idempotent, MAX-wins per (array,day), preserves a
literal 0-output day) → `build_fleet_tree` returns a COLUMN-level `daily` → front-end `arrayGraph`
falls back to it when per-inverter series are sparse (carry `daily` through BOTH fleet-store
`adaptTree` AND `toColumns`, both of which rebuild the column and will drop an unlisted field).
CRITICAL HONESTY RULE: Chint gives SITE-level history but NO per-inverter history — backfill at the
ARRAY level only; NEVER split a site total across inverters to fake per-inverter trends (it would
fool the peer-analysis engine). So the array graph fills instantly; per-inverter sparklines still
build honestly day-by-day.

### 6g-bis. Generalize the backfill to EVERY vendor (Ford: "needs to work with every vendor", Jun 2026)
The backend `site.daily` ingest + the fleet-tree column `daily` + the front-end `arrayGraph`
fallback are VENDOR-AGNOSTIC (the ingest loops every site regardless of provider). So "make the
graphs fill for all vendors" reduces to: each content script must EMIT a `site.daily[]` from
whatever multi-day history that portal already exposes. No `array_owners.py` change is needed to add
a vendor — which also keeps it collision-free when another agent is editing the backend. Per-vendor
history source (all SITE-level, summed, never per-inverter) — ⚠️ **ALL THREE capture-vendor
sources below are UNVERIFIED GUESSES, NONE confirmed against a real history HAR (see the §6g
correction):**
- **Chint** → RESOLVED (v1.9.39): NOT `weekETrend` (fictional). Integrate the production-chart
  endpoint `getSiteTimeSharingChart2` `data.pv[]` 30-min power curve → daily kWh (see §6g RESOLVED).
  Grounded + shipped against a real captured response body.
- **Fronius** → REUSING `/Chart/GetAnalysisChart` per-day was a plausible guess (only year/month/day
  query varies) — but the multi-day RESPONSE SHAPE was never re-HAR'd; treat as unverified.
- **SMA** → `POST /api/v1/measurements/search` channel `Measurement.Metering.TotWhOut.Pv`,
  `resolution:"OneDay"`, `aggregate:"Dif"` was a guess from the channel naming — never confirmed
  to return daily Wh in that shape; treat as unverified.
- **SolarEdge** → the ONLY REAL one: its live API yields per-inverter `daily`, persisted to
  `InverterDaily` (`_persist_daily_series`) + the 03:30 snapshot job. No backfill needed; it even
  has real PER-inverter history (the capture vendors only have site-level).
Make every history pull BEST-EFFORT (try/catch → `[]`): a failed/empty history just lets the graph
build up naturally; NEVER fabricate a value. CAVEAT (CORRECTED): NONE of the three capture-vendor
history sources is verified — Chint's `weekETrend` is FICTIONAL (doesn't exist), and Fronius/SMA's
were guessed from endpoint/channel naming, never re-HAR'd. The defensive try/catch means worst case
is an empty graph (which is what's happening), not a wrong one — but until each vendor's daily-ENERGY
history endpoint is confirmed against a real captured response body, the "instant backfill" simply
does not run for that vendor. The loud `site history backfill: <id> N day(s)` log will show `N=0`
when the guessed field is absent — treat N=0 as "this source was never real," not "no history yet."

## 6g-ter. Day/night ("Sleeping") flag for inverter cards — compute server-side, gate on SUN not zero (Jun 2026)
The liquid-fill (and any) inverter card needs a calm "Sleeping" night state so a zero reading at
dusk/overnight reads as rest, not alarm. HARD RULE (a design-spec ask, but a real correctness gate):
trigger "Sleeping" on `is_daylight==False AND output==0`, NEVER on `output==0` alone — a NOON FAULT
that zeroes every inverter would otherwise be mislabeled "asleep" and HIDE a real outage. Compute the
flag ONCE server-side in `build_fleet_tree` (not per-card) and expose it as `is_daylight` on every
fleet-tree column AND in `summary` (front-end ANDs it with per-inverter output==0).
- **No lat/long is stored anywhere** (no Array model column, no adapter supplies one; the front-end
  `col.lat` reads only exist for the SYNTHETIC demo fleet). So a precise per-array sunrise is
  impossible today — verify this assumption (`search_files lat|latitude|longitude` over api/) before
  believing a spec that says "you already have the lat/long."
- Do NOT use the spec's fixed-hour fallback (`h<5||h>=21`) — it's badly wrong seasonally (a VT winter
  6am is dark but reads "day"). Instead compute REAL solar elevation via the dependency-free NOAA
  solar-position algorithm (equation-of-time + declination + hour-angle → elevation) at a regional
  default (`_VT_LAT/_VT_LON` = central Vermont; the fleet is all-VT). `is_daylight = elevation > -2°`
  (a panel still trickles just below the horizon). `_solar_elevation_deg`/`_is_daylight` in
  inverter_fleet.py take optional `lat,lon,when` so the instant a per-array lat/long is captured
  (Chint's `site/retrieve` carries lat/long), pass it through for exact per-site sun — one-line change.
- Verify seasonally, not just "now": Jun local-noon=day, Jun midnight=night, Dec ~6am EST=night
  (the fixed-hour case), Dec noon=day. Test: tests/test_array_owners.py::test_solar_elevation_is_seasonally_correct.
- This was a pure ADDITIVE backend change (inverter_fleet.py only, no array_owners.py) — collision-free
  while another agent edited the backend. General lesson: when a UI-design spec hands you "one small
  backend ask," scope your change to the read-model builder (fleet-tree) and keep it additive.

## 6i. "Card shows no live data / no liquid fill" for capture vendors = the FRESHNESS GATE, not a bug (Jun 2026)
Symptom: SolarEdge inverter cards show live kW + the liquid-energy fill, but Fronius & SMA cards
all read "not producing right now" with NO fill — even though the DB has real power for them. DON'T
chase it as a card/render bug. Root cause: `inverter_fleet._live_power_w` only returns
`last_power_w` while `(now() - last_power_at) <= _POWER_FRESH`; older readings are nulled
ON PURPOSE (showing a 20-hour-old watt reading as "live" would be a lie). The asymmetry:
- **SolarEdge** pulls LIVE from its API on every fleet-tree build → always fresh → always shows.
- **Fronius / SMA / Chint** are extension-capture only → their power is as old as the last manual
  "Log in with X" capture. After the window expires, every card goes blank.

### 6i-WIDENED → the gate was raised 3h→24h, which turned "blank" into "shows STALE as LIVE" (the Tannery Brook bug, Jun 2026)
`_POWER_FRESH` was widened from 3h to **24h** (inverter_fleet.py line ~127) so capture-vendor cards
wouldn't blank out between captures. That traded one bug for a WORSE one: a reading captured at the
2pm peak is still inside the 24h window at 9pm, so our sandbox shows "Tannery Brook · 17 kW ·
producing now" all evening while SMA's OWN portal correctly shows ~0 (sun's down). Ford caught this
exactly right: "it says producing on OUR sandbox but not on SMA's site — we collect the data FROM
SMA." Showing a stale afternoon number as current is a DATA-INTEGRITY / trust bug, not cosmetic —
worse than a blank card, because it looks authoritative and is wrong. LIVE PROOF recipe (read-only
prod, the §6f base64 probe): print each inverter's `last_power_w` + `last_power_at` + `(now() -
last_power_at)`; the tell is a real watt value stamped hours ago in the AFTERNOON being shown at
night. (This session: 14–17 kW stamped 18:35 UTC = 2:35pm, shown live at ~9pm.) Also found a
data-hygiene issue alongside: DUPLICATE Tannery Brook arrays (ids 1297 AND 1299 both held the full
#1–#7 set, plus several empty Tannery Brook shells) — separate from the staleness bug but worth a
consolidation pass (§6f/§6j recipe).

THE FIX FORK (the honesty floor vs the real thing — present BOTH, ship the cheap one now):
- **Fix A — stop lying (cheap, same-day):** (1) NEVER render a captured reading as "producing now" —
  always label it "as of <capture time>" so it reads as a snapshot, not a live feed. (2) Respect the
  `is_daylight` flag we ALREADY built (§6g-ter): if the sun is down, show 0/sleeping regardless of a
  stale daytime capture. (2) alone kills the Tannery-Brook-at-night bug. This makes data honest but
  still only as fresh as the last manual capture.
- **Fix B — actually update constantly (the real ask, real work):** match SolarEdge — server-side
  polling. SMA/Fronius DO have APIs (SMA = ennexOS/OAuth, §6); store the owner's token and have the
  BACKEND poll every ~5–15 min so our numbers track the vendor's live site continuously, no manual
  re-capture. This is the only thing that truly makes "our data = the vendor's data, always." Scope
  its effort against ALL the manual-capture vendors (SMA, Fronius, Chint share the boat), and note
  the OAuth-onboarding + token-refresh cost — we went the extension route originally BECAUSE those
  creds weren't wired.
- A middle option (extension MV3-alarm background auto-recapture) only works while Chrome is open AND
  the owner is logged into the portal → unreliable as "constant"; mention but don't lead with it.
RULE: when Ford says "we need to be updating constantly," that is a request for Fix B (continuous
server pull), but Fix A (honest labeling + is_daylight gate) is the immediate must-ship floor — never
leave stale data presented as live while B is scoped. Don't just widen/narrow the window blindly;
"shown stale as live" and "blank between captures" are BOTH wrong — the label + sun-gate is the
escape from that false dichotomy.

DIAGNOSIS recipe (unchanged): DB shows real `last_power_w` but the card shows wrong/old/blank →
check `now() - last_power_at` vs `_POWER_FRESH` BEFORE touching any render code. The data isn't
lost, it's gated/stale.

### 6i-bis. A single inverter reading 0 while peers produce → peer-engine "Below its neighbors" (could be real OR a bad capture)
SMA Tannery Brook #7 captured `last_power_w=0.0` + 0 days of history while its 6 siblings read ~4-5kW
with 2 days each → the peer-analysis engine correctly flagged it amber "Below its neighbors." You
CANNOT tell from one stale 0 whether it's a real dead/faulted inverter (worth surfacing to the owner)
or a capture glitch (mangled serial, e.g. #7's "S/N 9052" looked malformed vs siblings' real serials).
The ONLY way to resolve: re-capture and re-check. If #7 still reads 0 while peers produce → real
outage, flag is right. If it reads normal kW → the 0 was a capture artifact. Never assert "dead
inverter" off a single stale zero.

## 6j. Tenant-reset / clean-slate requests — the surgical, low-regret recipe (Jun 2026)
"Delete all client data and accounts so I can start fresh, my dad's too, override and approve" is the
EXACT class my deletion-safety rule covers — an explicit override does NOT change the math on an
irreversible prod wipe. ALWAYS inventory first (read-only) then offer scoped REVERSIBLE choices via
clarify; never run a blind hard wipe. What the prod inventory revealed (and why blind-deleting was
catastrophic): 25 tenants, including Bruce's LIVE NEPOOL pilot (64 arrays / 184 days), Bruce's AO
data (277 days), AND the product demo (180 days) — all ACTIVE — buried among ~18 throwaway typo/plus
signups. "Start fresh" almost NEVER means "wipe the live pilot + demo"; it means a clean re-capture
slate for the user's OWN test tenant. The clarify offered 4 scopes; Ford picked the narrowest
(reset only his AO test arrays). EXECUTION recipe that worked:
- Tenant soft-delete = `t.active=False; t.subscription_status="cancelled"` (Tenant has NO `deleted_at`
  column — that's on Client/Array/Inverter). Reversible.
- Array/Inverter soft-delete = set `deleted_at=now`. DailyGeneration/InverterDaily are hard row-
  deletes (no soft flag) — so warn that those specific rows are gone for good.
- BEFORE wiping, surface the IRREPLACEABLE subset: long-history SolarEdge arrays (Starlake 90d,
  Londonderry 93d, Cover Catamount 89d) CANNOT be re-captured (portals expose only ~recent windows).
  A second clarify saved them — Ford chose "reset only the recent 2-7 day test arrays, KEEP the
  90-day history." Run it as ONE atomic txn with a `assert array.tenant_id==TARGET and aid not in
  KEEP` guard per array, then VERIFY the KEEP set is still active with full history after commit.
LESSON: every destructive prod op gets (1) read-only inventory, (2) clarify with reversible scoped
choices, (3) a flag for any data that can't be regenerated, (4) atomic txn + post-verify. The first
consolidation attempt this session failed on a wrong attr (`Tenant.deleted_at`) and committed NOTHING
precisely because it was atomic — that's the safety net working.

## 6k. Chrome Web Store privacy package — grounded in the manifest, not guessed (Jun 2026)
When Ford uploads a new extension build he'll ask for the privacy blurb. Store review REJECTS vague or
inaccurate policies, so GROUND it in the actual `extension/manifest.json` (permissions + host_permissions
+ content_scripts), never boilerplate. The extension's single purpose: "links the user's own utility +
solar-inverter accounts to their Array Operator / Solar Operator dashboard" — read-only, only after the
user clicks Connect, sends only to solaroperator.org/arrayoperator.com/nepooloperator.com, no sale/ads.
A complete package = (1) hosted PRIVACY policy (saved to extension/PRIVACY.md), (2) single-purpose
description, (3) per-permission justifications (storage=connection state+tokens; cookies/host=read own
portal data read-only; scripting=inject reader into the portal page; alarms=periodic refresh;
notifications=connect success/attention). Data checkboxes: collects "Authentication information"; NOT
payment/web-history/user-activity; certify not sold, not used beyond single purpose, not for
creditworthiness. ⚠️ The store REQUIRES a public privacy-policy URL — offer to publish it at
arrayoperator.com/privacy (write public/privacy.html + Netlify deploy) since a hosted link is mandatory.

## 6i. TWO DIFFERENT history graphs — don't fix one and claim both (Jun 2026)
"History graphs aren't showing" is AMBIGUOUS — there are TWO separate graphs fed by
TWO separate tables, and fixing one leaves the other broken (cost a "fixed this twice"
from Ford). Always disambiguate which graph WITH a screenshot before claiming done:
- **Array production graph** (the bigger line graph) ← `DailyGeneration` (array-level),
  surfaced as the fleet-tree COLUMN `daily`, rendered by `arrayGraph()`. Backfilled by
  `CaptureSite.daily[]` (Chint chart-integration §6g, Fronius/SMA site history §6g-bis).
- **Per-inverter SPARKLINE** (the tiny graph on EACH card, "no history yet" box) ←
  `InverterDaily` (per-inverter), the inverter's own `daily`, rendered by `invSpark()`.
  Backfilled by `CaptureInverter.daily[]` — a DISTINCT field added later; before it,
  capture wrote only ONE InverterDaily row/capture (today) so every sparkline showed
  "no history yet" even when the array graph was full.
BOTH need ≥2 days to render (`invSpark` returns "" / arrayGraph shows "history building"
when <2). Verify the EXACT render by node-eval'ing the real fn out of sandbox.js against
the data shape (1-day→empty, ≥2-day→polyline) — that's how this got confirmed not guessed.
Per-inverter backfill sources: Fronius `captureHistory()` returns BOTH site totals AND
per-device daily (keyed by displayName, from the SAME devwork chart it already fetched) →
attach to each inverter by name. SMA generalized `fetchHistory(componentId)` to query any
component; per-device is BEST-EFFORT/unverified (plant query grounded, per-device reuses
the channel — empty-safe). Chint has NO per-inverter history (site-only chart) → its
sparklines genuinely can't backfill, accumulate one day at a time; never fake it.
LESSON: when the user reports a "graph" bug, a screenshot tells you sparkline-vs-array in
one look; without it you'll fix the wrong table and ship a false "done."

## 6j. STALE-DATA freshness gate makes capture-vendor cards go blank (Jun 2026)
Cards showed "not producing right now" / no liquid fill for Fronius+SMA while SolarEdge
worked. Root cause was NOT a card bug: `_live_power_w` nulls `current_power_w` when
`last_power_at` is older than `_POWER_FRESH` (3h) — correct (a 20h-old reading isn't
"live"). But capture-only vendors (Fronius/SMA/Chint) ONLY refresh power on a manual
re-capture, so their cards blank 3h after every capture; SolarEdge refreshes live on each
page load so it never blanks. DIAGNOSIS recipe: DB shows real `last_power_w` but the card
shows nothing → check `now() - last_power_at` vs `_POWER_FRESH` before touching any
render code. The data isn't lost, it's gated stale. Fix options: re-capture (immediate),
or widen the window for capture vendors + show an "as of Xh ago" label.

## 6k. Liquid-card UI bugs (plate-fill, QA-harness) live in array-operator-card-ui.md
The plate-must-flex-fill pitfall and the visual-QA-harness-degeneracy lesson belong with the
other card-UI render patterns — see references/array-operator-card-ui.md (kept there to avoid
duplication; this file is for CAPTURE/ingest, that one for the owner-facing card render).

## 6h. The Sentry-autofix cron can hijack the working tree (git-churn trap, Jun 2026)
Ford twice said "delete everything, my dad's too, override and approve" — a FULL hard wipe
would have destroyed Bruce's LIVE NEPOOL pilot (64 arrays/184 days), the product demo
(180 days), and 90-day SolarEdge history. The override does NOT change the math on an
irreversible prod wipe. PROVEN SAFE SEQUENCE: (1) read-only INVENTORY every tenant first
(arrays/clients/inverters/DG-days, active flag, contact_email) and PRINT it — the dry-run
repeatedly flipped the decision (e.g. a tenant I'd flagged "throwaway" was actually
Bruce's). (2) Map the keep-emails to tenant_ids via `tenants.contact_email` (NOT a
`Tenant.deleted_at` column — that doesn't exist; tenant soft-delete is `active=False` +
`subscription_status="cancelled"`; ARRAY/inverter soft-delete IS `deleted_at`).
(3) clarify the SCOPE with explicit choices before any write, and flag irreversible
collateral (90-day history that can't be re-captured). (4) Prefer the narrowest action
that achieves "start fresh" — usually reset only the recent TEST arrays, keep long
history. (5) Run as ONE atomic txn so a bug commits nothing (a wrong-attr error did exactly
that — zero partial writes). (6) VERIFY preserved items still active+full after. Move data
across tenants safely by checking the three uniques first: Array `(tenant_id,name)`,
Inverter `(tenant_id,vendor,serial)`, DailyGeneration `(array_id,day)` — reactivate an
existing soft-deleted array rather than create a colliding new one.

## 6j. TWO DISTINCT graph paths — don't conflate them (the "I thought we fixed this" trap, Jun 2026)
"The history graphs aren't showing" is AMBIGUOUS — there are TWO separate graphs on the fleet
canvas, backed by TWO different tables and TWO different render fns. Fixing one and reporting "done"
when the user meant the other is what caused Ford's repeated "I thought we fixed this?":
- **ARRAY production graph** — backed by `DailyGeneration` rows `(array_id, day)`; rendered by
  `arrayGraph(invs, col.daily)`; needs the COLUMN-level `daily` (>=2 days). Fed by `site.daily[]`.
- **Per-inverter SPARKLINE** — the small graph ON each inverter card; backed by `InverterDaily`
  rows `(inverter_id, day)`; rendered by `invSpark(iv.daily, ...)`; needs PER-INVERTER `daily`
  (>=2 days, else literal "no history yet" box). Fed by per-inverter `ci.daily[]`.
DIAGNOSE BEFORE PATCHING: (1) look at the screenshot — is the empty graph the big array one or the
little per-card sparkline? (2) probe BOTH tables in prod: `DailyGeneration` count per array AND
`InverterDaily` count per inverter — they diverge (e.g. array had 8 days but each inverter only 1).
(3) run the REAL front-end fn out of sandbox.js via `node` eval against the actual fleet-tree shape
to see which branch (graph vs placeholder) fires — this is what definitively told us the array
graph rendered but the sparkline didn't. The capture-vendor backfill must populate the RIGHT table:
`CaptureSite.daily` → DailyGeneration (array graph); `CaptureInverter.daily` → InverterDaily
(sparkline). Adding the array backfill does NOT fix sparklines — that needs a separate per-inverter
`daily[]` (Fronius devwork per-device curves, SMA per-device measurements; Chint has NO per-inverter
history so its sparklines can only accumulate one day at a time — never fake them).

## 6k. Multi-user auth: capture must tolerate RECOVERABLE-inactive tenants (Jun 2026)
"Couldn't bring in that <vendor> account (HTTP 500/403)" on an EXISTING account, or "multiple users
can't use the site," can be an AUTH-LIFECYCLE bug, not a capture-parsing bug. Trace the tenant
lifecycle before assuming the signup/login path is broken — in this codebase it was actually sound
(new tenants `active=True`; signup dedups per `(contact_email, product)`; login is product-scoped +
active-first; `tenant_from_session` does NOT gate on `active` so logged-in users always reach their
dashboard; zero real same-email+product duplicates in prod — the "dozens of tenants" were Ford's own
typo/plus-variant test emails, genuinely distinct accounts dedup can't merge). THE REAL BUG: a
14-day trial with NO card auto-pauses at day end (scheduler.py: `active=False`,
`subscription_status="paused_no_card"`, intended "read-only, resume anytime"). But the extension
CAPTURE authed via the STRICT `app.tenant_from_bearer`, which hard-403s ANY inactive tenant — so the
instant a trial paused, every capture silently 403'd despite the read-only promise. The two auth
paths DISAGREED: session-login allowed inactive (correct for viewing), tenant-key capture forbade it.
FIX (scoped to the CAPTURE path only — never loosen the global strict gate that guards other
endpoints): a `_capture_tenant_by_key` that allows inactive tenants whose status is RECOVERABLE
(`paused_no_card`/`trialing`/`comped`/`active`/None) so data keeps flowing, and returns an actionable
**402** `{error:"subscription-cancelled", cta_url:"/account"}` (NOT a silent 403) for a hard-cancelled
tenant so the extension can prompt "add a card to resume". Only report DELIVERY gates on `active`
(scheduler filters, unchanged). LIFECYCLE FACTS worth keeping: capture auth = session-token-first,
tenant-key fallback; `app.tenant_from_bearer` (tenant-key, strict active-gated) vs
`account.tenant_from_session` (session token, NOT active-gated); a paused tenant has `trial_ends_at=
None`. Ford's monetization stance: chose the MOST GENEROUS option (keep data flowing for paused) —
he optimizes UX/"resume anytime" over squeezing card-less trials.

## 6i. Re-capture 500s — SELECT-then-INSERT on a unique (id,day) is a landmine (Jun 2026)
Symptom: re-adding an ALREADY-LINKED account (user didn't delete anything) → long pause →
"Couldn't bring in that <vendor> account (HTTP 500)". Sentry showed
`IntegrityError UniqueViolation "uq_inverter_daily_inv_day" Key (inverter_id, day)=(98, 2026-06-16) already exists`
at `db.commit()`. ROOT CAUSE was my own per-inverter backfill (§6g per-inv history): it did
SELECT-then-`db.add(InverterDaily(...))` per (inverter,day) in TWO independent blocks — the
today-row write AND the `ci.daily` history loop. On a re-capture (rows already exist) OR when
`ci.daily` contained today / a duplicate date, two INSERTs targeted the same (inverter_id, day) in
one uncommitted session and only blew up at commit. SELECT-then-INSERT is NOT safe against: rows
already in the DB, duplicate keys within the same payload, or the same key written by two code
paths in one transaction. FIX PATTERN: build ONE deduped `{day: kwh}` map per inverter (merge
today + history, max-wins), load existing rows ONCE via `day.in_(list(map))`, then update-or-insert.
Kills the class in one place. REGRESSION TEST MUST re-POST the SAME payload twice (the exact user
action) — a single-POST test passes and misses it. GENERAL RULE: any capture/upsert keyed on a
unique constraint must be a real upsert (dedup-map + load-once + update-or-insert, or INSERT…ON
CONFLICT), never per-row SELECT-then-add — capture endpoints are HIT REPEATEDLY by re-connects. The
sibling `uq_array_per_tenant` 500 (§6f) is the SAME class at the Array level, already fixed by
matching across soft-deleted arrays + reactivating. PROVE ON LIVE PROD (Ford's rule): call the real
endpoint fn in-process against the prod DB TWICE (monkeypatch `_tenant_from_bearer` to return the
tenant; `inverter_capture` is SYNC not async — don't await), assert both ok + max-wins, then DELETE
the probe rows. Bonus finding: Ford's AO tenant is `active=False`, so the tenant-key bearer path
403s — his browser works only via `tenant_from_session` (active session token); capture auth =
session-token-first, tenant-key fallback (`_tenant_from_bearer`).

### 6i-bis. The SAME bug at the ARRAY level — uq_daily_array_day (re-add SMA, Jun 2026)
After fixing the per-inverter `InverterDaily` upsert, re-adding an SMA array 500'd AGAIN — same
class, different table: `UniqueViolation uq_daily_array_day Key (array_id, day)=(1298, today)`.
Sentry merged it into the SAME issue (PYTHON-FASTAPI-3) but the constraint name had changed —
ALWAYS re-pull the latest event's `value`/DETAIL, don't assume it's the same write you already
fixed. ROOT CAUSE: the ARRAY-level `DailyGeneration` write also had TWO SELECT-then-INSERT blocks —
the today-row (from `site.energy_today_kwh`) AND the `site.daily` history-backfill loop. SMA's daily
series INCLUDES today, so on a re-add both targeted `(array_id, today)` and collided at commit. FIX:
identical dedup-map upsert (merge today + site.daily, max-wins, load-once via `day.in_()`, update-or-
insert). LESSON: when you fix this class in one write, GREP THE WHOLE ENDPOINT for EVERY other
`SELECT(...).scalar_one_or_none(); if None: db.add(...)` on a unique-keyed table and fix them all in
one pass — there were THREE in inverter_capture (InverterDaily today-row, InverterDaily history,
DailyGeneration today-row+history) and fixing them one Sentry-alert-at-a-time made Ford hit the same
"thought we fixed this" 500 twice. As of Jun 2026 the capture path has NO remaining per-row
SELECT-then-INSERT. Regression test must put TODAY in BOTH `energy_today_kwh` and `site.daily` then
POST twice; verified it FAILS on old code (stash the fix, run, see the UNIQUE violation) before
trusting it. Proven on live prod via in-process double-call + cleanup.

### 6i-ter. "Bill feels way too high" = ONE corrupt daily kWh row, not pricing (Jun 2026)
Bruce's Array Operator bill came out ~$4k/mo; Ford said "feels too high." It was NOT the price —
AO bills per-kWh metered (`jobs/usage_report.py`: quantity = Σ DailyGeneration.kwh over the Stripe
period, reported via `create_usage_record action="set"`). The bill is only as good as the kWh data.
ROOT CAUSE: ONE corrupt `DailyGeneration` row — a Fronius capture glitch wrote a cumulative/lifetime
677,533 kWh into a single DAILY slot for the 144 kW "west chester" array (~34× physical max). That
one row was 94% of the tenant's 730,754 kWh total. Clean total was ~54,100 kWh → ~$253/mo. DEBUG
RECIPE: `tenant_period_kwh(db, tid, since)` for the headline number, then GROUP BY array, then dump
the array's daily rows — the garbage row sticks out by orders of magnitude. The Stripe per-kWh price
is TIERED/graduated (`billing_scheme=tiered`, tiers in `unit_amount_decimal` CENTS: 0.5→$0.005,
0.45, 0.4) so `unit_amount` reads None — retrieve with `expand=["tiers"]` and walk tiers to compute
a real bill; don't trust a flat unit_amount. FIX (shipped): a physical-plausibility guard at ingest
in `inverter_capture` — a daily kWh value can never exceed `peak_kw × 24h` (peak from
`site.peak_power_kw` or summed inverter nameplates); DROP anything above it with a loud log, never
persist to DailyGeneration. Ceiling is ~4-5× a real sunny day so it only catches unit-error/
cumulative junk. ALSO corrected the existing bad prod row (→ median of sane days). UNIT-ECONOMICS
NOTE for Ford: at $0.005/kWh against his ~$0.05/kWh margin (20¢ revenue − 15¢ cost), AO takes ~10%
of the owner's margin — surface this "size of the bite" framing whenever pricing comes up.
SWEEP SCOPE PITFALL: the same glitch corrupts BOTH tables AND lands on EVERY tenant whose array
the bad capture touched. The 6/14 Fronius glitch poisoned (a) the array-level DailyGeneration row
AND (b) all 19 per-inverter InverterDaily rows, AND it existed on BOTH Ford's AND Bruce's
"west chester" arrays (same capture, two tenants). When sweeping, iterate ALL tenants/arrays/
inverters with `nameplate_kw>0`, check BOTH DailyGeneration (cap = Σ inverter nameplates × 24h) AND
InverterDaily (cap = inverter nameplate × 24h), correct to the median sane day, and RE-VERIFY both
tables read 0 implausible rows after — a tenant-scoped fix leaves the other tenant's copy behind.
The ingest guard now lives in BOTH write paths (array-level via site.peak_power_kw/summed nameplate,
per-inverter via ci.nameplate_kw) so new captures can't reintroduce it.

## 6h. The Sentry-autofix cron can hijack the working tree (git-churn trap, Jun 2026)
While shipping, found the repo sitting on the hourly autofix cron's branch
(`autofix/sentry-python-fastapi-2-…`) with an UNCOMMITTED edit to a tracked file. Symptoms that
wasted two retries: a manifest version bump kept "reverting" (the branch checkout reset it) and
`git push` reported "Everything up-to-date" while `origin/main` was actually behind. DIAGNOSIS:
`git branch --show-current` (are you even on main?), `git status -sb` (note the branch line +
uncommitted tracked files you didn't touch), `git log --oneline -1 origin/main` (is the remote
really current?). FIX: `git fetch` then push explicitly with `git push origin HEAD:main`. Leave the
autofix's uncommitted WIP ALONE (it's the bot's domain) — and note it's NOT on deployed main until
its own PR lands, so it won't ship with your work. Verify your code IS live with
`git show origin/main:<file> | grep <your-marker>` rather than trusting a "pushed" message.

SHARPER VARIANT (Jun 2026, the dead-endpoint prune): the cron doesn't only leave WIP — it can
COMMIT AND AUTO-PUSH the entire working tree mid-task, sweeping YOUR uncommitted edit into ITS
commit under ITS message. Pruning a dead endpoint, I made my edit, then `git diff` came back EMPTY
and `git status` reported the file clean while the change was still on disk — contradictory until I
saw the cron had fired, committed everything (my prune + another agent's in-flight capture-auth edit
together) as a single commit titled "fix(multi-user): capture works for paused-no-card tenants…",
and pushed to origin/main. Nothing was lost and both edits were intact, but the prune is now buried
under an unrelated message. DEFENSIVE WORKFLOW when you must edit a file in this repo that ALREADY
has another agent's uncommitted change in it: (1) `git diff <file> > /tmp/inflight.patch` and
`cp <file> /tmp/<file>.bak` as a safety net BEFORE you touch it; (2) make your edit surgically
(truncate/patch the exact dead region, leave their hunks alone); (3) don't assume your commit will
be yours — after editing, re-check `git log --oneline -1` + `git show HEAD:<file> | grep <marker>`
to find where your change actually landed (it may already be in a cron commit). The atomic-edit +
backup means even if the cron commits at the worst moment, you can confirm both changes survived.
When VERIFYING a prune specifically: confirm the dead route is gone AND the live route remains in
the COMMITTED+pushed HEAD (`git show HEAD:<file> | grep -c <route>`), not just on disk — the cron's
commit is what Railway deploys.
Shipping fix after fix (v1.9.3 enumeration → v1.9.4 SW proxy → …) WITHOUT a live browser round-trip.
Every layer verified against a HAR can still fail end-to-end in the browser. **After ONE failed live
test, STOP shipping blind fixes** — ship a DIAGNOSTIC build (loud logs + visible error path) and get
the console output / screenshot BEFORE the next code change. The user explicitly called this out
("still didn't work" x3); the screenshot (status=401) is what finally isolated it in one shot.

## 7. SELF-DIAGNOSING gate logs + the daylight units/freshness verification (Fronius, Jun 2026)
The Fronius live-power verification ("send me the per-inverter devwork line") FAILED with a console
showing only the portal's OWN warnings (Google Maps, apple-meta) and ZERO `[solar-operator/fronius]`
lines. The capture silently `return`ed at one of its gates and gave no signal which one. The fix is
a reusable pattern + it settled two real bugs:

- **Make every silent gate LOUD (the self-narrating tick loop).** The content-script poll
  (`solarweb_content.js tick()`) used bare `if(!(await hasIntent())) return;` /
  `if(!(await isSignedIn())) return;` / `catch(_){return;}` — three silent exits. Replaced with a
  LOG at each: a load banner `content script loaded vX.Y.Z on <host>` (ABSENCE = extension not
  injected on this tab — the #1 cause of a blank console), then per tick `intent: yes/NO`,
  `signed in: yes/NO`, `captured systems: N — inverters: N`, a logged capture-flow error (was
  swallowed), and a `✓ capture complete` confirmation. Now the console TELLS the user exactly where
  it stopped instead of nothing. GENERAL RULE for any armed/gated capture: every early-return gate
  gets a one-line LOG of why, and the load line carries the version so the user can confirm the build.
  This is how the user self-diagnosed in ONE round-trip (\"intent: NO\" for 5 ticks until they clicked
  Connect, then \"intent: yes → signed in: yes → per-inverter line\").

- **The devwork series is WATTS, not kW (the unit bug the verification was FOR).** The single live
  daytime line settled it: a `Primo 12.5` inverter (12.5 kW rated) read ~**1699**. As kW that's an
  impossible 1699 kW (135× rating); as **watts** it's 1.7 kW — correct partial-sun output. The code
  had assumed kW. FIX: normalize the whole devwork series watts→kW ONCE
  (`kwData = data.map(p => [p[0], p[1]/1000])`) so `integrateKwh` (→daily kWh), peak, and the live
  point are all kW; the downstream `current_power_w = lp.kw*1000` then yields correct watts. LESSON
  (the capture-grounding rule again): a plausible daily-kWh landing in prod did NOT prove the units —
  only one real reading against a KNOWN nameplate did. When you can't unit-test against a fixture,
  the cheapest proof is one live value next to the device's rated capacity.

- **The freshness window was tighter than the source's cadence (silent second bug).** Even after the
  units fix, the captured points were **34–35 min old** at midday, but `LIVE_FRESH_MS` was **30 min**
  → `current_power_w` nulled → card STILL shows "no live feed." Solar.web's devwork chart only
  updates ~every 30 min, so a 30-min window routinely rejects legitimately-recent readings. Widened
  to **60 min**: recent enough to mean "now," loose enough for the source's real granularity. RULE:
  a freshness gate must be looser than the SOURCE's update cadence, or it rejects the freshest data
  the source can give — and the symptom (blank card) is indistinguishable from "no data." When a
  diagnostic prints an age, compare it to the source's known cadence before trusting the gate.

The combined fix shipped as one extension version bump (manifest + load-line version string in lock-
step) via `scripts/build_extension_zip.sh` — see cut-extension-release-and-email.md for the
build/verify/deliver loop (the zip lands on Ford's C: Desktop; ALWAYS unzip + grep the built bundle
to confirm the fix is actually in it before telling him to load it — never trust the build blindly).
