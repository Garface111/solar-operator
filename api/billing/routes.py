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

import csv
import io
import json
import logging
import re as _re
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload

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

# File-type magic bytes (checked before extension so mis-named files are caught)
_MAGIC_PDF  = b"%PDF"
_MAGIC_XLSX = b"PK\x03\x04"   # ZIP / OpenXML (.xlsx, .xlsm, .docx, …)
_MAGIC_XLS  = b"\xd0\xcf\x11\xe0"  # OLE2 compound doc (.xls, .doc, …)


def _resolved_pricing_fields(s) -> dict:
    """The pricing actually applied to this customer (auto-resolved net rate +
    discount + provenance), for the UI card. Best-effort; never raises."""
    try:
        from .delivery import resolve_discount_pricing
        p = resolve_discount_pricing(s)
        return {
            "resolved_net_rate": round(p["net_rate"], 5),
            "resolved_discount_pct": round(p["discount_pct"], 5),
            "resolved_effective_rate": p["effective_rate"],
            "resolved_net_source": p["net_source"],
            "resolved_net_note": p.get("net_rate_note"),
        }
    except Exception:  # noqa: BLE001
        return {}


def _sub_dict(s: BillingReportSubscription) -> dict:
    return {
        "id": s.id,
        "customer_name": s.customer_name,
        "client_id": s.client_id,
        "array_id": getattr(s, "array_id", None),
        "utility_account_id": getattr(s, "utility_account_id", None),
        "allocation_pct": getattr(s, "allocation_pct", None),
        "array_allocations": getattr(s, "array_allocations", None),
        "billing_model": s.billing_model,
        "rate_per_kwh": getattr(s, "rate_per_kwh", None),
        "discount_pct": getattr(s, "discount_pct", None),
        "net_rate_per_kwh": getattr(s, "net_rate_per_kwh", None),
        # The pricing actually applied (auto-resolved net rate + discount, with
        # provenance) so the card can SHOW the auto rate instead of a blank box.
        **_resolved_pricing_fields(s),
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
        "invoice_number_start": getattr(s, "invoice_number_start", None),
        "invoice_number_next": getattr(s, "invoice_number_next", None),
        "budget_amount_usd": getattr(s, "budget_amount_usd", None),
        # A trimmed preview of the parsed workbook for the UI card.
        "preview": (s.parsed_map or {}).get("computed_invoice") if s.parsed_map else None,
    }


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (max 8 MB)")
    # Detect actual file type by magic bytes before trusting the extension.
    if data[:4] == _MAGIC_PDF:
        raise HTTPException(
            400, "That's a PDF — please upload the Excel (.xlsx) workbook "
                 "(the HCT Sun spreadsheet, not a printed copy)")
    name = (file.filename or "").lower()
    is_excel = data[:4] in (_MAGIC_XLSX[:4], _MAGIC_XLS)
    if not (name.endswith(".xlsx") or name.endswith(".xls") or is_excel):
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


# ─── GMP utility-bill selector (offtaker binding) ───────────────────────────

@router.get("/utility-accounts")
def list_utility_accounts(authorization: Optional[str] = Header(default=None)):
    """List this tenant's GMP and VEC/SmartHub utility accounts so the add-offtaker
    UI can let the operator SELECT the utility bill that connects to an offtaker.

    GMP offtaker invoices are computed EXCLUSIVELY from the chosen account's utility
    PAPER BILLS (Bill.kwh_generated per billing period) — never vendor/inverter
    data. VEC/SmartHub accounts carry no EXCESS+credit breakdown, so a VEC offtaker
    is priced as allocation_pct × the array's MEASURED generation × an operator-
    entered net rate (delivery enforces that rate; see build_manual_match).

    This endpoint surfaces each account with the array it feeds and a summary of the
    bills we hold (count + latest period + that period's kWh). The bill summary is
    GMP-shaped (Bill.kwh_generated, always null for SmartHub) so a VEC account shows
    empty bill stats — that's acceptable; the account still appears so it can be
    bound. The `provider` field lets the frontend label GMP vs VEC correctly.
    """
    from ..models import UtilityAccount, Bill, Array
    from ..adapters.smarthub import ALL_SMARTHUB_PROVIDERS

    t = tenant_from_session(authorization)
    out = []
    with SessionLocal() as db:
        accts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == t.id,
                or_(UtilityAccount.provider == "gmp",
                    UtilityAccount.provider.in_(sorted(ALL_SMARTHUB_PROVIDERS))),
                UtilityAccount.deleted_at.is_(None),
            ).order_by(UtilityAccount.nickname, UtilityAccount.account_number)
        ).scalars().all()
        for a in accts:
            # Latest bill that actually carries generation for this account.
            latest = db.execute(
                select(Bill)
                .where(Bill.account_id == a.id,
                       Bill.kwh_generated.isnot(None),
                       Bill.period_end.isnot(None))
                .order_by(Bill.period_end.desc())
            ).scalars().first()
            bill_count = db.execute(
                select(func.count(Bill.id)).where(
                    Bill.account_id == a.id,
                    Bill.kwh_generated.isnot(None))
            ).scalar() or 0
            arr = db.get(Array, a.array_id) if a.array_id else None
            out.append({
                "utility_account_id": a.id,
                "account_number": a.account_number,
                "nickname": a.nickname,
                "provider": a.provider,
                "array_id": a.array_id,
                "array_name": arr.name if arr else None,
                "bill_count": int(bill_count),
                "has_bill": latest is not None,
                "latest_period_end": latest.period_end.date().isoformat()
                    if latest and latest.period_end else None,
                "latest_period_label": latest.period_end.strftime("%Y-%m")
                    if latest and latest.period_end else None,
                "latest_kwh_generated": (int(latest.kwh_generated)
                    if latest and latest.kwh_generated is not None else None),
            })
    return {"ok": True, "utility_accounts": out}


@router.post("/utility-accounts/{utility_account_id}/vec-bill")
async def upload_vec_bill(utility_account_id: int,
                          file: UploadFile = File(...),
                          authorization: Optional[str] = Header(default=None)):
    """Upload a VEC/SmartHub bill PDF for a bound utility account → parse it into a
    settled net-meter Bill (kwh_sent_to_grid + solar_credit_usd + the bill's own
    credit rate). Once a parsed VEC bill exists, the offtaker invoice auto-prices
    from the bill exactly like a GMP offtaker — excess kWh × the bill's own rate, no
    operator-entered rate needed (see delivery.build_manual_match).

    Tenant-scoped: the account must belong to the caller's tenant. Caps at 12 MB and
    rejects anything that isn't a PDF (by %PDF magic). On a parse/guard failure
    returns HTTP 422 with the reason.
    """
    from ..adapters.vec_bill import ingest_vec_bill_pdf

    t = tenant_from_session(authorization)
    require_not_demo(t)
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "The uploaded file is empty")
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(413, "File too large (max 12 MB).")
    if raw[:4] != _MAGIC_PDF:
        raise HTTPException(415, "Upload a PDF bill.")
    with SessionLocal() as db:
        res = ingest_vec_bill_pdf(db, t.id, utility_account_id, raw)
        if not res.get("ok"):
            raise HTTPException(422, res.get("reason") or "Couldn't read that bill.")
        # A manual upload is its own bill-land path (no extension, no server pull) —
        # keep this account's offtaker generation-spreadsheet rows current too.
        from .sheet_tracker import maybe_append_for_account
        maybe_append_for_account(db, t.id, utility_account_id)
        db.commit()
        return res


# ─── subscriptions CRUD ─────────────────────────────────────────────────────

@router.get("/subscriptions")
def list_subscriptions(authorization: Optional[str] = Header(default=None)):
    from ..models import UtilityAccount
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        rows = db.execute(
            select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == t.id,
                   BillingReportSubscription.deleted_at.is_(None))
            .order_by(BillingReportSubscription.created_at.desc())
        ).scalars().all()
        subs = [_sub_dict(s) for s in rows]
        # Enrich each sub with the LINKED utility account's display name so the
        # card can say which GMP bill feeds the offtaker (the UI only had the id).
        ua_ids = {d["utility_account_id"] for d in subs if d.get("utility_account_id")}
        if ua_ids:
            amap = {a.id: (a.nickname or (f"GMP {a.account_number}" if a.account_number else None))
                    for a in db.execute(
                        select(UtilityAccount).where(UtilityAccount.id.in_(ua_ids))
                    ).scalars().all()}
            for d in subs:
                d["utility_account_name"] = amap.get(d.get("utility_account_id"))
        return {"ok": True, "subscriptions": subs}


@router.get("/list-bundle")
def list_bundle(authorization: Optional[str] = Header(default=None)):
    """One round-trip for the Reports / offtaker-list view.

    The tab needs three things to render the offtaker cards and their edit
    dropdowns: the subscriptions, the tenant's arrays, and the GMP utility
    accounts. The frontend used to fetch these as three parallel calls — and one
    of them was the *full fleet-tree* (per-inverter peer analysis), which is by
    far the heaviest. This folds all three into a single cheap call: the
    subscriptions + utility-accounts payloads are produced by the very same
    handlers (so the shapes stay byte-identical), and arrays are a LIGHT direct
    query (id / name / client) instead of the fleet-tree, since the offtaker
    dropdown only needs to name the arrays, not analyze them.
    """
    from ..models import Array

    t = tenant_from_session(authorization)
    arrays = []
    with SessionLocal() as db:
        rows = db.execute(
            select(Array)
            # Eager-load the client so the `a.client.name` read below doesn't fire
            # a lazy SELECT per array (one batched IN query instead of N). This
            # runs on every Reports-tab view.
            .options(selectinload(Array.client))
            .where(Array.tenant_id == t.id, Array.deleted_at.is_(None))
            .order_by(Array.name)
        ).scalars().all()
        arrays = [
            {
                "id": a.id,
                "name": a.name,
                "client_name": (a.client.name if (a.client_id and a.client) else None),
            }
            for a in rows
        ]
    # Reuse the existing endpoints' logic verbatim so the shapes never drift.
    subs = list_subscriptions(authorization)
    uacc = list_utility_accounts(authorization)
    return {
        "ok": True,
        "subscriptions": subs.get("subscriptions", []),
        "arrays": arrays,
        "utility_accounts": uacc.get("utility_accounts", []),
    }


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


def _sync_invoicing_quantity(tenant_id: str) -> None:
    """After an offtaker (BillingReportSubscription) is added or removed, keep the
    AO invoicing Stripe quantity in sync with the offtaker count. No-op unless the
    tenant is on the per-offtaker invoicing plan; best-effort so a Stripe hiccup
    never fails the offtaker mutation itself."""
    try:
        from ..stripe_helpers import reconcile_offtaker_quantity
        reconcile_offtaker_quantity(tenant_id)
    except Exception:  # noqa: BLE001 — the reconcile alerts on real failures
        pass


