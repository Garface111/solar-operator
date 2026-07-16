# Eversource Portal Research Plan

**Date:** 2026-07-15  
**Status:** Research required before adapter implementation  
**Utility:** Eversource  
**State:** Multi-state (CT, MA, NH)  
**Portal URL:** Unknown (requires verification)  

## Context

Eversource is a major investor-owned utility serving Connecticut, Massachusetts, and New Hampshire. Unlike rural co-ops that typically use SmartHub/NISC platforms, Eversource operates a bespoke customer portal.

## Research Required

### 1. Portal Discovery
- [ ] Identify the production login URL (likely `eversource.com` or subdomain)
- [ ] Determine if single portal serves all three states or if state-specific
- [ ] Check if Eversource uses a third-party platform (Oracle Opower, etc.) or fully custom

### 2. Authentication Flow
- [ ] Capture HAR file during manual login session
- [ ] Document auth endpoints (login POST target, session tokens, cookies)
- [ ] Identify CSRF/anti-bot protections (captcha, rate limits, device fingerprinting)
- [ ] Note if MFA is optional or mandatory

### 3. Data Access
- [ ] Locate usage/billing data endpoints (JSON API vs. HTML scraping)
- [ ] Document date range parameters and response formats
- [ ] Verify if hourly/daily granularity is available
- [ ] Check for net-metering/solar-specific data fields

### 4. Account Structure
- [ ] Determine if single login can access multiple service addresses
- [ ] Document account/premise identifier scheme
- [ ] Note any multi-account selection UI patterns

## Implementation Blockers

**Cannot proceed without:**
1. Live HAR capture from authenticated session (requires real Eversource customer credentials)
2. Verification that portal provides programmatic data access (not just PDF bills)
3. Confirmation of rate-limit tolerances for automated access

## Next Steps

1. **Immediate:** Flag this utility request as `status=reviewed` with note that HAR capture is required
2. **Human task:** Coordinate with Eversource customer to capture HAR during login + usage data fetch
3. **Post-HAR:** Implement adapter in `api/adapters/eversource.py` following patterns from `vec.py` (bespoke) or `gmp.py` (if Opower-based)
4. **Credential stage:** Add to `api/harvester/credentials.py` registry only after adapter is tested
5. **Catalog entry:** Add row to appropriate state CSV (`api/data/providers/CT.csv`, etc.) with `family=eversource`

## Security Notes

- Eversource likely has stricter anti-automation than rural co-ops
- May require User-Agent spoofing and realistic request timing
- Session tokens may be short-lived; adapter must handle re-auth gracefully
- Consider if Cloud Capture is viable or if extension-only is safer initially

## References

- SmartHub adapter (universal co-op platform): `api/adapters/smarthub.py`
- Bespoke utility examples: `api/adapters/vec.py`, `api/adapters/gmp.py`
- Credential staging: `api/cloud_capture.py`, `api/harvester/credentials.py`
- Provider catalog: `api/data/providers/*.csv`
