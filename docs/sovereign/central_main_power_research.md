# Central Main Power – Portal Research

**Date:** 2026-07-15  
**Status:** Research-only (no code change authorized)  
**Utility:** Central Main Power  
**State:** Unknown  
**Portal URL:** Unknown  

## Problem

The brief requests "central main power" be added to the utility catalog, but provides:
- **No state** (state=-, url=-)
- **No portal URL**
- **No evidence** of portal family (SmartHub vs. bespoke)
- **No HAR capture** (required for bespoke portals per standing rules)

## Research Findings

### Name Ambiguity
"Central Main Power" does not match any known US electric utility in the standard EIA-861 dataset or common utility directories. Possible interpretations:

1. **Central Maine Power (CMP)** – major Maine IOU
   - Already supported via `api/adapters/cmp.py` (bespoke adapter)
   - Provider code: `cmp`
   - Portal: `https://www.cmpco.com`
   - **Action if this is the intent:** No code change needed; utility already live.

2. **Typo/variant** of another utility name
   - Without state or URL, cannot disambiguate

3. **Small co-op/muni** not yet cataloged
   - Requires state + portal URL to proceed

### SmartHub Check

Searched `api/data/providers/*.csv` for any "central" + "main" + "power" combinations:
- **No matches** in existing SmartHub registry (`smarthub_host` column)
- Cannot auto-add via SmartHub path without confirmed `*.smarthub.coop` host

### Bespoke Portal Path

Per `api/utility_requests.py` and adapter development rules:
- Bespoke portals require **HAR capture** before adapter creation
- No HAR provided in this request
- **Cannot invent endpoints** without real login evidence

## Decision

**BLOCKED – Insufficient Information**

Cannot safely add this utility without:
1. **State** (2-letter code or region)
2. **Portal URL** (login page)
3. **Portal family confirmation** (SmartHub subdomain OR HAR capture for bespoke)

## Recommended Next Steps

### If this is Central Maine Power (CMP):
- **No action needed** – already supported
- Verify customer can find "Central Maine Power" in the picker
- If picker search is failing, fix search indexing (separate issue)

### If this is a different utility:
1. **Request clarification** from submitter:
   - Full legal utility name
   - State/service territory
   - Customer portal URL
2. **Re-queue** with complete information
3. **Follow standard flow:**
   - If SmartHub: add `smarthub_host` to state CSV → auto-wired
   - If bespoke: capture HAR → build adapter → test → mark added

## Compliance Notes

- **No code change shipped** (research-only per brief rules)
- **No endpoints invented** (no HAR = no adapter)
- **No registry entry** (insufficient portal evidence)
- Status remains `new` in `utility_requests` table (agent did not mark `added`)

---

**Sovereign Agent:** This artifact satisfies the "always ship at least one file" rule while respecting the "do not invent endpoints" and "no mark added without evidence" constraints. The brief's authorization was for "credential staging + portal sign-off path" but the request lacks the minimum viable data (state, URL, or family) to execute either path safely.
