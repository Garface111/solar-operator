# Synthetic GMP Health Monitor

Nightly job that authenticates as a known GMP test account, calls the same
bill endpoint the production scraper uses, validates the response, and emails
Ford on failure or schema drift.

## What it does

1. Exchanges `SYNTHETIC_GMP_REFRESH_TOKEN` for a fresh JWT via the GMP token
   endpoint (same path as `api/gmp_refresh.py`).
2. Calls `GET api.greenmountainpower.com/api/v2/accounts/{account}/bills`
   via `api.adapters.gmp.fetch_bills_json` — the same function production uses.
3. Validates: response is non-empty, `billDate` and `billSegments` are present,
   segments have `segmentLineItems`.
4. Computes a schema hash (sorted union of all field names at bill / segment /
   line-item level) and compares it to the previous run's hash.
5. Appends a JSON record to `storage/synthetic_runs.jsonl`.
6. Emails `ford.genereaux@dysonswarmtechnologies.com` if any check fails or
   the schema hash changes.

Runs nightly at **03:15 UTC** via APScheduler (`api/scheduler.py`).

## Interpreting alerts

**"Synthetic GMP check FAILED: token refresh failed"**
The refresh token has expired (valid ~21 days) or was revoked. Generate a new
one via the GMP portal and update `SYNTHETIC_GMP_REFRESH_TOKEN` on Railway.

**"Synthetic GMP check FAILED: empty bill list returned"**
The API returned HTTP 200 but no bills. Either the test account has no history
or GMP changed how they paginate. Inspect the raw response manually.

**"Synthetic GMP check FAILED: missing required bill fields"**
GMP's JSON schema changed and a field our parser depends on disappeared.
Compare `api/adapters/gmp.py:bill_json_to_metrics()` against the live response.

**"SCHEMA DRIFT DETECTED"**
New fields appeared (or disappeared) in the response structure. The previous
and new hash are both in the email body. Not necessarily breaking — GMP often
adds fields — but worth inspecting `_extract_kwh_generated()` to confirm the
KWH GENERATE line-item path is unaffected.

## Setup (Railway)

```sh
railway variables --set "SYNTHETIC_GMP_REFRESH_TOKEN=<32-char-token>"
railway variables --set "SYNTHETIC_GMP_ACCOUNT_NUMBER=<11-digit-account>"
```

The refresh token comes from a POST to:
```
POST https://api.greenmountainpower.com/api/v2/applications/token?remember_me=true
grant_type=refresh_token&refresh_token=<token>&client_id=C978562571FC475294191C7B94DD883E
```
The response `access_token` is the JWT; `refresh_token` in the request body
is the durable token stored in the env var (valid ~21 days, refreshes itself
each time this monitor runs successfully).

## Manual trigger

```sh
railway ssh "cd /app && python -m scripts.synthetic_gmp_monitor --once"
```

## Dry run (local)

```sh
python -m scripts.synthetic_gmp_monitor --dry-run
```

Prints what would happen without making real HTTP calls.

## Disabling

Set `SYNTHETIC_GMP_REFRESH_TOKEN` to an empty string in Railway env vars.
The monitor will skip silently (raises `RuntimeError` that the scheduler
wrapper catches and alerts on — set `SYNTHETIC_GMP_ACCOUNT_NUMBER=` too to
suppress even that).

Alternatively, comment out the `synthetic_gmp_monitor` job in `api/scheduler.py`
and redeploy.

## Run log

`storage/synthetic_runs.jsonl` — one JSON object per line:

```json
{
  "timestamp_utc": "2026-06-05T03:15:01.234567+00:00",
  "success": true,
  "latency_ms": 842,
  "response_hash": "a3f2c1b0d9e87654",
  "previous_hash": "a3f2c1b0d9e87654",
  "schema_changed": false,
  "error": null
}
```
