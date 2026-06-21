# "Inverters appear but no live data streams" — capture data-flow lag triage

Ford reports this for Chint and Fronius: "Chint login collects the inverters but the
data doesn't start flowing right away. Same with Fronius." Symptom on the canvas
(confirmed by the latest screenshot, Jun 2026): the inverter cards show up INSTANTLY
and read "All good", but every card says "not producing right now" (no kW) and "no
history yet"; the array panel shows "history building — graph appears once 2+ days are
stored."

## DIAGNOSE FROM THE CODE — the pipeline is sound, don't chase ghosts
The full chain was traced end-to-end and is correct:
- content script (`chint_content.js` / `solarweb_content.js`) reads the portal and ships
  `sites[].inverters[]` via SO_CAPTURE_LANDED;
- background.js (`CHINT_CAPTURED` / `FRONIUS_CAPTURED`) forwards `sites` faithfully;
- `POST /v1/array-owners/inverter-capture` persists Array + DailyGeneration + Inverter +
  InverterDaily ATOMICALLY in one transaction (immediate, not deferred);
- `sandbox.js handleCaptureLanded` calls `FleetStore.refetch()` after the POST;
- `build_fleet_tree` reads the per-inverter daily series STORAGE-FIRST (`_merged_daily`
  / `_stored_inverter_daily`), so persisted readings surface on that very refetch.
- the 10-min `_site_cache` in inverter_fleet.py is SolarEdge-API-only (keyed `vendor:site`,
  only populated when a connection has an api_key+site_id) — it does NOT gate extension
  vendors, so it is NOT the lag.

So rows appearing but readings lagging is NEVER a persistence / cache / broadcast bug.

## THE THREE ACTUAL CAUSES (in the order to check them)

### 1. SILENTLY-DROPPED capture field (the real bug found Jun 2026) — CHECK FIRST
"Cards show but no kW" = a per-inverter reading the content script CAPTURED was
discarded on ingest because the backend Pydantic model lacked the field. Verified:
Chint's `chint_content.js` emits `current_power_w` per inverter (real watts from
`commDevice.currentPower`, e.g. 51000.0 — grounded live on Bruce's Londonderry), but
`CaptureInverter` (api/array_owners.py) had NO `current_power_w` field. Pydantic
SILENTLY DROPS unknown fields, so the watts were deleted on arrival; the backend then
fell back to allocating the SITE-level reading across inverters by energy share, and
when that site field was absent every card got None → "not producing right now."
FIX (shipped): add `current_power_w` to `CaptureInverter`, and in the ingest loop PREFER
the inverter's own measured reading:
```python
if ci.current_power_w is not None and ci.current_power_w >= 0:
    iv.last_power_w = round(float(ci.current_power_w), 1); iv.last_power_at = now()
elif site_power_w is not None and _site_invs:   # fallback: allocate site total by energy share
    ...
```
RULE: real per-inverter readings BEAT a derived site-split — prefer measured, demote the
split to a fallback for vendors that report power only site-wide (Fronius gives a
site-level TotalPower; Chint & SMA give per-inverter). This was a PURE BACKEND fix — no
extension rebuild, goes live for the already-installed extension on deploy.
DEBUG MOVE: when a capture vendor shows rows but a value is blank, FIRST diff the content
script's emitted per-inverter object keys against the `CaptureInverter` / `CaptureSite`
schemas. Any field the script sends that the model doesn't declare is silently dropped —
that's the #1 suspect, before any portal/auth theory. (Sibling of the provider-keyed
"misattribution bug class" in SKILL.md: captured then thrown away.)
Regression test: `tests/test_array_owners.py::test_inverter_capture_chint_keeps_per_inverter_live_power`
POSTs per-inverter `current_power_w` and asserts it lands as `Inverter.last_power_w` AND
surfaces on `/fleet-tree`.

### 2. Chint is a structural TWO-STEP per-site manual capture
`/api/asset/site/retrieve` (site list) loads on the dashboard, but the per-inverter
`busTypeDevices` (carrying `eToday`) only fires when the owner OPENS each site. The
content script holds ~6 polls (~18s) then emits site-level only with `inverters: []`.
On a multi-site account each site must be clicked once. So even with the field-drop fixed,
the comb/readings can't flow until the owner opens each site. Make this explicit in the
connect UX (the tip is wired on the Chint login button — keep it loud).

### 3. Day-1 graphs are empty because capture grabs only TODAY — EXPECTED, not a bug
The sparkline, min/max, and peer_index all need MULTIPLE InverterDaily days. Capture
ships one day's `energy_today_kwh`, so on day 1 the comb shows but graphs are sparse and
the panel honestly says "graph appears once 2+ days are stored." History accrues via the
daily snapshot scheduler job. To populate graphs IMMEDIATELY, build a ~14-day history
backfill at capture time — both portals expose it: Chint `GET /openApi/v1/dashboard/daysEnergy?month=YYYYMM&userId=`,
Fronius analysis-chart over a date range. This is INDEPENDENT of the live-power fix; build
it only if the owner needs day-1 graphs.

## Likely additional bug to grep (not yet hit but flagged)
`chint_content.js invertersFrom()` reads only `dvc.eToday` for daily energy, but the
grounded contract (`chint-portal-api-contract.md`) says it can also be `dvc.energy.eToday`.
If a device nests it, `energy_today_kwh` comes through null → no InverterDaily row → empty
graph even after opening the site. Read `dvc.eToday ?? dvc.energy?.eToday`.

## Diagnose FAST with the screenshot
"Check the latest screenshot" (newest in OneDrive/Pictures/Screenshots 1/) IS the bug
report — vision-read it. The card text disambiguates instantly:
- "not producing right now" / no kW  → cause 1 (dropped live-power field) or 3 (off-peak).
- "no history yet" / "graph appears once 2+ days are stored" → cause 3 (history gap, expected).
- cards don't appear at all → not this class; it's a capture-failure (auth/world/CORS — see
  extension-capture-mv3-debugging.md).
Don't conflate the live-power drop with the history gap — they have different fixes.
