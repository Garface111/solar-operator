"""Feature suggestions from the AO dashboard ("we're always building").

Captures owner feature suggestions, emails Ford, and exposes admin endpoints so a
Claude Code agent can pull new ones and write back its review.

Public:
  POST /v1/feature-suggestion
  GET  /v1/feature-suggestion/{id}/status

Admin (X-Admin-Key):
  GET/POST review, list, status, screenshot, wait

The model is defined here (on the shared Base) so create_all picks it up at startup.
"""
from __future__ import annotations

import base64 as _b64
import hmac
import os
import re
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import SessionLocal
from .models import Base
from .notify import send_internal_alert

router = APIRouter()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Suggestion lifecycle (Tier 1 self-improving product, Ford 2026-07-10):
#   new       — just submitted, queued for agent review
#   reviewed  — agent reviewed (and possibly pushed a human-gated branch)
#   building  — judge tiered it AUTO; implement agent working
#   shipped   — auto-shipped: merged, deployed, verified live
VALID_STATUSES = ("new", "reviewed", "building", "shipped")


# ── Actionability gate (Ford 2026-07-17) ────────────────────────────────────
# The Energy Agent's mind/sovereign and the Improve box were filing questions,
# spoken-chatter fragments, and internal control text ("Call escalate_to_ford
# now") as build tickets — the customer then saw "Working on your change" for
# something they never asked to build. This gate strips the "[tag]" markup the
# mind/sovereign prepend, then requires the CORE ask to name an actual UI change.
# Conservative on purpose: it only rejects high-confidence non-requests, so a
# real "add / move / separate / make X bigger" ask still passes.
_CONTROL_ARTIFACTS = (
    "escalate_to_ford", "call escalate", "site improvement #", "site improve #",
)
_QUESTION_STARTS = (
    "what ", "what's", "whats", "why ", "how ", "when ", "where ", "who ",
    "which ", "is ", "are ", "should ", "shall ", "can i", "could ", "do i",
    "does ", "did ", "will ", "would i",
)
_CHANGE_SIGNALS = (
    "add ", "remove", "delete", "move ", "rename", "replace", "swap", "reorder",
    "sort ", "group ", "align", "resize", "shrink", "enlarge", "hide ", "show ",
    "split", "separate", "combine", "merge ", "fix ", "change", "put ", "place ",
    "highlight", "tint", "collapse", "expand", "enable", "disable", "toggle",
    "default to", "relabel", "color", "colour", "bigger", "smaller", "larger",
    "wider", "taller", "shorter", "bold", "instead of", "should be", "should show",
    "would be nice", "can you", "could you", "make it", "make the", "i want",
    "i'd like", "i would like", "let me", "please add", "please make",
    "please move", "put the", "move the", "turn the", "give me", "get rid of",
    "declutter", "reposition", "restyle", "rework", "redesign",
)
_UI_NOUNS = (
    "button", "tab", "row", "column", "chart", "graph", "icon", "panel", "card",
    "menu", "field", "header", "footer", "list", "table", "badge", "chip",
    "block", "dropdown", "modal", "popup", "banner", "tile", "legend", "tooltip",
    "spoken", "reply", "widget", "sidebar", "slider", "checkbox", "form",
    "dialog", "tab bar", "nav", "avatar", "thumbnail", "placeholder",
)


def _strip_suggestion_markup(text: str) -> str:
    """Drop the leading '[tag]' lines and 'Site improve:'/'[Sovereign]' prefixes
    so the gate judges the real ask, not the framing the mind/sovereign added."""
    out = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            continue  # a whole tag line, e.g. "[Proactive mind — …]"
        s = re.sub(r"^\[[^\]]*\]\s*", "", s)  # a leading inline [tag]
        s = re.sub(
            r"^(site improve[d]?|improve site|feature request|\[sovereign\])\s*:?\s*",
            "", s, flags=re.I,
        )
        if s:
            out.append(s)
    return " ".join(out).strip()


