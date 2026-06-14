"""Lightweight in-process sliding-window rate limiter.

Mirrors the per-IP limiter already used by the public preview endpoint
(api/array_owners.py): per-process, in-memory, no external dependency. Railway
runs a single web replica today, so a process-local limiter is sufficient; if we
scale to multiple replicas this should move to Redis (each replica would then
enforce its own share — still a meaningful brake, just less precise).

Protects UNAUTHENTICATED endpoints that are cheap to call but expensive or
abusable downstream:
  - /v1/auth/password-login  → brute-force guessing
  - /v1/auth/request         → email-bombing a victim + Resend cost
  - /v1/onboarding/start     → spam tenant + email creation
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, Request

# {"bucket:key": [monotonic_ts, ...]} — pruned per access; empty keys reaped
# when the table grows past _MAX_KEYS so unique-IP churn can't leak memory.
_HITS: dict[str, list[float]] = defaultdict(list)
_MAX_KEYS = 50_000


def client_ip(request: Optional[Request]) -> str:
    """Best-effort client IP. Honors X-Forwarded-For (Railway/Netlify proxy)."""
    if request is None:
        return "?"
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _reap() -> None:
    if len(_HITS) <= _MAX_KEYS:
        return
    for k in [k for k, v in list(_HITS.items()) if not v]:
        _HITS.pop(k, None)


def allow(bucket: str, key: str, *, max_hits: int, window_s: float) -> bool:
    """Record a hit and return True if it is within the window's budget."""
    now = time.monotonic()
    full = f"{bucket}:{key}"
    hits = [t for t in _HITS.get(full, ()) if now - t < window_s]
    if len(hits) >= max_hits:
        _HITS[full] = hits
        return False
    hits.append(now)
    _HITS[full] = hits
    _reap()
    return True


def enforce(
    request: Optional[Request],
    bucket: str,
    *,
    max_hits: int,
    window_s: float,
    key_extra: Optional[str] = None,
    message: str = "Too many requests — please slow down and try again shortly.",
) -> None:
    """Raise HTTPException(429) if `request` (optionally + key_extra, e.g. an
    email) has exceeded `max_hits` within `window_s`. No-op limiter friendliness:
    a missing request object never blocks (key '?')."""
    # The whole test suite shares one client IP; don't let cross-test accrual
    # trip the limiter. Unit-test allow() directly instead (test_ratelimit.py).
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    ip = client_ip(request)
    key = ip if key_extra is None else f"{ip}|{key_extra}"
    if not allow(bucket, key, max_hits=max_hits, window_s=window_s):
        raise HTTPException(429, message)
