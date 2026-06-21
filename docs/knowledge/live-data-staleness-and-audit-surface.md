# Live-data staleness diagnosis, the Audit surface, and the slick PDF brand kit (Jun 2026)

Three durable AO learnings from the Reports→Trends→Audit + "why no live data" session.

────────────────────────────────────────────────────────────────────────
## 1. "Why isn't the Arrays tab showing live data / why does this KEEP happening?"
────────────────────────────────────────────────────────────────────────
This recurs. The symptom (a bare `0` on the Arrays/sandbox cards) is almost NEVER a code
bug — it is one of three things, and they look identical until you probe. DIAGNOSE in this
order against the **prod image** (railway ssh + base64-stdin), do NOT guess:

DECISION TREE
1. **Is the sun down in Vermont?** `TZ=America/New_York date`. Night → 0 is correct, the
   poller is daylight-gated (`poll_all_sources` returns early when `_is_daylight()` is False).
   This is the FIRST thing to rule out; don't skip it.
2. **Does the fleet-tree live fetch succeed?** Run a probe that resolves each array's
   connection (`array_owners._resolve_connection`), checks `VENDORS[vendor].SUPPORTS_LIVE`,
   and calls `array_owners._cached_fetch_live(vendor, config)`. If it returns power with NO
   errors → the pipeline works; the value itself is the problem (go to 3). If it raises →
   auth/network (different issue).
3. **Is the `inverter_readings` time-series fresh?** `SELECT max(ts), count(*) FROM
   inverter_readings`. Frozen hours/days ago while `inverter_daily` (the daily rollup) is
   current = the LIVE poll specifically stalled, even though daily ingest is fine. (Column is
   `ts`, not `created_at`.)
4. **Run ONE poll cycle by hand** to separate "scheduler not firing" from "poll has nothing
   to write": `poller.poll_all_sources(force_daylight=True)`. Read the summary counters:
   - `arrays_skipped` (no pullable connection — GMP-billed arrays with no inverter API; EXPECTED
     for ~96% of the fleet).
   - `arrays_throttled` (budget governor spacing — see below).
   - `arrays_polled: 0` + `readings_written: 0` while daylight=True → the sites that got past
     the governor returned `current_power_w: None`/0, so nothing was written.
5. **Probe SolarEdge's raw `/overview` per site** (`adapters.solaredge.fetch_overview`):
   look at `currentPower.power`, **`lastUpdateTime`**, and `lastDayData.energy` (today Wh).
   - If `lastUpdateTime` is HOURS/DAYS old AND today's energy is 0 → **the SITE stopped
     reporting to SolarEdge** (router/ISP/gateway outage at the customer site). Our pipeline is
     correct; SolarEdge genuinely has no fresh data. This is the product DOING ITS JOB
     (catching an outage), but it PRESENTS as "app broken" because nothing surfaces the
     staleness.

ROOT CAUSES that make this "keep happening" (systemic, not a recurring bug):
- **Coverage**: only a handful of arrays have a live-capable (SolarEdge) connection at all;
  the rest are GMP-billed with no inverter API → no live feed by nature. Same gap the Audit
  tab exposes.
- **No freshness surfacing**: a dark site shows a bare `0` indistinguishable from night,
  broken, or down. The RECOMMENDED FIX (get Ford's steer before shipping — it's a live-tab UX
  change) is a **last-seen / "reported Xh ago" indicator** per card, flagging sites dark > N
  hours as a reporting outage. Do NOT fall back to "energy today" as the headline — when a
  site is dark, today's energy is ALSO 0, so it's not a viable substitute.

### 1b. SHIPPED (Jun 2026): the source-outage surfacing + the field-stripping pitfall that hid it
Ford greenlit the freshness indicator and asked it be "VERY clear … make it clear it's a data
outage from the SOURCE, not us." Shipped end-to-end; two durable lessons:
- **Backend** (`inverter_fleet.build_fleet_tree`): added `_source_status(inv_rows)` → per-array
  `{state: ok|stale|none, last_report, age_hours}` computed from the freshest inverter
  `last_report` (stale = older than `_SOURCE_STALE_HOURS`=6). Surfaced as a `source_status` field
  on every fleet-tree column. "none" = no live feed at all; "stale" = the VENDOR portal stopped
  receiving data (source-side outage, not ours).