def is_actionable_suggestion(text: str) -> tuple[bool, str]:
    """(ok, reason). True when `text` describes a real UI change worth building.
    Rejects questions, observations/chatter, and internal control artifacts."""
    core = _strip_suggestion_markup(text)
    low = core.lower()
    if len(core) < 12:
        return False, "too short to build from"
    if any(a in low for a in _CONTROL_ARTIFACTS):
        return False, "internal control text, not a change request"
    has_change = any(sig in low for sig in _CHANGE_SIGNALS)
    has_ui = any(n in low for n in _UI_NOUNS)
    if not (has_change or has_ui):
        looks_q = low.rstrip(" .!").endswith("?") or low.startswith(_QUESTION_STARTS)
        return False, ("looks like a question, not a change" if looks_q
                       else "no concrete change described")
    return True, "ok"


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
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    review: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Marked-up screenshot (base64 PNG/JPEG, no data-URL prefix)
    screenshot_b64: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional auto-filled build prompt (UX #18)
    auto_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)


# create_all only creates missing *tables* — it never ALTERs existing ones. When
# auto_prompt (or screenshot_b64) was added to the model, prod kept the old
# shape and every FeatureSuggestion SELECT died with UndefinedColumn
# (Sentry: /v1/sovereign/desk/ops → list_features). migrate.py has the ALTER,
# but this module is also the create_all path; self-heal so reads work even if
# migrate has not run yet.
_schema_ensured_ids: set[int] = set()


