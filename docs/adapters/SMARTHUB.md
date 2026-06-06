# SmartHub Universal Adapter

Solar Operator uses a universal JSON-API adapter for NISC SmartHub deployments.
Adding a new utility takes ~2 minutes — one entry in each of two registry files.

## Adding a new SmartHub utility

**Step 1 — Python registry** (`api/adapters/smarthub.py`):

```python
SMARTHUB_UTILITIES = {
    ...
    "NEWUTIL": {
        "host": "newutil.smarthub.coop",
        "name": "New Utility Electric",
        "provider": "newutil",  # lowercase, becomes UtilityAccount.provider
    },
}
```

**Step 2 — Extension registry** (`extension/smarthub_registry.js`):

```js
const SMARTHUB_REGISTRY = {
  ...
  "newutil.smarthub.coop": {
    provider: "newutil",
    name: "New Utility Electric",
  },
};
```

That's it. The adapter, autopop logic, manifest host permissions, and `/v1/sync`
dispatch all derive from these two registries automatically.

New utilities share the `vec_email` / `vec_username` / `vec_autopopulate` /
`vec_last_sync_at` columns on the `Client` model — no DB migration needed.

## serviceLocationNumber discovery

SmartHub stores meters under an internal `serviceLocationNumber` (not the
account number the customer sees on their bill). It must be discovered once
per account via:

```
GET https://{host}/services/secured/user-data?userId={primaryUsername}
```

Response key: `serviceLocationToUserDataServiceLocationSummaries`
(map of `locationId → list of location summaries`)

`api/jobs/smarthub_pull.py` calls `fetch_account_list()` on the first pull for
each account and caches the discovered `serviceLocationNumber` in
`UtilityAccount.extra["service_location_number"]`. Subsequent pulls use the
cached value.

**UNVERIFIED:** The exact `accountNumber` field within each location summary.
The implementation falls back to the `id` field or the location key itself.
Verify with a real WEC or STOWE account.

## Auth flow

1. `POST /services/oauth/auth/v2` with `userId` + `password` (form-encoded)
   → returns `authorizationToken` + `primaryUsername`
2. Use `Authorization: Bearer {authorizationToken}` + `X-Nisc-Smarthub-Username: {email}`
   on all subsequent requests
3. Session expires after ~300 seconds; re-authenticate before expiry

The Chrome extension now intercepts the login API response (`/services/oauth/auth/v2`)
via a `fetch` monkey-patch and sends the `authorizationToken` to the backend via
`/v1/sync`. This enables `smarthub_pull.py` to make server-side generation pulls
without requiring the operator to store their portal password in Solar Operator.

## MFA handling

**UNVERIFIED:** MFA (two-factor code) support. The SmartHub auth endpoint
accepts a `twoFactorCode` form field, but it is unknown whether any VT co-op
has MFA enabled for all accounts. If an operator's account requires MFA,
`authenticate()` will return HTTP 401 and the pull will be skipped with a
logged warning. The operator can still use the extension-scrape path (which
doesn't require MFA interception) for billing history.

## Session expiry

Stored `UtilitySession` rows do not expire automatically. The `smarthub_pull.py`
job attempts a pull using the stored token; if the API returns 401, the job
logs the error and skips. The operator simply signs into the portal again
(the extension re-captures the token).

## Manual smoke test (after adding a new utility)

DO NOT store real credentials in CI or env files. Test locally:

1. Set `SOLAR_DB_URL` to a throwaway SQLite file.
2. Run `python -c "from api.adapters.smarthub import authenticate, fetch_account_list, fetch_daily_generation; ..."` in a Python shell.
3. Call `authenticate('newutil.smarthub.coop', 'your@email.com', 'password')`.
4. Pass the session dict to `fetch_account_list(...)` and confirm `service_location_number` appears.
5. Call `fetch_daily_generation(...)` for a 7-day range and print the results.
6. Confirm kWh values are non-zero and `kwh_generated` reflects exported kWh (net-metering credit).

## Supported utilities (Jun 2026)

| Code | Host | Coverage |
|------|------|----------|
| VEC | vermontelectric.smarthub.coop | 33,000 members |
| WEC | washingtonelectric.smarthub.coop | 11,400 members |
| STOWE | stoweelectric.smarthub.coop | 4,530 members |
| HYDE_PARK | villageofhydepark.smarthub.coop | 1,400 members |
| LUDLOW | ludlow.smarthub.coop | ~1,500 members |
| ENOSBURG | villageofenosburgfalls.smarthub.coop | ~1,000 members |
| NHEC | nhec.smarthub.coop | 88,000 members |
