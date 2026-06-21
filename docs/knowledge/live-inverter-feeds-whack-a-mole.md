# Live inverter feeds — the recurring whack-a-mole + the systemic fix

Ford reports, one vendor at a time, "X inverter feed isn't working / cards say
idle / not producing." It FEELS like each vendor breaks separately (SMA, then
Fronius, both in one session). The durable lesson: **stop patching per-vendor
symptoms — most of these are the SAME class of bug surfacing differently, and the
real fix is server-side + honest degradation, not another vendor patch.**

## FIRST: ground the report on LIVE per-tenant DB data, never guess
Ford has MANY near-duplicate tenants per email (typo/+variant signups, BOTH
products). "It's not working on my dad's account" must be diagnosed on the EXACT
tenant his login resolves to — not the first tenant the email matches. Workflow
that worked (read-only, via `railway ssh ... python -c`):
1. List ALL tenants for the exact `contact_email` + their `product` + `active`.
   The AO login (`account.issue_magic_link`, product-scoped) picks
   `ORDER BY active DESC, created_at DESC` WITHIN the product → confirm which AO
   tenant that is. A NEPOOL tenant holds bills (0 inverters); the AO tenant holds
   the inverter feeds. Don't diagnose the wrong Waterford.
2. For each array dump: inverter count, `sum(last_power_w)`, `nonzero` count,
   newest `last_power_at`. A FRESH timestamp + ZERO power = capture is RUNNING but
   the live reading is junk (the real bug) — NOT a dead capture.
3. Call `inverter_fleet.build_fleet_tree(db, tenant)` IN-PROCESS and inspect the
   exact column/inverter dicts the frontend will render. This is the fastest way
   to see whether the bug is data, backend, or frontend. (Ford has clamped down
   on HTTP probes to prod endpoints — use in-process function calls, not curl.)

Grounding this way overturned two wrong hypotheses in one session (it was NOT a
tenant mixup, NOT Fronius-global) and pinned the true cause to one array's flaky
instantaneous reading.

## The two real root causes (both surfaced this session)

### A) Per-device endpoint DRIFTED to null → the WHOLE capture aborts (SMA)
SMA's `/overview/{plant}/devices` (Sunny Portal/ennexOS, `sunnyportal_content.js`)
started returning `pvPower = null` per device (live power moved to a separate
site-level `widgets/gauge/power` + `measurements/search` call). But the device
filter still REQUIRED non-null `pvPower` → every inverter filtered out →
`captureOnePlant` returns null → `captureFlow` throws "no producing inverters" →
the entire SMA capture FAILS and cards freeze at the last snapshot that happened
to carry pvPower. Live DB tell: inverters 1-3 DAYS stale while SolarEdge stays
live. FIX: never gate the inverter list on the drift-prone live field — keep any
real Device (has live power OR today's energy OR an id), leave `current_power_w`
null when the vendor omits it, and let the backend allocate the SITE-level live
power across inverters by energy/nameplate share (the path Fronius already uses,
`array_owners._persist_meter_accounts` / poller `_allocate_power`). RULE: a
capture must degrade per-field, never abort the whole site because one drifted
field is null.

### B) Card decides "producing" from the JITTERY instantaneous reading (Fronius)
The array card painted "IDLE / not producing / all good" on a healthy array that
generated 591 kWh that day, because its live reading was a flaky ~3 W/inverter
(30 W vs ~125 kW nameplate) — below the "producing" floor. The instantaneous
live wattage from extension-scraped vendors is NOISE; it dips/glitches constantly
and must NEVER be the sole signal for "is this array working." This is the mole:
a producing array reads broken whenever its live instant-reading dips.

THE SYSTEMIC FIX (kills the class, not one vendor):
1. Backend `inverter_fleet.build_fleet_tree` now computes TWO authoritative
   array-level fields:
   - `current_power_w` = sum of per-inverter live readings (None when none) — so
     no surface re-aggregates client-side (which also broke on a stale SPA shell).
   - `produced_today_kwh` = today's row from the array's OWN DailyGeneration
     history (`_array_daily`) — the source of truth the jittery live feed is NOT.
2. Frontend (`array-operator/public/sandbox.js` `arrayLiveState` + `arrayOutputBar`):
   when live ≈ 0/absent BUT `produced_today_kwh > 0`, show "Produced today · N
   kWh · live feed updating" (calm green) instead of "Not producing". A GENUINELY
   dead array (produced nothing today) still correctly reads "Not producing" — do
   NOT mask real failures. Night still reads "Idle (night)".
3. PITFALL (AO playbook §10): a new fleet-tree column field is STRIPPED unless
   forwarded in BOTH `fleet-store.js` `adaptTree` AND `toColumns` (both
   allow-lists). Add the field to both or the card never sees it.

## Why server-side polling can't save SMA/Fronius (know this before suggesting it)
The server-side poller (`api/poller.py`) only polls vendors with PULLABLE creds
(`_pullable_connection`: api_key+site_id, or OAuth client_id/secret/refresh).
Confirmed live: `fronius connections: 0` and `SMA connections: 0` with pullable
creds — these are EXTENSION-ONLY (no developer-app registration). Their only live
source is the extension's silent hourly recapture (`background.js` RECAP_ALARM,
60-min, opens a background tab, needs the owner's live portal session). So:
- A backend poller fix does NOTHING for SMA/Fronius until Ford registers a vendor
  dev app. Don't propose it as the fix.
- The extension capture depends on the owner having a live portal session; if
  expired, the hourly recap fails and he gets a one-tap reconnect nudge. The
  filter/parse bug breaks capture EVEN WITH a valid session — that's the part you
  can fix and stand behind. Say honestly you can't verify the live end-to-end
  refresh without the owner's session.

## Honest verification limits (state these, don't fake a screenshot)
The AO sandbox renders REAL arrays only behind the owner's login (anon = demo
data with no new backend fields). So you CAN: verify backend output on the real
tenant in-process, confirm the live bundle serves the new code
(`curl arrayoperator.com/sandbox.js | grep -c <fn>`), and unit-mirror the render
decision with the owner's exact numbers in Node. You CANNOT screenshot the
owner's logged-in card without their session — say so. Always tell Ford to HARD
REFRESH (Cmd/Ctrl+Shift+R) once after a frontend deploy (stale SPA shell, AO
playbook §17).

## Deploy recap for these fixes
- Backend: stage ONLY your files, `git push origin HEAD:main` → Railway (~80s);
  no migration if no new DB column (these were computed fields, none added).
- Extension: bump `manifest.json`, `bash scripts/build_extension_zip.sh`, VERIFY
  the fix is in the zip (unzip+grep), `gh release create ext-v<ver>`, curl the
  download URL for 200. Ford loads it MANUALLY.
- AO frontend: MANUAL via `python3 scripts/netlify_api_deploy.py` (the Netlify
  CLI is broken here — AO playbook §1).
