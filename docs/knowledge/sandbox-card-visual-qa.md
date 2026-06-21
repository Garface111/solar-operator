# Array Operator sandbox: the inverter-card UI, its bug class, and how to QA it

The live inverter-fleet visualization Ford calls the "sandbox visualization
system" is **`/root/array-operator/public/sandbox.js`** (vanilla JS, ~3300 lines,
NOT React; styles in `public/styles.css`). It renders the per-inverter cards
("Inverter N / sparkline / OUTPUT NOW % / status badge / vendor tag"). It is a
SEPARATE repo from solar-operator (Netlify-deployed; remote
github.com/Garface111/array-operator). The React app under
`solar-operator/web/app` is the NEPOOL Operator side and does NOT have these
cards.

Data source: `GET /v1/array-owners/fleet-tree` →
`solar-operator/api/inverter_fleet.py::build_fleet_tree`, peer-analyzed by
`api/inverters/peer_analysis.py`. The card strings ("All good", "not producing
right now", "OUTPUT NOW", "Sleeping") live ONLY in sandbox.js — they will NOT
turn up grepping solar-operator/web or the app_dist bundle. If you can't find a
live UI string in solar-operator, grep `/root/array-operator/public/`.

## The bug CLASS: conflated time-horizon signals ("two clocks")

The recurring defect: one card element is driven by a SLOW signal while another
is driven by a FAST signal, and the UI presents them as if they're one fact —
producing a self-contradiction the user spots immediately.

Concrete instance (the "All good while not producing" bug):
- The health BADGE was driven by `inv.status` — backend `peer_analysis` over a
  **14-day** daily-kWh window. It won't flag "dead" until `DEAD_DAYS=2` days of
  zero output. So a freshly-stalled inverter still carries `status:"ok"`.
- "OUTPUT NOW / not producing right now" is driven by `current_power_w` —
  **instantaneous**. The badge never consulted it.
- Result: green "All good" on a card reading 0 W. The two signals are on
  different clocks (14d vs now).

### The right fix: SEPARATE the axes, don't cross-wire them
Tempting quick fix is to make the badge also read the live value — but that
re-conflates two meanings into one label. The durable fix is to give each axis
its own labelled element:
- **NOW chip** (liveness): instantaneous, peer-checked. States:
  Producing / Idle (peers also idle → calm) / Not producing (dark while ≥2
  daylight peers produce → amber anomaly) / No signal (no reading) / Asleep
  (sun-down). Each carries a tooltip saying why it is/ isn't a concern.
- **HEALTH badge** (the 14-day peer verdict): purely `inv.status`. "All good"
  then means only one thing.
Now "● Not producing (now)" + "All good (health)" reads as two coherent facts,
not a contradiction. A live anomaly can still tint the whole card (data-tone) so
it draws the eye before slow health catches up.

Peer-check rule (mirror the backend's degenerate-cohort caveat): only raise a
live "dark" alarm when **≥2 sibling inverters are producing in daylight** — a
single cloud-edge or a 2-inverter array must not false-alarm. Gate all night
zeros behind `col.is_daylight === false` → "Asleep" (the Sleeping state owns
night zeros; never alarm them).

When you spot ANY card element that could disagree with another, ask: are these
two different time horizons or data sources wearing one label? If so, split them.

### Sub-pattern: a VERDICT shown without the EVIDENCE to back it (the "evidence guard")
A third instance of two-clocks, reported by Ford as "neighbors signage isn't
consistent." The HEALTH badge renders the peer verdict (`STATUS_LABEL[inv.status]`
→ "Below its neighbors"/"All good") from a 14-day `window_kwh` comparison — but
it was rendered even when the inverter had NO usable history (`window_kwh` null,
card literally shows "no history yet"). Result: a card reading "Below its
neighbors" while showing no history AND a healthy live OUTPUT NOW % right above it
— a self-contradiction. Worse, `FleetStore.recompute()` flagged the BOTTOM of any
healthy cohort (`pi < 0.85 × median`), so a tightly-clustered array always had its
lowest 1–2 members flagged even with no fault — and an 87%-now card could read
WORSE than a 75%-now one, because the badge is a different clock than the %.

The right fix = an **evidence guard** in the shared classifier (`recompute`), so
all three surfaces agree: only emit a real peer verdict when (a) THIS inverter has
real history (`nameplate_kw>0 && window_kwh>0`) AND (b) there are ≥2 producing
peers WITH history to form a cohort (`eligible.length>=2 && median>0`). Otherwise
set a NEUTRAL status `"monitoring"` (peer_index null, diagnosis "Gathering data…")
— claim nothing you can't show. Then thread `monitoring` through EVERY surface that
keys on status, or it leaks as a false alarm:
- `STATUS_LABEL.monitoring = "Monitoring"`, `STATUS_CLASS.monitoring = "info"`
  (reuse the existing blue-grey `.sb-inv-alert.info` — no new CSS); give the badge
  an honest tooltip ("not enough history yet to compare against its neighbors").
- `arrayHealth` + command-center `buildModel`: treat `monitoring` as NOT flagged
  (count it healthy, no flagged row, no priced $ loss) — `SEV.monitoring="ok"`,
  `alertFor` excludes it from `flagged`.
- `outputState` TINT: the card's whole-card amber tint fires via
  `(statusCls==="ok") ? "ok" : pctTone(pct)`. `monitoring`'s class is `"info"`, so
  a low-% Monitoring card would wrongly tint amber while a high-% one stays green —
  inconsistent again. Make the calm set `(statusCls==="ok" || statusCls==="info")`
  so a not-yet-judged card is calm regardless of its %. Only a genuinely flagged
  HEALTH status (warn/bad) may tint the card.
CRUCIAL: this must NOT suppress real alerts — an inverter WITH history running
~40% below evidenced peers still flags "Below its neighbors" (amber). QA proved
both: no-history cohort → all "Monitoring" (calm green), one real 40%-low unit →
still "Below its neighbors". Generalize: a card may only display a comparative
VERDICT when it can also display the EVIDENCE; if the data to judge isn't there,
show a neutral "gathering data" state, never a confident label.

## The TWO card tiers, and mirroring the inverter card UP to the array card
sandbox.js renders TWO card tiers, NOT one:
- **Inverter tooth** (`.sb-inv`, inside the collapsible `.sb-teeth` comb): the
  per-inverter card. Construction = liquid-fill energy layer (`liquidLayer`)
  behind a frosted plate (`.sb-inv-plate`, z-index 3) → name+size → sparkline
  (`invSpark`) → NOW chip (`liveState`) → output bar (`outputBar`) → health badge
  (`inv.status`) → vendor badge (`brandHTML`).
- **Array card** (`.sb-array`, the column header, ALWAYS visible): the per-array
  card. As of Jun'26 it is the inverter card's BIGGER SIBLING — same construction,
  every signal aggregated to the whole array. (It previously had a different
  two-inner-column "details | Alerts" body; Ford had that Alerts column removed
  and the card rebuilt to mirror the inverter tooth. Do NOT reintroduce the alerts
  column.)

When Ford asks to "make the array card match the inverter's construction/design"
(or add a level-appropriate version of an inverter element), the durable pattern
is **build array-level MIRROR helpers that reuse the inverter primitives**, not
copy-paste-and-tweak the markup:
- `arrayOutputState(invs, healthTone)` — sums each inverter's `current_power_w`
  and its (real OR `estNameplateW`-estimated) max → the array's AVERAGE production
  level as a % of combined capacity. Reuses `estNameplateW`/`pctTone` so the array
  % uses the SAME math as the tooth %.
- `arrayLiveState(col, os, healthTone, liveAnoms)` — the array's NOW chip
  (Producing / Idle / Asleep / Not-producing), gated by `col.is_daylight` for the
  Asleep state, amber "Not producing" ONLY when health is already flagged or
  `liveAnoms>0` (no independent cloud false-alarm at array scale).
- `arrayOutputBar(os)` — mirrors `outputBar`; emit the SAME `data-curw`/`data-maxw`
  attributes so the existing live ticker breathes the array bar in place too.
- `arrayLiquidLayer(col, os, healthTone)` — mirrors `liquidLayer`; seed bubbles/
  stars with `"a"+array_id` so they're stable across the 60s re-render.
- HEALTH badge for the array = the shared `arrayHealth(col)` verdict (`.tone`,
  `.flagged`, `.crit`) — same two-clocks rule, NOT re-derived.
Markup: wrap content in a `.sb-array-plate` (mirror of `.sb-inv-plate`, z-index 3)
over `${arrayLiquidLayer(...)}`, give `.sb-array` `overflow:hidden` +
`data-tone="${aCardTone}"` so the inverter card's `[data-tone]` tint rules apply.

PITFALL (z-index): the frosted plate is z-index 3 and covers the whole card.
`.sb-drag` (the ⠿ reorder grip) is `position:absolute` with NO z-index, so it
falls BEHIND the plate and becomes unclickable. Bump `.sb-drag` to `z-index:5`
when you put a plate on the array card.

PITFALL (live ticker scope): `startLiveTicker()` retones the card via
`bar.closest(".sb-inv")`. Array bars live in `.sb-array`, so broaden it to
`bar.closest(".sb-inv, .sb-array")` or the array card's tint won't track live.

When removing a card section (e.g. the Alerts column), grep the WHOLE repo for
the classes/vars first (`sb-alert `, `col.alert`, `ALERT_CLASS`) to confirm
nothing else reads them — the per-array `.sb-alert*` block was independent of the
`.sb-alerts-*` email-settings modal + 🔔 header button (those stay). Also tell
Ford the now-unused backend field (`col.alert`) is harmless but no longer
surfaced.

## The bug CLASS: mixed units from a missing-denominator fallback
Symptom Ford/Bruce report as "some cards show % and some show kW — make them all
%." It is NOT a vendor split and NOT random: the "OUTPUT NOW" element shows a
percentage only when the inverter has a `nameplate_kw` (rated max) to divide by;
when nameplate is absent it falls back to a bare "X.X kW · producing now"
because there's no denominator. So the split tracks "do we have a nameplate,"
which happens to correlate with vendor (Chint reports none; SOME SolarEdge sites
also report none) — hence the appearance of an arbitrary mix. All render paths
(`outputBar`, `liquidState`/`liquidLayer` fill height, `liveState`) funnel
through **`outputState(inv, statusCls)`**, which returns `pct:null` exactly when
`nameplate_kw == null`. Fix `outputState` once and every surface follows.

### The right fix: reuse the backend's inferred denominator, label it honestly
The backend ALREADY infers a missing nameplate for its health math —
`api/inverters/peer_analysis.py::_infer_nameplate` = `peak_daily_kwh / 4` (≈ a
4 kWh/day-per-kW temperate ceiling) — but `inverter_fleet.py` throws that
inferred value away when building the card payload (sends `nameplate_kw:null`).
The card payload DOES carry `inv.daily` (the kWh series), so the durable,
low-risk fix is FRONTEND-ONLY: have `outputState` compute the same estimate from
`inv.daily` when `nameplate_kw` is null, mirroring `peak/4` EXACTLY so the card %
matches the health verdict. Pattern:
- add `estNameplateW(inv)` = `(max(daily.kwh>0) / 4) * 1000`, or null if no history;
- in `outputState`, `maxW = realW ?? estW`, return an `estimated` flag + `maxW`;
- in `outputBar`, when `estimated` show "of ~est · cur/~max kW" (+ a `title`
  tooltip "max estimated from peak production") so a guess is NEVER passed off as
  a hardware spec — Ford trust-checks output and wants the estimate visible;
- a bare-kW last resort remains ONLY when there's live output but zero history to
  estimate from (rare; flag it in your QA assertion as expected-not-a-fail).
Prefer this frontend mirror over a backend/Railway change unless Ford explicitly
wants TRUE %s (then the deeper fix is capturing the real nameplate from the
vendor inventory API into the DB — bigger change, needs a live capture run; offer
it as a follow-up). DECISION POINT worth a clarify(): real-but-estimated % vs
strictly-literal kW vs deeper capture fix — Ford picked estimated-%-marked-honest.

### Generalize: a fallback that swaps the UNIT, not just the value, breaks consistency
Whenever a render branch falls back to a different unit/representation when one
input is missing (% → kW, $ → "n/a", date → "unknown"), expect the user to read
the mix as "broken." Prefer estimating the missing input from data already in the
payload (and labelling the estimate) over switching units. Check whether the
backend already computed the missing value for its OWN logic and you're just
discarding it on the way to the UI — that was the case here.

## Adding a NEW card interaction (gesture) — coexist with pan/zoom/drag
The sandbox canvas is a map-style view, NOT a scrolling page. Before adding any
gesture, know what the inputs ALREADY do (all wired in `render()` near the bottom):
- **Wheel = ZOOM the fleet** (`wirePanZoom`, bubble-phase `wheel` on `.sb-viewport`,
  `e.preventDefault()`). Shift+wheel = horizontal pan. So "scroll" does NOT scroll
  the page here — a new wheel gesture FIGHTS the zoom unless you sequence it.
- **Left/middle drag = pan** the canvas; **drag a `.sb-inv` card = reorder/move**
  it (`wireInvDrag`, PERSISTED to backend); **drag `.sb-array .sb-drag` grip =**
  reorder the column. Click a card = open detail (`showDetailCard`).
- `pointerdown` on `.sb-inv,.sb-drag,.sb-comb-empty` is deliberately excluded from
  panning so those stay grabbable.

Recipe that worked for "scroll-to-lift" (wheel over a card raises it toward you,
progressive + capped, click-off settles — `wireCardLift`):
- Bind the new `wheel` listener on `.sb-viewport` in **CAPTURE phase**
  (`{capture:true, passive:false}`) so it runs BEFORE the bubble-phase zoom. While
  you're consuming the gesture, `e.preventDefault()` + `e.stopPropagation()` so the
  canvas doesn't zoom underneath. When you're DONE consuming (e.g. lift hit its
  cap, or scrolling the "other" direction with nothing to undo), just `return`
  WITHOUT preventDefault → the event keeps bubbling and zoom takes over. This
  "consume-then-pass-through" is what keeps a wheel gesture from feeling trapped.
- Drive the visual with a CSS custom property the JS sets (`--lift` 0..1) and a
  `calc()`-based `transform`/`box-shadow`, placed in styles.css AFTER `.sb-inv:hover`
  so it wins on equal specificity (the hover rule has a competing `translateY`).
  Add a `.sb-settling` class with a springy `transition` for the return, and clear
  the class + custom prop on `transitionend` (with a `setTimeout` fallback for
  `prefers-reduced-motion`, where the transition may not fire). Always include a
  `@media (prefers-reduced-motion:reduce)` branch.
- `render()` re-runs and rebuilds `.sb-viewport` each time, so guard any
  DOCUMENT-level listener (e.g. the pointerdown that settles on click-off) behind a
  module-level "already bound" flag, and a per-element `vp._liftWired` flag, so
  re-renders don't stack duplicate listeners.
- DISCOVERABILITY caveat to raise with Ford: a novel wheel gesture competes with
  the expected wheel=zoom, so it may surprise users. Offer a modifier-gated variant
  (Alt+wheel) or a hint as a follow-up after he/Bruce feel it live.
- OUTCOME (Jun'26): scroll-to-lift was built, QA'd, shipped — then Ford reverted it
  ("doesn't belong"). Lesson: novel canvas gestures are a TASTE call Ford makes by
  FEELING it live, not a spec you can satisfy up front. Build it clean and reversible,
  but expect a real chance it gets pulled. Keep such a feature SELF-CONTAINED (one
  `wireX` fn + one call site + one labelled CSS block) precisely so a revert is a
  clean 3-cut removal. Do NOT re-propose a reverted gesture in a later session.

### Cleanly REVERTING a self-contained feature (the inverse of adding one)
When Ford says "undo X / it doesn't belong," remove ALL of it surgically — don't
just unwire it and leave dead code:
1. `grep -nE` the feature's every symbol across BOTH files (`wireCardLift|_liftCard|
   sb-lifted|sb-settling|--lift|LIFT_STEP`) to map every site — the JS fn+state, the
   `render()` call line, and the CSS block.
2. Remove the JS block, the call site, and the CSS block with separate `patch` edits.
3. PITFALL (don't invent the "next line"): when patching out a block, your
   replacement's trailing context must be the ACTUAL following code, not a plausible
   guess. I wrongly wrote `function wireCardButton(host){` as the line after the
   removed block when the real next code was `/* '+ Add array' */ function
   wireAddButton(`, producing a brace mismatch (`SyntaxError: Unexpected token ')'`
   at the IIFE close). Re-read the lines immediately AFTER the block first, or use a
   tight `old_string` whose tail is verbatim from the file.
4. VERIFY removal is total: `node -e "new Function(fs.readFileSync('sandbox.js'))"`
   parses, and `grep -cE '<every symbol>'` is 0 in both files. Then the same boot
   check (`scripts/diag-sandbox-boot.js`) + deploy + curl-the-live-asset-for-0-refs.

### QA an interaction (not just a static card): dispatch REAL events
The faithful-harness pattern below extends to behaviour. Slice the gesture code
out of sandbox.js by anchors (e.g. `let _liftCard = null` through the end of
`wireCardLift` by brace-match) + the matching CSS rules from styles.css, drop them
in a minimal viewport/card DOM, then in Playwright dispatch real input:
`page.mouse.move(cx,cy)` then `page.mouse.wheel(0,-120)` per tick, read state via
`page.evaluate`, and HARD-assert the mechanics (one tick = expected step, caps at
1.0, click-off clears the class AND the custom prop, zero `pageerror`s). THEN set
`--lift` to a mid value and `vision_analyze` the screenshot to confirm it reads as
real depth (elevation shadow), not a muddy/clipped transform. Same `_audit/` +
`rm -rf` throwaway discipline.

## How to VISUALLY QA a sandbox.js card change (faithful harness pattern)

The live app is behind auth, so you can't just open it. Build a throwaway
harness that renders the REAL card markup with the REAL CSS against crafted data
— never hand-copy the render logic (it diverges). Steps:

1. **Extract the real helpers by brace-matching** from sandbox.js — don't retype
   them. A small Node script reads sandbox.js as text and pulls
   `function NAME(...){...}` blocks (match braces from the header to depth 0) and
   `const NAME = {...};` blocks (to the depth-0 semicolon). Pull the card's
   dependency closure: esc, invSpark (+ its helper `_sparkTimeLabel`), pctTone,
   estNameplateW, outputState, outputBar, liquidState, liveVerdict, liveState,
   liquidLayer, brandHTML, _seedRand, and the consts BRAND / STATUS_LABEL /
   STATUS_CLASS.
   For an ARRAY-card change also pull: arrayHealth, arrayGraph, weatherBadge
   (+ synthWx/_hashStr/wxFromWmo/WX it needs), originLinks/originLinksHTML/
   brandLinkHTML (+ const PORTAL_URL), and the array mirrors arrayOutputState/
   arrayLiveState/arrayOutputBar/arrayLiquidLayer.
   PITFALL: helpers call other helpers (invSpark→_sparkTimeLabel). A
   `ReferenceError: X is not defined` + empty render means you missed one — add
   it and re-run.
   PITFALL (double-declare wipes everything): if your extractor PREPENDS a
   stub const (e.g. `const SB_WINDOW_DAYS=14;`) AND the harness HTML also defines
   it inline, you get `Identifier 'X' has already been declared` — a parse error
   that nukes the ENTIRE helpers.js so `window.__dump` is `undefined`. Define
   shared stubs in exactly ONE place. `dollarVal`/ENERGY_RATE/REC_PER_MWH are arrow
   consts the extractor's `function NAME` matcher won't catch — stub them in the
   harness (the array card shows no $, so a rough stub is fine).
2. **Lift the exact teeth-render template** as a slice between two stable anchors
   in sandbox.js (`const sCls = STATUS_CLASS[inv.status] || "ok";` … `}).join("")`)
   and wrap it as `function renderTooth(inv, col, sortedInvs){ <slice> }`. This
   keeps the markup byte-faithful to the live template.
3. Write `harness.html` that `<link>`s the real `../public/styles.css`, defines a
   `col` context (set `is_daylight` true/false to test day vs night), a crafted
   fleet reproducing the bug (e.g. backend `status:"ok"` + `current_power_w:0`
   for the "dark" cards), and renders one card per inverter into `.sb-teeth`.
4. **Screenshot + ASSERT + vision-verify** with Playwright
   (`/root/array-operator/node_modules/playwright`, chromium is installed):
   `deviceScaleFactor:2`, capture `pageerror`/console errors, screenshot
   fullPage, and `$$eval(".sb-inv", …)` to dump each card's NOW chip text/tone,
   health badge text, and `data-tone` for a HARD assertion (not just eyeballing).
   For ARRAY cards, the selector is `.sb-array` / `.sb-col` (NOT `.sb-inv`) —
   dump per-`.sb-col`: NOW chip text+class, output `.sb-ob-head` text+class, health
   badge text+class, `.sb-array data-tone`, the `.sb-array-src` link hrefs, and
   booleans for liquid/graph/weather presence. Assert the two-clocks split holds at
   array scale (a partly-down array reads NOW "Producing" from survivors yet HEALTH
   "N down"/bad — that is CORRECT, not a bug), single-vendor source is a clickable
   portal link, and mixed-vendor exposes ≥2 portal links.
   Then `vision_analyze` the PNG to catch clipping/overflow/contrast the DOM
   dump can't (e.g. a chip wrapping to two lines → fix with `white-space:nowrap`).
5. Put the harness in a throwaway dir like `_audit/` and `rm -rf` it before
   committing — never commit the harness.

This satisfies Ford's standing rule (own visual QA on EVERY UI change) without
needing the live authed app, and the brace-match extraction means the harness
can't silently drift from production code.

## Deploy — Ford's standing rule: commit AND deploy EVERY time, don't ask
For any array-operator UI change, the default is: git commit+push, THEN the manual
netlify deploy below, THEN curl-verify the live asset. He set this as a permanent
preference ("commit and deploy everytime") — never leave it at just a commit.

## Deploy — MANUAL, git push is NOT enough (this bit us once)
array-operator is Netlify but is **NOT git-connected** — it deploys via the
Netlify CLI. The signature: `.netlify/state.json` has ONLY a `siteId`, there's no
`netlify.toml` at repo root, and `netlify status` shows the project. So
`git push` updates GitHub ONLY; the live site at **arrayoperator.com** stays
stale until you run:

    cd /root/array-operator && netlify deploy --prod --dir=public \
      --message "what changed"

Publish dir is **`public/`** (files serve at `/sandbox.js`, NOT `/public/...`;
repo root has no index.html). Project=`array-operator-ea`, prod=arrayoperator.com
(also nepooloperator.com signage; `.org`/`.netlify.app` 404). API is proxied:
`/v1/*` → web-production-49c83.up.railway.app (redirect in `.netlify/netlify.toml`).

ALWAYS verify the deploy actually served the change — do NOT trust "Deploy is
live!" alone. curl the live asset for a marker string:

    curl -s https://arrayoperator.com/sandbox.js | grep -c sb-inv-now

If the live count is 0 but the committed file has it, the deploy didn't run / the
browser is caching → redeploy and tell Ford to hard-refresh (Ctrl+Shift+R).
Netlify keeps deploy history, so `--prod` deploys are rollback-able. NOTE this is
the SAME site serving Bruce's live pilot.

### HAZARD: `netlify deploy --dir=public` ships the WORKING TREE, not a commit
`netlify deploy` uploads on-disk files regardless of git state. Combined with the
multi-agent tree (another agent often has UNCOMMITTED edits to sandbox.js — see
the backend-safe-edit ref), this has TWICE shipped a half-finished tree and broken
live. The signature you'll see: live `sandbox.js` matches NEITHER `HEAD` NOR the
working tree (a third, in-between version), and `git status` shows the deployed
files still `M` (uncommitted) — i.e. the running code matches NO commit. Before
AND after any deploy: `git status --short` and diff the live asset against HEAD
and the working tree (`curl … -o /tmp/live.js; diff /tmp/live.js public/sandbox.js`).
After a deploy that fixes a break, COMMIT the deployed tree immediately and
`git push origin HEAD:main` so the live state matches a real commit — a
deployed-but-uncommitted fix is the exact state that caused the break and a later
`git checkout`/clean-tree deploy will silently re-break it. Worth proposing to
Ford: a deploy guard that refuses on a dirty tree (`git diff --quiet || exit 1`).

#### The positive recipe: deploy a CLEAN EXPORT of HEAD, never the live tree
When your change is already committed but the working tree ALSO carries another
agent's uncommitted WIP (the normal multi-agent state — `git status --short`
shows `M public/sandbox.js`/`fleet-store.js` you didn't write), do NOT
`netlify deploy --dir=public` (ships their half-done work to Bruce's live pilot)
and do NOT `git stash`/`checkout` their files (destroys their work). Instead
export just the committed HEAD to a temp dir and deploy THAT:

    git archive HEAD public/ | tar -x -C /tmp/ao-deploy
    # sanity: your change present, their WIP absent, proxy rules travel along
    grep -c '<your-marker>'   /tmp/ao-deploy/public/sandbox.js   # >0
    grep -c '<their-WIP-marker>' /tmp/ao-deploy/public/sandbox.js # 0
    test -f /tmp/ao-deploy/public/_redirects && echo ok          # /v1/* proxy + pretty URLs live in public/_redirects, so they ship with the export
    netlify deploy --prod --dir=/tmp/ao-deploy/public --message "…"

This ships exactly the committed code and nothing else. NOTE: Netlify may report
"0 files uploaded / Deploy is live!" when the CDN already cached those exact bytes
— that is NOT proof your change is live; still curl the live asset for your marker
(the deploy-verify rule above) before claiming done. Ford set "commit AND deploy
every time" as a standing rule, BUT in a shared repo "deploy every time" means
deploy a clean export of HEAD, not the dirty tree — surface this to him and make
the clean-export the default.

### The "sandbox isn't loading" bug CLASS: orphaned reference after partial edit
"Not loading" = a blank canvas = an uncaught `ReferenceError` thrown DURING
render (the API is fine; check `/v1/array-owners/fleet-tree` returns 401 unauth
and Railway `/health` is 200 to rule the backend out first). The recurring cause
is an INCONSISTENT HYBRID: an in-flight redesign removed a variable's DEFINITION
but a deploy shipped a tree where the OLD markup still REFERENCES it. Concrete
instance: the array-card redesign deleted `const a = col.alert` + `const aCls =
ALERT_CLASS[...]`, but the deployed sandbox.js still had `<div class="sb-alert
${aCls}">…${esc(a.headline)}` → `ReferenceError: aCls is not defined` on every
array card → whole sandbox blank.
- DIAGNOSE FAST by static analysis, not guesswork: grep the suspect file for the
  symbol's REFERENCES vs its DEFINITIONS. If refs > 0 and defs == 0, that's the
  crash. e.g. `grep -cE 'aCls|a\.headline' live.js` vs `grep -cE 'const aCls ='
  live.js`. A consistent file has either both or neither.
- The FIX is to deploy a CONSISTENT version — usually the agent's complete
  working tree (verify it has 0 orphaned refs and `node -e "new Function(fs…)"`
  parses) — NOT to hand-add the missing defs back into a file that was mid-rewrite
  (that fights the redesign). This ties directly to the deploy-working-tree hazard
  above: the break and the fix are the same root issue.
- Verify the fix the REAL way: boot the live page in Playwright and assert zero
  uncaught `pageerror`s with `#sandbox` rendering >1KB of content (auth/resource
  404s are expected and fine). See `scripts/diag-sandbox-boot.js`.
  PITFALL: a stubbed-FleetStore harness often falls into the empty/skeleton branch
  and renders ~668 chars for ALL versions, so it can't DISTINGUISH broken from
  fixed — don't rely on it to prove the regression. Static ref-vs-def grep is the
  decisive proof; the live boot check is the decisive verification.

Reusable boot/consistency check: `scripts/diag-sandbox-boot.js`.

## Downstream consistency check — DONE (keep it that way)
The grid/overview tiles (`sandbox.js::arrayHealth`) and the command center
(`command-center.js::buildModel`) read the same FleetStore (`public/fleet-store.js`)
but used to roll up off `inv.status` alone — so a live anomaly didn't surface
there until 14-day health flagged it. FIXED by a SINGLE shared classifier in
FleetStore: `liveVerdict(inv, peers, isDaylight)` / `isProducing` /
`isLiveAnomaly`, exported and called by all three surfaces so they can't drift.
The command center promotes a `status:"ok"` live anomaly to a `live_dark` row
(lowers fleet-healthy %, "N to check" with no $ claimed until health confirms).
When adding ANY new live-vs-health signal, route it through FleetStore's shared
classifier — never re-derive per surface. E2E-verify by stubbing `/fleet-tree`
in a Playwright harness that loads the three real scripts (as index.html does)
and asserting all three surfaces agree.
