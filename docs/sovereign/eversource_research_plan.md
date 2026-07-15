# Eversource Portal Research Plan

**Utility**: Eversource  
**State**: CT, MA, NH (multi-state investor-owned utility)  
**Status**: Research required — no portal family identified yet  
**Created**: 2026-07-15  

## Background

Eversource is a major investor-owned utility serving Connecticut, Massachusetts, and New Hampshire. Multiple utility-add requests have queued this utility, but we lack:

1. **Portal URL** — The customer-facing login portal (likely `eversource.com` or a subdomain)
2. **Portal family** — Is this:
   - A **SmartHub/NISC** deployment (can auto-add via existing adapter)
   - A **bespoke** portal (requires HAR capture + custom adapter)
3. **Authentication flow** — Login mechanism, session management, CSRF tokens
4. **Data endpoints** — How usage/billing data is retrieved (JSON API, HTML scraping, etc.)

## Research Tasks

### 1. Identify Portal URL
- [ ] Navigate to `eversource.com` and locate the customer login portal
- [ ] Document the exact login URL (e.g., `https://www.eversource.com/myaccount`)
- [ ] Check if different states use different subdomains (CT vs MA vs NH)

### 2. Determine Portal Family
- [ ] **SmartHub check**: Does the login page or source code reference "SmartHub" or "NISC"?
  - If YES → Add to `api/data/providers/{CT,MA,NH}.csv` with `smarthub_host` column
  - SmartHub utilities need NO custom adapter (handled by `api/adapters/smarthub.py`)
- [ ] **Bespoke check**: If not SmartHub, document the portal technology:
  - Framework (React, Angular, server-rendered HTML?)
  - Authentication method (form POST, OAuth, SSO?)
  - Session management (cookies, JWT, etc.)

### 3. Capture Authentication Flow (Bespoke Only)
- [ ] **HAR capture required**: Use browser DevTools to record a full login session:
  1. Open DevTools → Network tab → "Preserve log"
  2. Navigate to login page
  3. Enter test credentials and log in
  4. Navigate to usage/billing data
  5. Export HAR file (right-click → "Save all as HAR")
- [ ] Document key requests:
  - Login endpoint (URL, method, payload structure)
  - Session tokens (cookies, headers)
  - CSRF/anti-forgery tokens

### 4. Map Data Endpoints (Bespoke Only)
- [ ] Identify how usage data is retrieved:
  - JSON API endpoints (preferred)
  - HTML pages requiring scraping
  - PDF/CSV downloads
- [ ] Document request parameters:
  - Date range format
  - Account/meter identifiers
  - Required headers/cookies
- [ ] Sample response structure (redact PII)

## Next Steps

### If SmartHub
1. Add Eversource to the appropriate state CSV(s) in `api/data/providers/`:
   ```csv
   code,label,state,smarthub_host
   eversource_ct,Eversource (CT),CT,eversource.smarthub.coop
   ```
2. Verify the SmartHub host is correct (test login via `api/adapters/smarthub.py`)
3. Mark utility request as `added` in `api/utility_requests.py`

### If Bespoke
1. **DO NOT** create a fabricated adapter without HAR evidence
2. Create adapter skeleton in `api/adapters/eversource.py` with:
   - Documented endpoints (from HAR)
   - Authentication flow (from HAR)
   - Data parsing logic (from real responses)
3. Add to `api/auto_adapters.py` registry
4. Add to provider CSVs with `adapter=eversource`
5. Test with real credentials before marking `added`

## Security Notes

- **Never commit** real credentials or PII in HAR files
- Redact account numbers, addresses, payment info from documentation
- Store HAR files locally only (add to `.gitignore` if needed)
- Test adapter against a real account before shipping

## References

- SmartHub adapter: `api/adapters/smarthub.py`
- Provider catalog: `api/data/providers/*.csv`
- Utility requests: `api/utility_requests.py`
- Existing bespoke adapters: `api/adapters/gmp.py`, `api/adapters/vec.py`

---

**Status**: Awaiting portal research. Do not mark utility as `added` until login flow is verified with real credentials.
