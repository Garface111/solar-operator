# NISC SmartHub capture internals (verified June 2026)

Knowledge bank from the WEC immediate-capture build. Applies to any
`*.smarthub.coop` deployment (NISC platform, ~500+ US co-ops/munis).

## Auth & session
- Login API: `POST /services/oauth/auth/v2` with form-encoded `userId` + `password`
  → JSON with `authorizationToken` (+ `primaryUsername`). Session ~300s.
- Bearer headers for secured endpoints:
  `Authorization: Bearer <token>`, `X-Nisc-Smarthub-Username: <email>`.
- The extension also passively intercepts this response via a `window.fetch`
  monkey-patch (only matching `/services/oauth/auth/v2`) to harvest the token
  without asking for credentials.

## SPA hash credentials (26.x — key to immediate capture)
After login the Angular SPA lands on:
```
#/home?<base64("includeInactive=false&custNbr=…&acctNbr=…&userId=…")>
```
`atob()` the hash query segment, sanity-check it contains `acctNbr|custNbr|userId`,
parse with `URLSearchParams`. This gives the account number on the HOME page —
no navigation needed. Account numbers also appear in home-page `h2/h3` headings
shaped `"982501 - 1519 WRIGHTS MTN ROAD, BRADFORD, VT 05033"`; the customer
holder name appears in `.header-text` spans inside mat-cards (ALL CAPS).

## Billing history JSON API (cookie-authed, no bearer needed from page context)
```
GET /services/secured/billing/history/overview?acctNbr=NNNNNN   (credentials: include)
```
Returns array of bill rows. Useful fields per row:
- `acctNbr`, `custNbr`, `billProcessUuid`
- `billingDateTimestamp` (epoch ms)
- `adjustedBillAmount`, `totalAdjustments`
- `totalUsage` — kWh, present inline (richer than DOM scrape)
- `servLocs[0].address` → `{addr1, city, state, zip}`
- `servLocs[0].lastBillPrevReadDtTm` / `lastBillPresReadDtTm` — meter-read
  period (epoch ms) → maps to Bill.period_start/period_end

## Usage poll API (server-side, bearer-authed)
```
POST /services/secured/utility-usage/poll
{ timeFrame: "DAILY", userId: <email>, screen: "USAGE_EXPLORER",
  serviceLocationNumber, accountNumber, industries: ["ELECTRIC"],
  startDateTime: "<epoch ms as string>", endDateTime: "..." }
```
- Poll until `status == "COMPLETE"` (retry ~3x, 5s apart).
- Response: `data.ELECTRIC[]` entries; the one with `type == "USAGE"` carries
  `series[]` (name → data points `{x: epoch ms, y: kwh}`) and `meters[]`
  (`seriesId`, `flowDirection`).
- flowDirection channels: FORWARD = consumption, RETURN = generation credited,
  NET = combined (negative = export; NET takes priority when present).
  VEC confirmed FORWARD+RETURN; other co-ops unverified.
- Service-location discovery: `GET /services/secured/user-data?userId=<primaryUsername>`
  → `serviceLocationToUserDataServiceLocationSummaries`. Electric service key is
  usually `ELEC` (fallbacks: 1ELEC, VELEC, GELEC) — detect by scanning
  `serviceToServiceDescription` values for "electric".

## DOM layouts seen in the wild
- Layout A (legacy, VEC): flat 8-column `<table>`; PDF link href contains
  `billPdfService`; columns: Account / AutoPay / CustomerName / Address /
  BillingDate / BillAmount / Adjustments / TotalDue.
- Layout B (NISC 26.x responsive, WEC): 5 `mat-cell`s with `data-label`
  attributes (Account / Billing Date / Paperless / Adjustments / Total Due);
  "View Bill" is an Angular click handler with NO href. Parsers keyed on
  `cells.length >= 8` or `a[href*='billPdfService']` return 0 rows here.
- Bill PDF (legacy path): `/services/secured/billPdfService/{YYYY_MM_DD}_{acct}.pdf`.

## Usage-explorer aria-label variants
Standard: `"Jun 2023 Billing Period. Usage Dates: May 18 - June 17. Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 58 °F"`
WEC variant omits the middle type segment: `"… Meter 29747 - kWh: 1137 kWh"` —
regex must make the `- <type> -` segment optional and accept comma-formatted kWh.

## Live verification status (June 2026)
- WEC (washingtonelectric.smarthub.coop): hash-creds + overview API verified
  live (Rick Evans acct 982501).
- VEC (vermontelectric.smarthub.coop): legacy DOM path verified; 26.x hash-creds
  path same-platform but unconfirmed against a real VEC login since the change.
- Alternate WEC hostname exists: weci.smarthub.coop (registry maps it to wec).
