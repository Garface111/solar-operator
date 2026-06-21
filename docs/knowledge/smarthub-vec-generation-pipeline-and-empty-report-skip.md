# SmartHub/VEC generation pipeline + empty-report skip guard

Two related NEPOOL data-integrity learnings: (1) WHY VEC/SmartHub reports show
zero generation and the full fix, (2) the automatic-report empty-skip guard.

## ROOT-CAUSE TRAP: bill `totalUsage` is CONSUMPTION — never write it to kwh_generated

The single most damaging SmartHub bug class (live-confirmed Jun'26, the "VEC data
pull feeds 0 into the NEPOOL reports" report). The extension's API-bill capture
(`smarthub_content.js`) sets `kwh: r.totalUsage` from `billing/history/overview`.
`totalUsage` is the meter's NET CONSUMPTION for the period — for a net-EXPORTING
solar account it's ~0. The `/v1/sync` SmartHub branch (`api/app.py`) was storing
that into `Bill.kwh_generated`, which the GMCS report reads as PRODUCTION → every
VEC/WEC NEPOOL report rendered zeros. Live proof: real acct 6578300 had 36 bills
ALL `kwh_generated=0` and `DailyGeneration: NONE`, while seeded demo accounts (with
real kwh_generated + bill_prorate rows) reported fine — the contrast IS the tell.

FIX (shipped): in the `/v1/sync` SmartHub branch route bill `kwh`(=totalUsage) to
`Bill.kwh_consumed` and set `kwh_generated=None` from the bill path ENTIRELY.
SmartHub bills carry NO generation number; the ONLY generation source is the daily
utility-usage pull (negative net-export → DailyGeneration, source utility_meter /
smarthub). Regression test pins it: a SmartHub bill with `kwh` must land in
kwh_consumed and leave kwh_generated None (tests/test_smarthub_dispatch.py
`test_smarthub_bill_kwh_is_consumption_not_generation`). The `Bill` model already
has a `kwh_consumed` column, so no migration. DIAGNOSE before coding: a per-account
read-only prod query (`railway ssh` python) of Bill.kwh_generated distribution +
DailyGeneration source breakdown for SmartHub accounts tells you instantly whether
it's mislabeled-consumption (all 0) vs a no-data-landed gap.

## Backfilling the FULL reporting window (35-day window was too short)

The client-side daily pull (path C below) originally pulled only 35 days, so one
owner re-login backfilled ~1 month while the GMCS report needs 6 rolling quarters
(~18 months) — every historical quarter stayed zero. FIX (v1.9.47): widen
`fetchDailyGeneration` to ~19 months (`GEN_LOOKBACK_DAYS=580`) pulled in 90-day
CHUNKS (`GEN_CHUNK_DAYS=90`), walking newest→oldest so a partial failure still
leaves the most-recent data populated. A single 18-month DAILY POST gets
truncated/rejected by the NISC API — chunk it. The backend ingest
(`_persist_meter_accounts`) upserts EVERY day in the payload with no window cap, so
one re-login now fills the whole report.

## VEC/SmartHub: bills carry NO generation kWh — it lives only in the usage API

VEC (Vermont Electric, `vermontelectric.smarthub.coop`) and all NISC SmartHub
co-ops (WEC, Stowe, …) are routed through `api/adapters/smarthub.py`
(`api/adapters/vec.py` is a deprecated re-export shim, removal Aug 2026).

THE TRAP that made Bruce's reports zero for 3 years: the SmartHub BILLING page
has date/amount/PDF but **no generation kWh**. For a net-metering SOLAR account
the production lives ONLY in the usage API as a per-day NEGATIVE net-export
value (`POST /services/secured/utility-usage` → `{ELECTRIC:[{series:[{data:[{x:epoch_ms,y:kWh}]}]}]}`;
NEGATIVE daily y = export = generation, regardless of the meter's
flowDirection/isNetMeter flags — grounded on West Glover acct 6578300). So bills
land with `kwh_generated=0`/None and `DailyGeneration` stays empty → reports show
zero. Even the bill API capture's `kwh = r.totalUsage` is CONSUMPTION, not
generation, for these accounts.

### THE BILL-PATH POISON BUG (Jun'26 — supersedes the old "fix A"): totalUsage → kwh_consumed, NEVER kwh_generated
The `/v1/sync` SmartHub branch (`api/app.py`) was writing the bill's `kwh`
(sourced by the extension from `billing/overview.totalUsage`, and the
usage-explorer aria-label `kWh:`) into `Bill.kwh_generated`. That number is NET
CONSUMPTION, ~0 for a net-exporting solar array. The GMCS report reads
`kwh_generated` as production → every VEC/WEC NEPOOL report rendered ZEROS.
LIVE-CONFIRMED via a read-only prod diagnostic: the REAL VEC account `6578300`
(West Glover, tenants Norwich Racquet Club / Green Mountain Community Solar) had
36 bills ALL `kwh_generated=0` and `DailyGeneration` NONE — while the SEEDED demo
accounts (`9900000007+`, "Northeast Community Solar") had real kWh + bill_prorate
daily rows and reported fine. That demo-vs-real contrast is the fast tell.
FIX: route the bill `kwh` to `Bill.kwh_consumed` (the model already has that
column — no migration) and set `kwh_generated=None` from the bill path entirely.
A SmartHub bill carries NO generation number; generation lands ONLY via the daily
utility-usage pull (C below → `DailyGeneration`). Regression test:
`tests/test_smarthub_dispatch.py::test_smarthub_bill_kwh_is_consumption_not_generation`
(asserts `kwh_generated is None` AND `kwh_consumed == 12`). DO NOT reinstate the
old "UPDATE-don't-skip backfills kwh_generated" logic — that WAS the bug. The
update-don't-skip pattern is still correct, but it backfills `kwh_consumed` +
period bounds now, never `kwh_generated`.

NOTE on stale zeros: the fix stops FUTURE poisoning but the 36 already-stored
`kwh_generated=0` rows for 6578300 are harmless to the report (report reads
DailyGeneration first; a 0 bill contributes nothing) — leave them; the real
unblock is getting production to LAND (C), not cleaning the zeros.
B) `api/jobs/smarthub_pull.pull_all_smarthub()` + scheduler job (03:05 UTC):
   server-side daily-generation pull for every array with an enabled SmartHub
   account + a stored token, upsert `DailyGeneration(source="smarthub")`. NOTE:
   the v1.9.25 server-side-pull design was found WRONG — the backend canNOT
   replay the owner's httpOnly SmartHub session cookie. So B only works if the
   extension captured an `authorizationToken` (most VEC sessions don't expose one).
   The RELIABLE generation path is the CLIENT-SIDE pull (C), not B.
C) THE ROBUST FIX — client-side pull in the extension (`extension/smarthub_content.js`):
   `maybeSendMeterCapture()` fetches `/services/secured/utility-usage` same-origin
   (cookie rides along), reduces negative-y into per-day generation, and ships
   `accounts[].daily[]` via `SMARTHUB_METER_GEN_CAPTURED`. It was AO-GATED
   (`meterIntentArmed()` only true when an Array Operator "Connect" intent was
   armed) so NEPOOL NEVER triggered it. FIX: also fire when the extension is
   PAIRED to any tenant (`tenant_key` set). Then `background.js` POSTs the daily
   series to the dual-auth `POST /v1/array-owners/utility-meter-capture` with the
   stored tenant_key (no AO page needed; additive to the AO relay).

