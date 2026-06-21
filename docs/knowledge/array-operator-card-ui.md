# Array Operator inverter-card UI (sandbox.js) — render patterns, the liquid fill, visual-QA discipline

The owner-facing fleet canvas lives in `/root/array-operator/public/` (vanilla JS, NOT React —
the React spec'd components must be PORTED, not pasted). Cards render in `sandbox.js`
(`renderColumns` → per-inverter teeth), styled in `styles.css` (`.sb-inv*`), data plumbed through
`fleet-store.js` (`adaptTree` AND `toColumns` — BOTH rebuild the column object and silently DROP any
field you don't explicitly carry; when threading a new backend field like `daily`/`is_daylight` to a
card, add it in BOTH). Deploy: `netlify deploy --prod --dir public --site 966cb1f5-944e-41fd-855b-10053edc5d18`.

## The liquid-fill energy cards (design spec: Desktop liquid-cards/INTEGRATION-SPEC.md)
A bubbling fill rises BEHIND a frosted plate (`.sb-inv-plate`, z-index 3, bg opacity .82 so text
never washes out); fill height = capacity factor (current_power_w / nameplate*1000) — the SAME number
behind "Output now %", no new data. Implemented as `liquidLayer(inv, sCls, col.is_daylight)` +
`liquidState()` in sandbox.js. States + their fill color:
- ok = green; **clip/at-max (>=98%) = GREEN too** (brighter/fuller, NOT blue — "full output is GOOD,
  not a separate cool state"; Ford corrected an early blue clip). low = amber; fault = orange + still.
- **sleep** = calm indigo resting pool (~14% height) + moon, NO bubbles. Gated on
  `is_daylight===false AND current_power_w<=1` — NEVER zero-output alone (a noon fault that zeroes
  output must stay alarming, not read "asleep"). `is_daylight` comes from the backend fleet-tree
  (see extension-capture-mv3-debugging.md §6g-ter — real NOAA solar elevation, not a fixed hour).
Spec bug-list (all honored, keep honoring): plate opacity >=.8; deterministic per-serial bubble seed
(`_seedRand(inv.inverter_id)`) so bubbles DON'T teleport on the 60s poll; cap 6 bubbles;
`content-visibility:auto` on `.sb-inv` for 40+ card arrays; `prefers-reduced-motion` kill-switch;
`aria-hidden` on the decorative liquid.

## PITFALL: frosted plate must FILL the card height, or content looks "slid up" (Jun 2026)
In a `.sb-teeth` flex row, cards stretch to EQUAL height, but a normal-flow plate only grows to its
content. So a card with shorter content (1-line name, or shorter status pill like "All good" vs the
taller "Below its neighbors") leaves a strip of green liquid EXPOSED below the plate → content looks
shoved to the top. FIX: make `.sb-inv` `display:flex;flex-direction:column` and the plate
`flex:1 1 auto` so the plate always reaches the card's bottom edge regardless of content length. The
green then only shows as the intended thin rim around the plate. (The card already had
`position:relative; overflow:hidden` for the liquid to clip to rounded corners.)

## The Arrays-tab TOP BAR = Fleet Commander (command-center.js, NOT sandbox.js)
The bar above the sandbox on the Arrays tab is `#fleetCommander`, rendered by
`command-center.js` `render()` (NOT sandbox.js). Layout: `.fc-card` flex row of
`.fc-health` (big %) | `.fc-mid` (the health meter + counts) | `.fc-right` (recoverable $/mo,
"updated Ns ago"). Live 60s repaint is `paintKpis()` which patches by `data-kpi="..."`
attributes WITHOUT a full re-render — so any element it touches (e.g. the health meter,
`[data-kpi="healthmeter"]`, where it only sets `.style.width`) MUST keep that attribute when
you restyle it, or live updates silently stop.

### HORIZONTAL liquid energy bar (reuse the inverter liquid metaphor sideways) — Jun 2026
Ford asked for the inverter cards' liquid-fill "but running horizontally" as the top-bar
health meter. Pattern that worked: replace the flat `.fc-meter`/`.fc-meter-fill` with
`.fc-liquid` (frosted track) + `.fc-liquid-fill` (left→right fill, width = % healthy) reusing
the SAME visual language as `.sb-liquid` but rotated:
- traveling meniscus = `::before`/`::after` `radial-gradient` crest with `fcliqwave` translateX
  keyframes (the horizontal analogue of `sbliqwave`); bubbles = `.fc-liq-bubbles span` rising
  with `fcliqrise` translateY.
- tone variants mirror the card language: ok=green, warn=amber, bad=orange gradients; add
  `prefers-reduced-motion` kill-switch. KEEP `data-kpi="healthmeter"` on the fill div (see above).
- update the DAY-THEME rule too: `theme-day.css` styled `.fc-meter` track → repoint to
  `.fc-liquid`. Grep `fc-meter` across `public/*.css` + `*.js` after — leftover refs = the tell.

### THE WHOLE-BAR TANK + its 3-redirect evolution → FINAL shipped design (Jun 2026)
The `.fc-meter`/`.fc-liquid*` widgets are GONE. The top-bar health viz went through THREE Ford
redirects before landing — the trail (so you don't re-walk it):
  1. "liquid bar running horizontally" → I built a thin in-card horizontal fill widget (`.fc-liquid`). WRONG.
  2. "make that bar a box behind the top bar, fill the ENTIRE top bar from below" → whole-bar `.fc-tank`,
     bottom-up `height:<pct>%`, behind the content. CLOSER but he then said:
  3. "make it fill up horizontally and boost the contrast" → FINAL: whole-bar tank, but fills
     LEFT→RIGHT (`width:<pct>%`) + a frosted contrast scrim. Shipped `3915a8a`.
THE FINAL PATTERN (`.fc-tank`, what's live now):
- `.fc-card` = tank container: `position:relative; overflow:hidden`, padding trimmed 14→11px
  ("make the bar a little smaller").
- `.fc-tank` absolutely pinned LEFT edge (`left:0; top:0; bottom:0; height:100%; z-index:0`),
  `width:<pct>%`, left corners rounded. Horizontal gradient. Leading-edge crest = `::before/::after`
  `fctankwave` translateY (vertical crest riding the RIGHT/leading edge); bubbles `fctankrise`.
  Tone classes `.fc-tank--ok/--warn/--bad` + day-theme `.fc-tank--*` (also horizontal gradients).
- CONTRAST = the inverter-card FROSTED-PLATE trick applied as a full-card scrim: `.fc-card::after`
  is a translucent dark gradient at `z-index:1` (above liquid, below text); `prefers-reduced-motion`
  n/a (static). This is what "boost the contrast" meant — a scrim, not just darker liquid. Day theme
  uses a translucent WHITE scrim instead.
- ALL content rides ABOVE both: `.fc-card > .fc-health, > .fc-mid, > .fc-right{position:relative;
  z-index:2}` (z2, above the z1 scrim).
- LIVE REPAINT AXIS: `paintKpis()` sets `[data-kpi="healthmeter"].style.width` (the bottom-up
  detour briefly used `.height` — it's back to `.width`). Keep `data-kpi="healthmeter"` on `.fc-tank`.
- QA at MULTIPLE fill levels: ~92% (near-full) AND a 60–62% bad state — at low fill the leading edge
  sits mid-bar and the right portion is OVER the empty zone, so confirm text there still reads
  (the scrim is what makes it work).

#### Two follow-up corrections after the tank shipped (Jun 2026) — COLOR-MATCH + the scrim-dulls-saturation trap
After the horizontal tank landed Ford asked two more refinements, both reusable:
- "make the green match the sandbox" → the tank's `--ok` gradient had DRIFTED to a lighter mint
  (`rgba 24,175,92 / 80,240,150`). FIX = snap it to the EXACT `.sb-liquid` green values
  (`rgba 24,165,86 / 46,213,115`). GENERAL RULE for "match X's color": don't eyeball-pick a new
  green — grep the SOURCE element's rule (`grep -n "sb-liquid{" styles.css`) and copy its literal
  rgba stops. QA = render the new element + the source element as side-by-side swatches and
  vision-compare hue (note the scrim makes the matched one read slightly darker — that's expected,
  compare underlying hue not final lightness).
- "brighten it" (the amber/warn) → the CONTRAST SCRIM darkens whatever's under it, so a saturated
  warn-amber read as MUTED OLIVE while green/red still popped. FIX is TWO moves: (1) boost the warn
  liquid to a vivid gold (`rgba 245,158,11 / 255,193,64`), AND (2) LIGHTEN the scrim FOR THAT STATE
  ONLY — `.fc-card.warn::after{background:linear-gradient(90deg,rgba(8,12,18,.3),rgba(8,12,18,.2))}`
  (vs the default `.46/.32`). LESSON: a uniform dark contrast scrim costs more saturation on
  mid-luminance colors (amber/gold) than on already-dark (red) or already-bright (green) ones —
  if one tone reads muddy after adding a scrim, per-state-lighten the scrim there rather than only
  cranking the liquid. Shipped `5b7178c` (green) + `8980355` (amber).

#### FINAL redirect → the literal TWO-CARD overlapping system (frosted PLATE over tank), Jun 2026
After the horizontal tank + scrim + color work, Ford asked for the bar to use "the overlapping card
system like the inverter has — top card slightly see-through, bottom card is the energy tank." This
is the 4th redirect and it REPLACED the flat `.fc-card::after` contrast scrim with a REAL inset DOM
card, exactly mirroring the inverter `.sb-inv` (tank) + `.sb-inv-plate` (frosted top) structure.
THE SHIPPED STRUCTURE (`dea9750`, what's live now):
- `.fc-card` = bottom layer / tank container: `position:relative; overflow:hidden`, padding now
  just `5px` (the inset margin for the plate) — it NO LONGER lays out the content via flex.
- `.fc-tank` = the liquid (unchanged: `width:<pct>%`, horizontal, `z-index:0`, leading-edge crest).
- `.fc-plate` = NEW top card wrapping ALL content (`.fc-health`+`.fc-mid`+`.fc-right`): the flex row
  moved here. `position:relative; z-index:2; background:rgba(11,16,23,.62); backdrop-filter:blur(7px);
  border:1px solid rgba(255,255,255,.06); border-radius:12px` — i.e. the EXACT `.sb-inv-plate` recipe
  (translucent dark + blur) so the energy glows through the plate AND shows as a colored rim in the
  5px gap around it. The OLD flat `.fc-card::after` scrim + its per-state `.fc-card.warn::after`
  lighten rule were DELETED (the plate is the contrast mechanism now). Day theme: `.fc-plate` gets a
  translucent-white bg (was `.fc-card::after`).
- MARKUP CHANGE in command-center.js `render()`: wrap the three content blocks in one
  `<div class="fc-plate">…</div>`. (The two follow-up scrim per-state lightening corrections above are
  now MOOT — the plate replaced the scrim — but kept as the saturation-vs-scrim lesson.)
KEY LESSON: when Ford says "like the inverter card has" / points at an existing component, build the
LITERAL same DOM structure (a real inset translucent child card), NOT a CSS approximation (a `::after`
gradient). The earlier scrim "worked" visually but wasn't the two-LAYER thing he was picturing; the
real plate is. Same "mirror the referenced element's exact structure" rule below, applied one level
deeper — DOM layers, not just colors.

#### 5th refinement → bubbles must travel ALONG the fill axis (Jun 2026)
After the plate landed, Ford: "the bubbles need to be rotated so they point toward the right side
of the bar as well." When the tank flips from vertical (bottom-up) to HORIZONTAL (left→right), the
bubble animation must flip with it — bubbles should drift toward the LEADING EDGE of the fill, not
keep rising up. FIX: `fctankrise` keyframe `translateY(-Npx)` → `translateX(+Npx)`; anchor bubbles
at `left:0` (was `bottom:0`); and SPREAD them across varied vertical positions via
`:nth-child(n){top:..%}` so they don't all ride one line. GENERAL RULE for any fill-viz: the
bubble/particle drift direction is COUPLED to the fill direction — whenever you change the fill axis
(vertical↔horizontal) you must rotate the bubble motion + re-anchor + re-spread to match, or the
bubbles look like they're defying the liquid. (One more redirect in the same fill-viz set — Ford
notices axis-inconsistency between the fill and its particles.)

LESSON (Ford visual-fill asks — applies to AO cards AND the Mindspace canvas): a "liquid/fill/energy
bar" ask is wrong on up to THREE axes he cares about and will redirect on each: (a) FILL DIRECTION
(thin widget vs whole container; bottom-up vs left→right), (b) DEPTH (discrete bar vs liquid BEHIND
content), (c) CONTRAST (raw text on liquid reads poorly — he wants a frosted scrim/plate so text
stays crisp over any fill level, the SAME trick the inverter cards already use). Mirrors his
"build exactly what he described, minimally" + "don't pile elaboration on the first guess." BEST
move on a fill-viz ask: confirm direction+depth+contrast up front, OR ship minimal and expect ~3
redirects. When he points at an existing element ("that liquid bar/card"), MIRROR its exact
structure (frosted-plate-over-liquid) rather than inventing a parallel — that's the design he's
referencing.

### Relocating a control BETWEEN the two scripts (button move) — decoupled pattern
Moving the 🔔 Alerts button from the sandbox head into the top bar: the alerts SETTINGS MODAL
lives in sandbox.js (`openAlertsModal`, inside its IIFE). Don't duplicate it. Instead expose it
once — `window.__sbOpenAlerts = openAlertsModal` near the existing `window.__sbLoad` export —
then in command-center.js add the button to `.fc-right` and wire `onclick` to call the global
(guard with `typeof window.__sbOpenAlerts === 'function'`). Remove the old `#sbAlerts` button
from sandbox.js; its `wireAlerts()` already `if(!btn) return`s so nothing breaks. General rule:
the two AO frontends talk via `window.__sb*` globals + the shared `FleetStore` — cross-script
features should reuse those seams, not re-implement the dialog/logic on both sides.

### "Make ALL the fluid real WebGL sim" — SPIKE before converting (Jun 2026)
Ford floated a "bold idea": make every CSS liquid fill on the page an actually-simulated
energy fluid. The TRAP that decides everything: the demo fleet renders up to 100 arrays ×
N inverter cards = HUNDREDS of fill surfaces. A naive "one fluid sim per card" melts the
page (each wants its own WebGL context; browsers cap ~16 contexts). RIGHT architecture =
ONE shared full-page WebGL canvas behind everything that draws every fill rect in a SINGLE
draw call (cost is per-pixel, not per-card → 100 tanks ≈ 1 tank), a real fluid shader
(fbm turbulence + Gerstner-ish sloshing surface + metaball bubbles + glow) bound to each
element's fill%/tone read from the DOM rects each frame, with the CSS fill kept as the
reduced-motion / no-WebGL fallback. DISCIPLINE (spike skill): build ONE real WebGL element
first, prove it looks good AND holds 60fps on REAL hardware, THEN roll out — do not convert
100 cards blind. The spike lives at `array-operator/spikes/001-webgl-fluid-tank/index.html`
(self-contained: shared canvas + shader packing up to 12 rects as uniforms, "N tanks · 1
draw call" FPS readout). RENDER-IT GOTCHA: headless Chromium uses SwiftShader (CPU) and
reports 10-21fps — that is NOT the real number; a light fragment shader locks 60fps on a
real GPU. So headless QA proves the LOOK and the single-draw-call architecture, but
framerate MUST be confirmed by Ford on his machine before committing to the full rollout —
flag that as the one unknown, don't claim "smooth" off a SwiftShader number. To render
WebGL headless at all, launch Chromium with `--use-gl=angle --use-angle=swiftshader
--enable-unsafe-swiftshader --ignore-gpu-blocklist --enable-webgl`.

## The VANILLA Trends tab — multi-view architecture (trends.js, NOT the React TrendsView) — Jun 2026
The array-operator OWNER site has its OWN Trends tab (`public/trends.js` → `#trendsRoot`, fed by
`GET /v1/array-owners/fleet-trends`) — distinct from the React NEPOOL dashboard's `TrendsView.tsx`.
Data contract (from `api/array_owners.py` `array_owners_fleet_trends`): `{years[], monthly_by_year:
{"<yr>":[{month,kwh}]}, seasonal_yoy[], ttm_kwh, lifetime_kwh, ttm_savings_usd, by_array[]}`. The
LATEST year is usually PARTIAL (e.g. Jan–Jul) — every renderer must handle a partial/single year.

When Ford says "build ALL these concepts, spawn an agent for each" (he did this for 4 animated
trend visualizations — Liquid Energy / Solar Spiral / Energy Ridgeline / Heat-Field), the
collision-free pattern that let 4 parallel agents touch the same tab with ZERO conflicts:
- KEYSTONE (build yourself first, commit before fanning out): `trends-core.js` = shared brand
  tokens (read live from `styles.css` :root), per-year color (newest=bold green), number fmt,
  a `smoothPath` catmull-rom helper, a responsive hi-DPI auto-animating `createCanvas()` (auto-
  stops when detached from DOM), and a VIEW REGISTRY (`AOTrends.registerView(key, def)`).
- ONE FILE PER VIEW: `trends-view-<key>.js`, each self-registers. `trends.js` becomes a thin
  orchestrator (fetch + stat band + segmented switcher + by-array table) that mounts/unmounts the
  active view and persists the choice in localStorage. Because each agent owns exactly one
  `trends-view-<key>.js` + append-only CSS prefixed `.trv-<key>-`, they physically cannot collide.
- Give agents a standalone test harness (`trends-concepts-live.html`) that loads the REAL core +
  all view files against a MOCK payload with dataset toggles (3 years / single year / thin / down-
  month gap) so they can QA edge cases without the backend or a port conflict.
- All canvas charts: support `prefers-reduced-motion` (freeze the animation `t`), scale with the
  width passed to the draw fn (never hardcode px), and add a hover tooltip via one `.tr-tip` div.
- Brand tokens (array-operator `styles.css` :root): `--bg #0a0e14`, `--good #3fd68a` (solar green),
  `--good2 #7ff0bb`, `--gold #f5b942`, `--gold2 #ffd479`, `--sky #5ec2ff`, glassy `--card` glow.
- BUG caught in QA: a renderer referenced an undefined color var → `hexA(undefined)` threw
  `undefined.replace`, which halted that view's entire rAF frame loop (blank chart, no rings). When
  a canvas view renders PARTIALLY blank, get the pageerror/stack first — one throw kills the frame.

## Showing Ford visual work = SERVE IT ON LOCALHOST (his explicit standing preference, Jun 2026)
Ford: "next time just get the thing live on a localhost so I can quickly go to it." Do NOT hand him
a `file://` path or "open this file in your browser" — SERVE the artifact and give him an
`http://localhost:PORT/...` URL he can click. Pattern: `cd <dir> && python3 -m http.server 8899`
via terminal(background=true) (NOT `nohup …&` — Hermes blocks shell-bg wrappers in foreground),
then a one-line `curl -sI http://127.0.0.1:8899/<file>` readiness check, then give him the URL.
Applies to concept galleries, QA harnesses, any UI he needs to eyeball. (Also in user memory.)

## The Reports/billing tab (reports.js) — frontend lags its backend; check the backend FIRST (Jun 2026)
`public/reports.js` (`window.__aoLoadReports` → `#reportsRoot`, delegated from sandbox.js
`loadReports()`) is the owner-facing billing-reports tab: upload→match→schedule, the approval
inbox, and the subscriptions list. Its backend is `solar-operator/api/billing/routes.py`
(mounted `/v1/array-operator/billing`, proxied same-origin via `public/_redirects`).
KEY LESSON when Ford reports "there's no way to do X on the Reports tab": the BACKEND is usually
already complete — the gap is just an UNEXPOSED capability in `reports.js`. Read `routes.py` +
`matcher.py` before assuming you need new backend work. Two real gaps fixed this way (both
frontend-only, zero backend changes):
- **"Add another client" / manual customer**: `POST /subscriptions` already supports a MANUAL path
  (no file → `customer_name` + `array_id` + `allocation_pct`, model `percent_of_array`; see
  `_create_manual_subscription`). The fix was a collapsible "Add a customer manually" card in
  reports.js whose array dropdown is populated from `GET /v1/array-owners/fleet-tree` (`.arrays[]`
  → `{array_id,name,client_name}`) and which POSTs a `FormData` with NO file. GOTCHA: backend wants
  `allocation_pct` as a FRACTION in (0,1] — the UI takes a percent (e.g. 25) so divide by 100 before
  appending. Reuse the existing `.rb-seg`/`.rb-slider` segmented toggles + `wireSegments()`/`segValue()`.
- **Sample spreadsheet to download**: see the fixture-verify pattern below.

### Verify a SYNTHETIC fixture against the REAL parser before shipping it (reusable beyond billing)
For the "give users a sample billing spreadsheet" gap: do NOT ship a real customer's workbook (the
test fixtures `tests/fixtures/billing/{fairlee,norwich,valley_cares}.xlsx` are REAL customer data).
Generate a SYNTHETIC one in the exact schema the parser expects, then PROVE it parses before trusting
it. The matcher's schema is documented in `api/billing/matcher.py` (HCT family: a per-customer DATA
ledger sheet with a metadata label-row + value-row above a ledger header `Month|Date Start|Date End|
kWh whole array|kWh <Customer>|Tariff|Adder|...|Value|Bill|Savings`, plus a `Template` sheet that
drives the billing-model detection). PATTERN that worked:
1. Write a generator script under `solar-operator/scripts/` (committed, re-runnable) using openpyxl
   that builds the workbook from fictional inputs. Metadata label-row needs ≥3 recognized tokens
   (CUSTOMER/ADDRESS/ACCT/METER/"Percent of solar net metering credits"/"Price Factor"/EMAIL); the
   allocation % cell IS the `_META_TOKENS["allocation"]` value; the Template sheet text like
   "% of total array" picks the `percent_of_array` model.
2. Verify with the REAL matcher offline: `match_billing_workbook(bytes, allow_llm=False)` and assert
   `matched==True`, confidence near 1.0, and a non-empty `computed_invoice`. This catches schema drift
   without an LLM key. (The sample here matched at confidence 1.0.)
3. Copy the verified file into `array-operator/public/<name>.xlsx` — Netlify serves it statically
   (confirm no `_redirects` rule shadows the path), link it with `<a href download>`.
GENERAL RULE: any "downloadable example that must round-trip through our own importer" should be a
committed generator script + a programmatic assert against the real parser, not a hand-built blob.

### QA harness for reports.js specifically (auth + canned backend)
reports.js needs a signed-in session AND backend JSON to render past the sign-in prompt. The harness
that worked: a throwaway `public/_reports_qa.html` that loads the REAL `styles.css` + `command-center.css`
+ `reports.js`, then BEFORE the reports.js script tag stubs `window.localStorage` (so `so_session`
returns a fake token) and `window.fetch` (return canned `/billing/match`, `/billing/subscriptions`,
`/billing/drafts`, `/array-owners/fleet-tree` payloads). Serve on a localhost port, drive with Python
Playwright (1180-wide, deviceScaleFactor:2), screenshot default + manual-form-open + matched-preview
states, `vision_analyze` each, then DELETE the harness + shots. To exercise the upload path without a
real file picker, build a `File` in-page and dispatch a `change` event on `#rbFile`. Listen for
`pageerror`/console-error and report them — a thrown error mid-render leaves a half-painted tab.

## VISUAL-QA DISCIPLINE (Ford's standing rule: Playwright + vision_analyze EVERY UI change)
Never claim a UI change is done without rendering it and looking. Reliable harness pattern: a throwaway
`*_qa.js` under array-operator that reads the REAL `public/styles.css`, builds a few cards covering the
TRICKY states (varied content heights, fault/clip/sleep, day+night), screenshots to /tmp, then
`vision_analyze` each. Delete the harness after.
- PITFALL learned this session: a too-NARROW viewport collapses the flex cards to ~30px wide and wraps
  names one-char-per-line — a DEGENERATE render that doesn't test the real layout. Use a generous
  viewport (e.g. 900x340, deviceScaleFactor:2) so cards render at their true ~152px width. If the QA
  shot looks degenerate, FIX THE HARNESS and re-shoot — do NOT judge the fix off a broken render.
- Caught real bugs this way before shipping: moon emoji overlapping the kW nameplate (moved to corner
  gutter top:3px/right:5px), and the slid-up plate above. Crop with PIL + re-vision for fine detail.
- For LIVE data render (real Chint/SMA/Fronius driving fills), verify on a hard-refresh of
  arrayoperator.com or screenshot the prod dashboard — the synthetic harness only proves CSS/markup.
- PYTHON playwright is a reliable fallback when the Node playwright path won't resolve: the
  hermes venv has it — `/usr/local/lib/hermes-agent/venv/bin/python` with `from
  playwright.sync_api import sync_playwright`. Build the harness HTML inline (read the REAL
  `public/styles.css`, inject a representative DOM for the component, set viewport +
  `device_scale_factor=2`), `set_content` → `screenshot` → `vision_analyze`. Used this for the
  Fleet Commander top-bar liquid bar QA (the bar + relocated button verified at 1100x260).

## Visual-QA harness for the REACT dashboard (web/app), not just array-operator (Jun 2026)
The array-operator canvas is vanilla JS so its `*_qa.js` harness runs as plain Node. The NEPOOL
dashboard SPA at `solar-operator/web/app` is a Vite/React + Tailwind project — two extra gotchas when
QA-ing a React component (e.g. the trial-ended gate):
- `web/app/package.json` has `"type":"module"`, so a `require(...)` harness fails with "require is
  not defined in ES module scope". Name the harness `*.cjs` (CommonJS) to use `require`.
- Playwright is NOT installed under `web/app` — it lives at `/root/array-operator/node_modules`.
  Run the harness with `NODE_PATH=/root/array-operator/node_modules node gate_qa.cjs` to borrow it.
- For a component using Tailwind utility classes, the fastest faithful render is an HTML harness that
  loads `https://cdn.tailwindcss.com` (+ a small `tailwind.config` for any custom colors like
  emerald/amber) and pastes the component's JSX-as-HTML. Screenshot → vision_analyze → delete. This
  proves layout/contrast/clipping without standing up the whole SPA. CAVEAT: it does NOT exercise the
  real component state/routing — for that you'd need a `paused_no_card` tenant loaded in the live
  `/app`; flag that gap honestly rather than claiming the live SPA was verified.

## MERGING a feature into ANOTHER AGENT's existing React tab (cross-agent UI consistency) — Jun 2026
When Ford says "merge it with the [X] tab another agent made," the right move is to REUSE that
agent's components, not build a parallel. Concretely (built the data-sponge "energy history" view
to merge with another agent's billing **Trends** tab):
- First FIND their work: `search_files` for the feature name → the screen
  (`web/app/src/screens/TrendsView.tsx`) + its components
  (`web/app/src/components/reports/trends/MultiYearLineChart.tsx`, `trendUtil.ts`). Read them.
- Then REUSE: import the SAME chart (`MultiYearLineChart`), the SAME stat-tile / `Section` markup,
  the SAME formatters (`formatKwh`/`formatUsd`/`yearColor`). Transform your data INTO the shape their
  chart already takes (`YearSeries[] = {year, points:[{month,kwh,savings}]}`). The two views then read
  as ONE product with one visual language — which is what "merge" means to Ford, not a literal code
  merge. New screen goes as a SUB-ROUTE (e.g. `/account/energy-history` under account), lazy-loaded
  (`lazyWithRetry`), so it doesn't add a competing top-level tab.
- DEFENSIVE api client: new endpoints get a `normalize*`/coerce wrapper (every scalar `num()`-coerced,
  absent collections → `[]`) so a thin/not-yet-deployed backend never throws — same posture the trends
  `normalizeTrends` already used. Match the neighbor's conventions, don't invent.

## DEPLOY-VERIFY the React bundle actually rotated (two gotchas that fake "shipped") — Jun 2026
Shipping a `web/app` change is NOT done when the source compiles. Two traps, both hit this session:
1. **Stale local build**: after editing `src`, a `grep "your-new-route-string" dist/assets/*.js`
   returned NOTHING even though the source had it — the prior `dist` was stale. FIX: `rm -rf dist`
   then rebuild (`npm run build` or `bash build_app.sh`); confirm a new lazy chunk appears
   (`ls dist/assets | grep YourNewView`) AND the string is now in the bundle BEFORE committing.
2. **Prod still serves the OLD hashed bundle after deploy**: Railway can serve the old container for
   a minute, AND `curl prod/accounts/assets/<NewChunk>.js` returned HTTP 200 but EMPTY — because the
   old container 404s the new hash and the SPA falls back to index.html (a 200 that is NOT your JS).
   The reliable readiness check is: poll `curl prod/accounts/` and wait until its `index-*.js`
   reference equals your NEW local hash (`grep -o 'index-[A-Za-z0-9]*\.js' api/app_dist/index.html`).
   ONLY THEN verify the chunk is real: `content_type=application/javascript` + non-trivial `size` +
   `grep`-able for your strings. A `200` alone is a false positive on a CSR SPA.

## BUILD/DEPLOY the two frontends — committed dist bundles, run the build script BEFORE commit
The dashboard SPA is served by the FastAPI backend from a COMMITTED bundle, NOT built in CI:
- `api/app_dist/` is the deployed `/app` + `/accounts` bundle. `web/app/dist/assets/*` are GITIGNORED
  (only `dist/index.html` is tracked), so editing `web/app/src` and committing does NOTHING to prod
  until you run `bash build_app.sh` (npm ci + vite build → `cp -r web/app/dist api/app_dist`) and
  commit the refreshed `api/app_dist/`. Forgetting this ships source with a stale bundle. Same shape
  for the onboarding SPA: `build_onboarding.sh` → `api/onboarding_dist/`. Railway then deploys the
  backend (push to main → `railway up` auto-build, poll `railway deployment list`).
- The array-operator owner canvas is the ODD one out: deployed to NETLIFY straight from `public/`
  (`netlify deploy --prod --dir public --site 966cb1f5-...`), so a `git push` updates GitHub ONLY —
  live stays stale until the netlify deploy. (See top of this file + MEMORY.)
- RULE: after ANY `web/app` or `web/onboarding` source edit, run the matching `build_*.sh` and stage
  the regenerated `api/*_dist/` in the SAME commit; a `git status` showing only `dist/index.html`
  changed is the tell you forgot to rebuild (the hashed asset files rotate on a real build).
