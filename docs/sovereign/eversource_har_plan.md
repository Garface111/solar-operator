# Eversource HAR Capture & Adapter Plan

**Date**: 2026-01-XX  
**Agent**: Sovereign Coding Agent  
**Job**: HAR+adapter: Eversource  
**Status**: Awaiting HAR capture

## Context

This is a **succession smoke HAR stage** request. No HAR file has been provided yet. The brief authorizes HAR capture but does not include the actual HAR data.

**Utility**: Eversource  
**Provider Code**: Not yet assigned (will be `eversource` or similar)  
**Catalog Entry**: Not yet in `api/data/providers/*.csv`  
**Portal URL**: Unknown (to be discovered from HAR)

## Research Required

Before writing an adapter, we need:

1. **HAR capture** from an authenticated Eversource customer portal session
   - Login flow (POST endpoints, auth tokens, session cookies)
   - Data endpoints (usage/billing API calls)
   - Response formats (JSON structure for kWh, dates, billing)

2. **Portal identification**
   - Is Eversource using a white-label platform (SmartHub, etc.)?
   - Or a bespoke internal portal?
   - What are the actual endpoint URLs?

3. **Auth mechanism**
   - Username/password POST?
   - OAuth flow?
   - CSRF tokens or special headers?

## Adapter Design (Pending HAR)

Once HAR is available, the adapter will:

- Follow existing patterns in `api/adapters/`
- Parse login endpoints to establish session
- Extract daily/hourly kWh data
- Handle net-metering returns if applicable
- Map to our standard `DailyGeneration` / usage format

## Next Steps

1. **Human/extension**: Capture HAR from authenticated Eversource portal session
2. **Agent**: Parse HAR → identify endpoints → write `api/adapters/eversource.py`
3. **Agent**: Add Eversource to `api/data/providers/<STATE>.csv` (CT/MA/NH likely)
4. **Agent**: Update provider registry to wire in the new adapter
5. **Test**: Verify login + data pull with real credentials (staging/dogfood)

## Notes

- **Do not invent endpoints**: All URLs/auth flow must come from actual HAR capture
- **Mark utility added only with evidence**: Eversource goes live only after verified working adapter
- This document serves as the research artifact for this job (per rules: "if research-only with no safe code change, write a short plan under docs/sovereign/")

---

**Waiting on**: HAR file from succession capture ceremony.
