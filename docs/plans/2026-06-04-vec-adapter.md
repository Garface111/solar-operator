# Agent D — VEC adapter (NISC SmartHub)

## Context (verified by recon in browser session)

VEC = Vermont Electric Cooperative. Portal: https://vermontelectric.smarthub.coop
Built on NISC SmartHub — same UI/API likely powers Washington Electric Coop and
other small VT munis/coops. Building this adapter well unlocks N utilities.

Test account observed:
- Login: pbozuwa@gmail.com
- Customer: WEST GLOVER ROARING BROOK SOLAR LLC (solar generation entity)
- Account: 6578300
- Meter: 63698951
- Customer since: 2022-11-27

## Endpoints observed

### Billing history (HTML table)
Path: `/ui/billing/history` (Angular SPA — must scrape DOM, not raw HTML)
Each row gives: Account #, Auto Pay flag, customer LLC name, address, Billing
Date (MM/DD/YYYY), Bill Amount, Adjustments, Total Due, plus a "View Bill" link.

### Bill PDF download
URL pattern:
```
/services/secured/billPdfService/{YYYY_MM_DD}_{accountId}.pdf
  ?account={accountId}
  &timestamp={epochMs}
  &uuid={billUuid}
  &systemOfRecord=UTILITY
```
The `timestamp` (epoch ms) corresponds to the billing date and the `uuid`
is per-bill. Both come from the billing-history row.

### Usage data
Path: `/ui/#/usageExplorer`
Backed by: `POST /services/secured/utility-usage` with body containing
`accountNumber`, `serviceLocationNumber`, `industries=[ELECTRIC]`,
`timeFrame=MONTHLY`, `usageType=KWH`, `startDateTime`, `endDateTime`.

Critically: the rendered chart exposes EVERY data point as an `aria-label` on
SVG `<image>` elements. Pattern:
```
"Jun 2023 Billing Period. Usage Dates: May 18 - June 17.
 Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 58 °F"
```
This is the easiest scrape path — bypasses CSRF on the JSON API entirely.

### Settings/account list
Path: standard SmartHub "Manage Accounts" under Settings. Lists every account
the login has access to with account number, address, and active/inactive flag.
This is where you find multi-array tenants.

## Auth model

Cookie-based session after login form POST. JSON API requires the right
combination of cookies + CSRF headers — a direct fetch from a stale console
context got 403. Inside a real session (extension context, user's actual
browser), all calls go through. **The extension scrapes DOM + intercepts
XHR — it does NOT replay auth server-side.** Same model as the GMP adapter.

## Tasks

### 1. Add VEC host permissions to the extension
Edit `extension/manifest.json` host_permissions to include
`https://vermontelectric.smarthub.coop/*` and any other smarthub.coop subdomain
patterns. Also add a content_scripts entry so the adapter loads on VEC pages.

### 2. Build extension adapter `extension/src/adapters/vec.js`
Mirror the structure of `extension/src/adapters/gmp.js`. It must:
- Detect the current page (billing history vs usage explorer vs other) by URL
- Parse the billing-history Angular table rows → return Bill objects with
  {billing_date, amount, due, adjustments, pdf_url, account_id, customer_name}
- Parse the usage-explorer chart aria-labels → return UsageRow objects with
  {period_label, period_start, period_end, meter_id, kwh, avg_temp_f}
- POST results to the existing backend ingest endpoint with utility="VEC"
- Handle multi-account: walk the account switcher, capture data per account

### 3. Adapter router (`extension/src/adapters/index.js` or similar)
The extension currently runs only on GMP domains. Add a dispatcher that picks
the right adapter by hostname:
- gmp.com → gmp.js
- vermontelectric.smarthub.coop → vec.js
- Any other smarthub.coop hostname → vec.js with a runtime warning ("untested
  on <hostname>, treating as SmartHub")
This dispatcher should be the single content_script entry point.

### 4. Backend adapter `api/adapters/vec.py`
- Parallel to `api/adapters/gmp.py` for any server-side processing or
  reconciliation. At minimum: a class with `parse_bill(payload)` and
  `parse_usage(payload)` methods that normalize VEC's shape to the same
  Bill/UsageRow rows that GMP produces.
- Register it in whatever adapter registry GMP uses.

### 5. Data model: utility flag on UtilityAccount
Check `api/models.py` for `UtilityAccount.utility` field. If absent, add a
column (default "GMP" for backward compat) and write a migration in
`api/migrate.py`. If present but only ever "GMP", confirm it accepts "VEC".

### 6. Tests
- Unit tests for `vec.py` adapter using fixtures captured from the recon
  (billing-history HTML snippet + usage-explorer SVG aria-label samples).
- Save the test fixtures under `tests/fixtures/vec/` so future utilities can
  follow the pattern.

### 7. Documentation
Add `docs/adapters/vec.md` summarizing what's in this plan, the test account,
known limits ("kWh always shows 0 for this account because it's
generation-only — need a real production-side account to verify production data
shape"). Keep it short and actionable.

## SCOPE — only touch these areas
- `extension/`
- `api/adapters/`
- `api/models.py` AND `api/migrate.py` (only if utility column needs adding)
- `tests/` under a vec/ subfolder
- `docs/adapters/vec.md`

## DO NOT TOUCH
- `web/app/`, `web/onboarding/` (Agent E owns frontend copy)
- `api/account.py`, `api/app.py` unless adapter registration genuinely
  requires it — and if so, keep changes minimal
- Stripe code, anything in `api/stripe_*`
- Marketing site
- Onboarding flow

## DELIVERABLES
- Branch `agent/vec-adapter` with all commits
- 5-line summary: (1) files touched, (2) verification incl. test results,
  (3) deviations / assumptions, (4) what Ford should know before Agent E
  is reviewed, (5) confidence 1-10

## Known caveats to flag honestly
- This recon was done on ONE account that shows 0 kWh every month (generation-
  only meter). The usage-side parsing path is theoretical based on the chart
  format — should be solid (aria-labels are templated) but verify with a real
  production-side account when Bruce gives us one.
- VEC's JSON API requires session CSRF tokens — direct backend replay is not
  done in this task and is not needed (we scrape via extension). If a future
  feature needs server-side polling, that's a separate effort.