### Backend array-match pitfall (C)
`/v1/array-owners/utility-meter-capture` → `_persist_meter_accounts` matched
arrays by display NAME only and CREATED one on miss. NEPOOL VEC arrays are named
by full service address ("52 County RD, Glover, VT, 05839") and linked to a
UtilityAccount; the capture nickname (`addr1, city, state` — no zip) won't
name-match → DUPLICATE array. FIX: match by the array's linked
`UtilityAccount.account_number` first, then name, then create. Allowed vendors
`_UTILITY_CAPTURE_VENDORS = {gmp, vec, wec}`; daily upsert is idempotent
(max-kWh per (array, day), `source="utility_meter"`).

### Backfill workflow (what Ford does)
DIAGNOSE FIRST: run `scripts/diag_vec_report_feed.py` (in this skill's scripts/;
read-only, inline it via `railway ssh 'cd /app && python -c "..."'`) to see, per
SmartHub account, the `kwh_generated` distribution + DailyGeneration-by-source.
All-zeros bills + `daily: NONE` on a REAL account = the report is empty because
production never landed. ALSO check whether the server-side pull is even possible:
query the latest `UtilitySession` for the provider — VEC sessions store
`api_token=False` (no `authorizationToken` exposed), so the server-side pull (B)
CANNOT run and the CLIENT-SIDE pull (C) is the only path. Don't waste a build
chasing B for VEC.

