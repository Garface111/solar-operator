# AlsoEnergy (PowerTrack) inverter API contract

Grounded live June 2026 against api.alsoenergy.com (OpenAPI spec at
api.alsoenergy.com/swagger/v1/swagger.json). AlsoEnergy is the monitoring layer
behind many VT community-solar arrays (hmi.alsoenergy.com public displays). UNLIKE
Chint/Fronius/SMA it is a CLEAN documented REST API → a backend credential pull
like SolarEdge/Locus, NOT extension scraping.

## Vendor wiring (matches Locus two-file pattern)
- `api/adapters/alsoenergy.py` — HTTP logic, token cache, exception family
  (AlsoEnergyError / AlsoEnergyAuthError / AlsoEnergyScopeError).
- `api/inverters/alsoenergy.py` — vendor wrapper (validate/fetch_live/fetch_daily/
  discover_sites). CODE="alsoenergy", LABEL="AlsoEnergy (PowerTrack)".
- Registered in `api/inverters/__init__.py` VENDORS; auto-surfaces in
  vendor_catalog (the /v1/array-owners/inverter-vendors endpoint iterates VENDORS).
- Frontend VENDORS list is HARD-CODED in BOTH /root/array-operator/public/sandbox.js
  AND public/onboarding.html (not auto-rendered from backend catalog) — add new
  vendors to both, plus the BRAND map in sandbox.js + layout-view.js + fleet-store.js.
- **Account cascade (Jul 2026):** `POST /v1/array-owners/alsoenergy/connect-account`
  `{username, password, site_ids?}` → discover_sites → match/create arrays +
  InverterConnection per site (idempotent). UI: discover:true, Site ID optional.
  Single-site still works via connect-single when site_id is set.
  Public preview: `_preview_sites_for_vendor("alsoenergy")` uses discover_sites
  when site_id omitted.
- NOT extension-scrape: PowerTrack is a documented REST API. Cloud Capture
  harvester treats alsoenergy as API-only (nightly `inverter_pull` uses stored
  InverterConnection credentials).

## Auth — OAuth2 password grant, NO client_id needed (verified live)
- POST {BASE}/Auth/token, Content-Type application/x-www-form-urlencoded.
  Body: grant_type=password, username, password. (client_id/client_secret are
  forwarded if present but NOT required — tested: bad creds → 403
  {"error":"Wrong email or password."}.)
- Response = OAuth2Model: {access_token, token_type, expires_in (sec),
  refresh_token, userId, ...}. Cache per-username; refresh via
  grant_type=refresh_token before falling back to a fresh password grant.
- All data calls: header `Authorization: Bearer <token>`. The REST API requires
  auth — there is NO unauthenticated public-data shortcut (the public-display
  GUID at hmi.alsoenergy.com only serves an HTML SPA shell; GET /Sites/{id}
  without a bearer → 401). So a public-display link alone CANNOT authenticate;
  the owner needs real PowerTrack login creds.

## Data flow
- GET /Sites → SiteNodes {items:[{siteId, siteName, alertCount}]}
- GET /Sites/{siteId} → Site {siteId, name, location, timeZone, productionData,
  performanceEstimate, ...} (no explicit nameplate field — derive best-effort)
- GET /Sites/{siteId}/Hardware?includeSummaryFields=true → HardwareList
  {hardware:[HardwareListItem{id, stringId, functionCode, flags, name,
  serialNumber?, config}], summaryFields:[...]}. INVERTERS = functionCode "PV"/
  "Inverter"; exclude gateways/weather/meters.
- POST /Data/BinData?fromLocalTime&toLocalTime&binSizes — body = array of
  BinDataField [{hardwareId, fieldName, function:"Avg"|"Sum"|"Diff"}]. Response
  DataBinResults {info:[{hardwareId, dataIndex}], items:[{timestamp, data:[]}]}.
  (v2/Data/BinData accepts an explicit IANA tz.)

## UNVERIFIED — needs a live AlsoEnergy login to confirm
The adapter tries PRIORITIZED candidate field names and uses the first that
returns data, logging what it finds. Confirm against a real fleet:
- AC power field: tries PowerAC, WAC, KW, KwAc, PowerAc, AcPower, PvKw, W.
- Energy field: tries EnergyAC, KWHnet, WHsum, KwhAc, EnergyAc, KWHac, KWHdel,
  WHdel, Wh, KWH (daily bin via function=Diff).
- Exact functionCode strings for inverters; the DataBinResults info/items column
  mapping; peak_power_kw derivation. Paul Bozuwa's Danville site (site 59947) is
  the natural fixture but he lacked creds — needs a real login to exercise.
