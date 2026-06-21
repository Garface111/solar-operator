# Array Operator billing & reports subsystem

Map of the customer-billing + reporting code so a future session doesn't
re-derive it. Three SEPARATE spreadsheet systems exist — conflating them is the
#1 confusion here:

| System | Direction | File | Purpose |
|--------|-----------|------|---------|
| GMCS filing | WRITE | `api/writers/gmcs_writer.py` | NEPOOL-GIS quarterly *filing* workbook (NEPOOL Operator product). NOT a customer invoice. |
| Workbook matcher | READ | `api/billing/matcher.py` | Recognizes an uploaded *customer billing* workbook (HCT family: fairlee/norwich/valley_cares), extracts the data-ledger sheet + column map + billing model. Powers POST /match and workbook onboarding. |
| Invoice writer | WRITE | `api/billing/invoice_writer.py` | Reproduces the customer's OWN uploaded format, populated for a period. |

Do NOT rename/move gmcs_writer or matcher — other code + tests depend on them.

## Invoice reproduction technique (own-format)
`populate_invoice_workbook(sub, period_data=None) -> bytes`:
- A workbook-sourced subscription stores the ORIGINAL file bytes in
  `sub.source_workbook` (BLOB) + the parsed structure in `sub.parsed_map`
  (a `BillingMatch.to_dict()`).
- The writer LOADS the stored original with `openpyxl.load_workbook(BytesIO(...))`
  (NOT data_only — keep formulas), appends ONE month row to the data-ledger
  sheet at the matched columns, COPIES the previous data row's full cell style +
  number-format so the new row is visually identical, FORWARD-TRANSLATES the
  downstream formulas (Tariff+Adder/Value/Bill/Savings) with
  `openpyxl.formula.translate.Translator`, and bumps the Template sheet's
  INDIRECT "New Row #" pointer so the invoice refreshes — preserving ALL the
  customer's styling/merged cells/Template sheet. Regenerating from scratch would
  throw that away — never do that.
- Handles all 3 billing models the matcher detects: `percent_of_array`,
  `fixed_budget`, `flat_rate`.
- Manual (typed-in) customers have NO `source_workbook` → fall back to the
  standard generated invoice. Only workbook customers have an "own format".
- NEVER fabricate: if the period has no generation, raise `InvoiceWriterError`.

## Key billing endpoints (all under /v1/array-operator/billing)
- `GET /subscriptions` — rows. `POST /subscriptions` — workbook upload OR manual
  (no-file: customer_name + array_id + allocation_pct).
- `PATCH /subscriptions/{id}` — now accepts `allocation_pct` (0..1) + `array_id`
  for inline % edit on the redesigned Reports tab.
- `GET /subscriptions/{id}/preview?kind=invoice&fmt=xlsx` — for WORKBOOK subs
  returns the populated own-format workbook (invoice_writer); manual subs get
  the standard generated invoice.
- `GET /subscriptions/{id}/preview-math` — draft-less auditable math
  (array period kWh × allocation_pct × rate) so the Reports rows show
  gen×%=$ eagerly without creating a draft.
- Draft inbox: POST `/subscriptions/{id}/draft`, GET `/drafts`,
  POST `/drafts/{id}/gmp-invoice`, PATCH `/drafts/{id}`,
  POST `/drafts/{id}/approve`, POST `/drafts/{id}/dismiss`. NEVER auto-send —
  every path ends at a human "Approve & send".

## All-time fleet report (Task added Jun'26)
- `api/reports/fleet_report.py` `build_fleet_report(tenant, fmt='xlsx'|'pdf') -> bytes`.
  Aggregates ALL-TIME generation by year/month + per-array from DailyGeneration
  (joined Array→Client→Tenant) + Bill aggregates, anti-double-counting like the
  GMCS writer (a month covered by DailyGeneration uses daily; else Bill).
  Reads live DB each call → AUTO-reflects new months (the "auto-refresh").
  Excel via openpyxl, PDF via reportlab (both already in requirements.txt).
- `GET /v1/account/fleet-report?fmt=xlsx|pdf` — SESSION-authed (tenant_from_session).
- UI: `DownloadReport` (Excel/PDF) on `EnergyHistoryView.tsx`.
  PITFALL fixed: it was gated behind hasData (absorbed BILLS), which hid the
  button when only GENERATION existed — the report aggregates generation too, so
  render the download control in the empty/no-bills state as well.

## Reports tab redesign (billing-run, Jun'26)
`web/app/src/screens/ReportsTab.tsx` replaced the NEPOOL QuarterCard scaffolding
with a billing-run layout: "Current billing run" hero, per-customer run table
(inline gen×%=$ math, editable allocation % pill, status chips), inline
"Add a customer" (prominent CTA in the empty state — don't bury it under the
empty-state text), Manage mode (pauses sending), Review drawer (editable draft +
PDFs + Download-invoice-your-format + Approve&send), durable History section.
Design mockups + handoff: `/root/solar-operator/sketches/reports-redesign/`.

## NOT yet done (flagged, not built)
- The Customers tab still carries NEPOOL-GIS onboarding language ("STEP 2 · ADD
  NEPOOL-GIS IDs", "Import NEPOOL IDs", "Vermont's REC market") — wrong for an
  Array Operator billing user. The shell branding (brand.ts) was fixed but this
  in-tab banner was not. De-NEPOOL it when touching the Customers tab.
