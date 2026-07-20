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
    # compare_digest(str, str) raises TypeError on non-ASCII; bytes is safe.
    provided = (key or "").encode("utf-8")
    expected = ADMIN_API_KEY.encode("utf-8")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(403, "Forbidden")


@router.post("/v1/utility-requests")
def create_requests(body: RequestsIn, authorization: str | None = Header(default=None)):
    """Batch-create utility requests. Public (no auth) so the picker can queue fast."""
    # Optional: extract tenant_id/email from session if provided, else anonymous.
    tenant_id = None
    email = None
    if authorization:
        try:
            from .account import tenant_from_session
            t = tenant_from_session(authorization)
            tenant_id = t.id
            email = t.email
        except Exception:
            pass  # anonymous is fine

    with SessionLocal() as db:
        for req in body.requests:
            db.add(UtilityRequest(
                name=req.name.strip(),
                state=req.state.strip().upper() if req.state else None,
                url=req.url.strip() if req.url else None,
                note=req.note.strip() if req.note else None,
                tenant_id=tenant_id,
                email=email,
            ))
        db.commit()
    return {"queued": len(body.requests)}


@router.get("/v1/utility-requests")
def list_requests(
    status: str | None = Query(default=None),
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    key: str | None = Query(default=None),
):
    """List utility requests (admin only). Filter by status if provided."""
    _check_admin(api_key, key)
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
                    "created_at": r.created_at.isoformat(),
                    "name": r.name,
                    "state": r.state,
                    "url": r.url,
                    "note": r.note,
                    "status": r.status,
                    "result": r.result,
                    "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
                    "tenant_id": r.tenant_id,
                    "email": r.email,
                }
                for r in rows
            ]
        }


@router.patch("/v1/utility-requests/{req_id}/result")
def update_result(
    req_id: int,
    body: ResultIn,
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    key: str | None = Query(default=None),
):
    """Agent writeback: record research result + optionally change status."""
    _check_admin(api_key, key)
    if body.status and body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Must be one of {VALID_STATUSES}")
    with SessionLocal() as db:
        row = db.query(UtilityRequest).filter(UtilityRequest.id == req_id).first()
        if not row:
            raise HTTPException(404, "Request not found")
        row.result = body.result
        if body.status:
            row.status = body.status
        row.reviewed_at = _now()
        db.commit()
        return {"id": row.id, "status": row.status, "result": row.result}


@router.patch("/v1/utility-requests/{req_id}/status")
def update_status(
    req_id: int,
    body: StatusIn,
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    key: str | None = Query(default=None),
):
    """Change status only (e.g., researching → added after manual verification)."""
    _check_admin(api_key, key)
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Must be one of {VALID_STATUSES}")
    with SessionLocal() as db:
        row = db.query(UtilityRequest).filter(UtilityRequest.id == req_id).first()
        if not row:
            raise HTTPException(404, "Request not found")
        row.status = body.status
        row.reviewed_at = _now()
        db.commit()
        return {"id": row.id, "status": row.status}


@router.delete("/v1/utility-requests/{req_id}")
def delete_request(
    req_id: int,
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    key: str | None = Query(default=None),
):
    """Delete a request (admin only). Use sparingly; prefer status transitions."""
    _check_admin(api_key, key)
    with SessionLocal() as db:
        row = db.query(UtilityRequest).filter(UtilityRequest.id == req_id).first()
        if not row:
            raise HTTPException(404, "Request not found")
        db.delete(row)
        db.commit()
        return {"deleted": req_id}
