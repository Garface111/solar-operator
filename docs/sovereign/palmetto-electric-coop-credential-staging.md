# Palmetto Electric Cooperative – Credential Staging Plan

**Date:** 2026-07-15  
**Utility:** Palmetto Electric Cooperative (SC)  
**Portal:** https://epayment.palmetto.coop/onlineportal/Customer-Login  
**Family:** Bespoke (Meridian Cooperative OnlinePortal)  
**Members:** ~75,000 (Beaufort, Hampton, Jasper counties)  

---

## Status: Awaiting HAR Capture

Palmetto Electric Cooperative runs a **bespoke Meridian Cooperative OnlinePortal** at `epayment.palmetto.coop/onlineportal`. This is **not** a SmartHub deployment and cannot be auto-wired via the existing SmartHub adapter.

### Required Next Step: Browser HAR Capture

Before an adapter can be built, we need a **real logged-in session** captured as a `.HAR` file:

1. **Obtain test credentials** (account number/user ID + password) from a Palmetto Electric member willing to participate in testing.
2. **Open Chrome DevTools** → Network tab → enable "Preserve log".
3. **Navigate to** https://epayment.palmetto.coop/onlineportal/Customer-Login
4. **Sign in** with the test credentials.
5. **Navigate to billing/usage pages** (account summary, billing history, usage data if available).
6. **Export the HAR** (right-click in Network tab → "Save all as HAR with content").
7. **Sanitize the HAR** (redact account numbers, passwords, PII) and attach to the adapter implementation task.

### Adapter Implementation Plan (Post-HAR)

Once the HAR is available, the adapter will:

1. **Reverse-engineer the Meridian OnlinePortal auth flow**:
   - Identify the login POST endpoint (likely `/onlineportal/api/login` or similar).
   - Capture required headers, cookies, CSRF tokens, session management.
   - Document the account number/user ID + password submission format.

2. **Identify billing/usage data endpoints**:
   - Locate the JSON/HTML endpoints that return account balance, billing history, kWh usage.
   - Document required query parameters (date ranges, account identifiers).
   - Map response structure to Solar Operator's normalized schema.

3. **Build the adapter module** (`api/adapters/palmetto.py`):
   - Implement `login(username, password) -> session`.
   - Implement `fetch_bills(session, start_date, end_date) -> list[Bill]`.
   - Implement `fetch_usage(session, start_date, end_date) -> list[UsageDay]`.
   - Handle session expiry, rate limiting, error codes.

4. **Register in the providers catalog**:
   - Add entry to `api/data/providers/SC.csv`:
     ```csv
     code,label,state,login_host,smarthub_host,adapter_module,notes
     palmetto,Palmetto Electric Cooperative,SC,epayment.palmetto.coop/onlineportal,,palmetto,Meridian OnlinePortal (bespoke)
     ```
   - Update `api/auto_adapters.py` to route `palmetto` → `api.adapters.palmetto`.

5. **Test with real credentials**:
   - Verify login succeeds.
   - Verify billing/usage data is correctly parsed.
   - Verify Cloud Capture harvester can run the adapter end-to-end.

---

## Security & Compliance

- **No fabricated endpoints**: All API calls must be reverse-engineered from the HAR.
- **Credential encryption**: Passwords stored via Cloud Capture are encrypted at rest (enforced by `api/cloud_capture.py`).
- **Rate limiting**: Adapter must respect the portal's rate limits (observe `Retry-After` headers, implement exponential backoff).
- **Session management**: Sessions must be properly closed/logged out to avoid orphaned sessions.

---

## Blocked Until

- [ ] Test credentials obtained from a Palmetto Electric member.
- [ ] HAR file captured and sanitized.
- [ ] HAR file attached to adapter implementation task.

**Do not proceed with adapter implementation until the HAR is available.** Fabricating endpoints without observing real traffic will result in a non-functional adapter.

---

## References

- **Utility Request**: `api/utility_requests.py` (status: `reviewed`, awaiting HAR).
- **SmartHub Adapter** (reference for structure): `api/adapters/smarthub.py`.
- **Cloud Capture API**: `api/cloud_capture.py` (credential storage/retrieval).
- **Providers Catalog**: `api/data/providers/SC.csv` (add entry post-implementation).
