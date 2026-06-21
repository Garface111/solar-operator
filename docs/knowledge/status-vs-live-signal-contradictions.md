# Status/health vs live-reading contradictions on a card — and how to prove the fix

Split out of `static-site-build-verify-loop.md` (which hit the size cap). Two tightly related
lessons from auditing the Array Operator inverter cards: a UI bug CLASS where a slow health verdict
silently disagrees with an instantaneous reading, and the harness technique that PROVES a vanilla-JS
card-render fix without booting the authed app.

Sibling lesson already in `static-site-build-verify-loop.md`: "Status tint that double-codes live
output" (commit b66640e) — ONE signal coding two things. This file is the COMPLEMENT: TWO signals
disagreeing.

## Health BADGE contradicts the LIVE reading — two signals on different time horizons (commit 2f2c613)

Ford spotted inverter cards showing the green "All good" badge while OUTPUT NOW read "not producing
right now" — "how is not producing all good?". Root cause = two separate signals quietly disagreeing
because they're computed over DIFFERENT TIME HORIZONS:

- The **"All good" badge** is driven by `inv.status`, the BACKEND `api/inverters/peer_analysis.py`
  `analyze_cohort` verdict over a **14-day** daily-kWh window. It only flags `dead` after a trailing
  zero streak >= `DEAD_DAYS` (=2) while peers produced. So an inverter that stalled THIS MORNING
  keeps `status:"ok"` for up to ~2 days.
- The **"OUTPUT NOW"** line is `current_power_w` — an INSTANTANEOUS reading (SolarEdge live API; or
  the extension-captured snapshot for other vendors).
- The badge (`sandbox.js`, the teeth-render block: `isAlert = sCls !== "ok"` -> "All good" else the
  status label) never consulted the live number, so it parroted "All good" on an inverter dark right
  now. Two clocks, one card, silent contradiction.

### The fix — cross-check the slow verdict against the live signal, with a peer quorum
`liveVerdict(inv, peers, isDaylight)` in sandbox.js, applying the SAME peer-relative idea the backend
uses over days, but to RIGHT NOW:
- Producing (`curW > floor`, floor = max(25W, 1% of rated)) -> "ok".
- A confirmed health alert (`status != ok`) ALWAYS WINS and shows first — never override a real
  dead/fault/underperforming verdict with the live heuristic.
- Dark right now while a QUORUM of >=2 daylight siblings produce -> amber "Not producing — peers are"
  (a real-time anomaly the slow 14-day health hasn't caught yet).
- NO live reading at all (`curW == null`, distinct from a real 0) -> neutral "No live reading"
  (`.sb-inv-alert.info`, blue — added to styles.css) — a telemetry gap, NOT a confirmed power fault.
- Night/dusk (`is_daylight === false`, the server flag) -> always "ok"; zero is expected and owned by
  the Sleeping state, never alarmed.
- The >=2-lit-peer QUORUM prevents false alarms on a passing cloud-edge or a 2-inverter array —
  mirrors the backend's degenerate-cohort rule (a peer signal needs >=2 producing peers to mean
  anything). Below quorum -> "ok" (insufficient signal to judge).

GENERAL LESSON: when a STATUS/HEALTH indicator and a LIVE/INSTANTANEOUS reading sit on the same card,
confirm they're on the same time horizon — a slow peer/trend verdict (days) will keep saying "fine"
for a fault the live feed already shows. Make the badge CROSS-CHECK the live signal (with a peer
quorum so it can't false-alarm), let confirmed health alerts win, and distinguish a real live 0
(anomaly) from a missing reading (unknown/telemetry-gap, neutral). Whenever a card asserts health,
ask "computed over what window, and does it look at NOW?". Forward-looking product note offered to
Ford: make the card show liveness and health as TWO explicit, separate indicators rather than one
hybrid badge, so the next "how is X also Y?" contradiction can't arise.

## PROVE a card-render fix without the live app — brace-extract the REAL functions into a harness

The sandbox card UI is `/root/array-operator/public/sandbox.js` (vanilla JS, served inside index.html
behind auth — there is NO standalone `sandbox.html` to open). To visual-QA a card-render change
faithfully (per the own-the-visual-QA rule) WITHOUT standing up the whole authed app, build a harness
that EXTRACTS the real helper functions from sandbox.js by BRACE-MATCHING — never hand-copy, because
hand-copies drift from the shipped code and prove nothing. Recipe (proven Jun 2026):

- `grab(name)` walks from `function NAME(` to its matching `}` at brace-depth 0; `grabConst(name)`
  walks a `const NAME = {...};` to the depth-0 `;`. Pull every helper the card markup transitively
  needs (this session: BRAND / STATUS_LABEL / STATUS_CLASS, esc, _sparkTimeLabel, invSpark, pctTone,
  outputState, outputBar, liquidState, liveVerdict, liquidLayer, brandHTML, _seedRand). A MISSING
  TRANSITIVE DEP throws `ReferenceError: X is not defined` at render — grep sandbox.js for the
  undefined name and add it (e.g. invSpark needs _sparkTimeLabel).
- Slice the EXACT teeth-render snippet between two stable anchors in render() (the badge logic under
  test) and wrap it as `renderTooth(inv)` — so you exercise the SHIPPED template, not a paraphrase.
- Write an HTML file that `<link>`s the real `public/styles.css`, defines a `col` context (set
  `is_daylight:true` so daytime zeros are anomalies) + a hand-built fleet reproducing the bug (here:
  6 inverters all backend `status:"ok"`, two with `current_power_w:0`), and renders the cards.
- Screenshot with Playwright from a dir where `require('playwright')` resolves (use the absolute
  `/root/array-operator/node_modules/playwright`); capture `pageerror` / console-error into an array,
  and `$$eval(".sb-inv", ...)` to DUMP each card's name/output/badge text for a HARD textual
  assertion. The assertion catches the logic; `vision_analyze` on the PNG catches styling/contrast/
  clipping the selector counts miss — and a NEW badge class (`.info`) needs its own CSS in styles.css.
- Put the harness under a throwaway dir (e.g. `_audit/`) and `rm -rf` it before committing so it's
  not in the feature commit.

GENERAL: to prove a vanilla-JS render fix, reconstruct the render path from the SHIPPED source via
brace-extraction + a crafted fixture, assert the output text, then vision-review — far cheaper and
more faithful than booting the full app, and it can't drift from what actually ships.
