"""Feature suggestions from the AO dashboard ("we're always building").

Captures owner feature suggestions, emails Ford, and exposes admin endpoints so a
Claude Code agent can pull new ones and write back its review. Added by CC 2026-06-21.

The model is defined here (on the shared Base) so create_all picks it up at startup
— no models.py edit, no migration needed for a brand-new table.
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


def _now() -> datetime:
    return datetime.utcnow()


class FeatureSuggestion(Base):
    __tablename__ = "feature_suggestions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    product: Mapped[str] = mapped_column(String(32), default="array_operator")
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)  # new | reviewed
    review: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Marked-up screenshot (Ford 2026-07-10, the MindSpace annotate pattern): the
    # customer circles/highlights the live UI and the PNG rides along so the
    # review agent SEES the spatial intent, not just the words. Base64 PNG
    # (no data-URL prefix). Nullable — plain text suggestions unchanged.
    screenshot_b64: Mapped[str | None] = mapped_column(Text, nullable=True)


class SuggestionIn(BaseModel):
    text: str
    email: str | None = None
    screenshot_b64: str | None = None


class ReviewIn(BaseModel):
    review: str
    status: str | None = "reviewed"


def _check_admin(key_header: str | None, key_query: str | None) -> None:
    key = key_header or key_query
    if not ADMIN_API_KEY:
        raise HTTPException(503, "Admin API not configured (set ADMIN_API_KEY)")
    if not hmac.compare_digest(key or "", ADMIN_API_KEY):
        raise HTTPException(403, "Invalid or missing admin key")


@router.post("/v1/feature-suggestion")
def submit_suggestion(body: SuggestionIn, authorization: str | None = Header(default=None)):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Empty suggestion")
    text = text[:5000]
    email = (body.email or "").strip() or None
    tenant_id = None
    product = "array_operator"
    if authorization:
        try:
            from .account import tenant_from_session
            t = tenant_from_session(authorization)
            tenant_id = t.id
            email = email or getattr(t, "contact_email", None)
            product = getattr(t, "product", None) or product
        except Exception:
            pass  # anonymous / expired session — still capture the suggestion
    # Optional marked-up screenshot: accept a data-URL or bare base64 PNG/JPEG,
    # verify it decodes, cap at ~4MB decoded. Invalid/oversized image → keep the
    # TEXT (never lose the suggestion) and just drop the image.
    shot = None
    raw = (body.screenshot_b64 or "").strip()
    if raw:
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            import base64 as _b64
            decoded = _b64.b64decode(raw, validate=True)
            if 0 < len(decoded) <= 4_000_000 and (
                    decoded[:8] == b"\x89PNG\r\n\x1a\n" or decoded[:3] == b"\xff\xd8\xff"):
                shot = raw
        except Exception:
            shot = None
    with SessionLocal() as db:
        fs = FeatureSuggestion(text=text, email=email, tenant_id=tenant_id,
                               product=product, screenshot_b64=shot)
        db.add(fs)
        db.commit()
        db.refresh(fs)
        sid = fs.id
    try:
        send_internal_alert(
            subject=f"New {product} feature suggestion (#{sid})",
            body=(f"From: {email or 'anonymous'}\nTenant: {tenant_id or '-'}\n"
                  f"Product: {product}\n\n{text}\n"
                  + ("\n[Includes a marked-up screenshot — the review agent will read it.]\n"
                     if shot else "")
                  + "\n(Queued for Claude Code agent review.)"),
        )
    except Exception:
        pass
    return {"ok": True, "id": sid}


@router.get("/admin/feature-suggestions")
def list_suggestions(status: str = Query(default="new"),
                     x_admin_key: str | None = Header(default=None),
                     key: str | None = Query(default=None)):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        q = db.query(FeatureSuggestion)
        if status and status != "all":
            q = q.filter(FeatureSuggestion.status == status)
        rows = q.order_by(FeatureSuggestion.created_at.desc()).limit(100).all()
        out = [{
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "product": r.product, "email": r.email, "tenant_id": r.tenant_id,
            "text": r.text, "status": r.status, "review": r.review,
            # Flag only — the PNG is fetched via /screenshot, never inlined here.
            "has_screenshot": r.screenshot_b64 is not None,
        } for r in rows]
    return JSONResponse({"suggestions": out, "count": len(out)})


@router.get("/admin/feature-suggestions/{sid}/screenshot")
def suggestion_screenshot(sid: int,
                          x_admin_key: str | None = Header(default=None),
                          key: str | None = Query(default=None)):
    """The suggestion's marked-up screenshot as raw image bytes (admin/agent).
    PNG unless the upload was a JPEG."""
    _check_admin(x_admin_key, key)
    import base64 as _b64
    from fastapi.responses import Response
    with SessionLocal() as db:
        fs = db.get(FeatureSuggestion, sid)
        if not fs or not fs.screenshot_b64:
            raise HTTPException(404, "No screenshot on this suggestion")
        data = _b64.b64decode(fs.screenshot_b64)
    media = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"
    return Response(content=data, media_type=media)


@router.post("/admin/feature-suggestions/{sid}/review")
def review_suggestion(sid: int, body: ReviewIn,
                      x_admin_key: str | None = Header(default=None),
                      key: str | None = Query(default=None)):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        fs = db.get(FeatureSuggestion, sid)
        if not fs:
            raise HTTPException(404, "Suggestion not found")
        fs.review = (body.review or "")[:20000]
        fs.status = body.status or "reviewed"
        fs.reviewed_at = _now()
        text, email = fs.text, fs.email
        db.commit()
    try:
        send_internal_alert(
            subject=f"Claude Code review of feature suggestion #{sid}",
            body=f"Suggestion: {text}\nFrom: {email or 'anonymous'}\n\n--- Agent review ---\n{body.review}",
        )
    except Exception:
        pass
    return {"ok": True, "id": sid}
