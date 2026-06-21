# SmartHub capture & fleet-learning — implementation reference (Jun 2026)

Condensed from the v1.6.x build sessions. For when extending utility capture.

## NISC SmartHub 26.x zero-click capture (the WEC breakthrough)

- After login, the SPA hash carries base64 session params:
  `#/home?<base64 of "includeInactive=false&custNbr=…&acctNbr=…&userId=…">`
  → decode with atob, parse as URLSearchParams.
- Cookie-authed JSON endpoint with full billing history INCLUDING kWh + meter-read dates:
  `GET /services/secured/billing/history/overview?acctNbr=NNN` (credentials: "include")
  Fields: acctNbr, billingDateTimestamp, adjustedBillAmount, totalAdjustments,
  billProcessUuid, totalUsage (kWh), servLocs[0].address, lastBillPrevReadDtTm/lastBillPresReadDtTm.
- Home page DOM: one h2/h3 per account, text `"982501 - 1519 WRIGHTS MTN ROAD, ..."`;
  customer name in `.header-text` span (ALL CAPS).
- Auth token intercept: monkey-patch window.fetch, watch `/services/oauth/auth/v2`
  responses for authorizationToken + primaryUsername.

## Layout variance observed in the wild (n=2, expect more)

- VEC (older deployment): legacy 8-column flat table, View Bill href contains
  billPdfService with uuid/timestamp/account query params.
- WEC (26.x responsive): 5 mat-cells with `data-label` attrs (Account/Billing Date/
  Paperless/Adjustments/Total Due); View Bill is an Angular click handler, NO href.
- Usage aria-labels: VEC `"Meter N - Consumption - kWh: X kWh"`, WEC omits the middle
  segment: `"Meter N - kWh: 1,137 kWh"` (note comma). Regex keeps type segment optional:
  `Meter\s+(\d+)\s+-\s+(?:[^\n\-]+?\s+-\s+)?kWh:\s+([\d,.]+)\s+kWh`

## Server-side SmartHub API (universal adapter)

- Auth: `POST /services/oauth/auth/v2` form-encoded userId+password → authorizationToken.
  Session ~300s. Headers: Bearer + `X-Nisc-Smarthub-Username: <email>`.
- Meters: `GET /services/secured/user-data?userId=<primaryUsername>` →
  serviceLocationToUserDataServiceLocationSummaries; electric key usually "ELEC"
  (detect via serviceToServiceDescription containing "electric").
- Usage: `POST /services/secured/utility-usage/poll` timeFrame=DAILY, epoch-ms range,
  retry while status==PENDING. flowDirection FORWARD=consumption, RETURN=generation,
  NET (negative=export, takes priority).

## Discovered-utility loop (closes the misattribution bug class)

- Codes: `sh_<sanitized subdomain>` (lowercase alnum+underscore, ≤37 chars —
  provider column VARCHAR(40)). Minted identically extension-side
  (smarthub_registry.js detectProvider fallback) and backend-side
  (derive_provider_from_host).
- Backend treats payload `user.hostname` as AUTHORITATIVE over the claimed provider
  code — corrects legacy extensions claiming "vec" on other hosts.
- get_adapter routes any `sh_*` to the smarthub module.
- DiscoveredUtility row per host: capture_count, last_capture_method (api|dom|usage|miss),
  last_extension_version, alerted_at (one-time send_internal_alert), promoted_code.
- Promotion: scripts/promote_discovered_utility.py — appends provider CSV row,
  regens registry, backfills UtilityAccount/UtilitySession provider sh_*→curated.
- Drift radar: payloads carry captureMethod+extensionVersion; if ALL layers return
  0 rows on a billing/usage page the extension POSTs /v1/extension/scrape-miss (once/page).

## Domain naming conventions

- Array nickname = service ADDRESS (physical identity), title-cased via _title_address;
  customerName names the CLIENT. ("RICHARD G EVANS" arrays under a "Richard G Evans"
  client was the bug.)
- SmartHub scraped names arrive ALL-CAPS — title-case for display; never use a raw
  email as a client name.
- Provider catalog source of truth: api/data/providers/<STATE>.csv (smarthub_host column);
  api/providers.py derives SMARTHUB_HOSTS; registry JS is codegen.
