# Array Operator frontend features — delegate to Claude Code + verify with a Node DOM-sim

How AO (`/root/array-operator`, vanilla-JS static site, no build step) frontend
features get built and VERIFIED. Distilled from the "Layout tab" build (June 2026).
Ford prefers coding work delegated to Claude Code (opus) to conserve Hermes tokens.

## Delegation loop (multi-file vanilla-JS feature)
1. **Map the architecture first** (cheap, in Hermes): how the feature wires into
   existing files, the data source's public API, the DOM mount points, the code
   style. AO is IIFE modules, `const/let`, template strings, `esc()` for HTML
   escaping, NO framework/bundler/npm. The fleet data source is `window.FleetStore`
   (read-only public API: `subscribe/load/refetch/snapshot/toColumns/focusColumns/
   setFocus/isLoaded/isLive/lastUpdate`); `snapshot().arrays[].inverters[]` carries
   `status` (ok/underperforming/comm_gap/dead/fault), `current_power_w`,
   `nameplate_kw`, etc.
2. **Write a detailed brief to a file** in the repo (e.g. `FEATURE_BRIEF.md`) — hard
   constraints (no deps, no build, don't break existing views, reuse FleetStore,
   persist client-side in localStorage unless a backend endpoint exists), exact
   wire-in points (file + line), deliverables, and a "verify before finishing"
   checklist. Then delete the brief before committing (scratch doc).
3. **Hand off:** `claude -p "Read FEATURE_BRIEF.md and implement exactly... do NOT
   git commit; leave changes staged. End with a summary of files changed + assumptions."
   --model opus --permission-mode acceptEdits --max-turns 60 --output-format json`,
   run via `terminal(background=true, notify_on_complete=true)` in `workdir` the repo.
   Claude Code is authed via Ford's Max account (`claude auth status --text`).
4. **Claude Code's sandbox auto-denies `git`/`node` beyond `--version`** — so it
   CANNOT `git add` or `node --check` itself. It leaves files modified/untracked
   ("staged in the working tree"). That's expected; you do the verify + commit.

## Verification WITHOUT a headless browser (the reusable technique)
No Chrome/puppeteer is typically installed. Don't ship a DOM feature on the
self-report. Run a **Node DOM-simulation test** via `execute_code`:
- Build a tiny DOM shim: `mkEl()` returning objects with `classList`
  (add/remove/toggle/contains over a Set), `addEventListener` (stash handlers in
  `_ev` so you can fire them), `appendChild`, `getBoundingClientRect`, `innerHTML`,
  `dataset`, `hidden`, `style`.
- Shim `global.document` (getElementById from an `els` map, createElement→mkEl),
  `global.localStorage` (back it with a plain object), `global.window`.
- **GOTCHA:** the IIFE references the data store as a BARE global (e.g. `FleetStore`,
  not `window.FleetStore`) because fleet-store.js declares it globally. Set BOTH
  `global.FleetStore = mock` AND `global.window = {FleetStore: mock}` or you get a
  spurious `ReferenceError: FleetStore is not defined` that is a test-harness
  artifact, NOT a real bug.
- `eval(fs.readFileSync(file))` to load the real module, grab `window.<Module>`,
  then drive it: fire the tab buttons' `_ev.click()`, assert visibility toggles
  (`sandbox.hidden`), localStorage persistence, that nodes rendered
  (`innerHTML.length > N`), and that a store `"live"` callback doesn't throw.
- This caught real behavior (12/12) and proved the default view stays intact —
  the one thing that MUST NOT break when adding a sibling view/tab.
Then `node --check` each edited/new JS, `git status --short` to confirm only
intended files changed (watch for sibling-subagent edits to other files), commit,
`netlify deploy --prod --dir public --site 966cb1f5-944e-41fd-855b-10053edc5d18`,
and `curl` the live URL to confirm the new file (HTTP 200) + wiring is served.

## Honest-caveat habit Ford values
After shipping a UI feature verified only by logic/DOM-sim, state plainly that the
visual/interaction (drag feel, styling) is unconfirmed and his first look is the
real test — don't claim pixel-level verification you didn't do.

## Vendor status note (Chint/CPS)
Chint is in `_CAPTURE_VENDORS` but `api/inverters/chint.py` is an intentional
honest stub (`AVAILABLE=False`) — Chint/CPS has NO public API, so the only
automated route is the same extension-scrape pattern as SMA. It is BLOCKED on a
real Chint portal HAR: don't build it speculatively (the 7-version SMA trap).
Ask whether a customer has a Chint login to capture from before starting.
