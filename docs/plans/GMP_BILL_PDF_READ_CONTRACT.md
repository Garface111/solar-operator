# GMP Bill-PDF READ Contract  (ingestion ↔ Reports auto-attach)

> **STATUS: IMPLEMENTED (v1).** Both halves are now built by the Reports agent
> (the other agent stopped): the READ/auto-attach side AND the durable
> persistence + capture job. The only remaining external dependency is a valid
> GMP session token (auth) — see "Auth blocker" below.

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

## What's now built (Reports agent, both halves)
- **Durable storage:** `Bill.pdf_bytes` (LargeBinary) + `Bill.pdf_content_type`
  on the bills table (migration in api/migrate.py). `pdf_path` is kept but is
  ephemeral; the bytes are the durable source.
- **Capture job:** `api/worker.py`
  - `_pull_via_pdf` (fallback path) reads the downloaded PDF bytes and persists
    them in-row.
  - `_capture_current_bill_pdf` runs on the JSON-first path (the normal one):
    after upserting bill metrics it fetches the account's CURRENT bill PDF via
    `gmp.fetch_bill_pdf(currentBillUrlBinary)` and stores the bytes on the newest
    bill row. Best-effort, never fails the pull; validates `%PDF` magic so an
    auth-redirect HTML page is never stored as a "PDF".
- **Read/attach:** `api/reports/gmp_bill_pdf_read.get_bill_pdf_for_period` reads
  `Bill.pdf_bytes`; `delivery.generate_files` auto-attaches when the per-customer
  `auto_attach_gmp` toggle is on.

### Scope note (current bill only, for now)
The capture persists the **current** bill PDF (the only one `currentBillUrl`
addresses). That covers the live monthly/quarterly invoice cycle. Historical
back-capture would need a per-bill PDF URL confirmed in the bill JSON
(`raw_json`); not done because GMP auth is blocked (can't introspect a live
sample). Add it later if a per-bill URL field is confirmed.

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

## Auth blocker (the ONE remaining dependency)
- GMP session token was blocked as of 2026-06-18 (stale token → 401; refresh
  endpoint → 403 AUTHORIZATION_FAILURE, likely rate-limit/lockout). GMP does not
  rotate the refresh_token. **Nothing pulls a PDF until a fresh GMP capture
  lands** — re-capture the owner's GMP session via the extension (the bills pull
  uses UtilitySession.api_token). Until then `_capture_current_bill_pdf` returns
  `{"saved": False, "reason": "fetch failed: ..."}` and auto-attach attaches
  nothing (never fabricated; surfaced honestly in the UI).
- Keep the existing MANUAL upload (`POST /drafts/{id}/gmp-invoice`) as the
  fallback — when auto-attach finds nothing, Paul can still upload by hand.
