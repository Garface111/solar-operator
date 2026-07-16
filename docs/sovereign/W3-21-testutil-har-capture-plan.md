# W3-21: TestUtil HAR Capture Plan

**Status:** Awaiting HAR capture  
**Created:** 2026-01-11  
**Adapter:** `api/adapters/testutil.py`  

## Summary

Work item W3-21 authorized HAR capture for TestUtil through succession protocol. A placeholder adapter has been created at `api/adapters/testutil.py` to receive the real implementation once HAR data is available.

## What Was Done

1. **Created placeholder adapter** at `api/adapters/testutil.py`
   - Follows solar-operator adapter conventions (context manager pattern)
   - Documents exactly what HAR capture must provide
   - Returns safe empty data (no invented endpoints)
   - Logs warnings when called before implementation

2. **Documented requirements** for HAR capture session:
   - Login endpoint and authentication flow
   - Data retrieval endpoints (generation/consumption)
   - Response formats and field mappings
   - Session token/cookie handling

## What HAR Capture Must Provide

The HAR capture session should record a complete workflow:

1. **Login sequence:**
   - URL of login page/endpoint
   - HTTP method (GET/POST)
   - Required headers (Content-Type, User-Agent, etc.)
   - Request body format (form data, JSON, etc.)
   - Response handling (cookies, tokens, redirects)

2. **Data retrieval:**
   - URL(s) for generation/consumption data
   - Query parameters (date range format, meter ID, etc.)
   - Response format (JSON, HTML table, CSV, etc.)
   - Field names/locations for date and kWh values

3. **Session management:**
   - Cookie names and persistence
   - Token headers (Authorization, X-Auth-Token, etc.)
   - Session timeout behavior
   - Logout/cleanup steps

## Next Steps (After HAR Capture)

1. Parse HAR file to extract:
   - Base URL and endpoint paths
   - Authentication request/response pair
   - Data request/response pair

2. Update `testutil.py` with:
   ```python
   BASE_URL = "https://portal.testutil.example.com"  # from HAR
   LOGIN_ENDPOINT = "/api/auth/login"                # from HAR
   DATA_ENDPOINT = "/api/customer/usage"             # from HAR
   ```

3. Implement `authenticate()` method:
   - Build login request from HAR template
   - Extract session token/cookie from response
   - Handle errors and edge cases

4. Implement `get_daily_generation()` method:
   - Build data request with date range
   - Parse response format observed in HAR
   - Map fields to standard `{"date": ..., "kwh": ...}` format

5. Test with real credentials (if available)

6. Add TestUtil to providers catalog:
   - Update `api/data/providers/{STATE}.csv` with new row
   - Set `provider` column to `testutil`
   - Set `requires_credentials` to `true`
   - Add any state/region-specific metadata

## Design Decisions

- **No invented endpoints:** The adapter returns empty data rather than guess at API structure
- **Loud warnings:** Calling unimplemented methods logs warnings to prevent silent failures
- **Standard pattern:** Follows existing adapter conventions (SmartHub, SolarEdge, etc.)
- **Self-documenting:** Code comments explain exactly what HAR must provide

## Utility Request Integration

If TestUtil was requested via `api/utility_requests.py`:

1. The request row status should remain `"researching"` until HAR is processed
2. Once adapter is implemented, update to `"reviewed"` with result:
   ```
   Adapter created at api/adapters/testutil.py. Awaiting HAR capture to
   implement real endpoints. See docs/sovereign/W3-21-testutil-har-capture-plan.md
   for details.
   ```
3. After HAR implementation, update to `"added"` with result:
   ```
   TestUtil adapter implemented and added to providers catalog. Users can
   now connect TestUtil accounts.
   ```

## References

- Existing adapters: `api/adapters/smarthub.py`, `api/adapters/solaredge.py`
- Provider catalog: `api/data/providers/*.csv`
- Utility requests: `api/utility_requests.py`
