# GMP Bill-PDF READ Contract  (ingestion ↔ Reports auto-attach)

> **STATUS: CONSUMER-SIDE v0.** The Reports agent built the READ + auto-attach
> half. This document + `api/reports/gmp_bill_pdf_read.py` are the source of
> truth for what the INGESTION/extension agent must persist to make auto-attach
> light up. When the ingestion side lands durable PDF bytes, auto-attach works
> with zero further Reports-side change.

## The goal
Paul's offtaker invoices should ship with the **actual GMP utility bill PDF**
attached automatically — the right PDF for the right array and billing period —
so he never hand-uploads. Per-customer toggle:
`BillingReportSubscription.auto_attach_gmp` (default False).

## Ownership boundary
- **Ingestion/extension agent owns:** scraping bill rows off GMP (already done —
  `extension/vec_content.js` captures bill rows with direct `pdf_url`), pulling
  the PDF bytes (`api/adapters/gmp.fetch_bill_pdf` already downloads them), and
  **persisting those bytes durably**.
- **Reports agent (this side) is a READ-ONLY consumer.** It calls
  `gmp_bill_pdf_read.get_bill_pdf_for_period(array_id, period_start, period_end)`
  and attaches the returned bytes. It never scrapes, pulls, or writes.

## THE GAP to close (ingestion side)
Today `Bill.pdf_path` points at Railway's **ephemeral disk** → not durable, can't
be attached weeks later. To make auto-attach work, persist the verbatim PDF
**bytes in-row**, keyed by (utility_account_id, billing period):

```python
# Suggested additions to the Bill model (ingestion agent owns the migration):
Bill.pdf_bytes : LargeBinary | None       # the verbatim GMP bill PDF
Bill.pdf_content_type : str | None        # "application/pdf"
```

The read seam already reads `getattr(bill, "pdf_bytes", None)` defensively, so:
- Before the column exists / before bytes are captured → returns None →
  auto-attach attaches nothing (UI says "GMP bill will attach automatically once
  captured"). **Never fabricated.**
- After ingestion persists bytes → auto-attach attaches the right PDF, no
  Reports-side change needed.

## The read function (READ-ONLY, returns plain dict or None)
```python
from api.reports import gmp_bill_pdf_read as gbp

gbp.get_bill_pdf_for_period(array_id, period_start=None, period_end=None) -> {
    "bytes": <pdf bytes>, "filename": str, "content_type": "application/pdf",
    "account_id": int, "period_start": date|None, "period_end": date|None
} | None      # None until durable bytes are captured

gbp.has_capturable_gmp_account(array_id) -> bool   # array has a GMP account?
```

Matching: filters bills to the array's GMP accounts (provider=="gmp"), picks the
bill whose period overlaps [period_start, period_end] (newest-first), returns its
durable bytes. Account==meter; an array may sum several meters — the newest
matching bill with bytes wins.

## Blockers / notes
- GMP backfill auth was blocked as of 2026-06-18 (stale token / 403 on refresh);
  no PDFs can be captured until that's resolved. Auto-attach degrades silently
  (attaches nothing) until then.
- Keep the existing MANUAL upload (`POST /drafts/{id}/gmp-invoice`) as the
  fallback — when auto-attach finds nothing, Paul can still upload by hand.
