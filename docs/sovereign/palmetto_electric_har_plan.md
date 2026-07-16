# Palmetto Electric Cooperative – HAR Capture Plan

**Date:** 2026-01-XX  
**Utility:** Palmetto Electric Cooperative  
**Status:** Awaiting HAR capture (succession stage)  
**Provider ID:** TBD (will be assigned after HAR analysis)

## Context

This is a **full succession HAR stage** request. The utility has been authorized for capture, but we do not yet have:

- The utility's portal URL
- Login endpoint details
- Data/usage API endpoints
- Confirmation of underlying platform (SmartHub, custom, etc.)

## Next Steps

### 1. HAR Capture

Once the HAR file is available from the succession capture process:

1. **Identify the portal URL** – Look for the base domain in HAR entries
2. **Parse authentication flow** – Find POST requests to login/oauth endpoints
3. **Locate data endpoints** – Identify usage/billing data API calls (JSON responses preferred)
4. **Check for known platforms:**
   - **SmartHub (NISC)**: Look for `*.smarthub.coop` or `/services/oauth/auth/v2`
   - **Other known adapters**: Check against existing patterns in `api/adapters/`

### 2. Adapter Development

**If SmartHub:**
- Add entry to appropriate state CSV in `api/data/providers/`
- No new adapter code needed (uses `api/adapters/smarthub.py`)
- Provider code: use subdomain or assign unique code
- Mark utility as `added` in `utility_requests` table

**If Custom Portal:**
- Create `api/adapters/palmetto_electric.py` following patterns from:
  - `api/adapters/vec.py` (session-based)
  - `api/adapters/gmp.py` (complex auth)
  - `api/adapters/locus.py` (JSON API)
- Document endpoints with actual URLs from HAR (no invention)
- Include error handling for rate limits, session expiry
- Add provider entry to appropriate state CSV

### 3. Verification

- Test login flow with real credentials (in controlled environment)
- Verify data parsing for daily kWh values
- Check date range handling (historical + recent)
- Confirm net metering support if applicable

## Research Notes

**Palmetto Electric Cooperative** is likely a South Carolina electric cooperative. Common patterns:

- Many SC co-ops use **SmartHub/NISC** platforms
- Some use custom portals or third-party billing systems
- Portal often follows pattern: `palmettoelectric.com` or `*.smarthub.coop`

**No adapter will be written until HAR evidence confirms the actual endpoints.**

## Deliverables After HAR Review

1. Provider CSV entry (state determined from HAR/research)
2. Adapter code (if not SmartHub)
3. Update to `utility_requests` table marking status as `added`
4. Internal alert confirming utility is live

---

**Status:** 🟡 Blocked on HAR availability  
**Assigned Agent:** Research complete; awaiting succession HAR delivery
