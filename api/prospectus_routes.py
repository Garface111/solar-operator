"""api/prospectus_routes.py — Array Prospectus endpoints (Array Secondary Market v0).

Tenant-scoped build/list/read + revocable tokenized share link, plus ONE public
read-only route. No money, no fees, no brokerage — a document surface only.

Sharing posture (the legal line — hold it):
  • A minted link DEFAULTS TO UNPUBLISHED. The public route 404s until the owner
    deliberately publishes it — the FIRST external share is a deliberate act.
  • Offtaker PII is REDACTED by default; the owner opts in to reveal it.
  • Revocable; a revoked or unpublished token 404s. view_count gives "your lender
    opened it" feedback.
"""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from . import prospectus as prospectus_mod
from . import ratelimit
from .db import SessionLocal
from .models import AgentDocument, ProspectusShare, Tenant, now

router = APIRouter()


def _tenant(authorization: str | None) -> Tenant:
    # Lazy import keeps this module free of the heavy array_owners import chain at
    # load time; it's the canonical dashboard resolver (session token OR key).
    from .array_owners import _tenant_from_bearer
    return _tenant_from_bearer(authorization)


def _load_payload(doc: AgentDocument) -> dict:
    try:
        return json.loads(doc.content)
    except (ValueError, TypeError):
        raise HTTPException(500, "Stored prospectus is unreadable")


def _share_dict(sh: ProspectusShare) -> dict:
    return {
        "id": sh.id,
        "token": sh.token,
        "share_path": f"/v1/prospectus/{sh.token}",
        "published": bool(sh.published),
        "redact_offtaker_pii": bool(sh.redact_offtaker_pii),
        "revoked": sh.revoked_at is not None,
        "view_count": sh.view_count,
        "last_viewed_at": sh.last_viewed_at.isoformat() if sh.last_viewed_at else None,
        "content_sha256": sh.content_sha256,
        "created_at": sh.created_at.isoformat() if sh.created_at else None,
    }


# ─────────────────────────── build + persist ────────────────────────────────

class BuildBody(BaseModel):
    purpose: str = "sale"          # sale | refinance
    window_days: int | None = None


