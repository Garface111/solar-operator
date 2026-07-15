# Alaska Utility Research Plan

**Date**: 2026-07-15  
**Status**: Research-only (no adapter code yet)  
**Request ID**: #5  

## Problem

Utility-add request for "alaska" with:
- State: unknown
- URL: unknown
- Note: (none)

No identifying information to determine which Alaska utility or portal family.

## Research Required

### 1. Identify the Specific Utility

Alaska has multiple electric utilities:
- **Chugach Electric Association** (Anchorage area) - largest co-op
- **Golden Valley Electric Association** (Fairbanks area)
- **Matanuska Electric Association** (Mat-Su Valley)
- **Homer Electric Association**
- **Alaska Electric Light & Power** (Juneau)
- **Kodiak Electric Association**
- Others (rural/municipal)

**Action needed**: Contact the requesting user (via `tenant_id` or `email` from the utility_requests row) to clarify:
- Which Alaska utility do they use?
- Do they have a customer portal URL?
- What is the utility's full legal name?

### 2. Portal Family Detection

Once the specific utility is identified:

#### If SmartHub/NISC:
- Check if the utility uses `*.smarthub.coop` portal
- If yes: add one line to the appropriate `api/data/providers/AK.csv`:
  ```csv
  code,label,state,smarthub_host
  <utility_code>,<Utility Name>,AK,<subdomain>.smarthub.coop
  ```
- The existing `api/adapters/smarthub.py` universal adapter will handle it automatically
- Mark request status as `added`

#### If Bespoke Portal:
- Capture a HAR file (HTTP Archive) from a real login session:
  1. Open browser DevTools → Network tab
  2. Log in to the utility portal
  3. Navigate to usage/billing data
  4. Export HAR, redact credentials
- Analyze HAR to identify:
  - Authentication endpoints (session cookies? OAuth? form POST?)
  - Usage data API endpoints (JSON? HTML scraping?)
  - Rate limiting / anti-bot measures
- Draft adapter in `api/adapters/<utility_code>.py` based on HAR evidence
- Add provider entry to `api/data/providers/AK.csv`
- Mark request status as `reviewed` with plan in `result` field

### 3. Known Alaska Utilities Portal Status

From existing `api/data/providers/AK.csv`:
```
code,label,state,smarthub_host
cea,Chugach Electric Association,AK,
gvea,Golden Valley Electric Association,AK,
mea,Matanuska Electric Association,AK,
hea,Homer Electric Association,AK,
aelp,Alaska Electric Light & Power,AK,
kea,Kodiak Electric Association,AK,
```

**None have `smarthub_host` populated yet** — if any ARE SmartHub, they need the subdomain added.

## Next Steps

1. **Query the database** for the full utility_requests row to get contact info:
   ```sql
   SELECT * FROM utility_requests WHERE name ILIKE '%alaska%' AND status = 'new' ORDER BY created_at DESC LIMIT 1;
   ```

2. **Contact the requester** (via email or in-app notification) with:
   > "Thanks for requesting an Alaska utility! To add it, we need to know which one. Please reply with:
   > - Your utility's full name (e.g., 'Chugach Electric Association')
   > - Your customer portal URL (if you have one)
   > 
   > We'll add it within 24 hours once we have those details."

3. **Once identified**:
   - If SmartHub: add CSV line, test with `api/adapters/smarthub.py`, mark `added`
   - If bespoke: request HAR capture, draft adapter plan, mark `reviewed`

## Security Notes

- Do NOT fabricate endpoints without HAR evidence
- Do NOT commit credentials or API keys
- SmartHub utilities require only the subdomain (no reverse-engineering)
- Bespoke portals require real login flow analysis before adapter code

## Status Update for Request

**Current**: Cannot proceed without utility identification.  
**Recommendation**: Update `utility_requests` row:
```python
status = "reviewed"
result = "Need clarification: which Alaska utility? (Chugach, Golden Valley, Matanuska, Homer, AELP, Kodiak, other?). Please provide utility name and portal URL if available."
```

Agent will mark this in the database and notify the requester.
