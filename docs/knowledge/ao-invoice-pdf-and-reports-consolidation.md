# AO Reports — slick invoice PDF + tab-consolidation patterns (Jun 2026)

Session that produced this: redesigned the default invoice PDF "energy-juicy",
plus a long arc of Reports-tab consolidation/fold-in work. All on
`/root/array-operator` (frontend, Netlify) + `/root/solar-operator/api/billing`
(backend, Railway). Reuse these; don't re-derive.

## 1. Slick on-brand PDF invoices with reportlab (no new deps)

File: `api/billing/invoice.py` → `render_invoice_pdf`. The default generated
invoice (manual / percent_of_array offtakers + fallback). Workbook-upload subs
get their OWN .xlsx via `invoice_writer.populate_invoice_workbook` — DON'T touch
that path when restyling the default.

Design that landed (Ford: "really slick", "energy juicy", "match the site's
style", "monthly energy graph at the bottom"):
- **Dark hero band** painted via an `onFirstPage`/`onLaterPages` canvas callback
  (`doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)`). The band
  is NOT a flowable — it's drawn directly on the canvas behind the story, so set
  `topMargin = HERO_H + pad` to push the story below it. Deep-space bg `#0a0e14`
  + green radial glow (stack 3–4 translucent `colors.Color(r,g,b,alpha=...)`
  ellipses), sun-glyph brand mark (circle + 8 rays via `math.cos/sin`), wordmark,
  title, right-aligned AMOUNT DUE figure, footer.
- **Body** = normal platypus flowables (Tables) on white: BILL TO/PERIOD header,
  line items, a dark-green AMOUNT DUE banner (`ROUNDEDCORNERS` TableStyle),
  green solar-savings.
- **Monthly energy bar chart** at the bottom = a canvas-drawn Flowable.

Palette pulled from `array-operator/public/styles.css :root`: `--good #3fd68a`,
`--good2 #7ff0bb`, green-deep `#1f7d54`, `--bg #0a0e14`, `--ink #eaf0f7`,
`--muted #8b97a8`, `--gold #f5b942`, `--sky #5ec2ff`. Body uses darker on-white
ink (`#0f1722`, `#5a6675`, line `#e5ebf1`) so it stays printable/payable.

### PITFALL — custom chart Flowable must subclass `Flowable`
A bare class with `wrap/drawOn` crashes inside `doc.build` with
`AttributeError: '_Chart' object has no attribute 'getSpaceBefore'`. Platypus
calls `getSpaceBefore/getSpaceAfter/getKeepWithNext` on every flowable. FIX:
`from reportlab.platypus import Flowable` and subclass it; implement `wrap(self,
aW, aH)` (return `(w,h)`) + `draw(self)` (paint on `self.canv`, origin is the
flowable's lower-left). Factory pattern that worked:
```python
def _make_chart_flowable(periods, width, height, accent):
    from reportlab.platypus import Flowable
    class _Chart(Flowable):
        def wrap(self, aW, aH): return (width, height)
        def draw(self): _draw_chart(self.canv, 0,0, width,height, periods, accent)
    return _Chart()
```

### Juicy gradient bars on a canvas (no chart lib)
Per bar, fake a vertical gradient by stacking ~24 thin rects, each filled with
`colors.linearlyInterpolatedColor(green_deep, good2, 0, 1, t)` for `t` in
`[0,1)`. Glow-cap + value-annotate the peak month (`max(range(n), key=...)`).
NEVER fabricate: plot only `Period`s where `customer_kwh is not None`; honest
"No monthly production data yet" when the list is empty. `match.periods` carries
the monthly series (`month`, `customer_kwh`, `end`).

### Verify a PDF visually (no pdftoppm in this env)
`pip install pymupdf -q` then:
```python
import fitz; d=fitz.open(p); d[0].get_pixmap(matrix=fitz.Matrix(2,2)).save(png)
```
then vision_analyze the PNG. Catch header overlap (title vs logo / vs amount).

### Shared brand kit — `api/billing/_pdf_brand.py` (added Jun'26)
After the invoice landed, the styling was EXTRACTED into one module so invoice +
performance summary + quarterly never drift. Three exports:
- **palette constants** (GOOD/GOOD2/GREEN_DK/BG/INK/MUTED/INKDK/MUTEDDK/LINE/
  PAPER2…) — verbatim from styles.css :root.
- **`make_hero_decorator(title, subtitle, right_label, right_value, footer_right,
  hero_h)`** → returns the `onPage(c, doc)` callback (band + glow + sun-glyph +
  title/subtitle + right headline figure + footer). Reusable across every doc.
- **`make_chart_flowable(points, width, height, accent, empty_msg)`** +
  `draw_energy_chart(...)` — `points` = list of `(label, value)`; the Flowable
  subclassing + gradient-bar logic lives here so callers don't re-implement it.
`invoice.py` was refactored onto the kit (identical output). `summary.py`
`render_summary_pdf` was rebuilt to match: SAME hero band (headline = lifetime
generation), a 2×3 stat-card grid (this-period / YoY / TTM / lifetime-gen /
lifetime-savings / peer-health, each an inner Table card in a bordered outer
Table), and a trailing-12-month bar chart fed by `build_summary()['ttm_points']`.
Honest `—` on metrics with no data; honest empty chart state. **Quarterly report
is NOT a separate generator** — quarterly is just a cadence on the same sub, so
it runs through render_invoice_pdf + render_summary_pdf and inherits the look
automatically. Tests: `pytest tests/test_billing_delivery.py
tests/test_billing_matcher.py tests/test_invoice_writer.py
tests/test_billing_trends.py tests/test_deferred_billing_setup_mode.py` (49 pass;
sibling-agent test_data_coverage/solaredge/weather collection errors are NOT yours).

## 2. /match response is NESTED — read `data.match`, not top level

`POST /v1/array-operator/billing/match` returns
`{ok, filename, match: {matched, customer:{name,email,...}, periods, ...}}`.
The result is under the `match` key. Reading `m.matched` / `m.customer` off the
top-level response gives undefined → "Couldn't recognize that workbook" on a
PERFECTLY VALID file. The tab's `matchFile` does it right (`const m =
data.match`); mirror that anywhere you add an upload path (this bit the wizard
spreadsheet path). Always: `const mdata = await r.json(); const m = mdata.match;`
then guard `if (!r.ok || !mdata.ok || !m || !m.matched)`.

## 3. Reports = one billing object, three creation paths

All three converge on ONE `BillingReportSubscription` (backend
`POST /subscriptions`). Difference is the data SOURCE, set in the row:
- **Manual / wizard** → no file → `billing_model=percent_of_array`,
  `source_workbook=None`. Invoice = `allocation_pct × array generation` →
  the standard generated PDF (the one redesigned in §1).
- **Spreadsheet upload** → stores `source_workbook` (original .xlsx bytes) +
  `parsed_map`; each cycle `invoice_writer.populate_invoice_workbook` re-fills
  THAT file so the offtaker gets an invoice in their OWN format. This is a REAL
  capability ("bill in your format"), NOT legacy — fold, don't prune.

Spreadsheet upload was an orphan (standalone always-on card + absent from the
setup wizard). Fold pattern Ford approved:
- Tab: "＋ Add an offtaker" opens ONE panel with tabs **Type it in** / **Upload
  a spreadsheet** (dropzone + live doc-preview moved inside). `wireUpload()` +
  `renderDoc()` wire when the upload tab opens, not at load.
- Wizard step ③ gains "or upload your existing spreadsheet" → matches + creates
  the workbook sub IMMEDIATELY (defaults monthly·draft·to-me), flags it
  `from_upload:true`. Finish loop must `if (c.from_upload) {created++; continue}`
  so it doesn't double-create.

## 4. Offtakers tab merged into Invoice Generator (maintain-functionality refactor)

Ford drives consolidation as products mature ("delete X tab, do it through Y,
maintain functionality"). The Offtakers tab's detail editing (name/email/CC/
array/share/rate, via `saveCustCard` which reads `[data-f=...]` +
`.rb-cust-status`) was folded into each offtaker row's collapsible Edit panel
(`.rb-sub-more` "Offtaker details" section). `refreshList` now `fetchArrays()` +
`subCard(s, arrs)`. "Maintain functionality" = PROVE nothing lost: actually edit
a field through the new path and confirm it persisted via the API (he expects
this, not a description). Deleted dead `renderCustomers/custCard/wireCustCard`.

## 5. Overdesign-audit pattern (Ford asks "audit X for overdesign")

Method he rewards: render every state live (screenshot + vision_analyze) →
report findings WITH EVIDENCE → get steer (mcp_clarify with concrete trim
options) → execute → re-verify. Trims that landed on Reports: collapse offtaker
rows behind an "Edit ⌄" toggle (was ~11 always-on controls/row); drop redundant
discount chip (it showed 3× — chip + rate line + input → keep rate line +
input); remove decorative Solar Spiral from the customer-facing Quarterly report
(daily-bar is the real content); thin redundant eyebrow pills that duplicate the
heading/subtab name. CSS `[hidden]` trap: a class rule like
`.rb-sub-more{display:flex}` OVERRIDES the `hidden` attr → always-visible; add
`.rb-sub-more[hidden]{display:none}`.

## 6. auto_attach_gmp now defaults ON

Model + migration default `true`. One-time data flip for installs under the old
`false` default is GATED on the column's current default (Postgres
`information_schema.columns.column_default` contains "false") so it runs once and
never clobbers later per-offtaker opt-outs. Frontend reads
`auto = d.auto_attach_gmp !== false`. Removed the manual "＋ Attach GMP invoice"
button + its `attachGmp` handler — the captured bill attaches automatically.

## Cross-cutting reminders this session reinforced
- AO frontend deploy: Netlify CLI is wedged (stale session overrides token) →
  use `scripts/netlify_api_deploy.py` (REST file-digest deploy). Confirmed again.
- Secret-masker mangles inline `$(cat token)` / `$TOK` in shell AND sometimes
  garbles `write_file`/`terminal` tool-call ARGS mid-session (issue #15236) —
  re-issue the call; write tokens/scripts to a file and `bash` it.
- Backend ship: `git push origin HEAD:main` → Railway (~75–125s) → verify route
  is 401 not 500; pure-render changes (like invoice.py) need NO migrate. Stage
  ONLY your hunks on the shared tree (verify `git diff --cached --name-only`).
- Local QA: re-mint tenant token via `mint_session_for_tenant`; clean up test
  rows you create (subs/drafts) by id afterward.
