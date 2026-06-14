"""
Array Operator — automatic billing reports.

Upload a solar net-metering billing workbook (the HCT Sun Enterprises family:
a per-customer data ledger sheet + an invoice "Template" sheet + an annual
true-up) → the matcher recognizes its schema regardless of customer/sheet name
→ the generators rebuild the monthly invoice + a short performance summary →
the scheduler emails them on the cadence the operator chose.

Modules:
  matcher  — match_billing_workbook(bytes) → BillingMatch (schema detection)
  invoice  — render_invoice_xlsx / render_invoice_pdf
  summary  — render_summary_xlsx / render_summary_pdf
  routes   — FastAPI endpoints under /v1/array-operator/billing
"""
