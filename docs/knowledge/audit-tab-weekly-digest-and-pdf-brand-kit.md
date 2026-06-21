# Audit tab, weekly digest, and the shared PDF brand kit

New owner-facing surfaces shipped Jun'26 on Array Operator. The reconciliation
ENGINE already existed (api/reconciliation/reconcile_array) but had no route, no
tab, no schedule — "a brain wired to nothing." The pattern for surfacing it.

## Audit tab (the settlement auditor's face)
- Backend route: `GET /v1/array-owners/fleet-audit` (api/array_owners.py, mirror
  fleet-trends auth via `_tenant_from_bearer`). Runs `reconcile_array` across
  every owned array → fleet summary {total, auditable, ok, leak,
  leak_unconfirmed, incomplete_monitoring, insufficient_data, have_settlement,
  have_production, dollars_flagged, coverage_pct} + sorted per-array rows
  (leaks first). READ-ONLY, never fabricates — unauditable array =
  "insufficient_data", never a fake leak. Per-array try/except so one bad array
  can't 500 the fleet.
- Frontend tab (the AO new-tab pattern, per memory): add to index.html nav +
  panel + audit.css/audit.js includes; sandbox.js TABS map + tabFromHash +
  applyView dispatch (`window.__aoLoadAudit`); session key `so_session`. New
  files audit.js + audit.css, slick dark design matching trends/reports (conic
  coverage ring, dollars-flagged hero, status filter chips, per-array verdict
  rows with status spine + production-vs-settlement compare + $ at risk).
- Integrity guard (the moat's honesty): a gap backed only by UTILITY-sourced
  production (GMP meter) = `leak_unconfirmed` (shows $, says "connect inverter
  monitoring to confirm"), NOT an asserted `leak`. A confirmed leak needs an
  INDEPENDENT feed (solaredge/csv/manual). Variance hue is status-aware in the
  UI (leak=red, unconfirmed=gold) so a money gap never reads as green-positive.
- QA an empty-data tab honestly: seed a temp demo tenant (active=True,
  product="array_operator") with arrays spanning every status to screenshot the
  full spread, then CLEAN IT UP. The real fleet shows mostly "needs data" until
  GMP daily refresh fills the production leg.

## Weekly audit digest (scheduler)
- api/scheduler.py `deliver_weekly_audit_digest`, cron Mondays 13:00 UTC (after
  the morning refresh + 09:00 reports). Runs the audit per active
  product="array_operator" tenant → emails the owner (tenant.contact_email) an
  AO-themed digest (render_email_skin product="array_operator"): dollars flagged
  headline, flagged-array table, coverage line, Audit-dashboard CTA.
- HONEST by construction: sends an "all clear" when nothing's flagged (not
  silence), never invents a leak, SKIPS owners with no bills (have_settlement==0)
  so no empty noise. Reuses `reconcile_array` via a `_build_audit_for_tenant`
  helper (same summary shape as the route).
- VERIFY a new email end-to-end by actually sending through Resend to a seeded
  demo tenant (set RESEND_API_KEY in the script; the local DB usually has no
  key so _send_via_resend just logs). Confirm Resend last_event=delivered.

## Shared PDF brand kit (invoice + performance summary)
- api/billing/_pdf_brand.py = ONE source of truth: palette (from arrayoperator
  styles.css :root), dark "energy" hero band (brand mark + title + right
  headline figure + footer, drawn on the reportlab canvas), and the juicy
  green-gradient energy bar chart as a real Flowable. No new deps (reportlab
  canvas only). invoice.py + summary.py both import it — identical hero + chart.
- Invoice = produced kWh × rate; the monthly/TTM energy bar chart plots only
  months that carry a real kWh value (honest empty state otherwise, never
  fabricated bars). Stat cards need vertical breathing room (eyebrow / value /
  sub) — Ford flagged cramped rows; add explicit bottom-padding + leading.
- Quarterly reports inherit the look automatically (quarterly is a cadence, same
  invoice+summary generators — no separate generator).
- Rasterize the PDF (pymupdf) → vision_analyze to QA the real document; fix
  header/logo overlap and ring/legibility before claiming done.

## Folding/consolidation pattern (Ford's de-clutter drives)
When Ford says "delete tab X, do it through Y, maintain functionality": it's a
behavior-preserving refactor. Fold the deleted surface's UNIQUE capability into
Y (e.g. Offtakers tab → per-row Edit panel; standalone spreadsheet-upload card →
"Add an offtaker" tabbed Type-it-in/Upload panel + the setup wizard). PROVE
nothing was lost — actually edit a field through the NEW path and confirm it
persisted to the backend, then revert the test edit. Trace prune-vs-fold first
(is the feature legacy or necessary?) and let him choose. Spreadsheet upload was
NECESSARY (re-populates the owner's own .xlsx each cycle) — fold, don't prune.
PITFALL: the /match endpoint nests its result under a `match` key — read
`data.match.matched`, not `data.matched` (the wizard upload bug).