def ensure_feature_suggestion_columns(db_or_bind=None) -> None:
    """Idempotent ADD COLUMN for feature_suggestions fields create_all skips.

    DDL always runs on a short-lived engine connection that commits on its own.
    Never use the caller's session connection: on Postgres, ALTER is transactional,
    so a later request rollback would undo the column while this process still
    caches "ensured" — next SELECT dies with UndefinedColumn (Sentry PYTHON-FASTAPI-24
    triage_feature_queue recurrence after the list_features self-heal).
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session as SASession

    from .db import engine as default_engine

    if isinstance(db_or_bind, SASession):
        eng = db_or_bind.get_bind()
    else:
        eng = db_or_bind or default_engine

    key = id(getattr(eng, "sync_engine", eng))
    if key in _schema_ensured_ids:
        return

    needed = ("auto_prompt", "screenshot_b64")

    def _table_exists(connection) -> bool:
        dialect = connection.dialect.name
        if dialect == "sqlite":
            row = connection.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='feature_suggestions'"
            )).fetchone()
            return row is not None
        row = connection.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'feature_suggestions'"
        )).fetchone()
        return row is not None

    def _existing_cols(connection) -> set[str]:
        dialect = connection.dialect.name
        if dialect == "sqlite":
            rows = connection.execute(
                text("PRAGMA table_info(feature_suggestions)")
            ).fetchall()
            return {r[1] for r in rows}
        rows = connection.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'feature_suggestions'"
        )).fetchall()
        # PG may return identifiers with mixed case depending on driver/version
        return {str(r[0]).lower() for r in rows}

    def _apply(connection) -> None:
        if not _table_exists(connection):
            FeatureSuggestion.__table__.create(bind=connection, checkfirst=True)
            return
        cols = _existing_cols(connection)
        for col in needed:
            if col in cols:
                continue
            try:
                connection.execute(text(
                    f"ALTER TABLE feature_suggestions ADD COLUMN {col} TEXT"
                ))
            except Exception as exc:
                msg = str(exc).lower()
                if "already exists" in msg or "duplicate column" in msg:
                    continue
                raise

    # Commit outside any ambient request transaction (see docstring).
    with eng.begin() as conn:
        _apply(conn)
        if _table_exists(conn):
            cols_after = _existing_cols(conn)
        else:
            cols_after = {col.key for col in FeatureSuggestion.__table__.columns}
    if all(col in cols_after for col in needed):
        _schema_ensured_ids.add(key)


def _reset_schema_ensure_for_tests() -> None:
    """Test-only: clear the ensure cache so a fresh engine is re-checked."""
    _schema_ensured_ids.clear()


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
    # compare_digest(str, str) raises TypeError on non-ASCII; bytes is safe.
    provided = (key or "").encode("utf-8")
    expected = ADMIN_API_KEY.encode("utf-8")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(403, "Invalid or missing admin key")


def _customer_facing_outcome(status: str, review: str | None) -> dict:
    """Short lifecycle payload for the journey UI. No agent transcripts."""
    if status == "shipped":
        return {
            "status": "shipped",
            "detail": "Live on the site — refresh to see it.",
        }
    if status == "building":
        detail = "Energy Agent is building this now."
        if review and (
            "claimed live" in (review or "").lower()
            or "claimed this" in (review or "").lower()
            or "Sovereign claimed" in (review or "")
            or "Energy Agent claimed" in (review or "")
        ):
            detail = "Energy Agent claimed this and is building it now."
        return {
            "status": "building",
            "detail": detail,
        }
    if status == "new":
        return {
            "status": "new",
            "detail": "Received — Energy Agent is taking this into mind now.",
        }
    # reviewed / held
    reason = ""
    if review:
        # Prefer first non-empty line as a short hold reason
        for line in (review or "").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and len(line) < 240:
                reason = line
                break
    held = bool(re.search(r"\b(hold|reject|blocked|won't|cannot|human)\b", reason, re.I))
    return {
        "status": "reviewed",
        "detail": reason or "Reviewed — held for a human look.",
        "failed": held,
        "can_escalate": True,
        "outcome": "held" if held else "reviewed",
    }


def _parse_screenshot(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        decoded = _b64.b64decode(raw, validate=True)
        if 0 < len(decoded) <= 4_000_000 and (
            decoded[:8] == b"\x89PNG\r\n\x1a\n" or decoded[:3] == b"\xff\xd8\xff"
        ):
            return raw
    except Exception:
        return None
    return None


@router.post("/v1/feature-suggestion")
def submit_suggestion(body: SuggestionIn, authorization: str | None = Header(default=None)):
    """Public capture for Improve / wish pipeline. Always returns ok+id on success."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Empty suggestion")
    text = text[:5000]
    # Don't open a "building your change" journey for a question, an observation,
    # or stray control text — answer it in chat instead of filing a ticket.
    ok, _why = is_actionable_suggestion(text)
    if not ok:
        return {
            "ok": False,
            "not_actionable": True,
            "detail": "That reads like a question or a note, not a site change. "
                      "Ask me directly and I'll answer — or, for a change, tell me "
                      "what to add, move, or adjust.",
        }
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
            pass  # anonymous / expired — still capture

    shot = _parse_screenshot(body.screenshot_b64)
    auto_prompt = f"Site improve: {text[:200]}"

    with SessionLocal() as db:
        ensure_feature_suggestion_columns(db)
        fs = FeatureSuggestion(
            text=text,
            email=email,
            tenant_id=tenant_id,
            product=product,
            screenshot_b64=shot,
            auto_prompt=auto_prompt,
        )
        db.add(fs)
        db.commit()
        db.refresh(fs)
        sid = fs.id
        # Hand to Sovereign immediately (mind + queue + optional ship job)
        claimed = None
        try:
            from .energy_agent_sovereign_ops import claim_improvement_for_sovereign
            claimed = claim_improvement_for_sovereign(
                db,
                feature_id=sid,
                text=text,
                tenant_id=tenant_id,
                email=email,
                product=product,
                has_screenshot=bool(shot),
            )
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            claimed = None

    # Live wake: force cortex so Sovereign acts now (not next 5m tick)
    try:
        from .energy_agent_sovereign_subconscious import fire_and_forget_wake
        fire_and_forget_wake(
            "feature_suggestion",
            {
                "id": sid,
                "text": text[:1200],
                "tenant_id": tenant_id,
                "email": email,
                "product": product,
                "has_screenshot": bool(shot),
                "claimed": bool(claimed and claimed.get("ok")),
                "status": (claimed or {}).get("status") or "new",
            },
            source="feature_suggestion",
            force_cortex=True,
        )
    except Exception:
        pass

    try:
        send_internal_alert(
            subject=f"New {product} feature suggestion (#{sid})",
            body=(
                f"From: {email or 'anonymous'}\nTenant: {tenant_id or '-'}\n"
                f"Product: {product}\n\n{text}\n"
                + (
                    "\n[Includes a marked-up screenshot — the review agent will read it.]\n"
                    if shot
                    else ""
                )
                + "\n(Handed to Sovereign mind immediately + classic judge path.)"
            ),
        )
    except Exception:
        pass

    status_out = (claimed or {}).get("status") or "new"
    # Frontend (EA Improve + wish widget) requires ok:true + id
    return {
        "ok": True,
        "id": sid,
        "status": status_out,
        "auto_prompt": auto_prompt,
        "sovereign": {
            "claimed": bool(claimed and claimed.get("ok")),
            "job_id": (claimed or {}).get("job_id"),
        },
    }