Bump manifest, `bash scripts/build_extension_zip.sh` → Desktop, VERIFY the fix
is actually IN the zip (unzip + grep the changed lines — past builds shipped
without the fix). Ford reloads the extension MANUALLY, then the owner re-logs
into the SmartHub portal → client-side pull runs. The extension
logs a loud `[EnergyAgent] smarthub meter-capture POST -> <status>` console line
— ask Ford for that line if generation still doesn't land.

### Window must cover the FULL report period, pulled in CHUNKS (v1.9.47, Jun'26)
The original `fetchDailyGeneration` pulled only 35 days, so a single owner
re-login backfilled ~1 MONTH while the GMCS/NEPOOL report renders 6 ROLLING
QUARTERS (18 months) → every historical quarter stayed zero even after a
"successful" capture. This is a DATA-COVERAGE gap, not a rendering bug — same
class as the SolarEdge `days_back=90` nightly-pull cap.
FIX (shipped): widen to ~19 months (`GEN_LOOKBACK_DAYS = 580`) so one re-login
fills the whole window. CRITICAL — pull in 90-day CHUNKS (`GEN_CHUNK_DAYS = 90`),
NOT one big request: a single 18-month DAILY `utility-usage` POST gets
truncated/rejected by the NISC API. Walk newest→oldest and accumulate every
chunk into one per-day `byDay` map, stepping back one day past each chunkStart to
avoid double-counting the boundary — so a partial failure of an OLD chunk still
leaves the most-recent (most-needed) data populated. Refactor pattern that made
this clean: split the monolith into `_fetchUsageChunk()` (one window, cookie-only
then nisc-header retry, returns null on fail) + `_reduceUsageInto(data, byDay)`
(the negative-y → generation reducer) + a chunk-walk loop in
`fetchDailyGeneration`. The LOG line now reports `N day(s) over X ok / Y failed
chunk(s)`.
Backend needs NO change for a wide window: `_persist_meter_accounts` upserts
EVERY day in `acct.daily` with no cap, idempotent (`max(row.kwh, dk)`,
`source="utility_meter"`) — verified before widening, so confirm the ingest has
no window cap before assuming the frontend is the only lever.

TO DELIVER A FIXED BUILD TO THE OWNER (Bruce): the completing action is shipping
the new extension as a verified GitHub-release download link — see
`references/extension-build-release-and-email-bruce.md` for the
build→verify-fix-in-zip→publish-release→verify-URL→email pipeline and the
install-then-re-login-into-VEC step that actually triggers this client-side pull.

## Automatic reports must NEVER email a blank workbook (empty-skip guard)

