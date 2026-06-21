# AO PDF brand kit + fleet-audit (reconciliation) wiring — Jun'26

Covers two things shipped this session: (1) making the default invoice +
performance-summary + quarterly PDFs slick/on-brand via ONE shared brand kit,
and (2) wiring the production-vs-settlement audit engine to actually consume the
GMP daily data so it lights up when GMP refreshes.

## 1. Shared PDF brand kit  (api/billing/_pdf_brand.py)

The default generated PDFs (NOT the workbook-upload path, which repopulates the
owner's own .xlsx) were bland reportlab. Now there is ONE source of truth so
invoice / summary / quarterly never drift:

`api/billing/_pdf_brand.py` exports:
- Palette constants pulled from the site's `array-operator/public/styles.css :root`
  (BG `#0a0e14`, GOOD `#3fd68a`, GOOD2 `#7ff0bb`, GREEN_DK `#1f7d54`, GOLD,
  SKY, INKDK `#0f1722`, MUTEDDK, LINE, PAPER2). Keep these numerically in sync
  with styles.css if the site palette changes.
- `make_hero_decorator(title, subtitle, right_label, right_value, footer_left,
  footer_right, hero_h)` → returns an `onPage(canvas, doc)` callback that paints
  the dark "energy" hero band: deep-space rect + stacked translucent-green
  ellipses (radial glow, upper-right) + bright accent rule + sun-glyph brand mark
  + "Array Operator" wordmark + title/subtitle + a right-aligned headline figure
  + footer. Pass it to `doc.build(story, onFirstPage=deco, onLaterPages=deco)`.
- `draw_energy_chart(c, x, y, w, h, points, accent, empty_msg)` and
  `make_chart_flowable(points, w, h, accent, empty_msg)` → the juicy green
  gradient bar chart. `points` = list of `(label, value)` tuples. Bars are a
  vertical deep→bright green gradient (24 interpolated rects), peak month
  glow-capped + value-annotated, subtle gridlines.
- `_money(x)` helper.

Consumers:
- `invoice.render_invoice_pdf` — hero headline = AMOUNT DUE; body = BILL TO/PERIOD
  meta + line items + dark-green AMOUNT DUE banner + monthly energy chart.
- `summary.render_summary_pdf` — hero headline = LIFETIME GENERATION; body = a
  2×3 stat-card grid + trailing-12-month chart.
- Quarterly inherits automatically — it runs through the SAME invoice+summary
  generators (quarterly is a cadence, not a separate generator). No 3rd renderer.

### reportlab gotchas (cost real iterations)
- A custom chart Flowable MUST subclass `reportlab.platypus.Flowable`. A bare
  class with `wrap`/`drawOn` raises `AttributeError: 'X' object has no attribute
  'getSpaceBefore'` inside `frame._add`. Subclass it; implement `wrap(self,aW,aH)`
  + `draw(self)` (origin is the flowable's lower-left, use `self.canv`).
- Stat-card / multi-row tables cram by default (2px padding). Add explicit
  `BOTTOMPADDING` under the eyebrow row (~7) and under the value row (~6) +
  `LEADING` on the big-value row, or the value and sub-line touch. Ford WILL
  screenshot a cramped card and ask for spacing.
- Hero title can collide with the logo (same x) or the right-aligned figure for
  long titles — start title at the left margin, keep font ≤20pt, verify.

### Visual-QA recipe for PDFs (no browser)
`pip install pymupdf` then render page→PNG→vision_analyze:
```python
import fitz
d = fitz.open("/tmp/out.pdf")
d[0].get_pixmap(matrix=fitz.Matrix(2,2)).save("/tmp/shots/out.png")
```
Then `vision_analyze("/tmp/shots/out.png", "...")`. Always do this before
claiming a PDF redesign is done — layout bugs (overlap, cramping) are invisible
without it.

### Emailing a sample to Ford
He validates by seeing it in his own inbox. Build the PDF, base64 it into a
Resend `attachments:[{filename, content}]` payload, send `from
"Array Operator <ford@arrayoperator.com>"` to `ford.genereaux@gmail.com`, then
GET `/emails/{id}` and confirm `last_event=="delivered"` before saying sent.
(See the resend-email skill for the curl pattern + the `Bearer` masker
workaround.)

## 2. Reconciliation / fleet-audit wiring  (api/reconciliation/)

The auditor (the "selling intelligence" thesis) was already BUILT:
`reconcile_array(db, array_id, ws, we, rate)` in `api/reconciliation/reconcile.py`
compares metered **production** vs utility-**settled** kWh, with weather/irradiance
as the independent 3rd leg, and `classify_array` (single_site vs
group_net_metered; raw_text "Group Excess Shared" markers are PRIMARY, the
groupNetMetered JSON flag is noisy ~63/64 set). Statuses: `ok | leak |
leak_unconfirmed | incomplete_monitoring | insufficient_data`.

### The plumbing gap (this is the recurring class of bug on this project)
The audit reads the `DailyGeneration` table; the GMP daily backfill writes
`gmp_daily_generation`. Two ships passing → the audit was blind to GMP data.
FIX: `_production_over_window` now MERGES both per-day — DailyGeneration wins on a
day both cover, the `reports.gmp_daily_read.get_daily_series` seam fills the gaps
(no double-count). Always check WHICH table a consumer reads vs WHICH table a
backfill writes before assuming a feature is wired.

Also: `PRODUCTION_SOURCES` was silently dropping `extension_pull_corrected`
(real production on 2 arrays). When you see a source in `DailyGeneration.source`
that isn't in the engine's allow-list, that's a silent data drop.

### Integrity guard (the trust discipline — do not remove)
GMP interval data is the UTILITY'S OWN meter. Comparing it to the GMP bill is
utility-vs-itself → can't be an independent leak. So:
- `INDEPENDENT_SOURCES = ("solaredge","csv","manual")` (inverter vendor / owner
  export). `independent_feed = independent_kwh/production_kwh >= 0.5`.
- A variance >threshold backed only by utility-sourced production → status
  `leak_unconfirmed` (shows dollars_at_risk, `report_leak=False`, note tells the
  owner to connect inverter monitoring to confirm). A REAL asserted `leak`
  requires `independent_feed`. `gates["independent_feed"]` records it.
This is the auditor's whole credibility: never assert a leak the data can't back.

### Running the live fleet audit (the cheap thesis-test)
Enumerate live arrays, `reconcile_array` each over all-time, tally by status +
count how many have settlement / production / are fully auditable / flagged. Run
on the PROD image via `railway ssh "cd /app && echo <base64-script> | base64 -d
| python -"` (the inline-python masker workaround). Report the real numbers even
when ugly — Jun'26 baseline was 405 arrays / 232 settlement / 2 production / 0
auditable / 0 leaks, which correctly says "fix the feeds," not "the engine is
broken." Re-run after GMP refreshes to watch auditable climb.

### Test seeding gotcha (solar-operator models)
A reconciliation test fixture needs: `Tenant(tenant_key=...)`, `Bill(tenant_id,
account_id, ...)`, `DailyGeneration(tenant_id, array_id, ...)`, and
`GmpDailyGeneration(tenant_id, account_id, account_number, array_id, ...)` — all
carry NOT-NULL `tenant_id` (and Tenant needs `tenant_key`). Add an autouse
cleanup fixture that deletes seeded rows by tenant_id so the shared SOLAR_DATA_DIR
sqlite doesn't leak rows into other test files' global-count assertions.
