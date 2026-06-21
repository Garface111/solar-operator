# Live inverter feed debugging (Fronius / SMA / Chint "whack-a-mole")

The extension-scraped live inverter feeds break ONE vendor / ONE array / ONE
account at a time, and Ford experiences it as recurring whack-a-mole. This is the
methodology to localize the real cause FAST and the systemic patterns to stop
chasing symptoms. Discover refs by LISTING references/ (SKILL.md is >100k).

## 0. ARCHITECTURE TRUTH — why these feeds are fragile by construction
- Fronius (solarweb_content.js), SMA (sunnyportal_content.js), Chint
  (chint_content.js) are **EXTENSION-SCRAPED with NO server-side poller**.
  Confirm per-vendor: `railway ssh` → `select(InverterConnection).where(vendor==X)`
  → **0 rows** means the server poller (api/poller.py) CANNOT refresh that vendor
  (it requires pullable creds: api_key+site_id, or client_id+client_secret/
  refresh_token). Only SolarEdge has those. So Fronius/SMA/Chint live data exists
  ONLY because the extension captured it — browser-session-dependent, fragile.
- The extension's silent hourly recapture (background.js RECAP_VENDORS,
  chrome.alarms, RECAP_PERIOD_MIN=60) re-opens each vendor portal in a background
  tab and re-runs the content script — but only if the owner's portal SESSION is
  still alive. Expired session → recap fails → card freezes → 1 nudge/day.
- The array card's "kW now" is **summed CLIENT-SIDE in sandbox.js** from each
  inverter's `current_power_w`. The fleet-tree column does NOT carry an
  array-level `current_power_w` (verified: it's never set in build_fleet_tree).
  So a null/zero array reading can be a FRONTEND aggregation/cache issue, not a
  data issue.

## 1. GROUND IT IN PROD DATA BEFORE TOUCHING CODE (Ford's #1 expectation)
NEVER guess from the symptom. Write a read-only `railway ssh ... python -c` probe
that prints, per array: inverter count, `last_power_w`, `last_power_at`,
`inverter_source`. The shape of the data tells you the layer:
- Fresh `last_power_at` (today) but `last_power_w=0` on ALL inverters of ONE
  array, while a SIBLING array on the SAME vendor/account reads real power in the
  SAME capture run → that array's **per-inverter live feed is empty** (vendor
  served null per-device power, or per-device name-matching failed). NOT a global
  vendor outage.
- Stale `last_power_at` (hours/days old) across a whole vendor → capture not
  running (session expired, or the content-script filter rejects everything — see
  the SMA pvPower-null-filter bug in the SKILL history / git log).
- DB has live values but the user sees none → FRONTEND (stale SPA shell cache, or
  client-side render threshold), or WRONG TENANT (next section).

## 2. THE MULTI-TENANT TRAP (Ford has many near-dup tenants per email)
"Works on my account, broken on Dad's" for the SAME array name is almost always
a tenant/rendering split, NOT the vendor. Bruce/Ford each have MULTIPLE tenants
under one email AND variant emails (bruce.genereaux@ / bruce.genereaux1@ /
bruce.genereaux2@ / bruce.genereau1x@), spanning BOTH products (nepool +
array_operator). Each tenant has its OWN copy of "Waterford", "west chester" etc.
- nepool tenants hold BILLS, **0 inverters** — their Waterford will NEVER show
  live inverter data (by design). Only the array_operator tenant has the feed.
- Variant-email AO tenants ("SolarEdge owner", "SMA owner") each have a Waterford
  that may have NEVER captured — dead by neglect, not bug.
- Resolve which tenant the login lands on: `issue_magic_link`/`password_login`
  (api/account.py) pick among same-product tenants by
  `ORDER BY active DESC, created_at DESC`. Check
  `select(Tenant).where(contact_email==EXACT, product=="array_operator")` — if
  it's ONE tenant, login is deterministic; confirm THAT tenant's fleet-tree.
