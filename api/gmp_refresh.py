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
