"""Utility-add requests from the AO Master Account "add a utility login" picker.

When an operator searches for their utility and it isn't in the catalog yet, they
queue it here — many at a time, fast. Each becomes a row a Claude Code agent picks
up (scripts/review_utility_requests.py): it researches the portal, AUTO-ADDS the
easy ones (SmartHub / NISC co-ops need no reverse-engineering), and for bespoke
portals drafts the adapter + flags that a browser HAR capture is needed. Mirrors
api/feature_suggestions.py exactly (same shared Base → create_all, no migration).

Added by CC 2026-07-11 (Ford: "request a utility → an agent adds it on").
"""
import hmac
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import String, Integer, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from .db import SessionLocal
from .models import Base
from .notify import send_internal_alert

router = APIRouter()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Request lifecycle:
#   new         — just submitted, queued for the agent
#   researching — the agent is investigating the portal (SmartHub? bespoke?)
#   added       — wired into the providers catalog + verified (live to connect)
#   declined    — can't be added as described (agent explains in `result`)
#   reviewed    — investigated, needs a human/HAR step (agent's plan in `result`)
VALID_STATUSES = ("new", "researching", "added", "declined", "reviewed")


def _now() -> datetime:
    return datetime.utcnow()


class UtilityRequest(Base):
    __tablename__ = "utility_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    product: Mapped[str] = mapped_column(String(32), default="array_operator")
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))                 # what they searched / typed
    state: Mapped[str | None] = mapped_column(String(40), nullable=True)   # optional 2-letter or region hint
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)    # optional portal URL they know
    note: Mapped[str | None] = mapped_column(Text, nullable=True)          # optional free text
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)  # see VALID_STATUSES
    result: Mapped[str | None] = mapped_column(Text, nullable=True)        # agent writeback
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RequestItem(BaseModel):
    name: str
    state: str | None = None
    url: str | None = None
    note: str | None = None


class RequestsIn(BaseModel):
    # The picker submits the whole queue at once ("do a lot of them fast").
    requests: list[RequestItem]


class ResultIn(BaseModel):
    result: str
    status: str | None = "reviewed"


class StatusIn(BaseModel):
    status: str


def _check_admin(key_header: str | None, key_query: str | None) -> None:
    key = key_header or key_query
    if not ADMIN_API_KEY:
        raise HTTPException(503, "Admin API not configured (set ADMIN_API_KEY)")
    if not hmac.compare_digest(key or "", ADMIN_API_KEY):
        raise HTTPException(401, "Invalid admin key")


@router.post("/v1/utility-requests")
def create_requests(body: RequestsIn, authorization: str | None = Header(default=None)):
    """Batch-create utility-add requests. Public endpoint (no auth check yet)."""
    # TODO: extract tenant_id from session if we want per-tenant tracking.
    with SessionLocal() as db:
        for req in body.requests:
            db.add(UtilityRequest(
                name=req.name.strip(),
                state=req.state.strip().upper() if req.state else None,
                url=req.url.strip() if req.url else None,
                note=req.note.strip() if req.note else None,
            ))
        db.commit()
    return {"ok": True, "count": len(body.requests)}


@router.get("/v1/utility-requests")
def list_requests(
    status: str | None = Query(default=None),
    admin_key: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """List utility-add requests. Admin-only."""
    _check_admin(authorization, admin_key)
    with SessionLocal() as db:
        q = db.query(UtilityRequest)
        if status:
            if status not in VALID_STATUSES:
                raise HTTPException(400, f"Invalid status. Must be one of {VALID_STATUSES}")
            q = q.filter(UtilityRequest.status == status)
        rows = q.order_by(UtilityRequest.created_at.desc()).all()
        return {
            "requests": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat() + "Z",
                    "name": r.name,
                    "state": r.state,
                    "url": r.url,
                    "note": r.note,
                    "status": r.status,
                    "result": r.result,
                    "reviewed_at": r.reviewed_at.isoformat() + "Z" if r.reviewed_at else None,
                }
                for r in rows
            ]
        }


@router.patch("/v1/utility-requests/{request_id}/result")
def update_result(
    request_id: int,
    body: ResultIn,
    admin_key: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """Agent writeback: record research result + status. Admin-only."""
    _check_admin(authorization, admin_key)
    if body.status and body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Must be one of {VALID_STATUSES}")
    with SessionLocal() as db:
        row = db.query(UtilityRequest).filter(UtilityRequest.id == request_id).first()
        if not row:
            raise HTTPException(404, "Request not found")
        row.result = body.result
        if body.status:
            row.status = body.status
        row.reviewed_at = _now()
        db.commit()
    return {"ok": True}


@router.patch("/v1/utility-requests/{request_id}/status")
def update_status(
    request_id: int,
    body: StatusIn,
    admin_key: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """Update request status (e.g., mark as 'added' after wiring). Admin-only."""
    _check_admin(authorization, admin_key)
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Must be one of {VALID_STATUSES}")
    with SessionLocal() as db:
        row = db.query(UtilityRequest).filter(UtilityRequest.id == request_id).first()
        if not row:
            raise HTTPException(404, "Request not found")
        row.status = body.status
        if body.status in ("added", "declined", "reviewed"):
            row.reviewed_at = _now()
        db.commit()
    return {"ok": True}
