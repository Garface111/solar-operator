# Array Operator owner-site UX — Reports consolidation, folding, sandbox defaults

Vanilla-JS owner site at `/root/array-operator/public/` (separate repo, Netlify
manual deploy). This ref captures the de-clutter / consolidation / default-state
work Ford drives as the product matures. He thinks in END-USER journey terms and
files UX confusion as first-class bugs.

## Reports tab — current structure (Jun'26, after consolidation)
- **2 subtabs**: "Offtaker Invoice Generator" + "Quarterly reports". The old
  "Offtakers" subtab was DELETED and folded into the Invoice Generator.
- Per-offtaker row is COLLAPSED: name + status chips + rate line + 3 buttons
  (Draft invoice / Preview / Edit). The Edit toggle reveals `.rb-sub-more` with
  delivery/send-to/discount AND the folded-in offtaker DETAILS (name, email, CC,
  array, share %, rate) + "Save details" (reuses `saveCustCard`). The old tab's
  always-expanded cards became this expander — accept the +1-click tradeoff for a
  scannable list.
- "＋ Add an offtaker" lives in the offtaker-LIST header and opens ONE tabbed
  panel: "Type it in" / "Upload a spreadsheet". The standalone upload card was
  folded into this (no competing front doors). The upload zone + live doc-preview
  live inside the upload tab; `wireUpload()/renderDoc()` are called when that tab
  opens, not at load.
- The setup WIZARD step ③ also has the spreadsheet-upload path (creates the
  workbook sub immediately with monthly/draft/to-me defaults; Finish skips
  `from_upload` entries so nothing double-creates).
- GMP bill auto-attach is ON by default (model `auto_attach_gmp` defaults true +
  a one-time gated migration flipped existing rows); the manual "+ Attach GMP
  invoice" button was removed (captured bill attaches automatically).

## Spreadsheet upload is NECESSARY, not legacy
It stores the owner's original .xlsx bytes (`source_workbook`) and re-populates
THAT file each cycle (`invoice_writer.populate_invoice_workbook`) — bills in the
customer's own recognized format. Manual/wizard subs fall back to the generic
generated invoice. All three creation paths converge on ONE
`BillingReportSubscription`; only the data source differs. FOLD it in, don't prune.

## /match response shape gotcha
`POST .../match` nests its result under a `match` key:
`{ok, filename, match:{matched, customer:{name,email}, ...}}`. Read
`const m = data.match` THEN `m.matched` — reading `m.matched` off the top level
silently fails ("Couldn't recognize that workbook" on a valid file). Mirror the
tab path when adding new upload entry points (the wizard path hit this).

## Trends tab — all 4 visualizations stacked in a column (no tabbing through)
`teardown()` must clean up EVERY mounted view (it tracked one). Per-array filter:
`/fleet-trends?array_id=N` scopes aggregates to one owned array while `by_array`
stays full-fleet so the dropdown can switch; unowned id → 404; empty scoped array
returns `years:[]` → show inline empty state, keep the dropdown (never strand).

## Arrays sandbox — default to the whole-fleet picture
Owners should land zoomed-out, horizontal, all inverters revealed:
- view defaults to "canvas" (not "grid"); `getViewMode()` returns canvas unless
  the stored value is exactly "grid".
- inverters default EXPANDED: `hasExpandPref()` (is `EXPAND_KEY` set?) → when
  false, render every column with the `expanded` class (`expandAllDefault`).
  Respects the owner's choice once they collapse anything.
- orientation already defaults horizontal; `fitView` already auto-zooms on first
  render (`_fitDone` gate, zoom floored 0.72). The Show-all / Overview toggles
  read live DOM state so they stay correct against the new defaults.

## Deploy (AO frontend is MANUAL)
`git push origin main` updates GitHub only. Deploy via the REST API script
(CLI auth is wedged): `python3 scripts/netlify_api_deploy.py` (site
array-operator-ea). reports.js/sandbox.js use unescaped double-quotes in template
literals — patches must not backslash-escape. Visual-QA every UI change over
localhost http (never file://) with Playwright + vision_analyze.