@router.get("/v1/feature-suggestion/{sid}/status")
def suggestion_status(sid: int):
    """PUBLIC: lifecycle status for the journey UI (poll after submit)."""
    with SessionLocal() as db:
        fs = db.get(FeatureSuggestion, sid)
        if not fs:
            raise HTTPException(404, "Unknown suggestion")
        status = fs.status if fs.status in VALID_STATUSES else "reviewed"
        out = _customer_facing_outcome(status, fs.review)
        out["id"] = sid
        return out


@router.get("/admin/feature-suggestions/wait")
def wait_new_suggestions(
    timeout: int = Query(default=25, ge=1, le=60),
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    """Long-poll for new suggestions (agent pipeline)."""
    import time
    _check_admin(x_admin_key, key)
    deadline = time.time() + min(timeout, 55)
    while time.time() < deadline:
        with SessionLocal() as db:
            ensure_feature_suggestion_columns(db)
            rows = (
                db.query(FeatureSuggestion)
                .filter(FeatureSuggestion.status == "new")
                .order_by(FeatureSuggestion.created_at.asc())
                .limit(20)
                .all()
            )
            if rows:
                return {
                    "ok": True,
                    "suggestions": [
                        {
                            "id": r.id,
                            "created_at": r.created_at.isoformat() if r.created_at else None,
                            "product": r.product,
                            "email": r.email,
                            "tenant_id": r.tenant_id,
                            "text": r.text,
                            "status": r.status,
                            "has_screenshot": r.screenshot_b64 is not None,
                        }
                        for r in rows
                    ],
                    "count": len(rows),
                }
        time.sleep(1.2)
    return {"ok": True, "suggestions": [], "count": 0, "timeout": True}


@router.post("/admin/feature-suggestions/{sid}/status")
def set_suggestion_status(
    sid: int,
    body: StatusIn,
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    _check_admin(x_admin_key, key)
    status = (body.status or "").strip()
    if status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(VALID_STATUSES)}")
    with SessionLocal() as db:
        fs = db.get(FeatureSuggestion, sid)
        if not fs:
            raise HTTPException(404, "Suggestion not found")
        fs.status = status
        db.commit()
    return {"ok": True, "id": sid, "status": status}


@router.get("/admin/feature-suggestions")
def list_suggestions(
    status: str = Query(default="new"),
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        ensure_feature_suggestion_columns(db)
        q = db.query(FeatureSuggestion)
        if status and status != "all":
            q = q.filter(FeatureSuggestion.status == status)
        rows = q.order_by(FeatureSuggestion.created_at.desc()).limit(100).all()
        out = [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "product": r.product,
                "email": r.email,
                "tenant_id": r.tenant_id,
                "text": r.text,
                "status": r.status,
                "review": r.review,
                "has_screenshot": r.screenshot_b64 is not None,
            }
            for r in rows
        ]
    return JSONResponse({"suggestions": out, "count": len(out)})


@router.get("/admin/feature-suggestions/{sid}/screenshot")
def suggestion_screenshot(
    sid: int,
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        fs = db.get(FeatureSuggestion, sid)
        if not fs or not fs.screenshot_b64:
            raise HTTPException(404, "No screenshot on this suggestion")
        data = _b64.b64decode(fs.screenshot_b64)
    media = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"
    return Response(content=data, media_type=media)


@router.post("/admin/feature-suggestions/{sid}/review")
def review_suggestion(
    sid: int,
    body: ReviewIn,
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    _check_admin(x_admin_key, key)
    with SessionLocal() as db:
        fs = db.get(FeatureSuggestion, sid)
        if not fs:
            raise HTTPException(404, "Suggestion not found")
        fs.review = (body.review or "")[:20000]
        fs.status = body.status if body.status in VALID_STATUSES else "reviewed"
        fs.reviewed_at = _now()
        text, email, final_status = fs.text, fs.email, fs.status
        db.commit()
    try:
        send_internal_alert(
            subject=(
                f"AUTO-SHIPPED feature suggestion #{sid} — live on arrayoperator.com"
                if final_status == "shipped"
                else f"Claude Code review of feature suggestion #{sid}"
            ),
            body=(
                f"Suggestion: {text}\nFrom: {email or 'anonymous'}\n\n"
                f"--- Agent review ---\n{body.review}"
            ),
        )
    except Exception:
        pass
    return {"ok": True, "id": sid}
