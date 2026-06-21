# Capture-vendor staleness, card "wrong data", and billing data integrity (Jun 2026)

Hard-won during a live demo. Three intertwined lessons: how capture-only vendor
cards go stale, how to tell a DATA bug from a CODE bug, and how a single bad kWh
row poisons per-kWh billing.

## 1. "Cards don't match the vendor site" — DATA freshness, usually NOT a code bug
SolarEdge is pulled LIVE server-side every page load (API key) → always current.
Fronius + SMA are CAPTURE-ONLY: the card is only as fresh as the owner's last
"Log in with <vendor>" capture. They CANNOT self-refresh. So a Fronius/SMA card
showing 0 / "not producing" while the vendor's own site shows live watts is
almost always STALE DATA (the last capture was hours ago), not a bug.

DIAGNOSTIC ORDER (do this before touching code):
1. Ask / check: does the VENDOR site show live watts *right now*, or just today's
   history/totals? (The owner is logged in live there; we only see the last capture.)
2. Check the stored reading + its age on prod:
   `Inverter.last_power_w` + `last_power_at`; `(now() - last_power_at)`.
   If `last_power_at` is hours old → stale, the fix is a RE-CAPTURE, not code.
   If the stored `last_power_w` is itself 0 from an EVENING capture → it's just
   night; panels really are ~0. "Not producing" is then CORRECT but reads alarming.
3. Only if a FRESH capture still shows 0 while the site shows live watts is it a
   real bug — then inspect the backend allocation (below).

THE FRESHNESS GATE: `inverter_fleet._POWER_FRESH`. A capture-time power is shown
on the card only while younger than this; older → card blanks to "—"/"not
producing". Originally 3h, which made healthy Fronius/SMA fleets look DEAD just
hours after each capture (esp. evenings). WIDENED to 24h (Jun 2026) so the last
real DAYTIME reading stays visible until the next capture. SolarEdge unaffected
(its `m["last_power_w"]` is live so the gate never applies). `_live_power_w()`
prefers the live pull, falls back to the stamped capture power while fresh.

THE REAL FIX (post-demo): a SCHEDULED SERVER-SIDE Fronius/SMA pull like
SolarEdge has, so cards stay live without manual re-capture — needs the vendor
API credentials. Until then, "re-capture to refresh" is the honest answer and the
24h window keeps cards from looking dead between captures.

## 2. Backend live-power allocation (Fronius/SMA) is correct — don't chase it blind
`array_owners.inverter_capture`: Fronius/SMA portals expose live power SITE-WIDE
only (not per inverter). The backend splits `site.current_power_w` across the
array's inverters by each inverter's share of TODAY's energy (`_energy_sum`),
falling back to nameplate share, then even split. Chint is the exception — its
`commDevice.currentPower` is per-inverter and is PREFERRED over any split. So a
fresh capture WITH a real `site.current_power_w` will light the cards; if every
card is 0 after a fresh daytime capture, suspect the capture sent 0/None for site
power, not the allocation math.

VENDOR ASYMMETRY (confirmed Jun 2026, the "dead cards below Londonderry" report):
at the SAME instant Chint cards read "PRODUCING" while Fronius cards right below
read a dead "IDLE / not producing right now." Prod data showed why — per-inverter
`last_power_w` populated: Chint 16/16, SMA 42/42, Fronius only 132/225 (rest null
or 0.0). ROOT CAUSE: Fronius's Solar.web exposes ONLY one site-wide `TotalPower`
(solarweb_content.js maps `lv.TotalPower`), never per-inverter; the energy-share
split frequently yields null/0 per inverter. Chint's `chint_inject.js` hooks the
app XHR for REAL per-inverter power → always lit. So Fronius cards looking dead
next to Chint is this asymmetry, not a regression.