- **THE PITFALL THAT MADE THE BANNER NOT SHOW (the real bug):** the Arrays tab does NOT read the
  backend fleet-tree directly — it goes through **FleetStore**, which REBUILDS each column in TWO
  places: `adaptTree(t)` on ingest AND `toColumns(ids)` on render. BOTH drop any field they don't
  explicitly list. So `source_status` reached the browser but was stripped before the renderer →
  banner silently never rendered. FIX = carry the field through BOTH `adaptTree` (onto the
  canonical array) AND `toColumns` (back onto the column). GENERAL RULE for ANY new fleet-tree
  column field consumed by the sandbox: it must be added in THREE spots — the backend column dict,
  `fleet-store.adaptTree`, AND `fleet-store.toColumns` — or it vanishes in the data layer and looks
  like a render bug. (Same class as the Pydantic silent-field-drop in extension-capture-mv3-
  debugging §6e — a field that's "sent" but silently dropped mid-pipeline.)
- **Frontend signage** (sandbox.js + command-center.css): an amber in-card banner
  (`sb-srcout`: "<Vendor> stopped reporting Nh ago. This is a data outage at the source — not Array
  Operator. Live data resumes automatically when <Vendor> reconnects."), PLUS — because Ford wanted
  it unmissable — a whole-card treatment via `sb-array--srcout`: amber glowing frame + a corner
  "⚠ SOURCE OFFLINE" ribbon (`::after`). Renders nothing when `state!=="stale"`. Verify by stubbing
  the `/fleet-tree` fetch through the REAL FleetStore ingest (a DOM-injection hack misses the
  adaptTree/toColumns stripping that was the actual bug) — assert the stale array shows the banner+
  ribbon+frame and a fresh array shows none. scripts/ao_srcout_verify.py is that probe.
- **"juicy green" tweak**: Ford called the top fleet-commander bar's green "muted." The OK-state
  greens were ≤0.8-opacity mid-saturation under a frosted plate. Brightened `.fc-tank--ok` /
  `.fc-card.ok` (higher-saturation gradient + green glow + border). Only the `ok` (≥95% healthy)
  state was juiced; warn/bad stay amber/red. A 88%/95% screenshot is the OK state, so test there.

BUDGET GOVERNOR (api/poller.py) — why live is SPARSE by design:
- SolarEdge limits ~300 req/day/**key**. Many sites can share ONE key (this fleet: 16 sites
  on one key). `_min_interval_seconds(sites_under_key) = 16h * sites / 280`. With 16 sites →
  ~51 min between polls PER SITE. So "live" updates are tens-of-minutes apart, never
  real-time — that alone makes the tab look static. `_budget_state` is module-scoped and
  resets on redeploy (harmless).
- A manual `poll_all_sources` right after another tick will show everything `throttled`
  because `last_poll` spacing hasn't elapsed; call `poller._reset_budget()` first if you want
  to force one through for diagnosis.

KEY LESSON (matches the GMP-backfill "never happened" pattern): the failure mode for this
project is repeatedly **a background job/feed that is technically alive but silently serving
stale/empty data, with no watchdog or freshness signal** — so normal real-world gaps read as
software bugs. When Ford asks "why does this keep happening", name the SYSTEMIC cause (feed
unreliability + no staleness surfacing), not just today's symptom.

Re-runnable diag pattern: write a standalone `/tmp/diag_*.py`, `base64 -w0` it, and
`railway ssh "cd /app && echo <b64> | base64 -d | python -"` — the secret-masker mangles
inline python in the tool echo but the written file bytes are clean. (Same pattern as the
capture-vendor diags in references/capture-vendor-staleness-and-billing-data-integrity.md.)

────────────────────────────────────────────────────────────────────────
## 2. The Audit tab + weekly digest (the auditor's FACE)
────────────────────────────────────────────────────────────────────────
The reconciliation engine (`api/reconciliation/reconcile_array`) existed for a long time with
NO route, NO UI, NO schedule — classic "brain wired to nothing". This session gave it a face.

- **Route**: `GET /v1/array-owners/fleet-audit` (array_owners.py, mirrors fleet-trends auth via
  `_tenant_from_bearer`). Runs `reconcile_array` across every owned array, rolls up a summary
  (total / auditable / ok / leak / leak_unconfirmed / incomplete_monitoring /
  insufficient_data / have_settlement / have_production / dollars_flagged / coverage_pct) +
  a per-array list sorted leaks-first. A per-array exception must NOT 500 the whole audit
  (try/except → insufficient_data). NEVER fabricates: an array with no production feed returns
  `insufficient_data`, not a fake leak.
- **Frontend**: new `audit.js` + `audit.css`, tab wired in 3 places — `index.html` (nav `<a
  id="tabAudit" href="#audit">` + `<section id="panelAudit">` + the css/js `<link>`/`<script>`),
  and sandbox.js (the `TABS` map, `tabFromHash()`, and the `applyView()` dispatch calling
  `window.__aoLoadAudit`). The SPA tab contract = anchor `#hash` + `tabXxx`/`panelXxx` ids +
  a `window.__aoLoadXxx` loader, all switched by sandbox.js.
- **Design**: matched trends/reports vocabulary (card gradient, 18–22px radii, ambient glow,
  CSS vars so day/night themes inherit). Hero = coverage ring + dollars-flagged headline +
  meter; status filter chips; per-array verdict rows with a tinted status spine.
  - PITFALL (coverage ring): a conic-gradient ring with centered text needs the inner disc on
    `z-index:-1` with `isolation:isolate` on the ring + `z-index:1` on the text, else the text
    overlaps the gradient and is unreadable. Inner disc must be a SOLID color, not the card
    gradient.
  - PITFALL (variance hue semantics): for a leak/unconfirmed row, a `+32%` variance must NOT be
    green — a money gap reading as green-positive is wrong. Tint variance by STATUS
    (leak=red, unconfirmed=gold), green only for clean rows.
- **Weekly digest**: `scheduler.deliver_weekly_audit_digest` (Mondays 13:00 UTC), emails each
  active `product=="array_operator"` tenant at `tenant.contact_email`. Reuses
  `notify._send_via_resend(..., product="array_operator")` + `email_skin.render_email_skin`.
  Honest by construction: sends "all clear" when nothing flagged, skips tenants with no bills
  to audit (`have_settlement==0`) so it's not noise, never invents a leak.
- To QA all the audit states locally, seed a temp tenant with arrays in each state (1 leak via
  independent `solaredge` DailyGeneration source w/ prod>>settle, 1 unconfirmed via GMP-only
  `gmp_daily_generation`, ok rows, a no-production row) — set Tenant `active=True,
  product="array_operator"`, Bill needs `tenant_id`, GmpDailyGeneration needs
  `tenant_id/account_number/array_id`, Inverter needs `serial`. CLEAN IT UP after.

────────────────────────────────────────────────────────────────────────
## 3. Slick on-brand generated PDFs (invoice / performance summary / quarterly)
────────────────────────────────────────────────────────────────────────
Ford wanted the default generated invoice "really slick, energy-juicy, match the site". Built
a shared brand kit so every AO document is identical and the styling lives in ONE place.

- **`api/billing/_pdf_brand.py`** — one source of truth: the palette (pulled from
  array-operator `styles.css :root` — BG #0a0e14, GOOD #3fd68a, GOOD2 #7ff0bb, GOLD #f5b942),
  `make_hero_decorator(...)` (the dark energy hero band: green radial glow via stacked
  translucent ellipses, sun-glyph brand mark, title/subtitle, right-aligned headline figure,
  footer — painted via reportlab `onFirstPage`/`onLaterPages` canvas callback), and
  `make_chart_flowable(points, w, h)` (a juicy deep→bright-green gradient bar chart as a
  reportlab `Flowable` subclass — bars built from per-segment `linearlyInterpolatedColor`
  rects, peak glow-capped + value-annotated).
- `invoice.py::render_invoice_pdf` and `summary.py::render_summary_pdf` both consume the kit.
  Invoice = monthly-energy bars at the bottom; summary = a 2×3 stat-card grid + a TTM bar
  chart. Quarterly inherits automatically (it's just a cadence over the same generators —
  there is no separate quarterly PDF generator).
- NO new deps — all drawn on the reportlab canvas.
- PITFALLS:
  - A custom chart Flowable MUST subclass `reportlab.platypus.Flowable` (not a bare class) or
    platypus throws `AttributeError: 'X' object has no attribute 'getSpaceBefore'`.
  - Never fabricates: chart plots only months that carry a real kWh value; honest empty state
    ("No monthly production data yet") when none.
  - Header overlap: shrink the title font + nudge its baseline so a long title clears both the
    sun glyph (left) and the AMOUNT DUE figure (right).
- VERIFY a PDF visually: `pip install pymupdf`, render page→PNG (`fitz` matrix 2x), then
  `vision_analyze`. Catches header overlap / unreadable rings that imports/tests miss.
- Ford validates by EMAILING himself the artifact (resend-email skill, direct-curl path with
  the key file) and eyeballing it in his inbox — offer to send a sample, don't just describe.

────────────────────────────────────────────────────────────────────────
## 4. AO mobile + Trends/Reports consolidation notes
────────────────────────────────────────────────────────────────────────
- `public/mobile.css` is loaded LAST in index.html so it overrides every desktop sheet — if it
  loads before theme-day.css/trends.css its rules get overridden (the "loaded last to
  override" comment was a lie; fixed). Any mobile fix that "doesn't take" → check load order
  FIRST.
- Mobile overflow culprits: a `<table>` (Trends by-array) that pushes the page sideways needs
  its wrap pinned (`#trendsRoot{max-width:100%;overflow-x:hidden}` + `.tr-tablewrap` scrolls
  internally with a `min-width` on the table). A no-wrap flex hero (`.fc-plate` "88% fleet
  healthy") clips its right content → `flex-direction:column` on mobile. Verify with a 390px
  Playwright pass measuring `scrollWidth - clientWidth` per page.
- Reports consolidation arc this session: deleted the Offtakers subtab → folded all offtaker
  detail fields into the Invoice-Generator per-row Edit panel; folded the standalone
  spreadsheet-upload card into a tabbed "Add an offtaker" (Type-it-in / Upload-spreadsheet),
  and added the upload path to the setup wizard. PITFALL: the `/match` response nests its
  result under a `match` key — read `data.match.matched`, NOT `data.matched` (the wizard
  upload broke on exactly this). Trends: all-views-stacked column + per-array filter
  (`?array_id=` scopes aggregates, by_array stays full fleet) + Export CSV. Reports/Trends/
  audit all share the dark card vocabulary.