@router.post("/subscriptions")
async def create_subscription(
    file: Optional[UploadFile] = File(default=None),
    customer_name: Optional[str] = Form(default=None),
    array_id: Optional[int] = Form(default=None),
    utility_account_id: Optional[int] = Form(default=None),
    allocation_pct: Optional[float] = Form(default=None),
    array_allocations: Optional[str] = Form(default=None),
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
    include_summary: bool = Form(default=False),  # AO summary opt-in (Ford 2026-06-24)
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
            utility_account_id=utility_account_id,
            allocation_pct=allocation_pct, array_allocations=array_allocations,
            rate_per_kwh=rate_per_kwh,
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
        _sync_invoicing_quantity(t.id)
        return {"ok": True, "subscription": _sub_dict(sub)}


async def _create_manual_subscription(
    t, *, customer_name, array_id, allocation_pct, array_allocations=None,
    utility_account_id=None,
    rate_per_kwh, discount_pct,
    net_rate_per_kwh, cadence,
    send_mode, delivery_mode, client_email, cc_emails, operator_email, formats,
    include_summary, annual_trueup, enabled,
):
    """Create a workbook-less subscription from typed fields.

    THREE shapes (checked in priority order):
      * OFFTAKER ↔ UTILITY BILL — utility_account_id + allocation_pct. The
        offtaker's invoice is computed EXCLUSIVELY from that GMP account's utility
        PAPER BILLS (Bill.kwh_generated per period) — never vendor/inverter data,
        never the hourly interval data, and with NO fallback. This is the path
        Ford specified for offtaker reports.
      * single array  — array_id + allocation_pct (legacy, unchanged).
      * multi array    — array_allocations: JSON list of {array_id, allocation_pct}.
        The offtaker owns a share of several arrays; delivery sums each array's
        (period kWh × pct) into one combined invoice.
    """
    import json as _json
    from ..models import Array, UtilityAccount

    name = (customer_name or "").strip()
    if not name:
        raise HTTPException(400, "customer_name is required for a manual customer")

    # ── OFFTAKER ↔ UTILITY BILL path (highest priority) ──────────────────────
    if utility_account_id is not None:
        if allocation_pct is None:
            raise HTTPException(
                400, "allocation_pct is required when binding an offtaker to a utility bill")
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
            acct = db.get(UtilityAccount, utility_account_id)
            if (acct is None or acct.tenant_id != t.id
                    or acct.deleted_at is not None):
                raise HTTPException(404, f"Utility account {utility_account_id} not found")
            from ..adapters import is_smarthub_provider
            _prov = (acct.provider or "").lower()
            if _prov != "gmp" and not is_smarthub_provider(_prov):
                raise HTTPException(
                    400, "Offtaker reports bind to a GMP or VEC/SmartHub utility "
                         "account (utility-bill data only).")
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
                utility_account_id=utility_account_id,
                # Keep array_id populated (from the account's array) for list views,
                # but delivery uses the utility-bill path because utility_account_id
                # is set — it never reads vendor/array data for this offtaker.
                array_id=acct.array_id,
                allocation_pct=pct,
                array_allocations=None,
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
            _sync_invoicing_quantity(t.id)
            return {"ok": True, "subscription": _sub_dict(sub)}

    # Parse the optional multi-array allocations list.
    allocs: list[dict] = []
    if array_allocations:
        try:
            parsed = _json.loads(array_allocations)
        except (ValueError, TypeError):
            raise HTTPException(400, "array_allocations must be valid JSON")
        if not isinstance(parsed, list):
            raise HTTPException(400, "array_allocations must be a JSON list")
        for r in parsed:
            if not isinstance(r, dict):
                raise HTTPException(400, "each allocation must be an object")
            try:
                aid = int(r.get("array_id"))
            except (TypeError, ValueError):
                raise HTTPException(400, "each allocation needs a numeric array_id")
            try:
                p = float(r.get("allocation_pct"))
            except (TypeError, ValueError):
                raise HTTPException(400, "each allocation needs a numeric allocation_pct")
            if not (0.0 < p <= 1.0):
                raise HTTPException(400, "allocation_pct must be a fraction in (0, 1] "
                                         "(e.g. 0.25 for 25%)")
            allocs.append({"array_id": aid, "allocation_pct": p})
        if not allocs:
            raise HTTPException(400, "array_allocations had no usable rows")

    if not allocs:
        # Legacy single-array path.
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
    else:
        pct = None
    rate_val = _validate_rate(rate_per_kwh)
    disc_val = _validate_discount(discount_pct)
    net_val = _validate_rate(net_rate_per_kwh)

    with SessionLocal() as db:
        # Validate every referenced array belongs to this tenant.
        aids_to_check = [a["array_id"] for a in allocs] if allocs else [array_id]
        for aid in aids_to_check:
            arr = db.get(Array, aid)
            if arr is None or arr.tenant_id != t.id or arr.deleted_at is not None:
                raise HTTPException(404, f"Array {aid} not found")

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
            # Keep a single array_id/allocation_pct for back-compat + list views;
            # when multi-array, store the first as the representative and the full
            # list in array_allocations (which delivery prefers when present).
            array_id=(allocs[0]["array_id"] if allocs else array_id),
            allocation_pct=(allocs[0]["allocation_pct"] if allocs else pct),
            array_allocations=(allocs or None),
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
        _sync_invoicing_quantity(t.id)
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
    # Re-bind the offtaker to a different GMP utility bill (the billing source).
    # Validated to a GMP account the operator owns; array_id is refreshed from it.
    utility_account_id: Optional[int] = None
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
    # Sequential invoice numbering: the operator's starting invoice number. Setting
    # it (re)seeds the running counter so the next invoice uses it and each real send
    # adds 1. Explicit null clears it (back to the period-date number).
    invoice_number_start: Optional[int] = None
    # Budget billing: a fixed final amount that overrides the calculated Amount Due
    # (line items still show). Explicit null clears it (back to the calculated total).
    budget_amount_usd: Optional[float] = None


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
        if body.utility_account_id is not None:
            # Re-bind the offtaker's billing source to a different GMP or VEC/
            # SmartHub utility bill. Mirror creation: validate ownership +
            # provider, refresh array_id from it.
            from ..models import UtilityAccount
            from ..adapters import is_smarthub_provider
            acct = db.get(UtilityAccount, body.utility_account_id)
            if acct is None or acct.tenant_id != t.id or acct.deleted_at is not None:
                raise HTTPException(404, f"Utility account {body.utility_account_id} not found")
            _prov = (acct.provider or "").lower()
            if _prov != "gmp" and not is_smarthub_provider(_prov):
                raise HTTPException(
                    400, "Offtaker invoices bind to a GMP or VEC/SmartHub utility "
                         "account.")
            sub.utility_account_id = body.utility_account_id
            sub.array_id = acct.array_id
        if "rate_per_kwh" in body.model_fields_set:
            # null clears the override; a number sets it (validated).
            sub.rate_per_kwh = _validate_rate(body.rate_per_kwh)
        if "discount_pct" in body.model_fields_set:
            sub.discount_pct = _validate_discount(body.discount_pct)
        if "net_rate_per_kwh" in body.model_fields_set:
            sub.net_rate_per_kwh = _validate_rate(body.net_rate_per_kwh)
        if body.auto_attach_gmp is not None:
            sub.auto_attach_gmp = body.auto_attach_gmp
        if "invoice_number_start" in body.model_fields_set:
            v = body.invoice_number_start
            if v is None:
                sub.invoice_number_start = None
                sub.invoice_number_next = None
            else:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, "invoice_number_start must be a whole number")
                if not (0 <= n <= 9_999_999):
                    raise HTTPException(400, "invoice_number_start must be between 0 and 9999999")
                # Only (re)seed the running counter when the START actually changes,
                # so saving unrelated edits never resets an in-progress sequence.
                if sub.invoice_number_start != n or sub.invoice_number_next is None:
                    sub.invoice_number_start = n
                    sub.invoice_number_next = n
        if "budget_amount_usd" in body.model_fields_set:
            v = body.budget_amount_usd
            if v is None:
                sub.budget_amount_usd = None
            else:
                try:
                    amt = float(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, "budget_amount_usd must be a number")
                if amt < 0:
                    raise HTTPException(400, "budget_amount_usd can't be negative")
                sub.budget_amount_usd = amt
        db.commit()
        return {"ok": True, "subscription": _sub_dict(sub)}


# ── BULK OFFTAKER IMPORT (Ford, 2026-06-30) ──────────────────────────────────
# A roster CSV (name, email, percent share, account number) → many offtakers at
# once instead of one-at-a-time through the manual form. Built for operators
# scaling past a handful of offtakers — the "100 offtakers" case. Deterministic
# header detection (no LLM), matching the codebase's existing heuristic
# spreadsheet-column-detector pattern. CSV only (not .xlsx) — a roster has no
# visual layout to preserve, so plain CSV keeps parsing trivial and dependency-free.
_BULK_HEADER_ALIASES = {
    "name": {"name", "offtakername", "customername", "customer", "offtaker",
              "tenantname", "clientname", "fullname"},
    "email": {"email", "clientemail", "offtakeremail", "customeremail", "contactemail",
              "emailaddress"},
    "percent": {"percent", "pct", "share", "allocation", "allocationpct",
                "percentage", "offtakerpct", "sharepct", "%"},
    "account_number": {"accountnumber", "accountno", "account", "acctnumber", "acctno",
                        "gmpaccount", "utilityaccount", "meternumber", "meterno",
                        "accountnum", "acct"},
    "discount": {"discount", "discountpct", "discountpercent"},
}


def _bulk_norm_header(s: str) -> str:
    return _re.sub(r"[^a-z0-9%]", "", (s or "").lower())


