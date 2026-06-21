# Array Operator visualization consistency — multi-surface state drift + the two-axis card

This is a recurring bug CLASS in the Array Operator owner site (`/root/array-operator`,
Netlify static, no build — vanilla `public/*.js`). It is NOT the demo-masking class
(see session-auth-and-demo-fallback.md); it is about the THREE rendering surfaces
disagreeing with each other or with themselves about an inverter's state.

## The three surfaces and their shared store
- `public/fleet-store.js` — `window.FleetStore`, THE single source of truth. Loads
  `/v1/array-owners/fleet-tree`, normalizes to canonical arrays, exposes `toColumns()`
  (sandbox shape) + `snapshot()` (command-center shape). Both shapes carry
  `current_power_w`, `is_daylight`, `status` per inverter.
- `public/sandbox.js` — the per-array fleet TREE (cards) AND the OVERVIEW GRID tiles
  (`renderGrid` / `arrayHealth`). Same file, two views.
- `public/command-center.js` — the portfolio KPI strip + triage queue (`buildModel`).
- Script load order in index.html: fleet-store → sandbox → command-center. So a shared
  helper BELONGS in FleetStore; the other two can call it.

## Bug class A — TWO TIME HORIZONS conflated into one label (the "All good while not producing" bug)
Each inverter carries two independent truths that must never masquerade as one:
- **HEALTH** = `inv.status` — the backend 14-day `peer_analysis` verdict
  (`api/inverters/peer_analysis.py`). An inverter that just stalled keeps `status:"ok"`
  for up to `DEAD_DAYS=2` days.
- **NOW (liveness)** = `current_power_w` — instantaneous output.
The original card drove the "All good" badge SOLELY off `status`, so a dark inverter
(0 W now) still showed green "All good" next to "OUTPUT NOW: not producing right now".

FIX = split the card into two EXPLICIT, separately-labelled axes:
- A small **NOW chip** under the sparkline (dot+word): Producing (green) / Idle (grey,
  calm) / Not producing (amber) / No signal (blue) / Asleep (lavender). Carries a
  tooltip saying WHY each non-producing state is or isn't a concern.
- The **health badge** at the card foot = PURELY `inv.status`. It must NOT second-guess
  the live reading — that conflation is the original bug.
Two labelled axes in two places = "Not producing (now)" + "All good (health)" reads as
two coherent facts (a freshly-stalled unit), never a contradiction. General principle:
when a card shows "how is X also Y?", the cause is almost always two different metrics /
time-horizons collapsed into one ambiguous label — separate and label them, don't patch
the label.

## Bug class B — multi-surface DRIFT (each view derives state independently)
The tree learned the live-anomaly check, but the grid (`arrayHealth`) and command center
(`buildModel`) still did `if(inv.status === "ok") continue;` / `invHealthy++` — so a dark
inverter read "all good" on the grid tile and counted as healthy in the KPI %, contradicting
the tree card beside it. Fixing one surface does NOT fix the others (same drift shape as the
three demo-masking points).

FIX (root-cause, anti-drift): put ONE shared classifier in FleetStore and have all three
surfaces call it. Implemented as:
```
FleetStore.isProducing(inv)                        // > max(25W, 1% of rated)
FleetStore.liveVerdict(inv, peers, isDaylight)     // "ok" | "dark" | "stale"
FleetStore.isLiveAnomaly(inv, peers, isDaylight)   // status==="ok" && verdict==="dark"
```
`liveVerdict` logic: night (`isDaylight===false`) → "ok" (zero expected, owned by the
Sleeping state); producing → "ok"; else require a QUORUM of ≥2 lit daylight peers (a single
cloud-edge or a 2-inverter array must not raise a false alarm — mirrors peer_analysis's
degenerate-cohort rule); fresh 0 W reading while peers produce → "dark" (real anomaly);
NO reading at all → "stale" (unknown, not a confirmed fault). sandbox.js keeps a local
fallback copy that delegates to the store when present (`window.FleetStore.liveVerdict`)
so isolated render tests still work.

Surface wiring:
- `arrayHealth()` (grid): a `status:"ok"` inverter that is a live anomaly counts as flagged
  → tile goes amber/"N flagged", can't read "all good" while a card inside is dark.
- `buildModel()` (command center): promote a `status:"ok"` live anomaly to a synthetic
  `"live_dark"` row — drops it OUT of the healthy count (lowers fleet-healthy %), surfaces
  it in flagged/watch bucket, but claims $0 (UNPRICED like comm_gap — don't over-promise
  recoverable revenue on something that might be a 1-min dropout; the dollars get claimed
  only once 14-day health confirms dead/underperforming). Needs matching `STATUS_LABEL`,
  `SEV`, `ACTION`, and drawer `why`/`rec` entries for the new pseudo-status.
- PITFALL caught this session: the command-center right panel's "All clear 🌞" branch keyed
  on `riskMo >= 1` (dollars). A flagged-but-$0 live anomaly tripped "All clear" while the
  same card said "1 flagged". Add a middle branch: `riskMo>=1 ? $risk : k.flagged ? "N to
  check / live anomalies — no $ lost yet" (amber) : "All clear"`. Whenever you add an
  unpriced flagged state, audit every place that infers "clean" from dollars==0.

## Verification pattern — the FAITHFUL render harness (don't eyeball, don't re-type logic)
Ford's standing rule: Playwright screenshot + vision_analyze on EVERY UI change; never claim
done off green logic alone. Two harness shapes used here, both avoid divergence by exercising
the REAL code:
1. Single-surface card harness: in a Node script, read `public/sandbox.js`, EXTRACT the real
   helper functions by BRACE-MATCHING their headers (a `grab(name)` that counts `{`/`}` from
   `function NAME(`, and a `grabConst` that reads to the depth-0 `;`) + slice the real teeth-
   render template between two stable anchor strings. Write them into an `<html>` that links
   the real `public/styles.css`, render a crafted fleet, screenshot with Playwright, then BOTH
   assert badge/chip text via `$$eval` AND vision_analyze the PNG for clipping/contrast/wrap.
   Extracting (not hand-copying) the functions guarantees the harness can't drift from prod.
2. End-to-end three-surface harness: an `<html>` that stubs `window.fetch` to serve a crafted
   `/fleet-tree` BEFORE loading the three real scripts in index.html order, sets
   `localStorage.so_session` + `ao_sandbox_viewmode=grid`, waits ~1s for async load+subscribers,
   then reads back each surface's verdict (FleetStore classifier, command-center KPI strip via
   `[data-kpi=...]`, grid tile tone+flag) and screenshots. This proves all three AGREE on the
   same anomaly — the only real test for a drift fix.
Playwright + chromium are already installed under `/root/array-operator/node_modules`
(require it by absolute path). Put harness files in a throwaway `_audit/` dir and `rm -rf` it
before committing.

## Deploy
`/root/array-operator` → remote `Garface111/array-operator`, Netlify auto-deploys on push to
main. `git push origin HEAD:main` then `git fetch -q origin && git log origin/main -1` to
confirm it actually landed. (Cron-leaves-branch-checked-out trap applies to the OTHER repo,
solar-operator, not this one — but verifying origin after push is cheap insurance either way.)
