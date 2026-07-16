# Alaska Utility Research Plan

**Date**: 2026-07-15  
**Status**: Research-only (no safe code change without portal evidence)  
**Utility Name**: alaska  
**State**: Unknown  
**URL**: Unknown  
**Family Guess**: bespoke_alaska  

## Problem

The brief requests a utility adapter + credential staging for "alaska" but provides:
- No state code
- No portal URL
- No portal family confirmation
- No HAR capture or login evidence
- Only a guess: "bespoke_alaska"

## Research Required Before Implementation

### 1. Identify the Actual Utility

"Alaska" is ambiguous. Candidates:
- **Alaska Electric Light & Power (AEL&P)** - Juneau area
- **Chugach Electric Association** - Anchorage/Southcentral
- **Golden Valley Electric Association (GVEA)** - Fairbanks area
- **Homer Electric Association (HEA)** - Kenai Peninsula
- **Matanuska Electric Association (MEA)** - Mat-Su Valley
- Dozens of smaller municipal/co-op utilities

Without the operator's specific utility name or service territory, we cannot proceed.

### 2. Determine Portal Family

**If SmartHub/NISC**:
- Many Alaska co-ops use SmartHub (e.g., `*.smarthub.coop` hostnames)
- Check if the utility appears in `api/data/providers/AK.csv`
- If yes and `smarthub_host` is populated → wire via existing SmartHub adapter
- Add one CSV line, no custom adapter needed

**If Bespoke**:
- Requires HAR capture from a real login session
- Must reverse-engineer: login endpoint, auth flow, data API
- Cannot invent endpoints without evidence (per authorization rules)

### 3. Verify Against Existing Catalog

Check `api/data/providers/AK.csv` for:
- Existing entries matching "alaska" or common AK utility names
- Any with `smarthub_host` already configured
- Any marked as unsupported/bespoke

## Next Steps (Manual)

1. **Operator clarification**: Ask which specific Alaska utility they need
2. **Portal URL discovery**: Get the actual login portal URL
3. **Family identification**:
   - If hostname matches `*.smarthub.coop` → SmartHub path
   - If known portal (Eversource/CMP-style) → confirm adapter module
   - If unknown bespoke → capture HAR before coding
4. **State code confirmation**: Verify AK or other (some utilities serve multiple states)

## Why No Code Change

Per authorization rules:
- "Do not invent endpoints"
- "Do not mark added without evidence or portal sign-off"
- SmartHub requires registry entry (need exact `smarthub_host`)
- Bespoke requires HAR capture (need login evidence)

Without portal URL or family confirmation, any adapter code would be speculative and unsafe.

## Recommended Workflow

1. Update `utility_requests` table row for this request:
   - Set `status = 'reviewed'`
   - Set `result` to this research summary
2. Flag for human follow-up to gather:
   - Exact utility name
   - Portal URL
   - Optional: test credentials for HAR capture
3. Once evidence is available, re-queue with complete details

## References

- SmartHub adapter: `api/adapters/smarthub.py`
- Provider catalog: `api/data/providers/AK.csv`
- Utility request model: `api/utility_requests.py`
- Authorization: "Do not invent endpoints. Do not mark added without evidence."
