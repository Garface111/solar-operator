# GMP Token Refresh Worker — The 21-Day Wall Killer

## Why this is a big deal
Today: operators must log into greenmountainpower.com every ~21 days
or their JWT expires and we lose the ability to pull bills. This is a
huge UX & retention hole — every 3 weeks the operator gets dragged
back into the utility portal to keep the system alive.

**Tonight's recon recovered GMP's actual refresh flow.** We can call it
ourselves with the `refresh_token` we already capture and store in
`utility_sessions.refresh_token` for every GMP session. The operator
NEVER has to log in again as long as our cron runs.

## The recovered flow (verified live, returned 200 + new JWT)

```
POST https://api.greenmountainpower.com/api/v2/applications/token?remember_me=true

Headers:
  GMP-Source: web
  Content-Type: application/x-www-form-urlencoded

Body (form-urlencoded):
  grant_type=refresh_token
  refresh_token=<the 32-char value already stored in utility_sessions.refresh_token>
  client_id=C978562571FC475294191C7B94DD883E

Response 200:
  { "access_token": "<new JWT>", "token_type": "Bearer", "expires_in": 1814400 }

expires_in = 1_814_400 seconds = exactly 21 days.
```

`GMP-Source: web` header is required (recovered from GMP's bundle constants).
`CLIENT_ID` is a public client identifier hard-coded in their web app —
not a secret, no risk to hardcode.

## Tasks

### Task 1 — Create the refresh module
File: `api/gmp_refresh.py`

```python
"""GMP token refresh — keeps operator JWTs alive without re-login.

Recovered from greenmountainpower.com bundle 2026-06-05.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
import httpx

logger = logging.getLogger(__name__)

GMP_TOKEN_URL = "https://api.greenmountainpower.com/api/v2/applications/token?remember_me=true"
GMP_CLIENT_ID = "C978562571FC475294191C7B94DD883E"
GMP_SOURCE_HEADER = {"GMP-Source": "web"}


class GmpRefreshError(Exception):
    pass


def refresh_gmp_token(refresh_token: str, *, timeout: float = 15.0) -> tuple[str, datetime]:
    """Exchange a refresh_token for a fresh access_token.

    Returns (new_jwt, expires_at_utc_naive).
    Raises GmpRefreshError on any non-200 / network failure / malformed
    response. The caller decides whether to retry or fall back to user
    re-login.
    """
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": GMP_CLIENT_ID,
    }
    headers = {**GMP_SOURCE_HEADER, "Content-Type": "application/x-www-form-urlencoded"}

    try:
        r = httpx.post(GMP_TOKEN_URL, data=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise GmpRefreshError(f"network error: {exc!r}") from exc

    if r.status_code != 200:
        raise GmpRefreshError(f"refresh failed: HTTP {r.status_code} {r.text[:200]}")

    try:
        data = r.json()
        new_jwt = data["access_token"]
        expires_in = int(data.get("expires_in", 0))
    except (ValueError, KeyError) as exc:
        raise GmpRefreshError(f"bad response: {r.text[:200]}") from exc

    if not new_jwt or expires_in <= 0:
        raise GmpRefreshError(f"empty token or invalid expiry: {data}")

    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    return new_jwt, expires_at
```

### Task 2 — Scheduler hook
Add to `api/scheduler.py` a new function `refresh_expiring_gmp_tokens()`:

- Find every `UtilitySession` where `provider='gmp'` and `expires_at` is
  within the next 7 days AND `refresh_token` is not null.
- For each: call `refresh_gmp_token(sess.refresh_token)`.
- On success: update `sess.api_token`, `sess.expires_at`,
  `sess.captured_at = now()`. Log the refresh.
- On failure: log the error. Increment a counter on the session
  (`refresh_failures: int`) — add this column in models + migrate. If
  it hits 3, send a notification to the operator: "We can't refresh
  your GMP session — please log into greenmountainpower.com once to
  reconnect."

Wire into the existing scheduler tick. Hourly cadence is fine — refresh
is idempotent if we're not actually expiring.

### Task 3 — Schema migration
Add to `api/models.py` on `UtilitySession`:
- `refresh_failures: Mapped[int] = mapped_column(default=0)`
- `last_refresh_at: Mapped[datetime | None] = mapped_column(nullable=True)`

Add to `api/migrate.py`:
```sql
ALTER TABLE utility_sessions ADD COLUMN IF NOT EXISTS refresh_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE utility_sessions ADD COLUMN IF NOT EXISTS last_refresh_at TIMESTAMP NULL;
```

### Task 4 — Tests
`tests/test_gmp_refresh.py`:
1. `refresh_gmp_token` returns expected tuple on mocked 200.
2. Raises `GmpRefreshError` on 401 (expired refresh_token).
3. Raises on network failure.
4. Scheduler picks up tokens within 7 days of expiry.
5. Scheduler updates `api_token` + `expires_at` + `last_refresh_at` on success.
6. After 3 consecutive failures, scheduler triggers operator notification.
7. Sessions with `refresh_token IS NULL` are skipped.

Mock `httpx.post` — do NOT hit GMP live in tests.

### Task 5 — Operator-visible status
Tiny UI addition to the existing `UtilityConnectionsCard` in
`web/app/src/components/settings/`:
- Show `last_refresh_at` next to provider connection.
- If `refresh_failures > 0`: show amber warning chip "Re-auth needed".

You may need to expose `last_refresh_at` + `refresh_failures` from the
`/v1/account` endpoint — add it.

### Task 6 — Build + verify
- `pytest tests/test_gmp_refresh.py -v` — must all pass.
- `pytest tests/` — full sweep must stay green (currently 140 tests).
- `./build_app.sh` if you touched web/app.
- Commit per task. Do NOT push.

### Task 7 — Deploy notes (just the 5-line summary)
List Railway commands the orchestrator needs to run after merge:
1. Wait for deploy.
2. Run migration to add the 2 columns.
3. Verify scheduler picks up first refresh on its next tick.

## Constraints
- DO NOT modify any extension code — refresh tokens already flow in
  via the existing capture path.
- DO NOT touch GMCS writer.
- DO NOT log the actual refresh_token or full JWT — only prefixes/suffixes.
- HARDCODE the CLIENT_ID and SOURCE_HEADER values exactly as above.
  They're public GMP web-app constants; not secrets.
- Use type hints. Python 3.11+.
- Final summary: confidence + any caveats (e.g. "refresh fails if
  password changes; user must re-login then").