- VERIFY THE ACTUAL ENDPOINT: call `inverter_fleet.build_fleet_tree(db, tenant)`
  in-process for the resolved tenant and check the Waterford column's inverters'
  `current_power_w`. If the endpoint serves live data, the bug is client-side
  (cache/threshold), and per Ford's rules you canNOT probe prod HTTP without
  per-command approval — say so honestly rather than claiming "verified live".

## 3. THE DUSK / LOW-OUTPUT FALSE-NEGATIVE
`_live_power_w` (inverter_fleet.py) trusts a fresh telemetry value as-is but
daylight-gates the STORED-capture fallback. Near sundown inverters legitimately
read ~3 W; 10×3 W = 30 W summed. If the card hides "kW now" under a tiny
threshold it reads as "no live data" at dusk but fine at midday — which exactly
explains "works on my account" if the two were viewed at different times. ALWAYS
ask the user WHAT they see (blank / "0 kW" / "no live feed" / card missing) and
WHAT TIME — that single answer separates cache-bug from dusk-threshold.

## 4. KILL THE MOLE-CLASS, not just the mole (what Ford actually wants)
When one vendor/array breaks, Ford ALSO wants the systemic fix so it stops
recurring. The durable pattern: a bare `0`/blank stamped fresh is the mole —
indistinguishable from "app broken". Fix = when per-inverter live is missing/
stale BUT the array produced energy today (DailyGeneration has today's kWh),
render an honest "producing today · live feed updating" instead of a misleading
zero (the §11 source-status honesty pattern in ao-deploy-and-frontend-debugging
.md). Best done by computing an array-level `current_power_w` SERVER-SIDE (sum of
live inverters) so the card stops depending on client aggregation + stale
bundles, AND degrades gracefully for every vendor at once. This is a card
BEHAVIOR change → get Ford's go before shipping, but it's the real answer to
"we're playing whack-a-mole."

## 5. PER-VENDOR KNOWN FAILURE MODES (each surfaced the mole differently)
- SMA: `/overview/{plant}/devices` DRIFTED → per-device `pvPower=null`; a filter
  requiring non-null pvPower dropped EVERY inverter → whole capture failed →
  cards froze. Fix: keep any Device with power OR today's energy OR an id; live
  power comes from the site-level measurements/gauge call, allocated by the
  backend (same as Fronius). (Shipped v1.9.48.)
- Fronius: per-inverter live power keyed by devwork-chart DISPLAY NAME
  (`lastByName[displayName]`). 20 same-model "Primo" inverters on one system can
  collide / not bind → that array's per-inverter live all null while a
  sibling array binds fine. LIVE_FRESH_MS=60min (devwork cadence ~30min). Array
  still logs site-level kWh, so it "produced" but shows 0 live per inverter.
- VEC/SmartHub generation: separate pipeline (smarthub-vec ref) — bills carry
  consumption not generation; client-side daily pull is the only path.

## 5b. SHARED PHYSICAL SYSTEM captured into TWO tenants → one reads ~0 (Jun'26, the REAL Bruce-Waterford cause)
"Works on my account, broken on Dad's" for the SAME array can ALSO be: it's the
SAME physical system (identical `source_site_id` AND identical inverter
`serial`s) captured into BOTH tenants by two different browsers/portal logins,
and ONE browser pulls a bogus near-zero live wattage while the other reads the
truth. Confirmed: Ford's & Bruce's Waterford = same Fronius site
`6c97d4a9-…`, all 12 device GUIDs identical; Ford captured 2257 W/inverter,
Bruce captured 3 W/inverter in the same window (a shared/guest Solar.web feed
serves a delayed/reduced live value). NEITHER tenant was demo. This is NOT our
bug and NOT fixable by the §3/§4 display patches (those only make the bogus 0
honest, they don't recover the real kW).
DIAGNOSE: compare the two tenants' inverter `serial`→`last_power_w` maps for the
same array name. If serials match 1:1 but wattages diverge wildly (one ~0, one
real), it's this. PROVE same physical system via `source_site_id`.
FIX SHIPPED (`_cross_tenant_live_by_serial` + `_live_power_w(borrow=)` in
inverter_fleet.py, build_fleet_tree computes the map once): borrow the BEST fresh
reading per `(vendor, serial)` across ALL tenants. STRICT guardrails so it can
only CORRECT, never fabricate: upward-only (never drags a good local reading
down), serial-exact (same physical device, can't cross panels), freshness-gated
(`_POWER_FRESH`), positive-only, daylight-gated (at night everyone reads ~0 so
nothing to borrow). Verified: Bruce Waterford 36 W→27085 W; Ford Waterford
unchanged; a system NO tenant captured well (Ford West Chester) stays honestly 0
— borrow invents nothing. Tests: tests/test_cross_tenant_live_borrow.py (6).
META-LESSON (I missed 3x before landing this): for "works for me / broken for
Dad" on live feeds, do NOT stop at tenant-resolution or display logic — pull the
ACTUAL captured `last_power_w` for BOTH tenants of the SAME array and compare. If
the data itself diverges for identical serials, it's a capture-source problem
(borrow), not a render/tenant problem. Ask for the failing browser's
`[EnergyAgent] per-inverter last devwork point` console line EARLY rather than
iterating builds blind (cost me 3 wrong guesses; cost VEC 4 builds).

## 5c. CARD SELF-CONTRADICTS: "SOURCE OFFLINE 8h ago" + "PRODUCING 596W / All good" (Jun'26, SolarEdge)
A SolarEdge array (Cover Catamount) showed the amber SOURCE-OFFLINE banner AND a
live "PRODUCING / OUTPUT NOW 10% / 0.6kW" at the SAME time. Root cause: SolarEdge
`/overview` returns `currentPower` AND `lastUpdateTime` in ONE response; when the
site stops reporting, currentPower FREEZES at its last value while lastUpdateTime
ages. `_live_power_w` trusted the vendor `m["last_power_w"]` UNCONDITIONALLY as
"the real instant" (only the stored-capture fallback was daylight-gated), so the
frozen 596W read as live while `_source_status` correctly flagged stale. NOT the
borrow logic (single-tenant, unique serial), NOT a frontend bug — a backend
honesty gap. This is the SolarEdge-laggy case from MEMORY ("old lastUpdateTime+0
= SOURCE outage").
FIX SHIPPED (`_report_is_stale` + gate in `_live_power_w`): drop the vendor live
value when its OWN `last_report` is older than `_SOURCE_STALE_HOURS` (the SAME
threshold `_source_status` uses → the live number and the SOURCE-OFFLINE banner
can NEVER disagree). A missing/unparseable timestamp is NOT treated as stale
(only suppress when provably old, so a vendor that omits last_report keeps its
value). Verified: Cover Catamount cpw 596→None, src stays stale; all FRESH
SolarEdge/Fronius arrays unchanged (Starlake 15.5kW src=ok, etc.). Tests in
test_cross_tenant_live_borrow.py (stale dropped / fresh kept / missing-not-
suppressed / helper). RULE: any vendor live value carried WITH a timestamp must
be freshness-gated against the same window that drives the source-status banner —
a "live" number and a "source offline" banner on the same card is always a bug.

## 6. SHIPPING (recurring)
Extension fix → bump manifest, `bash scripts/build_extension_zip.sh`, VERIFY the
fix is IN the zip (unzip+grep), `gh release create ext-vX` + curl the asset 200,
email Bruce via the v<ver>_link.py pattern (see ref ao-deploy §12 + the
extension-distribution notes). Backend fix → push origin HEAD:main (Railway
auto-deploy ~80s) + verify route 401 not 404/500. AO frontend → manual
`scripts/netlify_api_deploy.py` (CLI session is dead). Ford has near-dup tenants
— always diagnose the EXACT product tenant the user logs into.
