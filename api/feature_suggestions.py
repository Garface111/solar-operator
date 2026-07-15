"""Feature suggestions from the AO dashboard (\"we're always building\").

Captures owner feature suggestions, emails Ford, and exposes admin endpoints so a
Claude Code agent can pull new ones and write back its review. Added by CC 2026-06-21.

The model is defined here (on the shared Base) so create_all picks it up at startup
— no models.py edit, no migration needed for a brand-new table.
"""
import hmac
import os
import re
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

# Suggestion lifecycle (Tier 1 of the self-improving product, Ford 2026-07-10):
#   new       — just submitted, queued for agent review
#   reviewed  — agent reviewed it (and possibly pushed a human-gated branch)
#   building  — judge tiered it AUTO; the implement agent is working on it now
#   shipped   — auto-shipped: merged, deployed, and verified live
# The widget polls the PUBLIC status endpoint so the customer watches their own
# suggestion go "building… → live — refresh the page".
VALID_STATUSES = ("new", "reviewed", "building", "shipped")


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
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)  # see VALID_STATUSES
    review: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Marked-up screenshot (Ford 2026-07-10, the MindSpace annotate pattern): the
    # customer circles/highlights the live UI and the PNG rides along so the
    # review agent SEES the spatial intent, not just the words. Base64 PNG
    # (no data-URL prefix). Nullable — plain text suggestions unchanged.
    screenshot_b64: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Auto-filled prompt for #18 (UX: put generated prompt directly into build box)
    auto_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)


class SuggestionIn(BaseModel):
    text: str
    email: str | None = None
    screenshot_b64: str | None = None


class ReviewIn(BaseModel):
    review: str
    status: str | None = "reviewed"


class StatusIn(BaseModel):
    status: str


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
    # Minimal #18 UX: auto-generate prompt from text for direct build-box fill
    auto_prompt = f"energy, live energy bulbs sending to people, upgrade to pipeline: {text[:200]}"
    with SessionLocal() as s:
        sug = FeatureSuggestion(text=text, email=email, screenshot_b64=body.screenshot_b64, auto_prompt=auto_prompt)
        s.add(sug)
        s.commit()
        s.refresh(sug)
    # fire-and-forget internal alert
    try:
        send_internal_alert("New feature suggestion", f"{email or 'anon'}: {text[:120]}")
    except Exception:
        pass
    return {"id": sug.id, "status": sug.status, "auto_prompt": auto_prompt}
