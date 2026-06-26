"""Array Operator — OPERATOR/TENANT-level generation-spreadsheet tracker.

Mounted under /v1/array-operator/tracker.

This mirrors the PER-SUBSCRIPTION tracker (api/billing/routes.py
`/subscriptions/{id}/tracker`, storage on BillingReportSubscription.tracker_*)
but keys the stored sheet to the TENANT instead of one offtaker. The operator
uploads ONE running generation spreadsheet for their whole operation — in
whatever columns they already use — and we:
  * detect its structure ("our magic") with the SAME deterministic, offline
    header heuristic the per-sub tracker uses (api.billing.sheet_tracker), then
  * store it on the tenant, and let them download or remove it.

v1 scope is deliberately upload / detect-columns / store / download / remove.
There is NO auto-append in v1 (deferred) — this surface never reads or writes
any billing computation, so it cannot affect the live invoice/billing path.

Design notes (kept consistent with the per-sub tracker):
  * AUTH — `tenant_from_session` + `require_not_demo`, the same bearer used
    everywhere else. Mutating routes refuse the shared demo tenant.
  * GATE — the whole feature is gated behind SPREADSHEET_TRACKER_ENABLED (the
    same flag as the per-sub tracker). When OFF, GET returns {enabled:false}
    (the UI hides the card) and the mutating routes 404, so a half-built state
    can never disturb the live page. When ON, GET returns enabled:true for a
    signed-in non-demo operator (the card shows by default) with
    has_sheet:false until one is uploaded.
  * RESPONSE SHAPE — IDENTICAL to the per-sub tracker's `_tracker_status_dict`
    so the existing frontend renderer is reusable verbatim.
  * STORAGE — the 4 nullable Tenant.tracker_* columns (additive migration); the
    xlsx bytes + detected mapping + original filename + updated_at.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse

from .db import SessionLocal
from .models import Tenant
from .account import tenant_from_session, require_not_demo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/array-operator/tracker",
                   tags=["array-operator-tracker"])

# Mirror the per-sub tracker's limits / type guards (api/billing/routes.py).
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB — these sheets are tens of KB
_MAGIC_XLSX = b"PK\x03\x04"          # ZIP / OpenXML (.xlsx, .xlsm, …)
_MAGIC_XLS = b"\xd0\xcf\x11\xe0"     # OLE2 compound doc (.xls, …)
_XLSX_MEDIA = ("application/vnd.openxmlformats-officedocument"
               ".spreadsheetml.sheet")


def _status_dict(t: Tenant) -> dict:
    """Tenant-level tracker card state. Byte-for-byte the SAME shape the per-sub
    tracker returns (`_tracker_status_dict`) so the frontend renderer is reused.
    Honest about whether a sheet is attached + what we detected."""
    m = getattr(t, "tracker_map", None) or {}
    has = bool(getattr(t, "tracker_workbook", None)) and bool(m.get("ok"))
    up = getattr(t, "tracker_updated_at", None)
    return {
        "enabled": True,
        "has_sheet": has,
        "filename": getattr(t, "tracker_filename", None),
        "columns": m.get("columns") if has else None,
        "headers": m.get("headers") if has else None,
        "header_row": m.get("header_row") if has else None,
        "sheet": m.get("sheet") if has else None,
        "data_rows": m.get("data_rows") if has else None,
        "last_period": m.get("last_period") if has else None,
        "updated_at": up.isoformat() + "Z" if up else None,
        "warnings": m.get("warnings") or [],
    }


def _get_tenant(db, tenant_id: str) -> Tenant:
    t = db.get(Tenant, tenant_id)
    if t is None:
        raise HTTPException(404, "Tenant not found")
    return t


@router.get("")
def tracker_status(authorization: Optional[str] = Header(default=None)):
    """Operator-level tracker state (drives the card). Returns {enabled:false}
    when the feature flag is off so the UI hides; otherwise enabled:true with
    has_sheet:false until one is uploaded."""
    from .billing.sheet_tracker import tracker_enabled
    t = tenant_from_session(authorization)
    if not tracker_enabled():
        return {"ok": True, "tracker": {"enabled": False}}
    with SessionLocal() as db:
        ten = _get_tenant(db, t.id)
        return {"ok": True, "tracker": _status_dict(ten)}


@router.post("")
async def tracker_upload(file: UploadFile = File(...),
                         authorization: Optional[str] = Header(default=None)):
    """Upload the operator's existing generation spreadsheet (XLSX or CSV). We
    detect its structure ('our magic'), normalize to xlsx, and store it on the
    tenant. Returns the detected mapping for review — same shape as GET."""
    from .billing.sheet_tracker import tracker_enabled, ingest_upload
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
    res = ingest_upload(raw, name)
    if not res.get("ok"):
        warn = "; ".join(res.get("warnings") or []) or "Couldn't read that sheet."
        raise HTTPException(422, warn)
    with SessionLocal() as db:
        ten = _get_tenant(db, t.id)
        ten.tracker_workbook = res["workbook"]
        ten.tracker_filename = name
        ten.tracker_map = res["mapping"]
        ten.tracker_updated_at = datetime.utcnow()
        db.add(ten)
        db.commit()
        db.refresh(ten)
        return {"ok": True, "tracker": _status_dict(ten)}


@router.get("/download")
def tracker_download(authorization: Optional[str] = Header(default=None)):
    """Stream the stored operator generation spreadsheet (404 if none). v1 does
    NOT append on download — it streams the file exactly as uploaded."""
    from .billing.sheet_tracker import tracker_enabled
    t = tenant_from_session(authorization)
    if not tracker_enabled():
        raise HTTPException(404, "Spreadsheet tracker is not enabled.")
    with SessionLocal() as db:
        ten = _get_tenant(db, t.id)
        if not getattr(ten, "tracker_workbook", None):
            raise HTTPException(404, "No spreadsheet uploaded yet.")
        blob = bytes(ten.tracker_workbook)
        base = getattr(ten, "tracker_filename", None) or "generation.xlsx"
        if base.lower().endswith(".csv"):
            base = base[:-4] + ".xlsx"
        elif not base.lower().endswith(".xlsx"):
            base = base + ".xlsx"
    return StreamingResponse(
        io.BytesIO(blob), media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{base}"'})


@router.delete("")
def tracker_remove(authorization: Optional[str] = Header(default=None)):
    """Detach the operator's BYO sheet. Returns {enabled:true, has_sheet:false}."""
    from .billing.sheet_tracker import tracker_enabled
    t = tenant_from_session(authorization)
    require_not_demo(t)
    if not tracker_enabled():
        raise HTTPException(404, "Spreadsheet tracker is not enabled.")
    with SessionLocal() as db:
        ten = _get_tenant(db, t.id)
        ten.tracker_workbook = None
        ten.tracker_filename = None
        ten.tracker_map = None
        ten.tracker_updated_at = datetime.utcnow()
        db.add(ten)
        db.commit()
        db.refresh(ten)
        return {"ok": True, "tracker": _status_dict(ten)}
