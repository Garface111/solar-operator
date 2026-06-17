"""
Array Operator — automatic billing-report endpoints.

Mounted under /v1/array-operator/billing. Authed as the calling tenant via the
same session bearer used everywhere else (account.tenant_from_session).

  POST   /match                          multipart .xlsx → match preview (saves nothing)
  GET    /subscriptions                  list this tenant's report schedules
  POST   /subscriptions                  multipart: create/replace a schedule (+workbook)
  PATCH  /subscriptions/{id}             edit cadence / slider / formats / emails
  DELETE /subscriptions/{id}             soft-delete a schedule
  POST   /subscriptions/{id}/send-now    test/manual send (test=1 forces to_me)
  GET    /subscriptions/{id}/preview     stream invoice|summary as pdf|xlsx
  GET    /subscriptions/{id}/trends      multi-year billing trends (JSON, CONTRACT 1)
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from ..db import SessionLocal
from ..models import BillingReportSubscription, Client
from ..account import tenant_from_session, require_not_demo
from .matcher import match_billing_workbook
from .delivery import deliver_subscription, build_match, generate_files, next_send_at

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/array-operator/billing", tags=["array-operator-billing"])

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB — these workbooks are tens of KB
VALID_MODES = {"to_me", "to_client", "to_both"}
VALID_CADENCE = {"monthly", "quarterly"}
VALID_FORMATS = {"pdf", "xlsx"}


def _sub_dict(s: BillingReportSubscription) -> dict:
    return {
        "id": s.id,
        "customer_name": s.customer_name,
        "client_id": s.client_id,
        "billing_model": s.billing_model,
        "cadence": s.cadence,
        "annual_trueup": s.annual_trueup,
        "send_mode": s.send_mode,
        "client_email": s.client_email,
        "cc_emails": s.cc_emails,
        "operator_email": s.operator_email,
        "formats": s.formats or ["pdf"],
        "include_summary": s.include_summary,
        "enabled": s.enabled,
        "source_filename": s.source_filename,
        "last_sent_at": s.last_sent_at.isoformat() if s.last_sent_at else None,
        "next_send_at": s.next_send_at.isoformat() if s.next_send_at else None,
        "last_invoice_number": s.last_invoice_number,
        # A trimmed preview of the parsed workbook for the UI card.
        "preview": (s.parsed_map or {}).get("computed_invoice") if s.parsed_map else None,
    }


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (max 8 MB)")
    name = (file.filename or "").lower()
    if not (name.endswith(".xlsx") or name.endswith(".xls")):
        raise HTTPException(400, "Upload an .xlsx billing workbook")
    return data


# ─── match preview ──────────────────────────────────────────────────────────

@router.post("/match")
async def billing_match(file: UploadFile = File(...),
                        authorization: Optional[str] = Header(default=None)):
    """Parse an uploaded billing workbook and return what we recognized. Saves
    nothing — this powers the upload→preview→confirm step in the UI."""
    tenant_from_session(authorization)  # auth only
    data = await _read_upload(file)
    match = match_billing_workbook(data)
    return {"ok": True, "filename": file.filename, "match": match.to_dict()}


# ─── subscriptions CRUD ─────────────────────────────────────────────────────

@router.get("/subscriptions")
def list_subscriptions(authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        rows = db.execute(
            select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == t.id,
                   BillingReportSubscription.deleted_at.is_(None))
            .order_by(BillingReportSubscription.created_at.desc())
        ).scalars().all()
        return {"ok": True, "subscriptions": [_sub_dict(s) for s in rows]}


def _parse_formats(raw: Optional[str]) -> list[str]:
    if not raw:
        return ["pdf"]
    try:
        vals = json.loads(raw) if raw.strip().startswith("[") else raw.split(",")
    except json.JSONDecodeError:
        vals = raw.split(",")
    fmts = [v.strip().lower() for v in vals if v.strip().lower() in VALID_FORMATS]
    return fmts or ["pdf"]


@router.post("/subscriptions")
async def create_subscription(
    file: Optional[UploadFile] = File(default=None),
    customer_name: Optional[str] = Form(default=None),
    cadence: str = Form(default="monthly"),
    send_mode: str = Form(default="to_me"),
    client_email: Optional[str] = Form(default=None),
    cc_emails: Optional[str] = Form(default=None),
    operator_email: Optional[str] = Form(default=None),
    formats: Optional[str] = Form(default=None),
    include_summary: bool = Form(default=True),
    annual_trueup: bool = Form(default=False),
    enabled: bool = Form(default=True),
    authorization: Optional[str] = Header(default=None),
):
    """Create a report schedule from an uploaded workbook. The workbook bytes
    are stored as the per-cycle source of truth."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    if file is None:
        raise HTTPException(400, "Upload the billing workbook for this customer")
    data = await _read_upload(file)
    match = match_billing_workbook(data)
    if not match.matched:
        raise HTTPException(
            422, "Couldn't recognize this as a billing workbook — "
                 "check the file or fill the fields in manually.")

    if cadence not in VALID_CADENCE:
        raise HTTPException(400, "cadence must be monthly or quarterly")
    if send_mode not in VALID_MODES:
        raise HTTPException(400, "send_mode must be to_me, to_client, or to_both")

    name = customer_name or match.customer.get("name") or "Customer"
    client_email = client_email or match.customer.get("email")

    with SessionLocal() as db:
        # Link or create the Client "underneath" the operator.
        client = db.execute(
            select(Client).where(Client.tenant_id == t.id, Client.name == name,
                                 Client.deleted_at.is_(None))
        ).scalar_one_or_none()
        if client is None:
            client = Client(tenant_id=t.id, name=name, contact_email=client_email,
                            active=True)
            db.add(client)
            db.flush()

        sub = BillingReportSubscription(
            tenant_id=t.id,
            client_id=client.id,
            customer_name=name,
            source_workbook=data,
            source_filename=file.filename,
            parsed_map=match.to_dict(),
            billing_model=match.billing_model,
            cadence=cadence,
            annual_trueup=annual_trueup,
            send_mode=send_mode,
            client_email=client_email,
            cc_emails=cc_emails,
            operator_email=operator_email or t.contact_email,
            formats=_parse_formats(formats),
            include_summary=include_summary,
            enabled=enabled,
            next_send_at=next_send_at(cadence),
        )
        db.add(sub)
        db.commit()
        return {"ok": True, "subscription": _sub_dict(sub)}