"The automatic reports are bogus" turned out to be: the quarterly scheduler fans
out to EVERY active client and built+mailed a zero-filled GMCS workbook for
clients with arrays but no data (or empty onboarding stubs). The "email-to-me"
button looked fine only because operators click it on clients they KNOW have
data. Both paths share `deliver_for_client → build_workbook` — identical output,
so the difference is WHICH clients get sent, not how they're built.

FIX:
- `writers/gmcs_writer.report_has_data(client_id)` — read-only coverage check
  using the EXACT same rolling-quarter window + sources (Bill calendar-day
  attribution + DailyGeneration) as `build_workbook`, so it never disagrees with
  the rendered cells.
- `delivery.deliver_for_client(skip_if_empty=...)`: skip + return
  `skipped_empty` instead of mailing a blank. Explicit operator/button sends
  leave it False (force-send always works); the scheduler +
  `deliver_for_tenant` bulk fan-out pass True.
- Scheduler internal-alerts a run summary listing skipped clients so Ford learns
  WHO has no data instead of silent nothing.
LESSON: an "automatic feature sends garbage" report on a shared builder is
almost always a FAN-OUT scope problem (sending to empties), not a builder bug —
confirm on real data (per-client coverage diagnostic) before touching the writer.

## Finish-setup banner (#gmpGate) completes the moment GMP connects

FORD'S RULE (filed this session): "once you connect GMP the finish-setup banner
needs to disappear." The AO `#gmpGate` banner ("Connect GMP to finish setting up
/ You're not done yet") is driven by `GET /v1/array-owners/onboarding-status` →
`complete`, which the frontend (`array-operator/public/app.js` `updateGmpGate`)
uses to HIDE the bar. It previously required `complete = gmp_connected AND
linked>0`, so after connecting GMP the banner STAYED UP (switched to "Link your
GMP accounts to finish") until every captured account was linked to an array —
read as "still not done." First fix was `complete = gmp_connected` alone — but
that was STILL wrong (see next paragraph).

THE REAL FIX (a gate must check ANY data source, not ONE vendor): Ford's AO
tenant `ten_a554c8e7a08f8cfa` had the banner STUCK ON despite being fully
connected — it has 19 arrays + ~6,000 `DailyGeneration` rows via **SolarEdge**
and **ZERO** GMP accounts. The gmpGate only counted GMP, so every owner who
connected through SolarEdge/Fronius/SMA/Chint or VEC/WEC was nagged to "Connect
GMP" forever. FIX: `onboarding-status` now computes `connected = ANY real
source` — GMP session/account OR any UtilityAccount (any provider) OR an
InverterConnection (or legacy `Array.solaredge_site_id`) OR any stored
`DailyGeneration` row — and `complete = connected`. Added response fields
`connected` / `has_inverter` / `has_utility_accounts` (additive; existing keys
intact). `next_step` keeps `connect_gmp` for the no-source case and the
GMP-unlinked `link_accounts` nudge, but no longer holds the gate for non-GMP
owners. Frontend already keys off `complete` (backend-only change); gate
self-refreshes after a capture (`sandbox.js` → `window.__aoRefreshGmpGate`); SPA
shell-caches `app.js` so a HARD refresh may be needed to see it clear.

GENERAL FORD PATTERN (two-layer lesson): a completion GATE clears on the action
that defines "done" with everything-else demoted to gentle nudges — BUT "done"
for a multi-vendor product is "has ANY working data source," never "connected
THE one vendor the banner is named after." When a gate is stuck despite the user
insisting they're connected, FIRST read their live tenant's data sources (which
provider, how many arrays/daily rows) before assuming the gate logic vs. a cache
— the answer here was a real data-shape the gate ignored, not a bug in the flip.
Diagnostic: a per-tenant onboarding-status probe (script that calls the endpoint
fn in-process with the tenant_key) is the fast way to see exactly why `complete`
is false. Watch for MULTIPLE tenants under one contact_email (Ford has a nepool
tenant AND an array_operator tenant on ford.genereaux@gmail.com) — diagnose the
exact `product` tenant the user is signed into.
