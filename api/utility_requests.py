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
        raise HTTPException(403, "Invalid or missing admin key")


@router.post("/v1/utility-requests")
def submit_requests(body: RequestsIn, authorization: str | None = Header(default=None)):
    """Queue one or many utility-add requests. Returns the created ids."""
    items = body.requests or []
    if not items:
        raise HTTPException(400, "No requests")
    items = items[:50]  # a sane batch cap
    email = None
    tenant_id = None
    product = "array_operator"
    if authorization:
        try:
            from .account import tenant_from_session
            t = tenant_from_session(authorization)
            tenant_id = t.id
            email = getattr(t, "contact_email", None)
            product = getattr(t, "product", None) or product
        except Exception:
            pass  # anonymous / expired session — still capture the request
    ids = []
    names = []
    with SessionLocal() as db:
        for it in items:
            name = (it.name or "").strip()[:200]
            if not name:
                continue
            row = UtilityRequest(
                name=name,
                state=(it.state or "").strip()[:40] or None,
                url=(it.url or "").strip()[:500] or None,
                note=(it.note or "").strip()[:2000] or None,
                email=email, tenant_id=tenant_id, product=product,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            ids.append(row.id)
            names.append(name)
    if not ids:
        raise HTTPException(400, "No valid requests")
    try:
        send_internal_alert(
            subject=f"{len(ids)} utility-add request(s) from {product} (#{ids[0]}"
                    + (f"–{ids[-1]}" if len(ids) > 1 else "") + ")",
            body=("From: " + (email or "anonymous") + "\nTenant: " + (tenant_id or "-") + "\n\n"
                  + "Requested utilities:\n" + "\n".join(f"  • {n}" for n in names)
                  + "\n\n(Queued for the utility-request agent — it researches + wires them up.)"),
        )
    except Exception:
        pass
    return {"ok": True, "ids": ids, "count": len(ids)}


@router.get("/v1/utility-request/{rid}/status")
def request_status(rid: int):
    """PUBLIC: this request's lifecycle status only (id-guessers learn nothing else)."""
    with SessionLocal() as db:
        r = db.get(UtilityRequest, rid)
        if not r:
            raise HTTPException(404, "Not found")
        status = r.status if r.status in VALID_STATUSES else "new"
    return {"status": status}


@router.get("/admin/utility-requests")
def list_requests(status: str = Query(default="new"),
                  x_admin_key: str | None = Header(default=None),
                  key: str | None = Query(default=None)):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        q = db.query(UtilityRequest)
        if status and status != "all":
            q = q.filter(UtilityRequest.status == status)
        rows = q.order_by(UtilityRequest.created_at.desc()).limit(200).all()
        out = [{
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "product": r.product, "email": r.email, "tenant_id": r.tenant_id,
            "name": r.name, "state": r.state, "url": r.url, "note": r.note,
            "status": r.status, "result": r.result,
        } for r in rows]
    return JSONResponse({"requests": out, "count": len(out)})


@router.post("/admin/utility-requests/{rid}/status")
def set_request_status(rid: int, body: StatusIn,
                       x_admin_key: str | None = Header(default=None),
                       key: str | None = Query(default=None)):
    """Lifecycle tick from the agent (e.g. 'researching' the moment it starts)."""
    _check_admin(x_admin_key, key)
    status = (body.status or "").strip()
    if status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(VALID_STATUSES)}")
    with SessionLocal() as db:
        r = db.get(UtilityRequest, rid)
        if not r:
            raise HTTPException(404, "Not found")
        r.status = status
        db.commit()
    return {"ok": True, "id": rid, "status": status}


@router.post("/admin/utility-requests/{rid}/result")
def write_result(rid: int, body: ResultIn,
                 x_admin_key: str | None = Header(default=None),
                 key: str | None = Query(default=None)):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        r = db.get(UtilityRequest, rid)
        if not r:
            raise HTTPException(404, "Not found")
        r.result = (body.result or "")[:20000]
        r.status = body.status if body.status in VALID_STATUSES else "reviewed"
        r.reviewed_at = _now()
        name, email, final_status = r.name, r.email, r.status
        db.commit()
    try:
        send_internal_alert(
            subject=(f"ADDED utility '{name}' (request #{rid}) — live to connect"
                     if final_status == "added"
                     else f"Utility request #{rid} ({name}) — {final_status}"),
            body=f"Utility: {name}\nFrom: {email or 'anonymous'}\n\n--- Agent result ---\n{body.result}",
        )
    except Exception:
        pass
    return {"ok": True, "id": rid, "status": final_status}