class SubscriptionPatch(BaseModel):
    customer_name: Optional[str] = None
    cadence: Optional[str] = None
    send_mode: Optional[str] = None
    client_email: Optional[str] = None
    cc_emails: Optional[str] = None
    operator_email: Optional[str] = None
    formats: Optional[list[str]] = None
    include_summary: Optional[bool] = None
    annual_trueup: Optional[bool] = None
    enabled: Optional[bool] = None


@router.patch("/subscriptions/{sub_id}")
def patch_subscription(sub_id: int, body: SubscriptionPatch,
                       authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        if body.cadence is not None:
            if body.cadence not in VALID_CADENCE:
                raise HTTPException(400, "cadence must be monthly or quarterly")
            sub.cadence = body.cadence
            sub.next_send_at = next_send_at(body.cadence)
        if body.send_mode is not None:
            if body.send_mode not in VALID_MODES:
                raise HTTPException(400, "invalid send_mode")
            sub.send_mode = body.send_mode
        if body.customer_name is not None:
            sub.customer_name = body.customer_name
        if body.client_email is not None:
            sub.client_email = body.client_email
        if body.cc_emails is not None:
            sub.cc_emails = body.cc_emails
        if body.operator_email is not None:
            sub.operator_email = body.operator_email
        if body.formats is not None:
            fmts = [f for f in body.formats if f in VALID_FORMATS]
            sub.formats = fmts or ["pdf"]
        if body.include_summary is not None:
            sub.include_summary = body.include_summary
        if body.annual_trueup is not None:
            sub.annual_trueup = body.annual_trueup
        if body.enabled is not None:
            sub.enabled = body.enabled
        db.commit()
        return {"ok": True, "subscription": _sub_dict(sub)}


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        sub.deleted_at = datetime.utcnow()
        sub.enabled = False
        db.commit()
        return {"ok": True}


@router.post("/subscriptions/{sub_id}/send-now")
def send_now(sub_id: int, test: bool = Query(default=True),
             authorization: Optional[str] = Header(default=None)):
    """Manually deliver this subscription now. Defaults to a TEST send (to the
    operator) so the slider can be exercised safely before going live."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        result = deliver_subscription(db, sub, t, triggered_by="manual", is_test=test)
        status = 200 if result.get("ok") else 422
        if not result.get("ok"):
            raise HTTPException(status, result.get("error", "send failed"))
        return {"ok": True, "result": result}


@router.get("/subscriptions/{sub_id}/preview")
def preview(sub_id: int, kind: str = Query(default="invoice"),
            fmt: str = Query(default="pdf"),
            authorization: Optional[str] = Header(default=None)):
    """Render the current invoice or summary on demand for download."""
    t = tenant_from_session(authorization)
    if fmt not in VALID_FORMATS:
        raise HTTPException(400, "fmt must be pdf or xlsx")
    if kind not in ("invoice", "summary"):
        raise HTTPException(400, "kind must be invoice or summary")
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        match = build_match(sub)
        if not match.matched:
            raise HTTPException(422, "stored workbook is no longer recognizable")
        import tempfile, pathlib
        with tempfile.TemporaryDirectory(prefix="ao-prev-") as tmp:
            tmpd = pathlib.Path(tmp)
            if kind == "invoice":
                from . import invoice as inv
                p = (inv.render_invoice_pdf(match, tmpd / "p.pdf") if fmt == "pdf"
                     else inv.render_invoice_xlsx(match, tmpd / "p.xlsx"))
            else:
                from . import summary as summ
                p = (summ.render_summary_pdf(match, tmpd / "p.pdf") if fmt == "pdf"
                     else summ.render_summary_xlsx(match, tmpd / "p.xlsx"))
            blob = p.read_bytes()
    media = ("application/pdf" if fmt == "pdf"
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    fname = f"{sub.customer_name.replace(' ', '_')}_{kind}.{fmt}"
    return StreamingResponse(io.BytesIO(blob), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/subscriptions/{sub_id}/trends")
def subscription_trends(sub_id: int,
                        authorization: Optional[str] = Header(default=None)):
    """Multi-year billing trends for a subscription (CONTRACT 1). Tenant-scoped:
    404 if the sub isn't owned. Thin/unreadable workbook → 200 with empty
    collections + null scalars; never 500 on real data."""
    t = tenant_from_session(authorization)
    from . import summary as summ
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        try:
            match = build_match(sub)
            trends = summ.build_trends(match)
        except Exception:  # noqa: BLE001 — thin/missing/corrupt workbook
            logger.warning("trends build failed for sub %s", sub_id, exc_info=True)
            trends = summ._empty_trends(sub.customer_name)
        if not trends.get("customer_name"):
            trends["customer_name"] = sub.customer_name
        return trends


def _get_owned(db, tenant_id: str, sub_id: int) -> BillingReportSubscription:
    sub = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.id == sub_id,
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(404, "Subscription not found")
    return sub