RESOLVED (v1.9.43, commit fbcb641): Fronius DOES have per-inverter power — we
just weren't reading it. The per-device `/Chart/GetAnalysisChart?channels=devwork`
response we ALREADY fetch for daily-kWh carries each inverter's full power CURVE.
solarweb_content.js captureInverters() now extracts the LATEST point of each
device's curve as `current_power_w` (kW → ×1000 W), gated by a 30-min freshness
guard (LIVE_FRESH_MS) so a stale daylight tail / night value never reads as
"now" — stale → null → card shows "produced today · no live feed". Backend
ingest already PREFERS per-inverter current_power_w (array_owners.py:2062), so no
backend change. A loud LOG() prints each device's last point (kW + age_min) so a
single DAYLIGHT capture verifies units + freshness in one console line.
DAYLIGHT VERIFICATION RUNBOOK (do in sun, ext v1.9.43+ loaded): (1) DevTools
console open on www.solarweb.com; (2) AO "Connect Fronius" to set intent; (3)
grep console for `[solar-operator/fronius] per-inverter last devwork point` — each
device should read e.g. `Primo 7.6=3.2kW/4m` (kW value, age in min). CONFIRM:
values are kW not W (single digits, not thousands) and age < 30m midday. (4) After
capture, hit /v1/array-owners/fleet-tree (or the card) — Fronius inverters should
now show real current_power_w like Chint. If the console shows W not kW, change
the *1000 to /1 in captureInverters; if ages are all >30m at midday the portal's
last point lags, widen LIVE_FRESH_MS. Logic unit-tested 6/6 in
/tmp/test_fronius_livepower.js (units + freshness + unsorted + empty).

