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
    """Return a JSON-safe, privacy-scrubbed, size-capped excerpt of raw_payload.

    - Drops auth.* entirely (contains apiToken / refreshToken).
    - Strips accounts[].extra.
    - Truncates accounts_summary from the end until the JSON fits in
      PAYLOAD_MAX_BYTES so the DB row stays small.
    """
    safe: dict = {}
    if "provider" in raw_payload:
        safe["provider"] = raw_payload["provider"]
    if "user" in raw_payload:
        # Portal profile (email, display name, username) — not auth tokens.
        safe["user"] = dict(raw_payload.get("user") or {})
    if "accounts" in raw_payload:
        safe["accounts_summary"] = [
            {k: v for k, v in a.items() if k in SAFE_ACCOUNT_KEYS}
            for a in (raw_payload.get("accounts") or [])
        ]
    # auth.* is never included (apiToken, refreshToken are bearer credentials).

    encoded = json.dumps(safe, default=str)
    if len(encoded.encode()) > PAYLOAD_MAX_BYTES:
        accounts = safe.get("accounts_summary", [])
        while accounts:
            accounts = accounts[:-1]
            candidate = {**safe, "accounts_summary": accounts, "_truncated": True}
            if len(json.dumps(candidate, default=str).encode()) <= PAYLOAD_MAX_BYTES:
                break
        safe["accounts_summary"] = accounts
        safe["_truncated"] = True
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
