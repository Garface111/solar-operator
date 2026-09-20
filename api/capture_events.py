"""
Capture-event instrumentation for /v1/sync.

Collects lightweight "what happened during this capture?" rows and bulk-inserts
them at the end of the request. Each call to /v1/sync gets a fresh capture_id
(UUID4) so all its events are queryable together.

Privacy — SAFE_ACCOUNT_KEYS allowlist controls what lands in payload_excerpt:
  Kept:    provider, user (email/username/display-name from portal profile),
           accounts_summary [{account_number, nickname, customer_number, service_address}]
  Stripped: auth.* (apiToken, refreshToken — bearer credentials),
            accounts[].extra (raw provider blobs; may contain binary bill-URL data)
Any field NOT explicitly included is omitted. Future contributors: add keys to
SAFE_ACCOUNT_KEYS or the user/provider blocks in _safe_excerpt only after
confirming they contain no auth secrets.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Optional

PAYLOAD_MAX_BYTES = 4096

# Keys retained from each account entry in the extension payload.
# 'extra' is deliberately excluded: it carries raw provider blobs that may
# include binary bill URLs, session cookies, or provider-specific tokens.
SAFE_ACCOUNT_KEYS = frozenset({"account_number", "nickname", "customer_number", "service_address"})


def _safe_excerpt(raw_payload: dict) -> dict:
    """Strict scalar allowlist and UTF-8 byte cap for disposable diagnostics."""
    if not isinstance(raw_payload, dict):
        return {}
    safe = {}
    truncated = False

    def scalar(value, limit=256):
        nonlocal truncated
        if not isinstance(value, (str, int, float, bool)):
            truncated = True
            return None
        value = str(value)
        truncated |= len(value) > limit
        return value[:limit]

    if "provider" in raw_payload:
        value = scalar(raw_payload["provider"], 64)
        if value is not None: safe["provider"] = value
    user = raw_payload.get("user")
    if isinstance(user, dict):
        allowed = {"email", "username", "name", "display_name", "displayName", "first_name", "last_name"}
        safe["user"] = {key:value for key,raw in user.items() if key in allowed
                        and (value := scalar(raw)) is not None}
        truncated |= bool(set(user) - allowed)
    accounts = raw_payload.get("accounts")
    if isinstance(accounts, list):
        safe["account_count"] = len(accounts)
        safe["accounts_summary"] = [
            {key:value for key,raw in account.items() if key in SAFE_ACCOUNT_KEYS
             and (value := scalar(raw)) is not None}
            for account in accounts[:20] if isinstance(account, dict)
        ]
        truncated |= len(accounts) > 20
    if truncated: safe["_truncated"] = True
    # A large profile alone used to escape the cap after all accounts were cut.
    while len(json.dumps(safe).encode("utf-8")) > PAYLOAD_MAX_BYTES:
        safe["_truncated"] = True
        if safe.get("accounts_summary"):
            safe["accounts_summary"].pop()
        elif safe.get("user"):
            safe["user"].pop(next(reversed(safe["user"])))
        else:
            safe = {"_truncated": True}
            break
    return safe


class CaptureContext:
    """Accumulates CaptureEvent rows during a single /v1/sync call.

    Usage:
        ctx = CaptureContext(tenant_id=tenant.id)
        ctx.add("ingest_received", decision="3 gmp accounts", payload=raw_payload)
        ctx.add("client_matched", decision="matched Jane Smith on gmp_email")
        ctx.add("array_created", decision="created Hilltop for account 1234-5678")
        ctx.flush(db)   # bulk-adds to session; caller commits
    """

    def __init__(self, tenant_id: str) -> None:
        self.capture_id = str(uuid.uuid4())
        self.tenant_id = tenant_id
        self._events: list[dict] = []
        self._last_t = time.monotonic()

    def add(
        self,
        stage: str,
        *,
        decision: str = "",
        payload: Optional[dict] = None,
    ) -> None:
        now_t = time.monotonic()
        duration_ms = (now_t - self._last_t) * 1000 if self._events else None
        self._last_t = now_t
        self._events.append({
            "tenant_id": self.tenant_id,
            "capture_id": self.capture_id,
            "stage": stage,
            "decision": (decision or "")[:500],
            "payload_excerpt": _safe_excerpt(payload) if payload else None,
            "duration_ms": duration_ms,
            "created_at": datetime.utcnow(),
        })

    def flush(self, db) -> None:
        """Bulk-add all accumulated events to the DB session (caller must commit)."""
        if not self._events:
            return
        from .models import CaptureEvent
        for ev in self._events:
            db.add(CaptureEvent(**ev))
        self._events.clear()

    def discard(self) -> None:
        """Drop any unflushed events without persisting them. Called when a flush
        was rolled back (e.g. inside a SAVEPOINT) so the same events aren't
        silently retried on a later commit of the same session."""
        self._events.clear()
