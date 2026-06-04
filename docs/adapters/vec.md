# VEC Adapter — Vermont Electric Cooperative (NISC SmartHub)

## Platform
Portal: https://vermontelectric.smarthub.coop  
Technology: **NISC SmartHub** — cookie-based session auth, Angular SPA frontend.

NISC SmartHub is used by several VT utilities:
- vermontelectric.smarthub.coop (VEC) — primary target, tested
- washingtonelectric.smarthub.coop (WEC) — untested, same code should apply
- stoweelectric.smarthub.coop (SED) — untested

The `vec_content.js` extension script runs on all `*.smarthub.coop` subdomains and
logs a runtime warning for any host other than vermontelectric.

## Auth Model
Cookie-based. No capturable JWT or localStorage token. The extension scrapes
already-rendered DOM while the user is logged in — no credentials are stored or
replayed. Server-side bill pulls are NOT implemented (would require session cookies
that the extension cannot safely export).

## Test Account (from recon, Jun 2026)
- Login: pbozuwa@gmail.com
- Customer: WEST GLOVER ROARING BROOK SOLAR LLC
- Account: 6578300
- Meter: 63698951

**Known limitation:** This account shows 0 kWh every month — it is a
generation-credit account, not a metered generation account. The aria-label
scraping path is structurally correct but MUST be verified against a real
generation-metered VEC account before trusting NEPOOL-GIS data from VEC clients.
Get Bruce to confirm he has VEC clients before investing more.

## Extension Scraping (vec_content.js)

### Billing History (`/ui/billing/history`)
Angular table. Columns: Account# | AutoPay | CustomerName | Address |
BillingDate (MM/DD/YYYY) | BillAmount | Adjustments | TotalDue | [ViewBill link]

The "View Bill" link URL encodes: accountId, timestamp (epoch ms), uuid per bill.
These are captured for future PDF download, not fetched today.

### Usage Explorer (`/ui/#/usageExplorer`)
SVG chart. Each `<image>` element has an `aria-label` with this NISC template:

```
Jun 2023 Billing Period. Usage Dates: May 18 - June 17.
 Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 58 °F
```

This is the primary kWh data source. Meter type can be "Consumption",
"Generation", or other NISC-defined labels — the parser accepts any word.

## Backend Adapter (api/adapters/vec.py)

### `parse_extension_payload(payload)` → normalized dict
Returns the standard shape for /v1/sync: provider, auth (empty), user, accounts.
Also returns `bills_raw` and `usage_raw` for future bill-storage pass.

### `parse_bill(row)` → bill dict
Normalizes one billing-history row from the extension.
Fields: account_id, customer_name, service_address, billing_date, bill_amount,
adjustments, total_due, pdf_url, bill_uuid, bill_timestamp.

### `parse_usage(aria_label)` → usage dict | None
Parses one NISC SmartHub aria-label string.
Fields: period_label, usage_dates_raw, meter_id, kwh, avg_temp_f, period_start, period_end.
Returns None if the label doesn't match the expected format.
Year-wrap is handled: Jan billing dates like "Dec 18 - Jan 19" get Dec assigned
to billing_year - 1.

## What's NOT Done Yet
- **Bill storage from extension data**: the /v1/sync endpoint stores accounts +
  sessions but not scraped bill/usage rows. Adding this requires a small app.py
  change to call `parse_bill` / `parse_usage` on the payload and upsert Bill rows.
  Deferred to the next VEC task.
- **Server-side pull**: worker.py will skip VEC accounts (no `fetch_bills_json`
  defined on the adapter). Bills only land in the DB via extension push.
- **PDF fetch**: VEC bill PDFs require session cookies — no server-side download.
  The PDF URL + uuid are captured for potential future extension-side fetch.
- **Multi-account walk**: the extension doesn't yet visit the account-switcher
  page; it only scrapes whichever account is displayed. Walk logic is future work.
