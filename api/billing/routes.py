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

THREE SPREADSHEET SYSTEMS — keep them straight (they're easy to conflate):
  1. api/writers/gmcs_writer.py        WRITES the NEPOOL-GIS *GMCS filing*
                                       workbook — the NEPOOL Operator product's
                                       quarterly output. NOT a customer invoice.
  2. api/billing/matcher.py            READS an uploaded *customer billing*
                                       workbook (HCT family) → BillingMatch.
                                       Powers POST /match and onboarding.
  3. api/billing/invoice_writer.py     WRITES the customer's invoice back into
                                       THEIR OWN uploaded format (loads the
                                       stored original, populates the period,
                                       preserves styling/formulas/Template).
                                       Served by GET /preview?kind=invoice&fmt=xlsx
                                       for workbook subs.
Manual (typed-in) customers have no uploaded workbook, so their invoice is the
standard generated one (api/billing/invoice.py).
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
from ..models import BillingReportSubscription, Client, ReportDraft
from ..account import tenant_from_session, require_not_demo
from .matcher import match_billing_workbook
from .delivery import deliver_subscription, build_match, generate_files, next_send_at

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/array-operator/billing", tags=["array-operator-billing"])

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB — these workbooks are tens of KB
MAX_PDF_BYTES = 12 * 1024 * 1024    # GMP invoice PDFs
VALID_MODES = {"to_me", "to_client", "to_both"}
VALID_CADENCE = {"monthly", "quarterly"}
VALID_FORMATS = {"pdf", "xlsx"}
VALID_DELIVERY = {"approval", "auto"}


def _sub_dict(s: BillingReportSubscription) -> dict:
    return {
        "id": s.id,
        "customer_name": s.customer_name,
        "client_id": s.client_id,
        "array_id": getattr(s, "array_id", None),
        "allocation_pct": getattr(s, "allocation_pct", None),
        "billing_model": s.billing_model,
        "rate_per_kwh": getattr(s, "rate_per_kwh", None),
        "discount_pct": getattr(s, "discount_pct", None),
        "net_rate_per_kwh": getattr(s, "net_rate_per_kwh", None),
        "auto_attach_gmp": getattr(s, "auto_attach_gmp", False),
        "cadence": s.cadence,
        "annual_trueup": s.annual_trueup,
        "delivery_mode": getattr(s, "delivery_mode", "approval") or "approval",
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


# Sanity ceiling for a $/kWh rate. VT solar value is ~$0.18/kWh; anything above
# this is almost certainly a units mistake (e.g. cents typed as dollars).
MAX_RATE_PER_KWH = 5.0


def _validate_rate(rate):
    """Coerce/validate an optional $/kWh rate. None passes through (no rate set).
    Rejects negatives and absurd values so a fat-fingered rate can't silently
    produce a wild invoice."""
    if rate is None:
        return None
    try:
        r = float(rate)
    except (TypeError, ValueError):
        raise HTTPException(400, "rate_per_kwh must be a number ($/kWh)")
    if r < 0 or r > MAX_RATE_PER_KWH:
        raise HTTPException(400, f"rate_per_kwh must be between 0 and {MAX_RATE_PER_KWH} $/kWh")
    return r


def _validate_discount(pct):
    """Validate an optional discount fraction in [0, 1). None passes through.
    Accepts a fraction (0.10 = 10% off). Rejects ≥1 (would zero/inverse the bill)."""
    if pct is None:
        return None
    try:
        d = float(pct)
    except (TypeError, ValueError):
        raise HTTPException(400, "discount_pct must be a number (fraction, e.g. 0.10 for 10%)")
    if not (0 <= d < 1):
        raise HTTPException(400, "discount_pct must be a fraction in [0, 1) — e.g. 0.10 for 10% off")
    return d


@router.post("/subscriptions")
async def create_subscription(
    file: Optional[UploadFile] = File(default=None),
    customer_name: Optional[str] = Form(default=None),
    array_id: Optional[int] = Form(default=None),
    allocation_pct: Optional[float] = Form(default=None),
    rate_per_kwh: Optional[float] = Form(default=None),
    discount_pct: Optional[float] = Form(default=None),
    net_rate_per_kwh: Optional[float] = Form(default=None),
    cadence: str = Form(default="monthly"),
    send_mode: str = Form(default="to_me"),
    delivery_mode: str = Form(default="approval"),
    client_email: Optional[str] = Form(default=None),
    cc_emails: Optional[str] = Form(default=None),
    operator_email: Optional[str] = Form(default=None),
    formats: Optional[str] = Form(default=None),
    include_summary: bool = Form(default=True),
    annual_trueup: bool = Form(default=False),
    enabled: bool = Form(default=True),
    authorization: Optional[str] = Header(default=None),
):
    """Create a report schedule for one customer. Two paths:

      * UPLOAD — a billing .xlsx is attached; the matcher recognizes it and the
        stored workbook bytes are the per-cycle source of truth.
      * MANUAL — no file; the operator typed the customer in (name + array_id +
        allocation_pct). No workbook is stored; allocation_pct × the array's
        period generation drives delivery/draft. (Paul Bozuwa's demo path.)
    """
    t = tenant_from_session(authorization)
    require_not_demo(t)

    if cadence not in VALID_CADENCE:
        raise HTTPException(400, "cadence must be monthly or quarterly")
    if send_mode not in VALID_MODES:
        raise HTTPException(400, "send_mode must be to_me, to_client, or to_both")
    if delivery_mode not in VALID_DELIVERY:
        raise HTTPException(400, "delivery_mode must be approval or auto")

    if file is None:
        return await _create_manual_subscription(
            t, customer_name=customer_name, array_id=array_id,
            allocation_pct=allocation_pct, rate_per_kwh=rate_per_kwh,
            discount_pct=discount_pct, net_rate_per_kwh=net_rate_per_kwh,
            cadence=cadence, send_mode=send_mode,
            delivery_mode=delivery_mode, client_email=client_email,
            cc_emails=cc_emails, operator_email=operator_email, formats=formats,
            include_summary=include_summary, annual_trueup=annual_trueup,
            enabled=enabled)

    data = await _read_upload(file)
    match = match_billing_workbook(data)
    if not match.matched:
        raise HTTPException(
            422, "Couldn't recognize this as a billing workbook — "
                 "check the file or fill the fields in manually.")

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
            rate_per_kwh=rate_per_kwh,
            discount_pct=_validate_discount(discount_pct),
            net_rate_per_kwh=_validate_rate(net_rate_per_kwh),
            cadence=cadence,
            annual_trueup=annual_trueup,
            delivery_mode=delivery_mode,
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


async def _create_manual_subscription(
    t, *, customer_name, array_id, allocation_pct, rate_per_kwh, discount_pct,
    net_rate_per_kwh, cadence,
    send_mode, delivery_mode, client_email, cc_emails, operator_email, formats,
    include_summary, annual_trueup, enabled,
):
    """Create a workbook-less subscription from typed fields. The customer's
    invoice is computed each cycle as allocation_pct × the array's period
    generation (see delivery.build_manual_match)."""
    from ..models import Array

    name = (customer_name or "").strip()
    if not name:
        raise HTTPException(400, "customer_name is required for a manual customer")
    if array_id is None:
        raise HTTPException(400, "array_id is required for a manual customer")
    if allocation_pct is None:
        raise HTTPException(400, "allocation_pct is required for a manual customer")
    try:
        pct = float(allocation_pct)
    except (TypeError, ValueError):
        raise HTTPException(400, "allocation_pct must be a number between 0 and 1")
    if not (0.0 < pct <= 1.0):
        raise HTTPException(400, "allocation_pct must be a fraction in (0, 1] "
                                 "(e.g. 0.25 for 25%)")
    rate_val = _validate_rate(rate_per_kwh)
    disc_val = _validate_discount(discount_pct)
    net_val = _validate_rate(net_rate_per_kwh)

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != t.id or arr.deleted_at is not None:
            raise HTTPException(404, "Array not found")

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
            array_id=array_id,
            allocation_pct=pct,
            rate_per_kwh=rate_val,
            discount_pct=disc_val,
            net_rate_per_kwh=net_val,
            source_workbook=None,
            source_filename=None,
            parsed_map=None,
            billing_model="percent_of_array",
            cadence=cadence,
            annual_trueup=annual_trueup,
            delivery_mode=delivery_mode,
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
    delivery_mode: Optional[str] = None
    send_mode: Optional[str] = None
    client_email: Optional[str] = None
    cc_emails: Optional[str] = None
    operator_email: Optional[str] = None
    formats: Optional[list[str]] = None
    include_summary: Optional[bool] = None
    annual_trueup: Optional[bool] = None
    enabled: Optional[bool] = None
    # Redesigned Reports tab: inline-edit a manual customer's allocation % and
    # (re)assign the array. allocation_pct is a fraction in (0, 1]. array_id ties
    # the manual customer to the array whose generation is split.
    allocation_pct: Optional[float] = None
    array_id: Optional[int] = None
    # Per-customer billing rate override ($/kWh). Send a number to set it, or
    # explicit null to CLEAR it (fall back to the operator's global rate). We
    # use model_fields_set in the handler to tell "null to clear" from "omitted".
    rate_per_kwh: Optional[float] = None
    # Discount-model overrides. Send a number to set, explicit null to clear
    # (falls back to the operator global, then the 10%/VT default).
    discount_pct: Optional[float] = None
    net_rate_per_kwh: Optional[float] = None
    # Per-customer 'auto-attach the captured GMP bill PDF' toggle.
    auto_attach_gmp: Optional[bool] = None


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
        if body.delivery_mode is not None:
            if body.delivery_mode not in VALID_DELIVERY:
                raise HTTPException(400, "delivery_mode must be approval or auto")
            sub.delivery_mode = body.delivery_mode
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
        if body.allocation_pct is not None:
            try:
                pct = float(body.allocation_pct)
            except (TypeError, ValueError):
                raise HTTPException(400, "allocation_pct must be a number in (0, 1]")
            if not (0.0 < pct <= 1.0):
                raise HTTPException(400, "allocation_pct must be a fraction in (0, 1] "
                                         "(e.g. 0.95 for 95%)")
            sub.allocation_pct = pct
        if body.array_id is not None:
            from ..models import Array
            arr = db.get(Array, body.array_id)
            if arr is None or arr.tenant_id != t.id or arr.deleted_at is not None:
                raise HTTPException(404, "Array not found")
            sub.array_id = body.array_id
        if "rate_per_kwh" in body.model_fields_set:
            # null clears the override; a number sets it (validated).
            sub.rate_per_kwh = _validate_rate(body.rate_per_kwh)
        if "discount_pct" in body.model_fields_set:
            sub.discount_pct = _validate_discount(body.discount_pct)
        if "net_rate_per_kwh" in body.model_fields_set:
            sub.net_rate_per_kwh = _validate_rate(body.net_rate_per_kwh)
        if body.auto_attach_gmp is not None:
            sub.auto_attach_gmp = body.auto_attach_gmp
        db.commit()
        return {"ok": True, "subscription": _sub_dict(sub)}


class GlobalRatePatch(BaseModel):
    default_billing_rate_per_kwh: Optional[float] = None
    # Discount model: the operator's global default net rate + discount.
    default_net_rate_per_kwh: Optional[float] = None
    default_discount_pct: Optional[float] = None


@router.get("/global-rate")
def get_global_rate(authorization: Optional[str] = Header(default=None)):
    """The operator's global billing defaults. The discount model:
    invoice = kWh × net_rate × (1 − discount). When unset, net rate falls back
    to the VT default and discount to the built-in 10%."""
    from .delivery import MANUAL_TARIFF, DEFAULT_DISCOUNT
    t = tenant_from_session(authorization)
    net = getattr(t, "default_net_rate_per_kwh", None)
    disc = getattr(t, "default_discount_pct", None)
    return {
        "ok": True,
        # legacy flat rate (kept for back-compat)
        "default_billing_rate_per_kwh": getattr(t, "default_billing_rate_per_kwh", None),
        # discount model + the effective defaults actually applied
        "default_net_rate_per_kwh": net,
        "default_discount_pct": disc,
        "effective_net_rate_per_kwh": net if net is not None else MANUAL_TARIFF,
        "effective_discount_pct": disc if disc is not None else DEFAULT_DISCOUNT,
    }


@router.put("/global-rate")
def set_global_rate(body: GlobalRatePatch,
                    authorization: Optional[str] = Header(default=None)):
    """Set (or clear, via null) the operator's global billing defaults — net
    rate and/or discount %. Every customer without a per-customer override uses
    these. Only the fields present in the request body are changed."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        from ..models import Tenant
        tt = db.get(Tenant, t.id)
        if tt is None:
            raise HTTPException(404, "Account not found")
        if "default_billing_rate_per_kwh" in body.model_fields_set:
            tt.default_billing_rate_per_kwh = _validate_rate(body.default_billing_rate_per_kwh)
        if "default_net_rate_per_kwh" in body.model_fields_set:
            tt.default_net_rate_per_kwh = _validate_rate(body.default_net_rate_per_kwh)
        if "default_discount_pct" in body.model_fields_set:
            tt.default_discount_pct = _validate_discount(body.default_discount_pct)
        db.commit()
        return {"ok": True,
                "default_net_rate_per_kwh": tt.default_net_rate_per_kwh,
                "default_discount_pct": tt.default_discount_pct,
                "default_billing_rate_per_kwh": tt.default_billing_rate_per_kwh}


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
                # Invoice in the customer's OWN uploaded format: for workbook
                # subs we load their stored .xlsx and populate it (preserving
                # all styling/formulas/the Template sheet). Manual customers (no
                # source_workbook) fall back to the standard generated invoice.
                if fmt == "xlsx" and getattr(sub, "source_workbook", None):
                    from .invoice_writer import (
                        populate_invoice_workbook, InvoiceWriterError)
                    try:
                        blob = populate_invoice_workbook(sub)
                    except InvoiceWriterError as e:
                        raise HTTPException(422, str(e))
                    media = ("application/vnd.openxmlformats-officedocument"
                             ".spreadsheetml.sheet")
                    fname = f"{sub.customer_name.replace(' ', '_')}_invoice.xlsx"
                    return StreamingResponse(
                        io.BytesIO(blob), media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
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


@router.get("/subscriptions/{sub_id}/preview-math")
def preview_math(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """Compute (without persisting a draft) the auditable billing math for a
    subscription's latest period: the array's period generation, the customer's
    allocation %, the resulting customer-share kWh, the $/kWh rate, and the
    dollar amount. Powers the run-table rows so every customer shows real
    numbers eagerly — no draft required.

    Never fabricates: when the array has no generation for the period yet,
    `has_data` is false and the kWh/amount fields are null so the UI can show a
    muted 'No generation data yet' instead of a bogus number.
    """
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
    try:
        match = build_match(sub)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"workbook unreadable: {e}")
    if not match.matched or not match.latest_period:
        return {
            "subscription_id": sub.id,
            "source": match.source,
            "has_data": False,
            "allocation_pct": match.allocation_pct,
            "array_total_kwh": None,
            "customer_kwh": None,
            "amount_usd": None,
            "rate": None,
            "period_start": None,
            "period_end": None,
        }
    ci = match.computed_invoice or {}
    array_total = ci.get("project_total_kwh") or ci.get("array_kwh")
    cust_kwh = ci.get("kwh")
    pct = match.allocation_pct
    if array_total is None and cust_kwh is not None and pct:
        array_total = round(cust_kwh / pct, 1)
    amount = ci.get("amount_owed")
    rate = None
    if cust_kwh and amount is not None and cust_kwh > 0:
        rate = amount / cust_kwh
    # "Has data" means the array actually produced generation this period — a
    # zero array total means we have nothing real to show yet.
    has_data = bool(array_total)
    return {
        "subscription_id": sub.id,
        "source": match.source,
        "has_data": has_data,
        "allocation_pct": pct,
        "array_total_kwh": array_total if has_data else None,
        "customer_kwh": cust_kwh if has_data else None,
        "amount_usd": amount if has_data else None,
        "rate": rate if has_data else None,
        "rate_source": ci.get("rate_source"),
        # Discount model: the savings story the customer sees.
        "net_rate_per_kwh": ci.get("net_rate_per_kwh"),
        "discount_pct": ci.get("discount_pct"),
        "effective_rate_per_kwh": ci.get("effective_rate_per_kwh"),
        "net_rate_source": ci.get("net_rate_source"),
        "discount_source": ci.get("discount_source"),
        "solar_savings_usd": (ci.get("solar_savings") if has_data else None),
        "kwh_source": ci.get("kwh_source"),
        "period_start": ci.get("period_start"),
        "period_end": ci.get("period_end"),
    }


@router.get("/subscriptions/{sub_id}/trends")
def subscription_trends(sub_id: int,
                        authorization: Optional[str] = Header(default=None)):
    """Multi-year billing trends for a subscription (CONTRACT 1). Tenant-scoped:
    404 if the sub isn't owned. Thin/unreadable workbook → 200 with empty
    collections + null scalars; never 500 on real data."""
    t = tenant_from_session(authorization)
    from . import summary as summ
    with SessionLocal() as db:
        sub, client = _resolve_trends_target(db, t.id, sub_id)
        if sub is None:
            # A valid CLIENT exists but has no billing workbook yet → honest
            # empty state, not a 404. (The reports UI is client-centric and
            # links trends by client id; a client without an uploaded workbook
            # simply has no history to chart.)
            return summ._empty_trends(client.name if client else None)
        try:
            match = build_match(sub)
            trends = summ.build_trends(match)
        except Exception:  # noqa: BLE001 — thin/missing/corrupt workbook
            logger.warning("trends build failed for sub %s", sub.id, exc_info=True)
            trends = summ._empty_trends(sub.customer_name)
        if not trends.get("customer_name"):
            trends["customer_name"] = sub.customer_name
        return trends


def _resolve_trends_target(db, tenant_id: str, ident: int):
    """Resolve the trends route's {id} to (subscription, client).

    The reports UI is client-centric and links by client id, but trends are
    derived from a BillingReportSubscription's stored workbook. Accept EITHER:
      1. a BillingReportSubscription.id (direct), or
      2. a Client.id → that client's newest non-deleted subscription.
    Returns (sub, client). Raises 404 only when `ident` matches neither a sub
    nor a client owned by this tenant. (sub may be None when a real client has
    no subscription yet — caller renders the empty trends state.)
    """
    sub = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.id == ident,
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
    ).scalar_one_or_none()
    if sub is not None:
        return sub, None

    client = db.execute(
        select(Client).where(
            Client.id == ident,
            Client.tenant_id == tenant_id,
            Client.deleted_at.is_(None))
    ).scalar_one_or_none()
    if client is None:
        raise HTTPException(404, "Subscription not found")

    csub = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.client_id == client.id,
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
        .order_by(BillingReportSubscription.id.desc())
    ).scalars().first()
    return csub, client


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


# ─── DRAFT → APPROVE → SEND inbox (Paul Bozuwa's core workflow) ───────────────
# "I get an email drafted... the customer invoice PDF, the GMP invoice PDF...
#  I go over it and approve it or modify it and then send." Nothing reaches a
#  real customer until the operator clicks Approve & send.

def _draft_dict(d: ReportDraft, sub=None, gmp_auto_status=None) -> dict:
    return {
        "id": d.id,
        "subscription_id": d.subscription_id,
        "customer_name": d.customer_name,
        "status": d.status,
        "period_label": d.period_label,
        "array_total_kwh": d.array_total_kwh,
        "allocation_pct": d.allocation_pct,
        "customer_kwh": d.customer_kwh,
        "amount_usd": d.amount_usd,
        "invoice_number": d.invoice_number,
        "has_gmp_pdf": d.gmp_invoice_pdf is not None,
        "gmp_filename": d.gmp_filename,
        "note": d.note,
        # Auto-attach state (resolved from the subscription when provided):
        #   auto_attach_gmp   — is the toggle on for this customer
        #   gmp_auto_status   — "ready" (a captured bill PDF will attach) |
        #                       "pending" (toggle on, GMP account exists, no PDF
        #                       captured yet) | "no_gmp" (array has no GMP account)
        #                       | None (toggle off / not resolvable)
        "auto_attach_gmp": (getattr(sub, "auto_attach_gmp", False) if sub else None),
        "gmp_auto_status": gmp_auto_status,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "sent_at": d.sent_at.isoformat() if d.sent_at else None,
    }


def _resolve_gmp_auto_status(db, sub) -> Optional[str]:
    """Honest auto-attach status for the draft card (never implies a PDF exists
    when it doesn't)."""
    if sub is None or not getattr(sub, "auto_attach_gmp", False):
        return None
    array_id = getattr(sub, "array_id", None)
    if array_id is None:
        return "no_gmp"
    try:
        from ..reports import gmp_bill_pdf_read as gbp
        if not gbp.has_capturable_gmp_account(array_id, db=db):
            return "no_gmp"
        found = gbp.get_bill_pdf_for_period(array_id, db=db)
        return "ready" if (found and found.get("bytes")) else "pending"
    except Exception:  # noqa: BLE001
        return "pending"


def _get_owned_draft(db, tenant_id: str, draft_id: int) -> ReportDraft:
    d = db.get(ReportDraft, draft_id)
    if d is None or d.tenant_id != tenant_id:
        raise HTTPException(404, "Draft not found")
    return d


@router.get("/drafts")
def list_drafts(status: str = Query(default="pending"),
                authorization: Optional[str] = Header(default=None)):
    """The approval inbox: drafts awaiting the operator's review. status=pending
    (default) | sent | dismissed | all."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        q = select(ReportDraft).where(ReportDraft.tenant_id == t.id)
        if status != "all":
            q = q.where(ReportDraft.status == status)
        rows = db.execute(q.order_by(ReportDraft.created_at.desc())).scalars().all()
        out = []
        for d in rows:
            sub = db.get(BillingReportSubscription, d.subscription_id) if d.subscription_id else None
            out.append(_draft_dict(d, sub=sub,
                                   gmp_auto_status=_resolve_gmp_auto_status(db, sub)))
        return {"drafts": out}


@router.post("/subscriptions/{sub_id}/draft")
def generate_draft(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """Build a pending draft for this subscription's latest billing period, from
    its stored workbook. This is what the (operator-built) GMP-detection backend
    will call when a new GMP invoice lands; the operator can also trigger it
    manually. Reuses an existing pending draft for the same period (idempotent)."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        try:
            match = build_match(sub)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"workbook unreadable: {e}")
        if not match.matched or not match.latest_period:
            raise HTTPException(422, "no current billing period in the stored workbook")

        ci = match.computed_invoice or {}
        inv_no = ci.get("invoice_number")
        period_label = None
        if ci.get("period_start") or ci.get("period_end"):
            period_label = f"{ci.get('period_start') or '—'} → {ci.get('period_end') or '—'}"

        # Idempotent: reuse a pending draft for the same period/invoice number.
        existing = db.execute(
            select(ReportDraft).where(
                ReportDraft.subscription_id == sub.id,
                ReportDraft.status == "pending",
                ReportDraft.invoice_number == inv_no,
            )
        ).scalars().first()
        d = existing or ReportDraft(
            tenant_id=t.id, subscription_id=sub.id,
            customer_name=sub.customer_name, status="pending")
        d.period_label = period_label
        # The period's TOTAL array generation is Paul's anchor number ("what GMP
        # reports"). The computed invoice carries the customer's SHARE (kwh) and
        # the allocation %, so the array total = share / pct. Fall back to any
        # explicit array field, else null.
        cust_kwh = ci.get("kwh")
        pct = match.allocation_pct
        array_total = ci.get("project_total_kwh") or ci.get("array_kwh")
        if array_total is None and cust_kwh is not None and pct:
            array_total = round(cust_kwh / pct, 1)
        d.array_total_kwh = array_total
        d.allocation_pct = pct
        d.customer_kwh = cust_kwh
        d.amount_usd = ci.get("amount_owed")
        d.invoice_number = inv_no
        if existing is None:
            db.add(d)
        db.commit()
        return {"ok": True, "draft": _draft_dict(d)}


@router.post("/drafts/{draft_id}/gmp-invoice")
async def attach_gmp_invoice(draft_id: int, file: UploadFile = File(...),
                             authorization: Optional[str] = Header(default=None)):
    """Attach the period's GMP utility-invoice PDF to a draft. Paul sends this
    alongside the customer invoice 'to prove we're not just making this up.'"""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF too large (max 12 MB)")
    head = data[:5]
    if head[:4] != b"%PDF":
        raise HTTPException(400, "that doesn't look like a PDF")
    with SessionLocal() as db:
        d = _get_owned_draft(db, t.id, draft_id)
        if d.status != "pending":
            raise HTTPException(409, "draft already resolved")
        d.gmp_invoice_pdf = data
        d.gmp_filename = (file.filename or "GMP_invoice.pdf")[:300]
        db.commit()
        return {"ok": True, "draft": _draft_dict(d)}


class DraftPatch(BaseModel):
    note: Optional[str] = None


@router.patch("/drafts/{draft_id}")
def patch_draft(draft_id: int, body: DraftPatch,
                authorization: Optional[str] = Header(default=None)):
    """Modify a draft before sending (Paul: 'approve it or MODIFY it and send')."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        d = _get_owned_draft(db, t.id, draft_id)
        if d.status != "pending":
            raise HTTPException(409, "draft already resolved")
        if body.note is not None:
            d.note = body.note[:2000]
        db.commit()
        return {"ok": True, "draft": _draft_dict(d)}


@router.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: int, authorization: Optional[str] = Header(default=None)):
    """Approve & send: deliver the drafted report to the customer (per the
    subscription's recipient slider), attaching the GMP invoice PDF the operator
    put on the draft. This is the single human gate in front of delivery."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        d = _get_owned_draft(db, t.id, draft_id)
        if d.status != "pending":
            raise HTTPException(409, "draft already resolved")
        sub = _get_owned(db, t.id, d.subscription_id)
        # Move the draft's GMP PDF onto the subscription so delivery's existing
        # attach hook (generate_files) rides it onto the email, then send live.
        if d.gmp_invoice_pdf is not None:
            sub.gmp_invoice_pdf = d.gmp_invoice_pdf
        db.commit()
        result = deliver_subscription(db, sub, t, triggered_by="approval",
                                      is_test=False, note=d.note)
        if not result.get("ok"):
            raise HTTPException(422, result.get("error", "send failed"))
        d.status = "sent"
        d.sent_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "draft": _draft_dict(d), "result": result}


@router.post("/drafts/{draft_id}/dismiss")
def dismiss_draft(draft_id: int, authorization: Optional[str] = Header(default=None)):
    """Discard a draft without sending."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        d = _get_owned_draft(db, t.id, draft_id)
        if d.status == "sent":
            raise HTTPException(409, "draft already sent")
        d.status = "dismissed"
        d.dismissed_at = datetime.utcnow()
        db.commit()
        return {"ok": True}
