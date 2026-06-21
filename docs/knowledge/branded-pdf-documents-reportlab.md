# On-brand PDF documents (invoice / performance summary / quarterly) — reportlab

When Ford asks to make AO's generated documents "slick", "energy juicy", "match
the site's style", or add a chart — this is the pattern. Built Jun'26 for the
default invoice + performance summary; quarterly inherits it (it's a cadence, not
a separate generator).

## Where it lives
- `api/billing/_pdf_brand.py` — the SHARED brand kit (ONE source of truth):
  palette, `make_hero_decorator(...)`, `make_chart_flowable(points, w, h, ...)`,
  `draw_energy_chart(...)`, `_money(...)`. ADD shared chrome here, never duplicate
  drawing code across generators.
- `api/billing/invoice.py::render_invoice_pdf` and
  `api/billing/summary.py::render_summary_pdf` both import `from . import _pdf_brand as brand`.
- XLSX paths (`render_invoice_xlsx`, `render_summary_xlsx`) are SEPARATE and
  untouched by styling work — they mirror the customer's own Template sheet.

## The look Ford signed off on
- Dark "energy" HERO BAND at the top, painted on the reportlab canvas via an
  `onPage(canvas, doc)` callback (`SimpleDocTemplate(..., onFirstPage=, onLaterPages=)`),
  NOT a flowable — set `topMargin = HERO_H + 0.35*inch` so the body clears it.
  Band = deep-space `#0a0e14` bg + green radial glow (stacked translucent
  ellipses upper-right) + sun-glyph brand mark + "Array Operator" wordmark +
  title + subtitle + a right-aligned headline figure (AMOUNT DUE on the invoice,
  LIFETIME GENERATION on the summary) + a 3px green accent rule under the band.
- Clean white PAYABLE body: invoice = BILL TO/PERIOD header + line items + a
  juicy dark-green (`#06140d` bg, `#7ff0bb` text) AMOUNT-DUE banner. summary =
  a 2×3 stat-card grid (eyebrow / big green value / sub-line per card).
- A juicy MONTHLY ENERGY BAR CHART at the bottom: per-month kWh as vertical bars
  with a deep→bright green vertical gradient (interpolate via
  `colors.linearlyInterpolatedColor` over ~24 stacked rects), subtle gridlines,
  the peak bar glow-capped (a small bright circle) + value-annotated, month
  labels. Invoice uses `match.periods` (last ~12 mo); summary uses
  `s["ttm_points"]`.
- Palette is pulled from the SITE: `array-operator/public/styles.css :root`
  (`--good #3fd68a`, `--good2 #7ff0bb`, `--green-deep/#1f7d54`, `--gold`, `--sky`,
  `--bg #0a0e14`, `--ink #eaf0f7`). Keep `_pdf_brand` constants in sync with it.

## Pitfalls (all hit this session)
- **A reportlab Flowable needs the real base class.** A bare class with
  `wrap/drawOn` fails with `AttributeError: 'X' has no attribute 'getSpaceBefore'`.
  Subclass `from reportlab.platypus import Flowable`, implement `wrap(self,aW,aH)`
  + `draw(self)` (use `self.canv`, origin = flowable lower-left). See
  `make_chart_flowable`'s inner `_Chart(Flowable)`.
- **NEVER fabricate chart bars.** Plot only points whose value is not None; render
  an honest empty-state string ("No monthly production data yet.") when the list
  is empty. Metrics with no data show "—", never a guessed number (Ford's hard
  rule). The summary's YoY / Peer-health "—" on one-year data is CORRECT, not a bug.
- **Header crowding:** the title can collide with the left logo and the right
  headline figure. Size title ~20pt, start it at the left margin BELOW the
  wordmark row, and right-align the figure with `drawRightString`. Verify by
  rendering, not by eye in code.
- **Stat-card row spacing:** reportlab table cells default to ~2px padding, which
  crams the big value against its sub-line (Ford flagged this). Set per-row
  `BOTTOMPADDING` (≈7 under eyebrow, ≈6 under value) + `LEADING` on the value row.

## Verify (visual QA without a browser)
`pip install pymupdf`, then render the PDF and rasterize a page:
```python
import fitz
d = fitz.open("/tmp/x.pdf"); d[0].get_pixmap(matrix=fitz.Matrix(2,2)).save("/tmp/x.png")
```
Then vision_analyze the PNG. Render from the real sample workbook:
`match_billing_workbook(open("array-operator/public/sample-billing.xlsx","rb").read())`.

## Emailing a sample to Ford (he validates in his own inbox)
Use the resend-email skill's direct-curl path. From=`Array Operator <ford@arrayoperator.com>`,
to=`ford.genereaux@gmail.com` (gmail for casual eyeball), `reply_to=admin@solaroperator.org`,
attach base64 PDFs via `attachments:[{filename, content}]`. Confirm delivery by
GET `https://api.resend.com/emails/{id}` → `last_event == "delivered"` before
claiming sent. Ford iterates on details (spacing, hero figure, chart height) —
expect a re-send after a tweak.

## Deploy
Backend rendering change → pure code, NO migration. `git push origin HEAD:main`
(Railway), then verify `from api.billing.summary import render_summary_pdf` +
`from api.billing._pdf_brand import make_hero_decorator` import on the prod image
via railway ssh. Stage ONLY your billing files on the shared tree.
