# Array Operator front-end: multi-view tabs + the "sublime polish" layer

Patterns proven building the Trends tab (`/root/array-operator/public/`) into a 4-view
animated dashboard, then polishing it to "sublime." The Array Operator owner site is a
NO-BUILD vanilla-JS stack (plain `<script>` tags, `:root` CSS tokens in `styles.css`,
canvas charts, no framework/bundler). Deploy is MANUAL: `netlify deploy --prod --dir=public`
from `/root/array-operator` — `git push` updates GitHub ONLY, the live site stays stale.

## Brand tokens (styles.css :root) — use these, don't invent colors
`--bg:#0a0e14` deep navy · `--good:#3fd68a` solar green (primary) · `--good2:#7ff0bb` ·
`--gold:#f5b942` / `--gold2:#ffd479` · `--sky:#5ec2ff` · `--ink:#eaf0f7` ·
`--muted:#8b97a8` · `--faint:#6b7686` · `--line:rgba(255,255,255,.08)` ·
`--card`/`--card2` glassy panel fills. The house look = deep navy + green/gold radial glow,
glassy cards with `0 …px …px rgba(0,0,0,.7)` shadows + inset hairline highlight.

## The Trends data contract (already live)
`GET /v1/array-owners/fleet-trends` (backend in `solar-operator/api/array_owners.py`):
`{years:[…], monthly_by_year:{"2025":[{month,kwh}]}, seasonal_yoy:[…],
ttm_kwh, lifetime_kwh, ttm_savings_usd, by_array:[{name,lifetime_kwh,years}]}`.
The latest year is usually PARTIAL (e.g. Jan–Jul) — every renderer must handle a partial
trailing year (don't compare a partial current year against a full prior year for YoY; compare
only the months present in BOTH — the live code does this in the stat band).

## Multi-view registry pattern (let N agents build N views with ZERO collisions)
When asked to build several interchangeable visualizations of the same data ("make all four,
spawn agents"), DON'T have agents share chart files. Instead make a registry seam so each
agent owns exactly one self-contained file:
- `trends-core.js` — KEYSTONE you write yourself. Owns: brand tokens read from CSS vars (with
  fallbacks), per-year color assignment, number formatting, a responsive hi-DPI auto-animating
  `createCanvas(container,{aspect})` helper (`.start(draw)`/`.stop()`, auto-stops when detached
  from DOM), `smoothPath()` catmull-rom, and a VIEW REGISTRY:
  `window.AOTrends.registerView(key,{label,badge,order,describe,mount(container,prepped,core)→stopFn})`.
- `trends.js` — KEYSTONE orchestrator. Fetch + stat band + switcher + by-array table + which
  view is active (persisted in localStorage). Crossfade on switch; calls the active view's
  `mount()`, stores the returned cleanup `stop` fn, calls it before mounting the next.
- `trends-view-<key>.js` — ONE PER VIEW. Each calls `registerView()` and draws into the canvas.
  An agent edits ONLY its own view file + APPENDS CSS rules prefixed `.trv-<key>-` in trends.css.
- `TRENDS-VIEWS-CONTRACT.md` — the brief each agent reads (data shape, helpers, hard rules).
- `trends-concepts-live.html` — standalone harness that loads the REAL core + all view files
  against a mock `/fleet-trends` payload, with dataset toggles (3yr / single-year / thin / gap).
  This is how you AND the agents QA without touching the live login-gated tab.

Why this beats shared-file contracts: the only shared file agents touch is trends.css, and they
APPEND-ONLY with a key-prefix — so the merge produces a CSS-append conflict ONLY (predictable,
trivially resolved by concatenating each branch's added block onto the keystone base; the JS
view files merge clean because they're disjoint). Verified: 4 agents, each touched only its own
view file + scoped CSS, all merged with one mechanical CSS resolution.

## The "sublime polish" layer (orchestrator-level, NOT per-chart)
When Ford says "polish it until it's sublime," the win is making the WHOLE tab feel like one
crafted instrument — apply effects at the orchestrator/CSS level so all views inherit them,
rather than piling more effects onto each chart (that violates his restraint rule). What landed:
- **Animated count-up** stat band: numbers ease from 0 (cubic ease-out, ~1.1s) on load.
- **Per-view accent retint**: a `--tr-accent` CSS var set per active view (green/gold/sky/amber);
  the switcher pill, badge glow, ambient backdrop, and tooltip border all `color-mix()` off it,
  so each view owns a color identity while staying one family.
- **Sliding indicator pill**: an absolutely-positioned element behind the segmented switcher that
  `translateX`/`width`-animates under the active segment (spring cubic-bezier). Re-measure it on
  resize (ResizeObserver) and after web-font settle (`setTimeout(moveIndicator, 220)`).
- **Crossfade view swap**: dim host out (~190ms) → `doMount(next)` → fade in. Calm, not jarring.
- **Ambient breathing glow**: a slow-pulsing accent-tinted radial in the panel corners for depth.
- **Staggered entrances**: stat cards + table rows rise in with `animation-delay:calc(.04s*var(--ri))`.
- **prefers-reduced-motion**: gate ALL of the above — count-up jumps to final value, animations
  off, transitions off. Both the JS (freeze the animation clock `t`) and a CSS media block.

## QA discipline (own it — Ford expects visual QA on every UI change)
- Playwright headless screenshot + `vision_analyze` EVERY view, switching, mobile (390px), and
  each edge-case dataset. Hover the canvas to trigger tooltips before the shot.
- Capture console errors via `page.on("console"/"pageerror")` — "renders" ≠ "no errors". One real
  bug this way: a view crashed on an undefined color (`C.gold2` referenced but only `C.good2`
  defined) and the uncaught throw HALTED that view's whole rAF loop → blank chart. Get the
  pageerror stack, fix the root cause, don't guess.
- Serve on a localhost port and give Ford the `http://localhost:PORT/...` URL (his standing
  preference) — never a file:// path. MECHANIC: start the static server with
  `terminal(background=true)` running `python3 -m http.server <port> --bind 0.0.0.0` from
  `/root/array-operator/public` — do NOT use shell `nohup`/`&`/`disown` (the terminal tool
  rejects shell-level backgrounding). Then verify readiness in a SEPARATE call
  (`curl -sI http://127.0.0.1:PORT/<file>` → HTTP 200). Reuse a high port (8899 worked) and
  the same server also serves the standalone harness + live files.
- After deploy, VERIFY ON PROD: `curl` the new files for 200 + grep the live bundle for a marker
  string only the new code has (e.g. `tr-switch-ind`, `countUp`), then headless-render the live
  harness URL and vision-check. "Deploy complete" in the log is not proof.

## Honesty rule that came up
Don't fake data that hasn't happened. The partial current-year ridge ends in a hard vertical
edge at the last month with data — leave it honest; tapering it would imply production that
doesn't exist yet. Ford prizes the truthful render over the prettier-but-fabricated one.