### SMA cards FROZEN for days — portal endpoint DRIFT broke the capture filter (v1.9.48, Jun'26)
Ford: "why is SMA data not updating the inverter cards live?" Live prod tell:
42 SMA inverters' `last_power_at` was 1-3 DAYS old (06-17/06-19, checked 06-20)
while SolarEdge stayed current. ROOT CAUSE: SMA's `/api/v1/overview/{plant}/devices`
endpoint DRIFTED — per-device `pvPower` is now `null` (live power moved to the
site-level `widgets/gauge/power` + `measurements/search` calls). But the device
filter in `extension/sunnyportal_content.js` `captureOnePlant()` still REQUIRED
non-null pvPower:
`.filter((d) => d && d.componentType === "Device" && d.pvPower !== null && d.pvPower !== undefined)`
→ EVERY inverter dropped → `inverters.length===0` → `captureOnePlant` returns null
→ `captureFlow` throws "no producing inverters" → the WHOLE SMA capture FAILS and
NOTHING updates. Cards froze at the last snapshot that happened to carry pvPower.
The downstream code (`fetchSiteLivePowerW` + null-power handling) was ALREADY built
for null per-device power — the filter was the lone contradiction killing capture.
FIX: keep any real inverter Device (live power OR today's energy OR an id), leave
`current_power_w` null when SMA omits it:
`if (!d || d.componentType !== "Device") return false; return (typeof d.pvPower==="number") || (typeof d.totWhOutToday==="number") || (d.serial!=null || d.componentId!=null);`
NO backend change: `_persist_meter_accounts`/`inverter_capture` already allocate
the SITE-level live power (from the gauge call) across inverters by energy/nameplate
share when per-inverter `current_power_w` is null (array_owners.py:~2589-2600) —
exactly how Fronius works. Once capture succeeds, the silent hourly recapture
(see below) refreshes each card.
LESSON (bug class): a portal endpoint that "drifted to null" on ONE field can
fail the WHOLE capture if a filter still requires that field. When ANY single
capture-vendor's cards freeze (vs all-three at once = a shared path, §10),
suspect a per-vendor portal-shape drift in that vendor's content script's
device/site filter — and check that the filter's required field hasn't gone null.

### The SILENT HOURLY RECAPTURE is the live-refresh mechanism (extension/background.js)
§1's old "the REAL FIX is a scheduled SERVER-SIDE pull" is WRONG for SMA (and any
extension-only vendor without pullable creds — see next paragraph). The real
live-refresh that EXISTS is the in-extension silent recapture IIFE: a
`chrome.alarms` cycle (`RECAP_PERIOD_MIN = 60`, hourly while Chrome runs) that, for
each vendor in `RECAP_VENDORS {fronius, sma, chint}`, arms `so_capture_intent` +
opens the portal in a BACKGROUND tab (`recaptureVendor`), lets the existing
content script capture, and POSTs the result via `recapPost → /v1/array-owners/
inverter-capture` with the stored tenant_key. The `*_CAPTURED` messages are hooked
(line ~1293) so the capture lands with NO AO page open. A 2.5-min watchdog
(`TAB_BUDGET_MS`) closes the tab + fires ONE gentle reconnect nudge/vendor/day if
the session expired. So once the owner is on a fixed build, cards self-refresh ~hourly
WITHOUT a manual reconnect — UNLESS the portal session expired (then the recapture
tab can't auth → nudge). When SMA/Fronius/Chint cards are stale, the two causes are
(a) a capture bug like the filter-drift above (breaks even WITH a valid session), or
(b) an expired portal session (recapture can't auth). Fix (a) in code; (b) is a
re-login, surfaced by the nudge.

### Server-side poller CANNOT poll SMA without a developer-app registration
`poller._pullable_connection` requires `client_id`+`client_secret` OR a
`refresh_token` — i.e. an SMA developer-app OAuth registration. Live check this
session: `select(InverterConnection).where(vendor=="sma")` returned **0 connections**
(Ford's SMA arrays have inverters but no pullable InverterConnection creds). So the
server-side poller (poller.py) silently SKIPS every SMA array — SolarEdge stays live
because it has an api_key+site_id, SMA has neither. `api/inverters/sma.py` is the
Monitoring-API adapter but is explicitly UNVERIFIED and registration-gated. Until
someone registers an SMA developer app, the EXTENSION (silent recapture above) is
SMA's ONLY live source — do not chase a server-side SMA fix expecting it to work.
Same logic for Fronius/Chint (extension-only). DIAGNOSE which world a vendor is in
FIRST: count its pullable InverterConnections before assuming the poller helps it.

## 3. "Bill feels way too high" = ONE corrupt daily kWh row, not pricing
AO bills per-kWh metered (`jobs/usage_report.py`: Σ DailyGeneration.kwh over the
Stripe period → `create_usage_record action="set"`). The bill is only as good as
the kWh data. A Fronius capture glitch once wrote a cumulative/lifetime value
(677,533 kWh) into a single DAILY slot for a 144 kW array (~34× physical max).
That one row was 94% of the tenant total → a ~$4k phantom bill. Clean total was
~54,100 kWh → ~$253/mo.
DEBUG RECIPE: `tenant_period_kwh(db, tid, since)` for the headline, then GROUP BY
array, then dump the array's daily rows — garbage sticks out by orders of magnitude.
STRIPE PRICE IS TIERED (`billing_scheme=tiered`, tiers in `unit_amount_decimal`
CENTS: 0.5→$0.005, 0.45, 0.4) so `unit_amount` reads None — retrieve with
`expand=["tiers"]` and walk tiers; don't trust a flat unit_amount.

## 4. Plausibility guard (shipped) — a daily kWh can't exceed nameplate × 24h
Guard now lives in BOTH ingest write paths in `inverter_capture`:
 - array-level (DailyGeneration): ceiling from `site.peak_power_kw` or summed
   inverter nameplates.
 - per-inverter (InverterDaily): ceiling from `ci.nameplate_kw` (fallback to the
   persisted inverter's nameplate).
Anything above the ceiling is DROPPED with a loud `log.warning`, never persisted.
Ceiling (24h @ full nameplate) is ~4-5× a real sunny day, so it only catches
unit-error/cumulative junk, never a strong production day.

## 5. SWEEP scope pitfall — the glitch hits BOTH tables AND every tenant
The same bad capture corrupted (a) array-level DailyGeneration AND (b) all 19
per-inverter InverterDaily rows, AND landed on BOTH Ford's AND Bruce's
"west chester" arrays (same capture, two tenants). A tenant-scoped cleanup leaves
the other tenant's copy behind — the post-deploy verify caught one I'd missed.
SWEEP RECIPE: iterate ALL tenants/arrays/inverters with nameplate>0; check BOTH
DailyGeneration (cap = Σ inverter nameplates × 24h) and InverterDaily (cap =
inverter nameplate × 24h); correct to the inverter/array median sane day; then
RE-VERIFY both tables read 0 implausible rows.

## 6. The watchdog (shipped): `jobs/generation_watchdog.py`
Daily 03:45 UTC (after the 03:30 snapshot, BEFORE the 04:00 usage report so a bad
row is caught before it bills). `scan_implausible_generation()` returns
{daily, inverter, ok}; `run_generation_watchdog()` alerts via send_internal_alert
with the offending rows, SILENT when clean. Read-only — alerts, never mutates
(correction stays a deliberate human-run sweep so owner data is never silently
rewritten). Registered in `scheduler.start()` as id="generation_watchdog".

## 8. Manual customer-input billing path (shipped Jun 2026) — no xlsx required
Original gap: a billing subscription could ONLY be created by uploading an .xlsx
(POST /v1/array-operator/billing/subscriptions did `if file is None: raise 400`).
Paul needed to TYPE IN a customer. Fix shape: `file` made optional; a no-file POST
takes customer_name + array_id + allocation_pct (0..1), creates the Client +
BillingReportSubscription with source_workbook=None. Two nullable columns added
idempotently (the migrate.py `column_exists` pattern): allocation_pct: float,
array_id FK. Delivery for a manual sub computes customer share =
allocation_pct × array period generation (DailyGeneration, fallback Bill.kwh_generated).
The xlsx path is unchanged. Frontend: an Add-customer card in ReportsTab.tsx
(web/app/src/components/reports/AddCustomerCard.tsx). REMEMBER after any TS change:
`cd web/app && npm run build` so the served bundle + dist/index.html stay consistent
(a subagent left them mismatched once). PROD GOTCHA: the two new columns need
`railway ssh "cd /app && python -m api.migrate"` AFTER deploy or the form 500s on prod.
NEVER seed a real user's (Paul's) live tenant with INVENTED customer names/splits —
only Ford has the real percentages; offer a clearly-LABELED sample on Ford's own
account instead (deletion-safety / no-fabrication-into-prod).

## 9. Morning fleet-health digest (shipped Jun 2026) — tenant-facing-email job pattern
api/jobs/morning_fleet_digest.py mirrors the watchdog job shape (§6) but sends a
TENANT-FACING email instead of an internal alert. build_digest_html(tenant, tree)
renders self-contained inline-CSS HTML from inverter_fleet.build_fleet_tree(db, tenant)
(KPIs, top/lowest producer, amber/red attention callouts, green all-healthy banner).
HONESTY: returns None (→ dash / no-recent-data / asleep), never an invented kWh, when
an array has no recent reading. run_morning_digest() iterates active product=='array_operator'
tenants with per-tenant try/except; registered in scheduler.py via CronTrigger(hour=12)
(~8am ET), id=morning_fleet_digest. KEY: send via branding.from_address('array_operator')
NOT a hardcoded From (see resend-email skill — branding domain status drifts stale).
PROVE a new email feature by rendering the real builder + sending one real email to Ford
via the resend-email curl path and confirming last_event: delivered, not just unit tests.

## 10. "Vendor arrays show no data" — DIAGNOSE before patching (capture vs ingest vs display)
When Ford reports CHNT/SMA/Fronius (or any capture-vendor) arrays "not showing
data," do NOT assume the data is missing and do NOT patch blind. These three are
the EXTENSION-CAPTURE vendors (no server API key, unlike SolarEdge/Locus); three
failing at once = a SHARED path, and the failure can be at THREE layers. Walk
them in order and get GROUND TRUTH at each before moving on:

  CAPTURE  → did the extension read the portal? (so_bridge.js → background.js →
             *_CAPTURED msg → page POSTs /v1/array-owners/inverter-capture)
  INGEST   → did the POST persist rows? (DailyGeneration + InverterDaily)
  DISPLAY  → does build_fleet_tree SERVE them, and does the card render them?

Last time the symptom was "no data" but the truth was: data WAS captured, stored,
AND served — the only blank field was `current_power_w` (expected: capture-only
vendors have no live feed, so live power expires after _POWER_FRESH=24h between
captures; the daily graphs still render). A real but ALREADY-SELF-HEALED
IntegrityError (see below) had caused earlier failures. The lesson: the visible
symptom misdescribed the bug — only the layered probe revealed it. So when the
DB+API both have the data, it's a RENDERING / FOCUS-FILTER / WRONG-ACCOUNT
question, NOT an ingest fix. Suspect: (a) the demo/sample tenant
(ten_demo_readonly_v1) which is intentionally stale; (b) the focus filter that
collapses HEALTHY arrays out of view (sandbox defaultFocusIds shows "worst few"
unless setFocus(all)); (c) the 24h live-power staleness reading as "idle". ASK
Ford which account + what exactly is blank before changing code.

INGEST-CRASH signature (the real bug class here): a single IntegrityError in
inverter_capture rolls back the ENTIRE payload (all sites) → that capture lands
NOTHING. Seen as `uq_array_per_tenant` (Array name collide on create) and
`uq_daily_array_day` (DailyGeneration dup at db.commit). Both are the
"upsert/match-including-soft-deleted" bugs the fix commits address; before
re-fixing, CHECK whether the errors stopped AFTER the fix deployed (compare last
Sentry event time vs the fix commit's `git show -s --format=%ci`). If they
stopped, the crash is fixed and the leftover symptom is failed-captures needing
RE-CAPTURE, not more code.

DISPLAY-LAYER FIX (shipped Jun 2026, array-operator/public/sandbox.js): a
capture-vendor inverter with NO live feed (`current_power_w == null`, the Fronius
case) was rendered IDENTICALLY to a genuinely-idle one ("not producing right
now") — false, since it produced energy today. Added `liveReadingMissing(inv)` (==
`current_power_w == null`) + `todayKwh(inv)` (last daily point IF its date ==
today). The outputBar not-reporting branch now: null-feed + produced-today →
"X.X kWh · produced today · no live feed" + a calm liquid pool (~45%); null-feed +
nothing-today → "no live feed from this inverter"; a REAL numeric 0 (SolarEdge/
SMA/Chint at night) → UNCHANGED "not producing right now". RULE: gate every such
fix strictly on `current_power_w == null` so API-key/per-inverter vendors are
never touched. This is the PAINT fix; the true cure (per-inverter Fronius live
power) needs an EXTENSION change grounded on a DAYLIGHT Solar.web capture + live
console — a 4-build trap if done blind at night, scope it for daytime.
LOCAL-QA NOTE: the AO signed-in canvas falls back to a 100-array DEMO fleet, so a
freshly-seeded single array may not appear. To prove a pure card-render fix,
EXTRACT the changed pure functions (they live in an IIFE, not exported) into a
tiny `node` test asserting ALL branches incl. the "unchanged for live-0"
guarantee — faster + more rigorous than fighting the demo-fleet gate.

## 11. Diagnosis tooling that works here (Sentry + prod DB + fleet-tree probe)
- SENTRY (read token at ~/.hermes/secrets/sentry_auth_token): org
  `dyson-swarm-technologies`, project `python-fastapi`. The masker MANGLES inline
  curl with the bearer → put the whole probe in a PYTHON SCRIPT FILE
  (urllib + `req.add_header("Authorization","Bearer "+open(tok).read().strip())`)
  and run it; never one-line the token into bash. Useful endpoints:
  `/organizations/{org}/projects/`, `/projects/{org}/{slug}/issues/?query=is:unresolved&statsPeriod=14d`,
  `/issues/{id}/events/latest/` (walk entries[type=exception].values[].stacktrace.frames
  for inApp frames → exact file:line + the `>> ` context line), `/issues/{id}/events/?limit=8`
  for per-event timestamps (to prove a fix stopped the bursts). Filter culprit by
  `inverter-capture` etc. See scripts/sentry_issue_probe.py.
- RUN A LOCAL DIAGNOSTIC AGAINST PROD: `railway ssh` runs the DEPLOYED image,
  which does NOT have your new local script. Pipe it in via base64 stdin:
  `B64=$(base64 -w0 scripts/mydiag.py) && railway ssh "cd /app && echo $B64 | base64 -d | python -"`.
  This dodges quoting + masker issues and runs your script with prod's DB + env.
  Read-only diagnostics only; for mutations use api.migrate / a deliberate sweep.
- GROUND-TRUTH the three layers with two scripts (both saved under scripts/):
  diag_capture_arrays.py (counts Inverter/DailyGeneration/InverterDaily rows +
  latest day per capture vendor → proves INGEST) and diag_fleet_tree.py (calls
  inverter_fleet.build_fleet_tree for a tenant, prints per-array daily series +
  current_power_w → proves DISPLAY/API). If both show data, stop coding and ask.

## 12. SolarEdge (the LIVE vendor) shows 0 at midday — SOURCE outage, not our bug (Jun 2026)
§1 says SolarEdge is "always current" because we pull it live — but live pull
returns whatever SolarEdge HAS, and a site that stopped reporting TO SolarEdge
gives back a stale 0. Symptom: Arrays tab shows 0/dead at midday; Ford asks "why
isn't it showing live data, why does this KEEP happening?"

DIAGNOSIS that nailed it (run via the base64-over-railway-ssh probe, §11):
- First rule out night: `TZ=America/New_York date` — if the sun's up, it's real.
- For each pullable array, call `array_owners._cached_fetch_live(vendor, cfg)` and
  also the RAW `adapters/solaredge.fetch_overview(api_key, site_id)`. Read THREE
  fields: `currentPower.power`, **`lastUpdateTime`**, and `lastDayData.energy`.
  If `lastUpdateTime` is HOURS/DAYS old AND today's energy is also 0, the SITE
  has gone dark at the source — SolarEdge itself has no fresh data. Confirmed
  per-site, NOT credential-wide: in one fleet 1 of 3 sites read 13.8 kW live
  (fresh `lastUpdateTime`) while 2 read 0 with `lastUpdateTime` 13h and 33h old.
- So `today_Wh` fallback is NOT a fix here — when a site is dark, daily energy is
  0 too. The only honest move is to SURFACE the staleness.

THE GOVERNOR RED HERRING (poller.py): a manual `poll_all_sources()` can report
`arrays_polled:0, arrays_throttled:N` even on a FRESH process. That's the
per-CREDENTIAL budget governor (`_governor_allows`): all N SolarEdge sites share
ONE api_key, so `_min_interval_seconds(N)` spaces each site to ~once/51min on a
16-site key (16×16h ÷ 280/day). The poll firing + throttling is BY DESIGN (300
req/day cap), not a dead scheduler. Don't "fix" the throttle — it's protecting
the budget. `inverter_readings` frozen for hours is the same story (nothing fresh
to write), while `inverter_daily` updates fine.

THE SHIPPED FIX — `source_status` (NOT a data-source change, pure transparency):
- BACKEND `inverter_fleet._source_status(inv_rows)` → `{state, last_report,
  age_hours}` where state ∈ ok|stale|none, computed from the FRESHEST inverter
  `last_report` (stale = older than `_SOURCE_STALE_HOURS=6.0`; none = no live
  feed at all). Added to each column dict in `build_fleet_tree` as `source_status`.
- FRONTEND `sandbox.js` `sourceStatusHTML(col)` renders an AMBER banner on the
  array card ONLY when `state==="stale"`: "<Vendor> stopped reporting Nh ago.
  This is a data outage at the source — not Array Operator. Live data resumes
  automatically when <Vendor> reconnects." Renders "" for ok/none. CSS class
  `.sb-srcout` in command-center.css (amber = attention, not failure-red).
- WHY this is the right fix and the "keeps happening" answer: solar sites lose
  connectivity constantly (router/ISP/gateway). A bare 0 is indistinguishable
  from "app broken," so every real-world outage READS as our bug. Naming the
  source + age turns it into the product DOING ITS JOB (catching the outage).
  This is also Array Operator's thesis surfacing — a dark site IS the finding.

## 12b. Vendor TIMEZONE bug — "GMP says out 24h, we say 19h" (Jun 2026)
A multi-hour gap between OUR outage clock and the utility's is two distinct things,
diagnose both:
- REAL BUG (fleet-wide, fixed): SolarEdge timestamps (equipment telemetry `date`,
  overview `lastUpdateTime`) are in the SITE'S LOCAL time with NO tz marker, but
  `inverter_fleet._source_status` read a naive timestamp as UTC. A Vermont site
  (America/New_York, EDT=UTC−4) thus looked ~4h MORE stale than reality
  (Londonderry `02:39` local read as `02:39Z` = 15.6h old; truth `06:39Z` = 11.6h).
  FIX in `adapters/solaredge.py`: fetch the site's IANA `timeZone` from
  `/site/{id}/details` (`location.timeZone`), CACHE it per site (`_SITE_TZ_CACHE`,
  never changes), and convert naive→tz-aware UTC via `_localize_to_utc_iso(ts, tz)`
  using `zoneinfo`. Unknown tz → leave naive (= prior behavior, safe fallback).
  The corrected age only refreshes on the NEXT poll (re-stamps last_report).
- DIFFERENT-CLOCKS (not a bug, label it): GMP times the utility METER's last
  reading; we time the INVERTER telemetry's last report — two independent feeds
  that drop/resume at different moments, so they never match exactly even with
  correct tz. The `.sb-srcout` banner now SAYS so ("this is the inverter feed;
  your utility meter, e.g. GMP, tracks separately and may show a different time")
  so the two numbers aren't mistaken for the same measurement.
- LESSON / bug class: any vendor timestamp parsed as UTC when it's actually
  site-local is a silent multi-hour error. Audit Fronius/SMA parsing for the same
  naive-as-UTC assumption before trusting their ages.

## 12c. OAuth REFRESH-TOKEN ROTATION — "SMA worked until I reconnected" (Jun 2026)
Symptom: an OAuth inverter vendor (SMA) works right after connecting, goes dark a
while later, comes back ONLY on manual reconnect, repeats. ROOT CAUSE: the vendor
ROTATES the refresh_token on every refresh grant (returns a NEW one, invalidates
the used one). The adapter discarded the new token, so the 1st refresh (~1h after
connect) worked but the 2nd reused the now-dead original → 401 → dark. Reconnect
just writes a fresh token, hence the temporary recovery. The TELL it's rotation
(not a blip / bad creds): reconnecting fixes it — reconnect only supplies a new
token. FIX (mirror `adapters/alsoenergy.py`, PLUS durable persistence the others
lack): in `inverters/sma._get_token` (a) cache the rotated refresh_token + reuse
the freshest (`new_refresh = body.get("refresh_token") or refresh_token`), (b)
write it back into the passed `config` dict in place, (c) on 401 clear the dead
token so the next call falls back to client_credentials. Then the POLLER must
PERSIST the mutated config to the DB — JSON columns don't auto-detect nested
mutation, so `poller._persist_config_if_changed(conn, cfg)` does
`conn.config = cfg; flag_modified(conn, "config")` after each fetch_live (and in
the error branch). This makes the rotated token survive access-token expiry AND a
redeploy (the in-memory cache alone does not). Adapters marked "unverified against
a live account" are logic-correct + unit-tested but only PROVEN by leaving the
live connection alone for a day and confirming it doesn't go dark — say so.

## 13. Making the Fleet-Commander top bar "juicier green" (Ford UX ask)
Ford called the healthy top bar "muted." The Fleet-Commander OK state lives in
styles.css: `.fc-card.ok`, `.fc-tank` / `.fc-tank--ok`. Muted = mid-saturation
greens at ≤0.8 opacity UNDER a frosted dark `.fc-plate`. Juicier = brighter,
higher-opacity green gradient (e.g. `rgba(34,210,110,.96)→rgba(82,245,158,.86)`),
an inner glow on the tank (`box-shadow: … inset`), and a green border + outer
glow on `.fc-card.ok`. NOTE the bar is STATUS-TINTED: ≥95%=ok(green),
≥85%=warn(amber), else bad(red) — so a 88% demo reads amber correctly; force a
≥95% (ok) state to QA the green. The screenshot Ford flagged was 95% (green).

QA-WITHOUT-REAL-DATA trick used here (the signed-in canvas falls back to a 100-
array DEMO fleet, and fetch-stubbing FleetStore is unreliable): drive Playwright
to the live page, then `pg.evaluate()` to (a) force the `.fc-card`/`.fc-tank`
into the `ok`/`fc-tank--ok` classes + set width 96% + healthy="96", and (b)
inject a `.sb-srcout` banner node onto the first `.sb-array-plate` — screenshot +
vision_analyze that. Verifies pure CSS/markup changes without seeding a tenant.
Pure JS helpers living in an IIFE (sourceStatusHTML, fmtAge) → re-implement them
in a tiny `node -e` to assert all branches (stale→banner, ok/none→"").

## 7. UNIT-ECONOMICS framing for Ford (he asks "how big is the bite")
At $0.005/kWh against his ~$0.05/kWh margin (20¢ revenue − 15¢ cost), AO takes
~10% of the owner's margin. Surface this "size of the bite" framing whenever
pricing comes up — he decides fast once the math is explicit.