@router.post("/v1/array-owners/arrays/{array_id}/prospectus")
def build_prospectus_ep(array_id: int, body: BuildBody | None = None,
                        authorization: str | None = Header(default=None)) -> dict:
    """Build a verified prospectus for an array and persist it as an AgentDocument
    (doc_type='prospectus'). Pure read of the array's own history + compose."""
    tenant = _tenant(authorization)
    body = body or BuildBody()
    with SessionLocal() as db:
        try:
            payload = prospectus_mod.build_prospectus(
                db, tenant, array_id,
                window_days=body.window_days, purpose=body.purpose)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        doc = AgentDocument(
            tenant_id=tenant.id,
            doc_type="prospectus",
            title=f"Array Prospectus — {payload.get('array_name') or array_id}",
            content=json.dumps(payload),
            content_format="json",
            created_by="owner",
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        return {
            "ok": True,
            "document_id": doc.id,
            "array_id": array_id,
            "array_name": payload.get("array_name"),
            "content_sha256": payload.get("content_sha256"),
            "generated_at": payload.get("generated_at"),
            "prospectus": payload,
        }


@router.get("/v1/array-owners/prospectuses")
def list_prospectuses_ep(array_id: int | None = Query(default=None),
                         authorization: str | None = Header(default=None)) -> dict:
    """List this tenant's generated prospectuses (newest first) + their shares."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        docs = db.execute(
            select(AgentDocument)
            .where(AgentDocument.tenant_id == tenant.id,
                   AgentDocument.doc_type == "prospectus")
            .order_by(AgentDocument.created_at.desc())
        ).scalars().all()
        shares_by_doc: dict[int, list[ProspectusShare]] = {}
        for sh in db.execute(
            select(ProspectusShare).where(ProspectusShare.tenant_id == tenant.id)
        ).scalars().all():
            shares_by_doc.setdefault(sh.agent_document_id, []).append(sh)

        out = []
        for doc in docs:
            try:
                payload = json.loads(doc.content)
            except (ValueError, TypeError):
                continue
            aid = payload.get("array_id")
            if array_id is not None and aid != array_id:
                continue
            out.append({
                "document_id": doc.id,
                "array_id": aid,
                "array_name": payload.get("array_name"),
                "purpose": payload.get("purpose"),
                "generated_at": payload.get("generated_at"),
                "content_sha256": payload.get("content_sha256"),
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "shares": [_share_dict(s) for s in shares_by_doc.get(doc.id, [])],
            })
        return {"ok": True, "prospectuses": out}


@router.get("/v1/array-owners/prospectus/{document_id}")
def get_prospectus_ep(document_id: int,
                      authorization: str | None = Header(default=None)) -> dict:
    """Return a stored prospectus payload (owner view — full, un-redacted)."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        doc = db.get(AgentDocument, document_id)
        if doc is None or doc.tenant_id != tenant.id or doc.doc_type != "prospectus":
            raise HTTPException(404, "Prospectus not found")
        shares = db.execute(
            select(ProspectusShare)
            .where(ProspectusShare.agent_document_id == doc.id,
                   ProspectusShare.tenant_id == tenant.id)
        ).scalars().all()
        return {"ok": True, "document_id": doc.id,
                "prospectus": _load_payload(doc),
                "shares": [_share_dict(s) for s in shares]}


@router.get("/v1/array-owners/prospectus/{document_id}/document")
def owner_render_ep(document_id: int, format: str = Query(default="pdf"),
                    authorization: str | None = Header(default=None)) -> Response:
    """Owner-scoped render of a stored prospectus (full, un-redacted) as PDF or
    HTML — the "download / preview before you share" path. Auth required; the SPA
    fetches this with its Bearer header and blob-opens the result."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        doc = db.get(AgentDocument, document_id)
        if doc is None or doc.tenant_id != tenant.id or doc.doc_type != "prospectus":
            raise HTTPException(404, "Prospectus not found")
        payload = _load_payload(doc)
    if format == "pdf":
        pdf = prospectus_mod.render_prospectus_pdf(payload)
        return Response(content=pdf, media_type="application/pdf", headers={
            "Content-Disposition": f'inline; filename="prospectus-{document_id}.pdf"'})
    return HTMLResponse(prospectus_mod.render_prospectus_html(payload))


# ─────────────────────────── share management ───────────────────────────────

class ShareBody(BaseModel):
    # Defaults enforce the safe posture: minted OFF, PII redacted.
    published: bool = False
    redact_offtaker_pii: bool = True


@router.post("/v1/array-owners/prospectus/{document_id}/share")
def mint_share_ep(document_id: int, body: ShareBody | None = None,
                  authorization: str | None = Header(default=None)) -> dict:
    """Mint a share link for a prospectus. Defaults to UNPUBLISHED + PII-redacted —
    nothing is exposed until the owner deliberately publishes."""
    tenant = _tenant(authorization)
    body = body or ShareBody()
    with SessionLocal() as db:
        doc = db.get(AgentDocument, document_id)
        if doc is None or doc.tenant_id != tenant.id or doc.doc_type != "prospectus":
            raise HTTPException(404, "Prospectus not found")
        payload = _load_payload(doc)
        sh = ProspectusShare(
            tenant_id=tenant.id,
            array_id=payload.get("array_id"),
            agent_document_id=doc.id,
            token=secrets.token_urlsafe(24),
            content_sha256=payload.get("content_sha256"),
            published=bool(body.published),
            redact_offtaker_pii=bool(body.redact_offtaker_pii),
        )
        db.add(sh)
        db.commit()
        db.refresh(sh)
        return {"ok": True, "share": _share_dict(sh)}


class PatchShareBody(BaseModel):
    published: bool | None = None
    redact_offtaker_pii: bool | None = None
    revoked: bool | None = None


@router.patch("/v1/array-owners/prospectus/share/{share_id}")
def patch_share_ep(share_id: int, body: PatchShareBody,
                   authorization: str | None = Header(default=None)) -> dict:
    """Publish / unpublish, toggle PII redaction, or revoke a share link."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        sh = db.get(ProspectusShare, share_id)
        if sh is None or sh.tenant_id != tenant.id:
            raise HTTPException(404, "Share not found")
        if body.published is not None:
            sh.published = bool(body.published)
        if body.redact_offtaker_pii is not None:
            sh.redact_offtaker_pii = bool(body.redact_offtaker_pii)
        if body.revoked is not None:
            sh.revoked_at = now() if body.revoked else None
        sh.updated_at = now()
        db.commit()
        db.refresh(sh)
        return {"ok": True, "share": _share_dict(sh)}


# ─────────────────────────── public share view ──────────────────────────────

@router.get("/v1/prospectus/{token}")
def public_prospectus_ep(token: str, request: Request,
                         format: str = Query(default="html")) -> Response:
    """PUBLIC read-only prospectus by token. 404s unless the link is published and
    not revoked. Applies PII redaction per the share's flag. Rate-limited; never
    leaks the tenant id."""
    ratelimit.enforce(request, "prospectus_public", max_hits=60, window_s=60.0,
                      message="Too many requests — please slow down.")
    with SessionLocal() as db:
        sh = db.execute(
            select(ProspectusShare).where(ProspectusShare.token == token)
        ).scalar_one_or_none()
        # A single 404 for missing / revoked / unpublished — don't distinguish.
        if sh is None or sh.revoked_at is not None or not sh.published:
            raise HTTPException(404, "Not found")
        doc = db.get(AgentDocument, sh.agent_document_id)
        if doc is None or doc.doc_type != "prospectus":
            raise HTTPException(404, "Not found")
        payload = _load_payload(doc)
        if sh.redact_offtaker_pii:
            payload = prospectus_mod.redact_prospectus(payload)

        # View receipt (best-effort; never block the render).
        try:
            sh.view_count = (sh.view_count or 0) + 1
            sh.last_viewed_at = now()
            db.commit()
        except Exception:
            db.rollback()

    if format == "pdf":
        pdf = prospectus_mod.render_prospectus_pdf(payload)
        return Response(content=pdf, media_type="application/pdf", headers={
            "Content-Disposition": f'inline; filename="prospectus-{token[:8]}.pdf"'})
    return HTMLResponse(prospectus_mod.render_prospectus_html(payload, public=True))
