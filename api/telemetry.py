"""Usage telemetry — time-on-site via minute-bucket session pings.

POST /v1/telemetry/ping  {path?}
  Auth: session token (SPA) or tenant key (tests/programmatic).
  Upserts one row per (tenant_id, minute_bucket). Visible-tab pings only
  (enforced client-side); server is idempotent and cheap.

Time on site for a window = COUNT(DISTINCT minute_bucket) for that tenant.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import SessionPing, Tenant

log = logging.getLogger("solar.telemetry")
router = APIRouter(tags=["telemetry"])


def _tenant_from_auth(authorization: str | None) -> Tenant:
    """Session first, then tenant-key bearer (same dual path as dashboard)."""
    from .array_owners import _tenant_from_bearer

    return _tenant_from_bearer(authorization)


def _floor_minute(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


class PingBody(BaseModel):
    path: Optional[str] = Field(
        default=None,
        max_length=300,
        description="Current location path (e.g. /, /#arrays)",
    )


@router.post("/v1/telemetry/ping")
def session_ping(
    request: Request,
    body: PingBody | None = None,
    authorization: str | None = Header(default=None),
):
    """Record one minute of visible signed-in presence for this tenant.

    Client should only call while document.visibilityState === 'visible'.
    Server de-dupes to one row per tenant per minute.
    """
    # Light rate limit per IP+tenant — pings are every 60s; burst protect.
    try:
        from . import ratelimit

        ratelimit.enforce(
            request,
            "telemetry_ping",
            max_hits=30,
            window_s=60,
            message="Telemetry rate limit — slow down.",
        )
    except HTTPException:
        raise
    except Exception:
        pass

    t = _tenant_from_auth(authorization)
    now = datetime.utcnow()
    bucket = _floor_minute(now)
    day = bucket.date()
    path = None
    if body and body.path:
        path = (body.path or "").strip()[:300] or None
    email = (getattr(t, "contact_email", None) or "")[:200] or None

    with SessionLocal() as db:
        # Prefer update-then-insert to stay portable (sqlite tests + postgres).
        existing = db.execute(
            select(SessionPing).where(
                SessionPing.tenant_id == t.id,
                SessionPing.minute_bucket == bucket,
            )
        ).scalar_one_or_none()
        if existing:
            if path is not None:
                existing.path = path
            if email is not None:
                existing.email = email
            db.commit()
            return {
                "ok": True,
                "tenant_id": t.id,
                "minute_bucket": bucket.isoformat() + "Z",
                "day": day.isoformat(),
                "created": False,
            }

        row = SessionPing(
            tenant_id=t.id,
            email=email,
            day=day,
            minute_bucket=bucket,
            path=path,
        )
        db.add(row)
        try:
            db.commit()
            created = True
        except IntegrityError:
            db.rollback()
            # Race: another request won the unique key — update that row.
            existing = db.execute(
                select(SessionPing).where(
                    SessionPing.tenant_id == t.id,
                    SessionPing.minute_bucket == bucket,
                )
            ).scalar_one_or_none()
            if existing:
                if path is not None:
                    existing.path = path
                if email is not None:
                    existing.email = email
                db.commit()
            created = False

    return {
        "ok": True,
        "tenant_id": t.id,
        "minute_bucket": bucket.isoformat() + "Z",
        "day": day.isoformat(),
        "created": created,
    }