def _bulk_classify_columns(header_row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, h in enumerate(header_row):
        nh = _bulk_norm_header(h)
        if not nh:
            continue
        for field, aliases in _BULK_HEADER_ALIASES.items():
            if field in mapping:
                continue
            if nh in aliases:
                mapping[field] = idx
                break
    return mapping


async def _read_csv_upload(file: UploadFile) -> bytes:
    """Same size/empty guards as _read_upload, but NO xlsx-magic-bytes gate — that
    gate is specific to the billing-workbook flow and would reject every CSV
    outright (caught live: it raised "Upload an .xlsx billing workbook" against a
    real CSV during testing). A roster import is plain text, not a workbook."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (max 8 MB)")
    return data


@router.post("/subscriptions/bulk-import")
async def bulk_import_offtakers(
    file: UploadFile = File(...),
    dry_run: bool = Form(default=True),
    cadence: str = Form(default="monthly"),
    delivery_mode: str = Form(default="approval"),
    authorization: Optional[str] = Header(default=None),
):
    """Create many offtakers at once from a roster CSV.

    Always parses + matches first (dry_run defaults True — a PREVIEW, no writes).
    The frontend shows the matched/unmatched table, the operator fixes or accepts,
    then re-posts with dry_run=false to actually create. Only rows with ZERO
    errors are ever created — a row this can't confidently match is reported
    back, never guessed at (the same "never fabricate" rule as everywhere else
    in this codebase: a wrong offtaker binding means a wrong invoice).
    """
    t = tenant_from_session(authorization)
    require_not_demo(t)
    if cadence not in VALID_CADENCE:
        raise HTTPException(400, "cadence must be monthly or quarterly")
    if delivery_mode not in VALID_DELIVERY:
        raise HTTPException(400, "delivery_mode must be approval or auto")

    raw = await _read_csv_upload(file)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception:
            raise HTTPException(422, "Couldn't read that file as text — please upload a CSV "
                                      "(export from Excel/Google Sheets: File → Download → CSV).")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise HTTPException(422, "That file is empty.")
    header, *data_rows = rows
    colmap = _bulk_classify_columns(header)
    missing = [f for f in ("name", "percent") if f not in colmap]
    if missing:
        raise HTTPException(
            422, "Couldn't find a column for: " + ", ".join(missing) + ". "
                 "Include a header row with Name and Percent (and ideally Account Number "
                 "so each offtaker links to the right utility bill).")

    from ..models import UtilityAccount
    with SessionLocal() as db:
        accts = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == t.id, UtilityAccount.deleted_at.is_(None)
        )).scalars().all()
    by_number = {(a.account_number or "").strip().lower(): a for a in accts if a.account_number}
    single_acct = accts[0] if len(accts) == 1 else None

    def _cell(row: list[str], field: str) -> str:
        idx = colmap.get(field)
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    def _pct(raw: str) -> tuple[Optional[float], Optional[str]]:
        if not raw:
            return None, "missing percent"
        try:
            v = float(raw.replace("%", "").strip())
        except ValueError:
            return None, f'"{raw}" isn\'t a number'
        if v > 1.0:
            v = v / 100.0  # accept "25" or "25%" or "0.25" — never "2500%"
        if not (0.0 < v <= 1.0):
            return None, "percent must be between 0 and 100"
        return v, None

    results = []
    for i, row in enumerate(data_rows, start=2):  # row 1 is the header
        name = _cell(row, "name")
        if not name and not any(c.strip() for c in row):
            continue  # silently skip a wholly-blank trailing line
        errors = []
        if not name:
            errors.append("missing name")

        pct, pct_err = _pct(_cell(row, "percent"))
        if pct_err:
            errors.append(pct_err)

        email = _cell(row, "email") or None
        acct_num = _cell(row, "account_number")
        acct = None
        if acct_num:
            acct = by_number.get(acct_num.strip().lower())
            if acct is None:
                errors.append(f'no connected utility account matches "{acct_num}"')
        elif single_acct:
            acct = single_acct  # only one utility account on file — safe unambiguous default
        else:
            errors.append("no account number given, and more than one utility account is "
                           "connected — add an Account Number column to disambiguate")

        discount_raw = _cell(row, "discount")
        discount = None
        if discount_raw:
            try:
                discount = float(discount_raw.replace("%", "").strip())
                if discount > 1.0:
                    discount = discount / 100.0
            except ValueError:
                errors.append(f'"{discount_raw}" isn\'t a valid discount')

        results.append({
            "row": i, "name": name, "email": email, "allocation_pct": pct,
            "account_number": acct_num or None,
            "matched_account_id": acct.id if acct else None,
            "matched_account_label": (
                f"{(acct.provider or '').upper()} · {acct.account_number}"
                + (f" · {acct.nickname}" if acct.nickname else "")
            ) if acct else None,
            "discount_pct": discount,
            "errors": errors,
        })

    if not results:
        raise HTTPException(422, "No offtaker rows found below the header.")

    if dry_run:
        return {"ok": True, "dry_run": True, "rows": results, "summary": {
            "total": len(results),
            "ready": sum(1 for r in results if not r["errors"]),
            "needs_attention": sum(1 for r in results if r["errors"]),
        }}

    # COMMIT — only rows with zero errors are created; everything else reports back
    # untouched so the operator can fix the source CSV and re-run (idempotent: a
    # second import just creates new rows for whatever's still missing — it doesn't
    # know about "already imported", so re-uploading the FULL roster after a partial
    # fix will duplicate the rows that already landed. Surfaced in the frontend copy.)
    created, failed = [], []
    for r in results:
        if r["errors"]:
            failed.append(r)
            continue
        try:
            out = await _create_manual_subscription(
                t, customer_name=r["name"], array_id=None, allocation_pct=r["allocation_pct"],
                utility_account_id=r["matched_account_id"], rate_per_kwh=None,
                discount_pct=r["discount_pct"], net_rate_per_kwh=None,
                cadence=cadence, send_mode=("to_client" if r["email"] else "to_me"),
                delivery_mode=delivery_mode, client_email=r["email"], cc_emails=None,
                operator_email=None, formats=None, include_summary=False,
                annual_trueup=False, enabled=True,
            )
            created.append({"row": r["row"], "name": r["name"],
                             "subscription_id": out["subscription"]["id"]})
        except HTTPException as e:
            failed.append({**r, "errors": [str(e.detail)]})
    return {"ok": True, "dry_run": False, "created": len(created), "failed": failed,
            "rows": created}


class GlobalRatePatch(BaseModel):
    default_billing_rate_per_kwh: Optional[float] = None
    # Discount model: the operator's global default net rate + discount.
    default_net_rate_per_kwh: Optional[float] = None
    default_discount_pct: Optional[float] = None


@router.get("/reconcile-bills")
def reconcile_bills_route(authorization: Optional[str] = Header(default=None)):
    """Compare each offtaker invoice's produced-kWh against the captured GMP bill
    for the same array + period — a READ-ONLY trust check before sending.

    Per array: our_kwh (what the invoice uses) vs gmp_kwh (the utility's metered
    generation), with a match|mismatch|no_bill verdict. 'no_bill' = no GMP bill
    is linked to that array yet (awaiting capture) — reported honestly, never
    fabricated.
    """
    from .reconcile_bills import reconcile_tenant
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        return reconcile_tenant(db, t.id)


@router.get("/invoice-export.csv")
def invoice_export_csv(authorization: Optional[str] = Header(default=None),
                       account_code: str = Query(default="")):
    """QuickBooks / Xero batch invoice-export (Anna/Bruce's ask #3).

    Emits the current period's offtaker invoices as a CSV in the exact column
    layout of Anna's bookkeeping export, ready to import into QB or Xero. Only
    offtakers with a real billable invoice are included — never a fabricated $0
    row. `account_code` fills the trailing account-code column if the operator
    maps solar income to a specific account.
    """
    from .qb_export import build_invoice_register
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        csv_text, count = build_invoice_register(db, t.id, account_code=account_code)
    fname = f"offtaker-invoices-{date.today().isoformat()}.csv"
    return Response(
        content=csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"',
                 "X-Invoice-Count": str(count)})


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


# ─── First-run setup wizard ──────────────────────────────────────────────────

@router.get("/setup-state")
def setup_state(authorization: Optional[str] = Header(default=None)):
    """One call that powers the first-run Reports setup wizard. Returns the
    owner's arrays (with the age/utility/location we need for auto-rates and
    what's still MISSING), whether any customers exist yet, and the global
    rate/discount defaults. The UI shows the wizard when has_customers is False.
    """
    from ..models import Array, UtilityAccount
    from .delivery import DEFAULT_DISCOUNT
    from ..rate_schedule import resolve_net_rate, array_age_bucket, AGE_THRESHOLD_YEARS
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == t.id, Array.deleted_at.is_(None))
            .order_by(Array.name)
        ).scalars().all()
        out_arrays = []
        for a in arrays:
            acct = db.execute(
                select(UtilityAccount).where(UtilityAccount.array_id == a.id)
            ).scalars().first()
            provider = acct.provider if acct else None
            fc = a.first_connect_date
            # The auto rate this array's customers would get today.
            rr = resolve_net_rate(db, provider=provider, region=a.region,
                                  first_connect_date=fc, period_end=None)
            out_arrays.append({
                "array_id": a.id,
                "name": a.name,
                "region": a.region,
                "provider": provider,
                "first_connect_date": fc.date().isoformat() if fc else None,
                "install_year": fc.year if fc else None,
                "age_known": fc is not None,
                "age_bucket": array_age_bucket(fc) if fc else None,
                "auto_net_rate": round(rr.rate, 5),
                "auto_net_source": rr.source,
                "auto_net_note": rr.note,
            })
        n_customers = db.execute(
            select(func.count(BillingReportSubscription.id)).where(
                BillingReportSubscription.tenant_id == t.id,
                BillingReportSubscription.deleted_at.is_(None))
        ).scalar() or 0
        return {
            "ok": True,
            "has_customers": n_customers > 0,
            "customer_count": int(n_customers),
            "age_threshold_years": AGE_THRESHOLD_YEARS,
            "arrays": out_arrays,
            "global": {
                "default_net_rate_per_kwh": getattr(t, "default_net_rate_per_kwh", None),
                "default_discount_pct": getattr(t, "default_discount_pct", None),
                "effective_discount_pct": (getattr(t, "default_discount_pct", None)
                                           if getattr(t, "default_discount_pct", None) is not None
                                           else DEFAULT_DISCOUNT),
            },
        }


class ArrayAgeBody(BaseModel):
    # Either an install year (YYYY) or a full ISO date; year is the friendly path.
    install_year: Optional[int] = None
    first_connect_date: Optional[str] = None
    region: Optional[str] = None   # north | central | south (optional location)


@router.patch("/arrays/{array_id}")
def set_array_setup(array_id: int, body: ArrayAgeBody,
                    authorization: Optional[str] = Header(default=None)):
    """Set an array's install age (feeds the auto rate buckets ≤11 vs >11 yr)
    and optional region. Tenant-scoped. Year is validated to a sane range."""
    from datetime import date as _date
    from ..models import Array
    t = tenant_from_session(authorization)
    require_not_demo(t)
    fc = None
    if body.first_connect_date:
        try:
            fc = datetime.fromisoformat(body.first_connect_date[:10])
        except ValueError:
            raise HTTPException(400, "first_connect_date must be YYYY-MM-DD")
    elif body.install_year is not None:
        yr = int(body.install_year)
        if yr < 1990 or yr > _date.today().year:
            raise HTTPException(400, f"install_year must be 1990–{_date.today().year}")
        fc = datetime(yr, 1, 1)
    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != t.id or arr.deleted_at is not None:
            raise HTTPException(404, "Array not found")
        if fc is not None:
            arr.first_connect_date = fc
        if "region" in body.model_fields_set and body.region is not None:
            arr.region = body.region.strip().lower() or None
        db.commit()
        return {"ok": True, "array_id": arr.id,
                "first_connect_date": arr.first_connect_date.date().isoformat()
                if arr.first_connect_date else None,
                "region": arr.region}


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        sub.deleted_at = datetime.utcnow()
        sub.enabled = False
        # Dismiss any pending drafts so a deleted offtaker leaves nothing behind in
        # the approval inbox (they used to orphan there — the "(sample)" leftovers).
        for d in db.execute(select(ReportDraft).where(
                ReportDraft.subscription_id == sub.id,
                ReportDraft.status == "pending")).scalars().all():
            d.status = "dismissed"
            d.dismissed_at = datetime.utcnow()
        db.commit()
        _sync_invoicing_quantity(t.id)
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
            variant: Optional[str] = Query(default=None),
            authorization: Optional[str] = Header(default=None)):
    """Render the current invoice or summary on demand for download.

    variant (invoice PDF only): "default" forces OUR standard format; "template"
    forces the operator's template even when it's toggled off (a preview-only
    compare for the approval-inbox buttons); None = exactly what gets sent."""
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
                p = None
                if fmt == "pdf":
                    # Mirror the SEND chain so the preview is exactly what gets
                    # delivered: own-workbook repro → operator-template repro →
                    # operator token-HTML template → standard. `variant` lets the
                    # approval-inbox compare buttons force one side (see docstring).
                    from .delivery import (_render_from_repro,
                                           _render_from_operator_template_repro,
                                           _render_from_operator_template)
                    pp = tmpd / "p.pdf"
                    if variant == "default":
                        pass                          # force OUR standard format
                    elif variant == "template":       # force the template (even if off)
                        if (_render_from_repro(match, sub, pp)
                                or _render_from_operator_template_repro(match, sub, pp, force=True)
                                or _render_from_operator_template(match, sub, pp, force=True)):
                            p = pp
                    elif (_render_from_repro(match, sub, pp)
                            or _render_from_operator_template_repro(match, sub, pp)
                            or _render_from_operator_template(match, sub, pp)):
                        p = pp
                if p is None:
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


# ─── Bring-your-own generation spreadsheet tracker ───────────────────────────
# The operator uploads their OWN running generation sheet (whatever columns they
# use); we detect its structure and keep appending a monthly row as fresh GMP
# bills land. A "Download latest spreadsheet" button streams the kept-current
# file. Whole feature gated behind SPREADSHEET_TRACKER_ENABLED.

_XLSX_MEDIA = ("application/vnd.openxmlformats-officedocument"
               ".spreadsheetml.sheet")


def _tracker_status_dict(sub) -> dict:
    """The tracker card state for the offtaker editor. Honest about whether a
    sheet is attached, what we detected, and when we last appended."""
    m = getattr(sub, "tracker_map", None) or {}
    has = bool(getattr(sub, "tracker_workbook", None)) and bool(m.get("ok"))
    up = getattr(sub, "tracker_updated_at", None)
    return {
        "enabled": True,
        "has_sheet": has,
        "filename": getattr(sub, "tracker_filename", None),
        "columns": m.get("columns") if has else None,
        "headers": m.get("headers") if has else None,
        "header_row": m.get("header_row") if has else None,
        "sheet": m.get("sheet") if has else None,
        "data_rows": m.get("data_rows") if has else None,
        "last_period": m.get("last_period") if has else None,
        "updated_at": up.isoformat() + "Z" if up else None,
        "warnings": m.get("warnings") or [],
    }


def _friendly_period(lbl: str) -> str:
    """'2026-05' -> 'May 2026' for human-readable upload feedback; passthrough on anything odd."""
    try:
        import calendar
        y, m = str(lbl).split("-")[:2]
        return f"{calendar.month_name[int(m)]} {y}"
    except Exception:  # noqa: BLE001
        return str(lbl)


@router.get("/subscriptions/{sub_id}/tracker")
def tracker_status(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """Tracker state for one offtaker (drives the card). 404 only on a missing
    sub; returns {enabled:false} when the feature flag is off so the UI hides."""
    from .sheet_tracker import tracker_enabled
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        if not tracker_enabled():
            return {"ok": True, "tracker": {"enabled": False}}
        return {"ok": True, "tracker": _tracker_status_dict(sub)}


@router.post("/subscriptions/{sub_id}/tracker")
async def tracker_upload(sub_id: int,
                         file: UploadFile = File(...),
                         authorization: Optional[str] = Header(default=None)):
    """Upload the offtaker's existing generation spreadsheet (XLSX or CSV). We
    detect its structure ('our magic'), normalize to xlsx, and store it as the
    running ledger we keep current. Returns the detected mapping for review."""
    from .sheet_tracker import tracker_enabled, ingest_upload
    t = tenant_from_session(authorization)
    require_not_demo(t)
    if not tracker_enabled():
        raise HTTPException(404, "Spreadsheet tracker is not enabled.")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (8 MB max).")
    name = file.filename or "generation.xlsx"
    is_x = raw[:4] in (_MAGIC_XLSX, _MAGIC_XLS)
    is_csv = name.lower().endswith(".csv") or (not is_x)
    if not is_x and not is_csv:
        raise HTTPException(415, "Upload an .xlsx or .csv generation sheet.")
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        # Do NOT pass the offtaker's name as a column hint here: on sheets that carry BOTH a
        # 'kWh whole array' column (populated) and an empty 'kWh <offtaker>' column, the name
        # boost dragged generation onto the empty named column, so appended rows landed blank.
        # The generation column is detected by keyword + data-presence, which is correct here.
        res = ingest_upload(raw, name, None)
        if not res.get("ok"):
            warn = "; ".join(res.get("warnings") or []) or "Couldn't read that sheet."
            raise HTTPException(422, warn)
        sub.tracker_workbook = res["workbook"]
        sub.tracker_filename = name
        sub.tracker_map = res["mapping"]
        sub.tracker_updated_at = datetime.utcnow()
        db.add(sub)
        # Transparency: immediately reconcile the freshly-uploaded sheet against the offtaker's
        # GMP bills, so the operator SEES that we processed it and produced an updated
        # spreadsheet — not a silent store. Best-effort: a reconcile hiccup never fails upload.
        from .sheet_tracker import update_subscription_sheet
        try:
            recon = update_subscription_sheet(db, sub) or {}
        except Exception:  # noqa: BLE001
            recon = {"status": "error"}
        db.commit()
        db.refresh(sub)
        added = recon.get("periods") if isinstance(recon.get("periods"), list) else (
            [recon["period"]] if recon.get("status") == "appended" and recon.get("period") else [])
        return {"ok": True, "tracker": _tracker_status_dict(sub),
                "processed": {
                    "sheet": (res.get("mapping") or {}).get("sheet"),
                    "status": recon.get("status"),
                    "added": [_friendly_period(p) for p in added],
                    "added_count": len(added),
                    "normalized": (res.get("mapping") or {}).get("normalized") or 0,
                    "ai": recon.get("ai"),   # {sane, explanation, via} when the AI planner ran
                }}


@router.patch("/subscriptions/{sub_id}/tracker")
def tracker_remap(sub_id: int, body: dict,
                  authorization: Optional[str] = Header(default=None)):
    """Operator correction of the detected column mapping (nice-to-have). Body:
    {"columns": {"period": <idx>, "generation": <idx>, ...}}. Validates indices
    against the stored headers and updates the mapping in place."""
    from .sheet_tracker import tracker_enabled
    t = tenant_from_session(authorization)
    if not tracker_enabled():
        raise HTTPException(404, "Spreadsheet tracker is not enabled.")
    cols = (body or {}).get("columns")
    if not isinstance(cols, dict):
        raise HTTPException(400, "columns object required")
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        m = dict(getattr(sub, "tracker_map", None) or {})
        if not m.get("ok"):
            raise HTTPException(409, "No tracker sheet to remap.")
        ncol = len(m.get("headers") or [])
        clean = {}
        for k in ("period", "generation", "consumption", "rate", "amount"):
            if k in cols and cols[k] is not None:
                ci = int(cols[k])
                if ci < 0 or (ncol and ci >= ncol):
                    raise HTTPException(400, f"{k} column out of range")
                clean[k] = ci
        if "generation" not in clean:
            raise HTTPException(400, "A generation/kWh column is required.")
        m["columns"] = clean
        sub.tracker_map = m
        sub.tracker_updated_at = datetime.utcnow()
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return {"ok": True, "tracker": _tracker_status_dict(sub)}


@router.delete("/subscriptions/{sub_id}/tracker")
def tracker_remove(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """Detach the BYO sheet from this offtaker."""
    from .sheet_tracker import tracker_enabled
    t = tenant_from_session(authorization)
    if not tracker_enabled():
        raise HTTPException(404, "Spreadsheet tracker is not enabled.")
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        sub.tracker_workbook = None
        sub.tracker_filename = None
        sub.tracker_map = None
        sub.tracker_updated_at = datetime.utcnow()
        db.add(sub)
        db.commit()
        return {"ok": True, "tracker": _tracker_status_dict(sub)}


@router.get("/subscriptions/{sub_id}/tracker/download")
def tracker_download(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """Stream the current, kept-current generation spreadsheet. Before streaming
    we opportunistically append the latest period (idempotent) so 'Download
    latest' always reflects the freshest computed bill even between worker runs."""
    from .sheet_tracker import tracker_enabled, update_subscription_sheet
    t = tenant_from_session(authorization)
    if not tracker_enabled():
        raise HTTPException(404, "Spreadsheet tracker is not enabled.")
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        if not getattr(sub, "tracker_workbook", None):
            raise HTTPException(404, "No spreadsheet uploaded yet.")
        # Keep-current on demand: append the latest period if it isn't there.
        res = update_subscription_sheet(db, sub)
        if res.get("status") == "appended":
            db.add(sub)
            db.commit()
            db.refresh(sub)
        blob = bytes(sub.tracker_workbook)
        base = (getattr(sub, "tracker_filename", None) or "generation.xlsx")
        if base.lower().endswith(".csv"):
            base = base[:-4] + ".xlsx"
        elif not base.lower().endswith(".xlsx"):
            base = base + ".xlsx"
    return StreamingResponse(
        io.BytesIO(blob), media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{base}"'})


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
        "net_rate_note": ci.get("net_rate_note"),
        "discount_source": ci.get("discount_source"),
        "solar_savings_usd": (ci.get("solar_savings") if has_data else None),
        "kwh_source": ci.get("kwh_source"),
        "period_start": ci.get("period_start"),
        "period_end": ci.get("period_end"),
    }


@router.get("/subscriptions/{sub_id}/daily-series")
def subscription_daily_series(
    sub_id: int,
    period: Optional[str] = Query(default=None, description="YYYY-MM month, or YYYY-Qn quarter; default = latest month with data"),
    authorization: Optional[str] = Header(default=None),
):
    """Real DAILY generation for an offtaker's array over a billing period —
    powers the daily-generation bar graph in reports. Points are the array's
    measured DailyGeneration rows, scaled by the offtaker's allocation_pct so the
    bars show THAT offtaker's daily share. Never fabricates: when the array has no
    daily rows for the window, returns points:[] + has_data:false so the UI shows
    an honest empty state instead of invented bars.
    """
    from datetime import date as _date
    from ..models import DailyGeneration, Array
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        array_id = sub.array_id
        alloc = sub.allocation_pct if sub.allocation_pct is not None else 1.0
        if array_id is None:
            return {"subscription_id": sub_id, "has_data": False, "points": [],
                    "reason": "no_array", "allocation_pct": alloc}

        # Resolve the window. Explicit period wins; else the latest month that has
        # any daily rows for this array.
        start = end = None
        label = None
        if period:
            p = period.strip().upper()
            try:
                if "Q" in p:
                    yr, q = p.split("-Q") if "-Q" in p else (p[:4], p[-1])
                    yr = int(yr); q = int(q)
                    sm = 3 * (q - 1) + 1
                    start = _date(yr, sm, 1)
                    end = (_date(yr + (sm + 2) // 12, ((sm + 2) % 12) + 1, 1)
                           if sm + 3 > 12 else _date(yr, sm + 3, 1)) - timedelta(days=1)
                    label = f"Q{q} {yr}"
                else:
                    yr, mo = p.split("-"); yr = int(yr); mo = int(mo)
                    start = _date(yr, mo, 1)
                    end = (_date(yr + 1, 1, 1) if mo == 12 else _date(yr, mo + 1, 1)) - timedelta(days=1)
                    label = start.strftime("%B %Y")
            except (ValueError, IndexError):
                raise HTTPException(400, "period must be YYYY-MM or YYYY-Qn")
        if start is None:
            latest = db.execute(
                select(DailyGeneration.day)
                .where(DailyGeneration.array_id == array_id)
                .order_by(DailyGeneration.day.desc()).limit(1)
            ).scalar_one_or_none()
            if latest is None:
                return {"subscription_id": sub_id, "has_data": False, "points": [],
                        "reason": "no_daily_data", "allocation_pct": alloc}
            start = latest.replace(day=1)
            end = (_date(latest.year + 1, 1, 1) if latest.month == 12
                   else _date(latest.year, latest.month + 1, 1)) - timedelta(days=1)
            label = start.strftime("%B %Y")

        rows = db.execute(
            select(DailyGeneration.day, DailyGeneration.kwh, DailyGeneration.source)
            .where(DailyGeneration.array_id == array_id,
                   DailyGeneration.day >= start, DailyGeneration.day <= end)
            .order_by(DailyGeneration.day.asc())
        ).all()
        arr = db.get(Array, array_id)
        # Surface data provenance so the UI can mark estimated days (split out of a
        # bill via bill_prorate) distinctly from measured rows — never render an
        # estimate as if it were measured.
        points = [{"day": d.isoformat(),
                   "array_kwh": round(k or 0.0, 1),
                   "kwh": round((k or 0.0) * alloc, 1),
                   "source": src or "csv",
                   "is_estimated": (src == "bill_prorate")} for (d, k, src) in rows]
        total = round(sum(p["kwh"] for p in points), 1)
        return {
            "subscription_id": sub_id,
            "has_data": bool(points),
            "period_label": label,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "allocation_pct": alloc,
            "array_name": arr.name if arr else None,
            "total_kwh": total,
            "points": points,
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

def _calc_credit_for_budget(sub):
    """The CALCULATED solar credit value (pre-budget-override) for a budget-billed
    offtaker, so the email preview can show it alongside the budgeted amount as two
    distinct rows. None when there's no budget. Best-effort — a slow/unreadable
    workbook just yields None (the preview falls back to a single row)."""
    if sub is None or getattr(sub, "budget_amount_usd", None) is None:
        return None
    try:
        ci = build_match(sub).computed_invoice or {}
        return ci.get("solar_credit_value")
    except Exception:  # noqa: BLE001 — never break draft serialization on a parse hiccup
        return None


def _draft_dict(d: ReportDraft, sub=None, gmp_auto_status=None, operator_name=None) -> dict:
    return {
        "id": d.id,
        "subscription_id": d.subscription_id,
        # Display name follows the LIVE subscription (renaming the offtaker
        # anywhere updates the draft card + email preview), not the draft's frozen
        # snapshot. Falls back to the draft's stored name if the sub is gone.
        "customer_name": ((getattr(sub, "customer_name", None) or d.customer_name) if sub else d.customer_name),
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
        # Email-envelope fields so the inbox can render a FAITHFUL reproduction of
        # the message the offtaker receives (from/to/subject + which attachments).
        "client_email": getattr(sub, "client_email", None) if sub else None,
        "send_mode": getattr(sub, "send_mode", None) if sub else None,
        "include_summary": (getattr(sub, "include_summary", True) if sub else True),
        "operator_name": operator_name,
        # Auto-attach state (resolved from the subscription when provided):
        #   auto_attach_gmp   — is the toggle on for this customer
        #   gmp_auto_status   — "ready" (a captured bill PDF will attach) |
        #                       "pending" (toggle on, GMP account exists, no PDF
        #                       captured yet) | "no_gmp" (array has no GMP account)
        #                       | None (toggle off / not resolvable)
        "auto_attach_gmp": (getattr(sub, "auto_attach_gmp", False) if sub else None),
        "gmp_auto_status": gmp_auto_status,
        # Editable offtaker details, surfaced so the approval inbox can edit the
        # offtaker inline (live-update) without leaving the draft. These mirror the
        # SubscriptionPatch fields; the money-affecting ones (allocation/discount/
        # rate/utility bill) recompute the draft via generate_draft on change.
        "cadence": (getattr(sub, "cadence", None) if sub else None),
        "cc_emails": (getattr(sub, "cc_emails", None) if sub else None),
        "discount_pct": (getattr(sub, "discount_pct", None) if sub else None),
        "net_rate_per_kwh": (getattr(sub, "net_rate_per_kwh", None) if sub else None),
        "utility_account_id": (getattr(sub, "utility_account_id", None) if sub else None),
        "budget_amount_usd": (getattr(sub, "budget_amount_usd", None) if sub else None),
        # Calculated solar credit value (pre-budget-override). When a budget is set the
        # email shows BOTH: this value + the budgeted amount (amount_usd). None otherwise.
        "solar_credit_value": _calc_credit_for_budget(sub),
        "has_workbook": ((getattr(sub, "source_workbook", None) is not None) if sub else False),
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "sent_at": d.sent_at.isoformat() if d.sent_at else None,
    }


def _resolve_gmp_auto_status(db, sub) -> Optional[str]:
    """Honest auto-attach status for the draft card (never implies a PDF exists
    when it doesn't)."""
    if sub is None or not getattr(sub, "auto_attach_gmp", False):
        return None
    try:
        from ..reports import gmp_bill_pdf_read as gbp
        # Prefer the offtaker's BOUND utility account — the exact bill the invoice is
        # computed from, so the attached PDF matches the invoice's source. (The old
        # array-keyed path returned whichever of the array's sibling accounts had the
        # newest captured PDF, which can be the wrong bill.) Fall back to the array's
        # GMP accounts for legacy array-based subscriptions with no bound account.
        uaid = getattr(sub, "utility_account_id", None)
        if uaid is not None:
            found = gbp.get_bill_pdf_for_account(uaid, db=db)
            return "ready" if (found and found.get("bytes")) else "pending"
        array_id = getattr(sub, "array_id", None)
        if array_id is None:
            return "no_gmp"
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
                                   gmp_auto_status=_resolve_gmp_auto_status(db, sub),
                                   operator_name=getattr(t, "name", None)))
        return {"drafts": out}


@router.get("/subscriptions/{sub_id}/draft-versions")
def draft_versions(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """All draft VERSIONS for one offtaker — one per billing period, LATEST FIRST — so the
    operator can look back at older invoices in the approval inbox. The newest is the live
    pending draft; earlier periods are superseded/sent. Read-only."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        sub = _get_owned(db, t.id, sub_id)
        drafts = db.execute(
            select(ReportDraft).where(ReportDraft.subscription_id == sub.id)
            .order_by(ReportDraft.created_at.desc())
        ).scalars().all()
        # Dedupe to the most-recent draft per billing period; sort latest period first.
        seen, out = set(), []
        for d in drafts:
            key = d.period_label or ("#" + str(d.id))
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        out.sort(key=lambda d: (d.period_label or ""), reverse=True)
        return {"versions": [_draft_dict(d, sub) for d in out]}


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
        # ALWAYS pull the freshest GMP bill for this offtaker's bound account first, so
        # the invoice reflects the latest statement the moment it's generated — don't wait
        # for the 6h scheduler. Best-effort: a lapsed session (or a vendor hiccup) just
        # falls back to the last captured bill, so a refresh failure never blocks a draft.
        # READ COMMITTED → build_match's fresh bill query below sees the committed pull.
        uaid = getattr(sub, "utility_account_id", None)
        if uaid:
            try:
                from ..worker import pull_account_bills
                pull_account_bills(t.id, uaid)
            except Exception:  # noqa: BLE001
                pass
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

        # Idempotent + self-healing. A draft generated BEFORE the customer's
        # first utility bill landed has invoice_number=None and no period; once
        # the bill arrives its invoice_number/period drift to e.g. "2026-05", so
        # the old strict `invoice_number == inv_no` match MISSED that placeholder
        # and spawned a SECOND pending draft — a stale $0 duplicate in the
        # approval inbox (hit live on Paul Bozuwa / HCT Sun, draft 8, hand-
        # refreshed via a prod script). Instead, refresh THE pending draft that
        # belongs to this period — same invoice number, same period label, or the
        # not-yet-periodised placeholder (invoice_number NULL) — and fold any
        # extra duplicates into one so the invariant "one pending draft per
        # (subscription, period)" holds going forward.
        pendings = db.execute(
            select(ReportDraft).where(
                ReportDraft.subscription_id == sub.id,
                ReportDraft.status == "pending",
            ).order_by(ReportDraft.created_at.asc())
        ).scalars().all()

        def _same_period(dr: ReportDraft) -> bool:
            if inv_no is not None and dr.invoice_number == inv_no:
                return True            # exact period key — true idempotency
            if dr.invoice_number is None:
                return True            # pre-bill placeholder — adopt it now
            if period_label is not None and dr.period_label == period_label:
                return True            # invoice number reformatted, same period
            return False

        matches = [dr for dr in pendings if _same_period(dr)]
        existing = matches[0] if matches else None
        for dup in matches[1:]:        # collapse pre-existing duplicates
            dup.status = "dismissed"
            dup.dismissed_at = datetime.utcnow()
        # SUPERSEDE older-period drafts: build_match always uses the LATEST bill, so any
        # OTHER pending draft is from an earlier period and must not linger — otherwise the
        # approval inbox can surface a stale bill (Paul Bozuwa: a May $3,167 draft sat in
        # front of the new June one). Keep exactly ONE pending draft per offtaker: this one.
        for dr in pendings:
            if dr not in matches:
                dr.status = "dismissed"
                dr.dismissed_at = datetime.utcnow()

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
        # Pass `sub` so the recompute response carries the SUBSCRIPTION-derived fields
        # (budget_amount_usd + the CALCULATED solar_credit_value) — without it the
        # frontend's post-edit refresh got nulls, so the "How we calculated" panel lost
        # the budget split and back-derived a fake rate (budget ÷ kWh) from amount_usd,
        # and the email preview collapsed to one row. Mirror the /drafts list overlay.
        return {"ok": True, "draft": _draft_dict(
            d, sub=sub,
            gmp_auto_status=_resolve_gmp_auto_status(db, sub),
            operator_name=getattr(t, "name", None))}


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


@router.get("/drafts/{draft_id}/gmp-bill")
def get_draft_gmp_bill(draft_id: int, authorization: Optional[str] = Header(default=None)):
    """Stream the GMP utility-bill PDF that rides with this draft — the manually
    attached one if present, else the auto-captured bill for the period (the same
    PDF the send path attaches). 404 when none is captured yet (the 'pending' chip)."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        d = _get_owned_draft(db, t.id, draft_id)
        if d.gmp_invoice_pdf:
            fn = "".join(c for c in (d.gmp_filename or "gmp_bill.pdf")
                         if c.isalnum() or c in "._- ") or "gmp_bill.pdf"
            return StreamingResponse(io.BytesIO(d.gmp_invoice_pdf), media_type="application/pdf",
                headers={"Content-Disposition": "inline; filename=" + fn})
        sub = db.get(BillingReportSubscription, d.subscription_id) if d.subscription_id else None
        if sub is not None and getattr(sub, "auto_attach_gmp", False):
            # Resolve the SAME PDF the send path attaches: the BOUND utility account's
            # bill for THIS draft's period — so the download matches the invoice exactly
            # (incl. older versions in the version dropdown), not just the global newest
            # across the array. Falls back to the array-keyed lookup only for legacy
            # array-based subs with no bound account.
            from ..reports import gmp_bill_pdf_read as gbp
            from .delivery import _parse_iso_date
            ps = pe = None
            if d.period_label and "→" in d.period_label:
                parts = [p.strip() for p in d.period_label.split("→")]
                if len(parts) == 2:
                    ps, pe = _parse_iso_date(parts[0]), _parse_iso_date(parts[1])
            uaid = getattr(sub, "utility_account_id", None)
            found = None
            try:
                if uaid is not None:
                    found = gbp.get_bill_pdf_for_account(uaid, ps, pe, db=db)
                elif getattr(sub, "array_id", None):
                    found = gbp.get_bill_pdf_for_period(sub.array_id, ps, pe, db=db)
            except Exception:
                found = None
            if found and found.get("bytes"):
                return StreamingResponse(io.BytesIO(found["bytes"]),
                    media_type=found.get("content_type") or "application/pdf",
                    headers={"Content-Disposition": "inline; filename=gmp_bill.pdf"})
        raise HTTPException(404, "No GMP bill captured for this period yet")


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


@router.post("/drafts/{draft_id}/test")
def test_draft(draft_id: int, authorization: Optional[str] = Header(default=None)):
    """Send a TEST copy of this draft (the exact email + invoice the offtaker would
    get, with your edited note) to the OPERATOR — you — so you can check it before
    approving. Goes only to you; never reaches the customer; bypasses the
    utility-bill send gate since it's a self-test."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        d = _get_owned_draft(db, t.id, draft_id)
        sub = _get_owned(db, t.id, d.subscription_id)
        # Attach the draft's GMP PDF for this test in-memory only (no commit — a
        # test must not mutate the live subscription).
        if d.gmp_invoice_pdf is not None:
            sub.gmp_invoice_pdf = d.gmp_invoice_pdf
        result = deliver_subscription(db, sub, t, triggered_by="test",
                                      is_test=True, note=d.note)
        if not result.get("ok"):
            raise HTTPException(422, result.get("error", "test send failed"))
        return {"ok": True, "result": result}


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


@router.post("/drafts/{draft_id}/ai-email")
def ai_email_for_draft(draft_id: int, authorization: Optional[str] = Header(default=None)):
    """Write a tailored cover email for this draft from the REAL invoice figures +
    the context that applies (budget/flat bill, annual true-up, banked-not-cashed
    credits, $0 period). Returns the text for the operator to review/edit — it does
    NOT save or send. Plain text, no invented numbers."""
    t = tenant_from_session(authorization)
    from .repro.llm import call_json, llm_available, LLMUnavailable
    if not llm_available():
        raise HTTPException(503, "AI email isn't configured (no ANTHROPIC_API_KEY).")
    with SessionLocal() as db:
        d = _get_owned_draft(db, t.id, draft_id)
        sub = db.get(BillingReportSubscription, d.subscription_id) if d.subscription_id else None
        ci = {}
        if sub is not None:
            try:
                ci = build_match(sub).computed_invoice or {}
            except Exception:  # noqa: BLE001
                ci = {}
        operator = (getattr(t, "company_name", None) or getattr(t, "operator_name", None)
                    or getattr(t, "name", None) or "your solar operator")
        # The ReportDraft row is a FROZEN snapshot — the offtaker's CURRENT name + the
        # recomputed amount live on the subscription / fresh match (this is exactly what
        # _draft_dict overlays for the card). Read d.* directly and you resurrect stale
        # values, e.g. a since-corrected name typo. Mirror the live overlay here.
        name = (getattr(sub, "customer_name", None) or d.customer_name) if sub else d.customer_name
        amt = ci.get("amount_owed")
        if amt is None:
            amt = d.amount_usd
        ctx = {
            "offtaker_name": name,
            "billing_period": d.period_label,
            "their_production_kwh": d.customer_kwh,
            "amount_due_usd": amt,
            "is_budget_bill": getattr(sub, "budget_amount_usd", None) is not None,
            "is_annual_trueup": bool(getattr(sub, "annual_trueup", False)),
            "solar_credit_rate_per_kwh": ci.get("net_rate_per_kwh"),
            "credit_banked_not_cashed": ci.get("net_rate_source") == "gmp_credit_reference",
            "zero_due": isinstance(amt, (int, float)) and abs(amt) < 0.005,
            "operator_company": operator,
            "attachments": "the invoice PDF" + (
                " and a production summary" if getattr(sub, "include_summary", True) else ""),
        }
        system = (
            "You write the short cover email a solar operator sends to an offtaker "
            "alongside their solar-credit invoice. Warm, professional, concise — 3 to 5 "
            "sentences. Use ONLY the real figures provided. Reflect whichever context "
            "applies: a flat/budget bill (do not imply it was metered this period), an "
            "annual true-up, credits that were BANKED not cashed (trued up later), or a "
            "$0 period. Open with a simple greeting, mention the billing period and the "
            "amount, note that the attachments are included, and sign off as the "
            "operator's company. Reference the figures plainly but DO NOT claim that "
            "kWh times the rate equals the total — a discount may apply, so the "
            "arithmetic won't line up. Plain text only — no subject line, no markdown, "
            "no placeholder brackets, no invented numbers."
        )
        try:
            out = call_json(
                system=system,
                user_text="Invoice context (JSON):\n" + json.dumps(ctx, default=str),
                max_tokens=600,
                schema={"type": "object",
                        "properties": {"email": {"type": "string"}},
                        "required": ["email"],
                        "additionalProperties": False})
        except LLMUnavailable:
            raise HTTPException(503, "AI email isn't configured.")
        except Exception as e:  # noqa: BLE001
            logger.warning("ai-email generation failed: %s", e)
            raise HTTPException(502, "Couldn't write the email right now — try again.")
        email = (out.get("email") or "").strip()
        if not email:
            raise HTTPException(502, "The AI returned an empty email — try again.")
        return {"ok": True, "email": email}


# ─── offtaker invoice TEMPLATE (operator's own format) ──────────────────────────
# Stage 1: upload + store the operator's invoice template per-tenant so generated
# offtaker invoices can LATER reproduce THEIR exact format. Rendering from the
# template (Stage 2) is gated behind `enabled`; storing one changes nothing about
# what is actually sent today (offtaker invoices keep using the standard PDF).

TEMPLATE_MAX_BYTES = 12 * 1024 * 1024  # 12 MB
TEMPLATE_EXT = {".pdf", ".html", ".htm", ".docx", ".doc", ".png", ".jpg", ".jpeg",
                ".xlsx", ".xls", ".xlsm"}  # Excel: we find the invoice sheet & seed HTML


async def _read_template_upload(file: UploadFile):
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty")
    if len(data) > TEMPLATE_MAX_BYTES:
        raise HTTPException(400, "File too large (max 12 MB)")
    name = (file.filename or "template").strip()
    ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    # Detect actual type by magic bytes and override the extension when possible.
    if data[:4] == _MAGIC_PDF:
        ext = ".pdf"
    elif data[:4] == _MAGIC_XLSX[:4]:
        if ext not in (".xlsx", ".xlsm"):
            ext = ".xlsx"   # mis-named OpenXML file — treat as xlsx
    elif data[:4] == _MAGIC_XLS:
        ext = ".xls"
    if ext not in TEMPLATE_EXT:
        raise HTTPException(400, "Upload a PDF, Word doc, HTML, or image of your invoice template")
    return data, name


def _template_dict(tpl) -> dict:
    return {
        "has_template": tpl is not None and (tpl.file_bytes is not None or bool(tpl.html)),
        "filename": getattr(tpl, "filename", None),
        "content_type": getattr(tpl, "content_type", None),
        "enabled": bool(getattr(tpl, "enabled", False)),
        "has_html": bool(getattr(tpl, "html", None)),
        "updated_at": tpl.updated_at.isoformat() if tpl and tpl.updated_at else None,
    }


class InvoiceTemplatePut(BaseModel):
    html: Optional[str] = None
    enabled: Optional[bool] = None


class InvoiceTemplatePreview(BaseModel):
    html: Optional[str] = None


@router.get("/invoice-template")
def get_invoice_template(authorization: Optional[str] = Header(default=None)):
    """Status + the editable token-HTML of this tenant's invoice template (seeds the
    default template when none saved, so the editor/preview work out of the box)."""
    t = tenant_from_session(authorization)
    from ..models import OfftakerInvoiceTemplate
    from .template_render import DEFAULT_TEMPLATE_HTML, AVAILABLE_TOKENS
    with SessionLocal() as db:
        tpl = db.execute(select(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id == t.id)).scalars().first()
        d = _template_dict(tpl)
        d["html"] = (tpl.html if tpl and tpl.html else DEFAULT_TEMPLATE_HTML)
        d["is_default_html"] = not (tpl and tpl.html)
        d["tokens"] = AVAILABLE_TOKENS
        return {"ok": True, "template": d}


@router.put("/invoice-template")
def put_invoice_template(body: InvoiceTemplatePut,
                         authorization: Optional[str] = Header(default=None)):
    """Save the editable token-HTML + the enable toggle. Enabling means real offtaker
    invoices render from this template (Stage 2); a render failure at send time falls
    back to the standard PDF, so it can never break a send."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    from ..models import OfftakerInvoiceTemplate
    from .template_render import DEFAULT_TEMPLATE_HTML
    with SessionLocal() as db:
        tpl = db.execute(select(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id == t.id)).scalars().first()
        if tpl is None:
            tpl = OfftakerInvoiceTemplate(tenant_id=t.id)
            db.add(tpl)
        if "html" in body.model_fields_set:
            tpl.html = body.html
        if "enabled" in body.model_fields_set:
            tpl.enabled = bool(body.enabled)
            # Enabling with no custom HTML seeds the default so it works immediately.
            if tpl.enabled and not tpl.html:
                tpl.html = DEFAULT_TEMPLATE_HTML
        tpl.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(tpl)
        return {"ok": True, "template": _template_dict(tpl)}


@router.post("/invoice-template/preview")
def preview_invoice_template(body: InvoiceTemplatePreview,
                             authorization: Optional[str] = Header(default=None)):
    """Render the (provided or stored) token-HTML with SAMPLE data → PDF preview."""
    t = tenant_from_session(authorization)
    from ..models import OfftakerInvoiceTemplate
    from .template_render import render_template_pdf, SAMPLE_CONTEXT, DEFAULT_TEMPLATE_HTML
    html = body.html
    if html is None:
        with SessionLocal() as db:
            tpl = db.execute(select(OfftakerInvoiceTemplate).where(
                OfftakerInvoiceTemplate.tenant_id == t.id)).scalars().first()
            html = (tpl.html if tpl and tpl.html else DEFAULT_TEMPLATE_HTML)
    try:
        pdf = render_template_pdf(html, SAMPLE_CONTEXT)
    except Exception as e:  # noqa: BLE001 — surface a readable message to the editor
        raise HTTPException(400, f"Couldn't render this template: {e}")
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=invoice_preview.pdf"})


@router.post("/invoice-template")
async def upload_invoice_template(file: UploadFile = File(...),
                                  authorization: Optional[str] = Header(default=None)):
    """Upload (or replace) the operator's own invoice template. Stored per-tenant;
    does NOT change what is sent today (render-from-template is gated, Stage 2)."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    data, name = await _read_template_upload(file)
    from ..models import OfftakerInvoiceTemplate
    with SessionLocal() as db:
        tpl = db.execute(select(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id == t.id)).scalars().first()
        if tpl is None:
            tpl = OfftakerInvoiceTemplate(tenant_id=t.id)
            db.add(tpl)
        tpl.filename = name[:300]
        tpl.content_type = file.content_type or "application/octet-stream"
        tpl.file_bytes = data
        lname = name.lower()
        is_excel = (lname.endswith((".xlsx", ".xls", ".xlsm"))
                    or data[:4] == _MAGIC_XLSX[:4] or data[:4] == _MAGIC_XLS)
        if lname.endswith((".html", ".htm")):
            try:
                tpl.html = data.decode("utf-8", "replace")
            except Exception:
                pass
        elif is_excel:
            # Find the invoice sheet anywhere in the workbook and seed editable
            # token-HTML from it (Stage-1; rendering stays opt-in). Never fatal —
            # a failed extract just leaves the editor on the default template.
            try:
                from .matcher import excel_to_template_html
                sheet, html = excel_to_template_html(data)
                if html:
                    tpl.html = html
                    logger.info("invoice template: seeded HTML from Excel sheet %r", sheet)
            except Exception as e:  # noqa: BLE001
                logger.warning("invoice template Excel extract failed: %s", e)
        # A freshly uploaded template is used by DEFAULT — uploading IS the opt-in,
        # so the operator doesn't have to separately flip it on (they switch to the
        # standard format with the slider). Only when it's actually renderable
        # (xlsx pixel repro, or seeded token-HTML); otherwise leave it off.
        if is_excel or tpl.html:
            tpl.enabled = True
        tpl.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(tpl)
        return {"ok": True, "template": _template_dict(tpl)}


@router.get("/invoice-template/file")
def get_invoice_template_file(authorization: Optional[str] = Header(default=None)):
    """Stream the stored original template file (inline preview / download)."""
    t = tenant_from_session(authorization)
    from ..models import OfftakerInvoiceTemplate
    with SessionLocal() as db:
        tpl = db.execute(select(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id == t.id)).scalars().first()
        if not tpl or not tpl.file_bytes:
            raise HTTPException(404, "No invoice template on file")
        ct = tpl.content_type or "application/octet-stream"
        fn = "".join(c for c in (tpl.filename or "template") if c.isalnum() or c in "._- ") or "template"
        return StreamingResponse(io.BytesIO(tpl.file_bytes), media_type=ct,
            headers={"Content-Disposition": "inline; filename=" + fn})


@router.get("/invoice-template/preview.pdf")
def get_invoice_template_preview_pdf(default: bool = False,
                                     authorization: Optional[str] = Header(default=None)):
    """Render OUR REPRODUCTION of the operator's template, the RIGHT way: the real
    uploaded file rendered by the real engine (xlsx/Word → Gotenberg/LibreOffice;
    PDF passthrough) — pixel-identical to their format. Deep-research finding: never
    reproduce a document by converting it to HTML (the lossy anti-pattern); fill/
    render the real artifact. Token-HTML is kept only as a last-resort fallback for
    image/HTML templates or when no renderer is configured.

    ?default=1 renders OUR DEFAULT invoice format (the standard Array Operator layout)
    with the same sample data instead of the reproduction, so the operator can compare
    their own template against our default."""
    t = tenant_from_session(authorization)
    if default:
        # OUR default format = the REAL Array Operator invoice renderer
        # (invoice.render_invoice_pdf — the same one a live send uses), rendered
        # from the operator's LATEST computable offtaker so the button shows their
        # actual latest invoice in our standard layout, not a generic token-HTML
        # sample. (Old behavior rendered DEFAULT_TEMPLATE_HTML + SAMPLE_CONTEXT —
        # a fake mock that never reflected the real invoice.) Falls back to that
        # sample only when the operator has no offtaker with a computable invoice.
        import tempfile, pathlib as _pl
        from . import invoice as _inv
        chosen = None
        with SessionLocal() as db:
            subs = db.execute(select(BillingReportSubscription).where(
                BillingReportSubscription.tenant_id == t.id,
                BillingReportSubscription.deleted_at.is_(None),
            ).order_by(BillingReportSubscription.id.desc())).scalars().all()
            for s in subs:
                try:
                    m = build_match(s)
                except Exception:  # noqa: BLE001
                    continue
                ci = m.computed_invoice or {}
                if m.matched and isinstance(ci.get("amount_owed"), (int, float)) and ci.get("amount_owed"):
                    chosen = m
                    break
        if chosen is not None:
            try:
                with tempfile.TemporaryDirectory(prefix="ao-defprev-") as tmp:
                    p = _inv.render_invoice_pdf(chosen, _pl.Path(tmp) / "default.pdf")
                    pdf = p.read_bytes()
                return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
                    headers={"Content-Disposition": "inline; filename=default_format_preview.pdf"})
            except Exception as e:  # noqa: BLE001
                logger.warning("default-format real-invoice render failed, falling back: %s", e)
        from .template_render import (render_template_pdf, SAMPLE_CONTEXT,
                                      DEFAULT_TEMPLATE_HTML)
        try:
            pdf = render_template_pdf(DEFAULT_TEMPLATE_HTML, SAMPLE_CONTEXT)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"Couldn't render default-format preview: {e}")
        return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
            headers={"Content-Disposition": "inline; filename=default_format_preview.pdf"})
    from ..models import OfftakerInvoiceTemplate
    with SessionLocal() as db:
        tpl = db.execute(select(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id == t.id)).scalars().first()
        if not tpl or (not tpl.file_bytes and not tpl.html):
            raise HTTPException(404, "No invoice template on file")
        fb = bytes(tpl.file_bytes) if tpl.file_bytes else b""
        name = (tpl.filename or "template").lower()
        pdf = None
        if fb[:4] == b"%PDF":
            pdf = fb                                    # already a PDF — passthrough
        elif (fb[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")
              or name.endswith((".xlsx", ".xls", ".docx", ".doc", ".odt", ".ods"))):
            # OUR reproduction (direct-cell-write pipeline), not the raw upload — for
            # xlsx, write a sample through the same path a real send uses so the pane
            # shows what our engine produces. Falls back to a plain render otherwise.
            if fb[:4] == b"PK\x03\x04":
                try:
                    from .repro.template_repro import reproduce_template_preview
                    pdf = reproduce_template_preview(fb)
                except Exception as e:  # noqa: BLE001
                    logger.warning("template reproduction preview failed: %s", e)
            if pdf is None:
                try:
                    from .repro import render as _repro_render
                    if _repro_render.renderer_available():
                        pdf = _repro_render.render_office_to_pdf(fb, tpl.filename or "template.xlsx")
                except Exception as e:  # noqa: BLE001
                    logger.warning("template preview headless render failed: %s", e)
        if pdf is None:
            from .template_render import (render_template_pdf, SAMPLE_CONTEXT,
                                          DEFAULT_TEMPLATE_HTML)
            try:
                pdf = render_template_pdf(tpl.html or DEFAULT_TEMPLATE_HTML, SAMPLE_CONTEXT)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(422, f"Couldn't render template preview: {e}")
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=template_preview.pdf"})


@router.delete("/invoice-template")
def delete_invoice_template(authorization: Optional[str] = Header(default=None)):
    """Remove the tenant's invoice template."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    from ..models import OfftakerInvoiceTemplate
    with SessionLocal() as db:
        tpl = db.execute(select(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id == t.id)).scalars().first()
        if tpl:
            db.delete(tpl)
            db.commit()
        return {"ok": True}


# ── Per-offtaker invoice template ────────────────────────────────────────────
# Each offtaker can have its OWN uploaded template; it OVERRIDES the tenant-wide
# default at render time (see delivery._effective_template_row). These mirror the
# tenant endpoints above, scoped to one subscription the caller owns.

def _owned_sub(db, t, sub_id: int):
    """Fetch a subscription the caller's tenant owns, or 404."""
    from ..models import BillingReportSubscription
    sub = db.get(BillingReportSubscription, sub_id)
    if not sub or sub.tenant_id != t.id:
        raise HTTPException(404, "Offtaker not found")
    return sub


def _seed_template_from_bytes(tpl, data: bytes, name: str, content_type):
    """Store an uploaded template's file + seed editable token-HTML (HTML verbatim;
    Excel → its invoice sheet's HTML). Mirrors the tenant upload path; never fatal."""
    tpl.filename = name[:300]
    tpl.content_type = content_type or "application/octet-stream"
    tpl.file_bytes = data
    lname = name.lower()
    is_excel = (lname.endswith((".xlsx", ".xls", ".xlsm"))
                or data[:4] == _MAGIC_XLSX[:4] or data[:4] == _MAGIC_XLS)
    if lname.endswith((".html", ".htm")):
        try:
            tpl.html = data.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
    elif is_excel:
        try:
            from .matcher import excel_to_template_html
            _sheet, html = excel_to_template_html(data)
            if html:
                tpl.html = html
        except Exception:  # noqa: BLE001
            logger.warning("per-offtaker template: Excel HTML seed failed", exc_info=True)


@router.get("/subscriptions/{sub_id}/invoice-template")
def get_sub_invoice_template(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """This offtaker's own invoice-template status + editable token-HTML (seeds the
    default template HTML when none saved, so the editor works out of the box)."""
    t = tenant_from_session(authorization)
    from ..models import OfftakerSubscriptionTemplate
    from .template_render import DEFAULT_TEMPLATE_HTML, AVAILABLE_TOKENS
    with SessionLocal() as db:
        _owned_sub(db, t, sub_id)
        tpl = db.execute(select(OfftakerSubscriptionTemplate).where(
            OfftakerSubscriptionTemplate.subscription_id == sub_id)).scalars().first()
        d = _template_dict(tpl)
        d["html"] = (tpl.html if tpl and tpl.html else DEFAULT_TEMPLATE_HTML)
        d["is_default_html"] = not (tpl and tpl.html)
        d["tokens"] = AVAILABLE_TOKENS
        return {"ok": True, "template": d}


@router.post("/subscriptions/{sub_id}/invoice-template")
async def upload_sub_invoice_template(sub_id: int, file: UploadFile = File(...),
                                      authorization: Optional[str] = Header(default=None)):
    """Upload (or replace) THIS offtaker's own invoice template. Render-from-template
    is gated behind `enabled` (the format toggle), same as the tenant default."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    data, name = await _read_template_upload(file)
    from ..models import OfftakerSubscriptionTemplate
    with SessionLocal() as db:
        _owned_sub(db, t, sub_id)
        tpl = db.execute(select(OfftakerSubscriptionTemplate).where(
            OfftakerSubscriptionTemplate.subscription_id == sub_id)).scalars().first()
        if tpl is None:
            tpl = OfftakerSubscriptionTemplate(subscription_id=sub_id, tenant_id=t.id)
            db.add(tpl)
        _seed_template_from_bytes(tpl, data, name, file.content_type)
        db.commit()
        db.refresh(tpl)
        return {"ok": True, "template": _template_dict(tpl)}


@router.put("/subscriptions/{sub_id}/invoice-template")
def put_sub_invoice_template(sub_id: int, body: InvoiceTemplatePut,
                             authorization: Optional[str] = Header(default=None)):
    """Save THIS offtaker's editable token-HTML + the enable toggle ("Use this
    template" vs "Default format"). A render failure at send falls back to standard."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    from ..models import OfftakerSubscriptionTemplate
    with SessionLocal() as db:
        _owned_sub(db, t, sub_id)
        tpl = db.execute(select(OfftakerSubscriptionTemplate).where(
            OfftakerSubscriptionTemplate.subscription_id == sub_id)).scalars().first()
        if tpl is None:
            tpl = OfftakerSubscriptionTemplate(subscription_id=sub_id, tenant_id=t.id)
            db.add(tpl)
        if body.html is not None:
            tpl.html = body.html
        if body.enabled is not None:
            tpl.enabled = bool(body.enabled)
        db.commit()
        db.refresh(tpl)
        return {"ok": True, "template": _template_dict(tpl)}


@router.delete("/subscriptions/{sub_id}/invoice-template")
def delete_sub_invoice_template(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """Remove THIS offtaker's own template (falls back to the tenant default/standard)."""
    t = tenant_from_session(authorization)
    require_not_demo(t)
    from ..models import OfftakerSubscriptionTemplate
    with SessionLocal() as db:
        _owned_sub(db, t, sub_id)
        tpl = db.execute(select(OfftakerSubscriptionTemplate).where(
            OfftakerSubscriptionTemplate.subscription_id == sub_id)).scalars().first()
        if tpl:
            db.delete(tpl)
            db.commit()
        return {"ok": True}


# ─── file library — every stored file we hold for the operator ───────────────
# A small repository for the Offtaker Invoice Generator: the operator's uploaded
# invoice template, any uploaded billing workbooks, and captured GMP utility-bill
# PDFs — newest first, so the UI features the latest upload. Each entry carries a
# `download` URL the frontend fetches (with the session bearer) and opens as a blob.

_BILLING_BASE = "/v1/array-operator/billing"


@router.get("/files")
def list_files(authorization: Optional[str] = Header(default=None)):
    """List every stored file for this operator, newest-first."""
    t = tenant_from_session(authorization)
    from ..models import OfftakerInvoiceTemplate, Bill, UtilityAccount
    files: list[dict] = []
    with SessionLocal() as db:
        tpl = db.execute(select(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id == t.id)).scalars().first()
        if tpl and tpl.file_bytes:
            ts = tpl.updated_at or tpl.created_at
            files.append({
                "key": "template", "kind": "template",
                "name": tpl.filename or "invoice template",
                "role": "Invoice template" + (" · in use" if tpl.enabled else ""),
                "content_type": tpl.content_type,
                "size": len(tpl.file_bytes),
                "uploaded_at": ts.isoformat() if ts else None,
                "download": f"{_BILLING_BASE}/invoice-template/file",
            })
        subs = db.execute(select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == t.id,
            BillingReportSubscription.deleted_at.is_(None))).scalars().all()
        for s in subs:
            if s.source_workbook:
                files.append({
                    "key": f"workbook-{s.id}", "kind": "workbook",
                    "name": s.source_filename or "billing workbook.xlsx",
                    "role": f"Billing workbook · {s.customer_name}",
                    "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "size": len(s.source_workbook),
                    "uploaded_at": s.updated_at.isoformat() if s.updated_at else None,
                    "download": f"{_BILLING_BASE}/files/workbook/{s.id}",
                })
        bills = db.execute(
            select(Bill, UtilityAccount.nickname)
            .join(UtilityAccount, Bill.account_id == UtilityAccount.id)
            .where(Bill.tenant_id == t.id, Bill.pdf_bytes.isnot(None))
            .order_by(Bill.bill_date.desc().nullslast()).limit(24)
        ).all()
        for b, nick in bills:
            per = b.bill_date.strftime("%Y-%m") if b.bill_date else "bill"
            label = nick or f"GMP {b.account_id}"
            ts = b.pulled_at or b.bill_date
            files.append({
                "key": f"gmp-{b.id}", "kind": "gmp_bill",
                "name": f"GMP bill — {label} {per}.pdf",
                "role": f"GMP utility bill · {label}",
                "content_type": b.pdf_content_type or "application/pdf",
                "size": len(b.pdf_bytes) if b.pdf_bytes else 0,
                "uploaded_at": ts.isoformat() if ts else None,
                "download": f"{_BILLING_BASE}/files/gmp-bill/{b.id}",
            })
    files.sort(key=lambda f: f.get("uploaded_at") or "", reverse=True)
    return {"files": files, "count": len(files)}


@router.get("/files/workbook/{sub_id}")
def get_workbook_file(sub_id: int, authorization: Optional[str] = Header(default=None)):
    """Stream an uploaded billing workbook (inline view / download)."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        s = _get_owned(db, t.id, sub_id)
        if not s.source_workbook:
            raise HTTPException(404, "No workbook on file")
        fn = "".join(c for c in (s.source_filename or "workbook.xlsx")
                     if c.isalnum() or c in "._- ") or "workbook.xlsx"
        return StreamingResponse(io.BytesIO(s.source_workbook),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "inline; filename=" + fn})


@router.get("/files/gmp-bill/{bill_id}")
def get_gmp_bill_file(bill_id: int, authorization: Optional[str] = Header(default=None)):
    """Stream a captured GMP utility-bill PDF (inline view / download)."""
    t = tenant_from_session(authorization)
    from ..models import Bill
    with SessionLocal() as db:
        b = db.get(Bill, bill_id)
        if not b or b.tenant_id != t.id or not b.pdf_bytes:
            raise HTTPException(404, "No GMP bill PDF on file")
        return StreamingResponse(io.BytesIO(b.pdf_bytes),
            media_type=b.pdf_content_type or "application/pdf",
            headers={"Content-Disposition": "inline; filename=gmp_bill.pdf"})
