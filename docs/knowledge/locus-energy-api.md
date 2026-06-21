# Locus Energy (SolarNOC) v3 API — integration reference

Locus Energy is a solar monitoring platform (portals: `locusnoc.datareadings.com`,
`suns-locusnoc.datareadings.com`, admin at `admin.datareadings.com`). Owned by **Stem**
(via the AlsoEnergy acquisition) — support routes through them, but an existing Locus
account manager contact is the fastest path. Built into the inverter framework as a
fully-scrapable vendor (Jun 2026): `api/adapters/locus.py` (HTTP source of truth) +
`api/inverters/locus.py` (vendor wrapper) + two discover/connect endpoints in
`api/array_owners.py`. Registered in `VENDORS` → the daily pull (`api/jobs/inverter_pull.py`)
picks it up with NO job changes.

## Auth gate (the real blocker)
OAuth needs FOUR fields and Locus issues two of them ONLY via an account manager — there
is no self-serve API section in the portal:
- `client_id`, `client_secret`  ← account-manager issued (the gated pair)
- `username`, `password`        ← the customer's SolarNOC login (in the portal/vault)
Partner ID alone is NOT enough to authenticate. Having the SolarNOC login is only half.

## API contract (verified from Locus's own published Slate docs)
Docs source: `github.com/LocusEnergy/api-docs` (raw markdown under `source/includes/`).
To re-read a section: `curl -sL https://raw.githubusercontent.com/LocusEnergy/api-docs/master/source/includes/<path>.md`

Base: `https://api.locusenergy.com/v3`   Auth base: `https://api.locusenergy.com`

### OAuth2 — Resource Owner Password grant
```
POST /oauth/token   Content-Type: application/x-www-form-urlencoded
grant_type=password&client_id=...&client_secret=...&username=...&password=...
```
Response: `{access_token, refresh_token, token_type:"bearer", expires_in:3600, issued_at}`.
Refresh: same endpoint, `grant_type=refresh_token&client_id=...&client_secret=...&refresh_token=...`.
All data calls: header `Authorization: Bearer <token>`, `Accept: application/json`.

### Endpoints used
- `GET /v3/partners/{partnerId}/sites` — enumerate the WHOLE fleet under a partner in ONE
  call. This is the "paste one credential, attach all arrays" call — cleaner than SolarEdge
  (no pagination). Response `{sites:[{id, clientId, name, address1, locale3, localeCode1,
  postalCode, locationTimezone}, ...]}`.
- `GET /v3/sites/{siteId}` — single site (FLAT object, no nested `site` wrapper). Parse
  defensively: `body.get("site") if isinstance(body.get("site"),dict) else body`.
- `GET /v3/sites/{siteId}/components` — devices on a site (optional; site-level data is
  enough for energy).
- `GET /v3/sites/{siteId}/data?fields=Wh_sum&start=...T00:00:00&end=...T00:00:00&tz=UTC&gran=daily`
  — daily energy series in ONE call (no per-day loop, unlike the SMA adapter). Rows
  `{ts:"YYYY-MM-DDThh:mm:ss±tz", Wh_sum}`. **Wh → kWh: divide by 1000.** Skip rows where
  Wh_sum is null or 0 (offline). Parse day from `ts[:10]`. Use the site's `locationTimezone`
  for `tz` when known, else "UTC".
- `GET /v3/sites/{siteId}/data?fields=W_avg&gran=latest` — live power (start/end omitted
  with gran=latest; returns one row). Map `W_avg` → current_power_w, `ts` → as_of.

### HTTP errors
401 = bad creds → InverterAuthError. 403 = valid creds, no permission for that
partner/site → InverterScopeError. 429 = rate limit (Locus default concurrency is **1
request at a time**) → InverterError "Locus rate limit (429)". 5xx → InverterError.

## Adapter implementation notes
- Token cache lives in the ADAPTER (`api.adapters.locus._TOKEN_CACHE`), keyed by client_id,
  storing `(access_token, refresh_token, expires_at)` with `expires_in - 60s` TTL — same
  shape as the SMA `_TOKEN_CACHE` pattern. Refresh-grant-first when a cached refresh token
  exists and access expired; on 401 from refresh, fall back to a fresh password grant.
- `partner_id` is per-tenant config passed in — NEVER hardcode a partner ID.
- Daily fetch must raise only InverterError subclasses (the scheduler catches those; a raw
  exception would crash the pull).
- Pilot data point (Jun 2026): Bruce's sites are on a SolarEdge account; Locus support is
  for OTHER customers' arrays. A Locus login keyed to one company name (e.g. "Johnson
  Hardware and Rental") may be a single sub-account, not the partner master — confirm it
  can read at partner level before promising fleet-wide discovery.
