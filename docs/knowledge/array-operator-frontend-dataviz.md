# Array Operator owner-site frontend + dataviz (the Netlify owner UI)

Scope: the OWNER-FACING site at `/root/array-operator/public/` — vanilla no-build
HTML/CSS/JS (NOT the React NEPOOL Operator in `solar-operator/web/app`). This is the
"Log in with <vendor>" + canvas + Trends owner experience. Same-origin `/v1/*` fetches
hit the Railway backend (`solar-operator/api/`).

## DEPLOY (manual — same trap as the rest of the project)
`git push` updates GitHub ONLY; LIVE stays stale. Deploy is MANUAL:
`netlify deploy --prod --dir=public` from `/root/array-operator`. (Mirrors the
`array-operator-card-ui` deploy note in memory.) New standalone pages dropped in
`public/foo.html` ship at `/foo.html` after that deploy.

## Brand tokens (canonical — `public/styles.css` `:root`, also in `command-center.css`)
Use THESE, never invent colors. The look is dark glowing "mindspace":
- `--bg:#0a0e14` `--bg2:#0e131c` ; body bg = `radial-gradient(1200px 700px at 75% -10%, #16202e 0%, var(--bg) 55%)`
- `--ink:#eaf0f7` `--muted:#8b97a8` `--faint:#6b7686`
- `--good:#3fd68a` (solar green — the brand hero / latest-year accent) `--good2:#7ff0bb`
- `--gold:#f5b942` `--gold2:#ffd479` ; `--sky:#5ec2ff` ; `--bad:#ff6b6b` ; `--warn:#ffb454`
- `--line:rgba(255,255,255,.08)` ; cards = `linear-gradient(165deg,var(--card),var(--card2))`,
  `--card:rgba(255,255,255,.035)` `--card2:rgba(255,255,255,.018)`, radius ~18–24px,
  `box-shadow:0 24px 60px -28px rgba(0,0,0,.7)`. Value/hero cards add a green radial glow.
House style: glassy panels, pill badges, soft radial glows, `font-variant-numeric:tabular-nums`
on every number, latest year = bold green and drawn on top.

## Trends tab data contract — `GET /v1/array-owners/fleet-trends`
Served by `array_owners_fleet_trends()` in `solar-operator/api/array_owners.py` (~L452).
Derived from real `DailyGeneration` telemetry; thin-history owners get empty collections
(never a 500). Shape any Trends UI must consume:
```
{ years:[2024,2025,2026],
  monthly_by_year:{ "2025":[{month:1,kwh:...},...], ... },   // partial current year = fewer months
  seasonal_yoy:[{month,label,by_year:{"2025":...},latest_delta_pct},...],
  ttm_kwh, ttm_savings_usd, lifetime_kwh,
  by_array:[{array_id,name,lifetime_kwh,years:[...]}] }
```
Live impl: `public/trends.js` exposes `window.__aoLoadTrends()`; `sandbox.js`'s
`applyView()` calls it when `#trends` is active. PITFALL it already encodes: compute
LATEST-YOY only over months present in BOTH years (a partial current year vs a full prior
year reads a scary false −47%); null when no overlapping month.

## "Make it slick / go crazy" dataviz request → STANDALONE CONCEPT GALLERY first
When Ford asks for a "sublime / high-tech / liquid / higher-dimensional" presentation of
existing data, DON'T rewrite the live tab blind (he does visual QA on every UI change and
decides fast once he can SEE options). Build a self-contained animated gallery page first.
1. Ground in the REAL data contract + brand tokens (above) so concepts aren't off-brand.
2. Write ONE `public/<tab>-concepts.html` — dependency-free, canvas/SVG, `requestAnimationFrame`,
   the `:root` tokens copied verbatim, fed REALISTIC MOCK data shaped exactly like the live
   endpoint (e.g. seasonal solar curve `[.34,.46,.66,.84,.97,1,.99,.92,.74,.55,.36,.29]`,
   YoY growth, a partial current year). 3–4 labelled concept cards, each with a one-line "why".
3. Proven concept families for cyclical multi-year energy data (all map to the glow brand):
   - LIQUID AREA FILL — current year as a flowing gradient body w/ animated wave displacement
     + bright meniscus orb on the leading edge; prior years as ghost ridgelines. Most premium.
   - SOLAR SPIRAL — climate/warming-spiral lineage: 12 months wrap a clock, each year a glowing
     ring (radius = that month's kWh), central sun. Seasonality = a readable shape. Airiest —
     tighten canvas height if chosen.
   - RIDGELINE / JOYPLOT — one luminous ridge per year stacked back-to-front; summer peaks =
     mountain range; growth = each ridge rising. Scales to a decade.
   - HEAT-FIELD — month×year glow grid, green→gold heat ramp, values on bright cells. The
     "higher-dimensional" read; densest/calmest; growth diagonal + summer band pop instantly.
   Recommendation that landed: LIQUID hero up top + HEAT-FIELD analytical companion (replaces
   the flat seasonal strip); SPIRAL/RIDGELINE as a toggle/alt view.
4. QA EVERY concept yourself before showing him (his rule): Playwright screenshot at
   device_scale_factor=2 + `vision_analyze` each section critically (readable? clipping at
   canvas edges? dead space? contrast of value text on bright cells?). Use the hermes venv
   python (`/usr/local/lib/hermes-agent/venv/bin/python`) — it has Playwright.
5. CANVAS-CRASH PITFALL: an uncaught exception inside a `requestAnimationFrame` `frame()`
   (e.g. `hexA(undefined)` → "Cannot read properties of undefined (reading 'replace')")
   HALTS that canvas's whole loop → it renders blank/partial while OTHERS look fine. Don't
   eyeball-guess — capture `page.on("pageerror")` (and console) in the Playwright script to
   get the exact file:line, fix the root cause (here: referenced `C.gold2` but only defined
   `C.good2` in the color map), re-shoot, confirm "NO CONSOLE ERRORS".
6. Only after Ford picks → wire the winner into `trends.js` against the real endpoint,
   keeping the existing stat band + by-array table; then `netlify deploy --prod --dir=public`.
