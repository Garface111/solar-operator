# Sandbox Vendor⇄Utility data-stream slider

Ford's dad asked for a slider in the sandbox that switches between "vendor data"
and "utility data" — showing each array's data from ONE source at a time, while
the system still INTEGRATES both underneath. Shipped Jun'26.

## The mental model (why two streams)
Each array carries DailyGeneration rows tagged by `source`. After GMP↔vendor dedup
a single array (e.g. Londonderry) holds BOTH: 3000+ `solaredge` days (inverter
telemetry) + `utility_meter`/`gmp_api` days (the meter's settled generation). The
blended `daily` (one row per (array,day), dedup keeps the strongest source) is the
integrated truth used by Trends/reconciliation. The slider just lets the owner VIEW
each feed alone — it does NOT change the integration.

## Stream classification (api/inverter_fleet.py)
`_VENDOR_SOURCES` = solaredge, fronius, sma, chint, extension_pull,
extension_pull_corrected, csv, manual (operator-supplied independent production).
`_UTILITY_SOURCES` = gmp_api, gmp_portal_scrape, utility_meter, smarthub,
bill_prorate. `_daily_stream(src)` → 'vendor'|'utility'|'other'.

## Backend
`_array_daily_split(db, array_id, days=14)` → {vendor:[{date,kwh}], utility:[...],
has_vendor:bool, has_utility:bool}. Splits DailyGeneration by stream AND folds in
the GmpDailyGeneration per-account meter table on the utility side (sum per day,
setdefault so it only fills days the array's own utility rows miss). Emitted as
`column.daily_split` in build_fleet_tree alongside the back-compat blended `daily`.
Tests: tests/test_inverter_fleet.py (split routing + GMP-table fold + empty-side
flags). No migration (reads existing source column).

## Frontend (array-operator/public/sandbox.js + styles.css)
- `getStream()/setStream()` persisted under `ao_sandbox_stream` (default "vendor"),
  mirrors getOrient(). Header segmented control `.sb-streamtoggle` (#sbStreamVendor
  green / #sbStreamUtility blue) wired by `wireStreamToggle(host)` → setStream +
  renderFromStore.
- render() puts `sb-stream-<stream>` on `.sb-canvas`. CSS rule
  `.sb-canvas.sb-stream-utility .sb-comb{display:none}` COLLAPSES the per-inverter
  comb in utility mode (inverters are a vendor-only concept — there's no per-inverter
  meter data). Also hides the expand toggle, dims the vendor src link.
- `arrayGraph(sortedInvs, arrayDaily, col, stream)` is stream-aware: vendor mode
  aggregates per-inverter `inv.daily` (with array-daily fallback for sparse Chint);
  utility mode reads `col.daily_split.utility`. Shared renderer `_renderArraySeries`
  draws the SVG; utility stroke is blue (var(--util,#5b8def)), vendor green. Graph
  label switches "Array production"→"Utility meter". Empty-state when the selected
  stream has no data ("no utility-meter data yet — connect GMP…").
- Decision (Ford): GLOBAL slider (one control flips the whole board) + Utility mode
  COLLAPSES the comb (option A), not dim-in-place.
- Known minor: array card's live "OUTPUT NOW %"/output bar + "Open in <vendor>" link
  stay vendor-derived in utility mode (live power is inherently a vendor reading).
  Acceptable — the headline data STREAM (the graph) is what switches.

## Offline visual QA (no prod hit — Ford clamped interactive prod probing)
sandbox.js exposes `window.__sbRenderTree = render` (debug hook, kept). Harness =
a standalone HTML that stubs `window.FleetStore=null` + sets so_session, loads the
real sandbox.js + styles.css, and calls __sbRenderTree(mockTree) with arrays in 3
shapes (vendor+utility / vendor-only / utility-only). A `__qaSetStream(s)` helper
flips localStorage + re-renders. Drive with Playwright at
/root/array-operator/node_modules/playwright (chromium in ~/.cache/ms-playwright),
screenshot + assert: vendor mode shows combs + "Array production"; utility mode has
0 visible combs, "Utility meter" labels, empty-state on vendor-only arrays. Always
vision-check 1280px AND 390px. Clean up the _qa_*.html after (don't deploy it).

## ⚠️ PITFALL: "the slider doesn't respond to clicks" (cost a round-trip)
The sandbox header uses a FLOATING command-center layout (command-center.css):
`.sb-head-left{ pointer-events:none }` and ONLY `.sb-head-left .sb-head-btns{
pointer-events:auto }` re-enables clicks. A new control placed as a direct child of
`.sb-head-left` but OUTSIDE `.sb-head-btns` (like `.sb-streamtoggle`) inherits
pointer-events:none → every click is silently swallowed. FIX: add
`.sb-head-left .sb-streamtoggle{ pointer-events:auto !important; position:relative;
z-index:8 }` (z-index because the .sb-canvas uses transform → its own stacking
context that can paint over the header). SECOND gap: renderGrid() (the Overview tile
view, NOT the default canvas) had its OWN wiring block that didn't call
wireStreamToggle, and tileSpark() ignored the stream — slider dead in grid mode.
Fixed: wire toggle in renderGrid + stream-aware tileSpark.

THIRD (belt-and-suspenders after a SECOND "still not working" report where my
headless tests all PASSED against live assets): per-button `.onclick` is fragile —
a transparent canvas/pan overlay floating over the header can intercept the bubbling
click, and the handler is lost on every re-render. ROBUST FIX = one document-level
listener in the CAPTURE phase delegating on `.sb-stream-seg`:
`document.addEventListener('click', e=>{const seg=e.target.closest('.sb-stream-seg');
if(seg){e.preventDefault();e.stopPropagation();applyStream(seg.id===...)}}, true)`
installed once (guard with a module flag). Capture fires before any bubbling overlay
can stopPropagation, and delegation survives button re-creation. Keep the per-button
onclick too as a fallback.

QA LESSON (important): my first QA drove the toggle via a JS helper (__qaSetStream)
which BYPASSED the click path → missed the pointer-events bug. ALWAYS test with a
REAL Playwright `page.click()` through the actual deployed CSS. Build the harness
against the LIVE deployed files (link https://arrayoperator.com/styles.css +
command-center.css, `<script src=https://arrayoperator.com/sandbox.js>`), nest
#sandbox inside #sbWrap inside #acctList, and stub a minimal real-shaped
window.FleetStore ({isLoaded:()=>true, focusColumns:()=>TREE, toColumns, snapshot,
subscribe, liveVerdict, setFocus}) then call window.__sbLoad(). To localize a dead
click: hit-test `document.elementFromPoint(cx,cy)` at the button center + read
getComputedStyle(...).pointerEvents on the button AND its ancestors. If headless
tests pass but the USER still reports failure, it's almost certainly (a) their stale
SPA cache (have them hard-refresh Cmd/Ctrl+Shift+R) or (b) an environment-specific
overlay — ship the capture-phase handler and give them a 1-line console probe
(`document.addEventListener('click',e=>console.log(e.target.id, document.elementFromPoint(e.clientX,e.clientY)?.className),true)`)
rather than guessing further.

## Deploy
- Backend: git push origin HEAD:main → Railway (~85s). Verify
  `railway ssh "python -c import api.inverter_fleet; hasattr(...,'_array_daily_split')"`.
- Frontend: `python3 scripts/netlify_api_deploy.py` (REST file-digest; CLI broken).
  Verify markers: `curl arrayoperator.com/sandbox.js | grep sbStreamUtility`.
- Shared solar-operator tree: stage ONLY inverter_fleet.py + its test.
