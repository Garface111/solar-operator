# Eversource Portal Research Plan

**Date**: 2026-07-15  
**Status**: Research phase — no adapter implementation yet  
**Utility**: Eversource  
**State**: Multi-state (CT, MA, NH)  
**URL**: Unknown (not provided in brief)  

## Context

Eversource is a major investor-owned utility serving Connecticut, Massachusetts, and New Hampshire. The brief requests credential staging + portal sign-off but provides:
- No portal URL
- No state-specific information
- No evidence of portal family (SmartHub vs. bespoke)
- No HAR capture

## Research Required

### 1. Portal Discovery
- **Action**: Identify the customer portal URL(s)
- **Expected**: Likely `eversource.com` or similar
- **Multi-state consideration**: Verify if CT/MA/NH share one portal or have separate login systems

### 2. Portal Family Classification
- **SmartHub/NISC**: Check if Eversource uses NISC SmartHub infrastructure
  - If YES: Add to `api/data/providers/<STATE>.csv` with `smarthub_host` column
  - If YES: No adapter code needed — existing `api/adapters/smarthub.py` handles it
- **Bespoke**: If custom portal:
  - Requires HAR capture of actual login flow
  - Requires reverse-engineering of API endpoints
  - Cannot safely implement without real traffic evidence

### 3. Authentication Architecture
- **Action**: Document login flow (username/password, MFA, OAuth, etc.)
- **Action**: Identify session management (cookies, tokens, headers)
- **Action**: Capture API endpoints for:
  - Authentication
  - Account/meter enumeration
  - Usage data retrieval (daily/hourly kWh)

### 4. Data Availability
- **Action**: Confirm portal exposes:
  - Historical usage data (daily minimum)
  - Meter-level granularity
  - Net metering / solar production data (if applicable)

## Implementation Blockers

**Cannot proceed with adapter implementation because**:

1. **No portal URL** — Cannot verify login endpoint
2. **No HAR capture** — Cannot reverse-engineer API calls
3. **No SmartHub confirmation** — Cannot use existing universal adapter
4. **No state specificity** — Multi-state utility may have regional variations

## Next Steps

### If SmartHub (Low Effort)
1. Confirm SmartHub subdomain (e.g., `eversource.smarthub.coop`)
2. Add one line per state to CSV catalogs:
   ```csv
   eversource_ct,Eversource (CT),CT,eversource.smarthub.coop
   eversource_ma,Eversource (MA),MA,eversource.smarthub.coop
   eversource_nh,Eversource (NH),NH,eversource.smarthub.coop
   ```
3. Existing `smarthub.py` adapter handles everything automatically
4. Mark as `added` after portal sign-off confirms login works

### If Bespoke (High Effort)
1. **REQUIRED**: Capture HAR file from real login session
2. Extract:
   - Login POST endpoint + payload structure
   - Session token format (cookie/header)
   - Usage data API endpoint + parameters
3. Implement adapter in `api/adapters/eversource.py` following patterns from:
   - `api/adapters/gmp.py` (bespoke utility with custom API)
   - `api/adapters/vec.py` (SmartHub-based, for comparison)
4. Add to `api/auto_adapters.py` registry
5. Add to state CSV catalogs with `adapter=eversource`
6. Test with real credentials before marking `added`

## Compliance with Brief

> "Do not invent endpoints. Do not mark added without evidence or portal sign-off."

✅ **Compliant**: This plan documents research requirements without inventing API endpoints.  
✅ **Compliant**: No adapter code written without HAR evidence.  
✅ **Compliant**: No CSV catalog entries added without portal family confirmation.  
✅ **Compliant**: Status remains in research phase pending real portal evidence.

## Recommendation

**BLOCK** adapter implementation until:
1. Portal URL is identified
2. SmartHub vs. bespoke determination is made
3. If bespoke: HAR capture is provided

Update `utility_requests` table row for Eversource:
- `status` → `reviewed`
- `result` → Link to this research plan
- `reviewed_at` → Current timestamp

This ensures the agent workflow doesn't falsely mark Eversource as `added` without the required evidence.
