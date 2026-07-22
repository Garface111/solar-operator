"""FastAPI routes for Performance Verification (Array Operator).

Paths are full (no router prefix). Auth matches other array-owner endpoints:
session token preferred, tenant-key bearer fallback.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Array, Tenant
from .engine import (
    build_array_verification,
    build_month_verification,
    build_portfolio_verification,
)
from .auditor import auditor_zip_bytes
from .intervention import build_intervention_verification
from .report_pack import render_verification_pdf
from .standards import (
    DEFAULT_DEVIATION_THRESHOLD,
    IEC_ALIGNMENT_NOTE,
    METHOD_SUMMARY,
    REPORT_FOOTER,
)

router = APIRouter(tags=["perf-verification"])

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _tenant_from_bearer(authorization: str | None) -> Tenant:
    """Prefer session; fall back to tenant-key (same dual-auth as array_owners).

    Importing ``array_owners._tenant_from_bearer`` can pull a large module graph;
    we re-use it when available and otherwise mirror the small helper.
    """
    try:
        from ..array_owners import _tenant_from_bearer as _ao_tenant
        return _ao_tenant(authorization)
    except ImportError:
        pass
    from ..account import tenant_from_session
    from ..array_owners import _capture_tenant_by_key

    try:
        return tenant_from_session(authorization)
    except HTTPException:
        pass
    return _capture_tenant_by_key(authorization)


def _parse_period(period: str | None) -> str | None:
    if period is None or period == "":
        return None
    if not _PERIOD_RE.match(period):
        raise HTTPException(400, "period must be YYYY-MM")
    y, m = period.split("-")
    mi = int(m)
    if mi < 1 or mi > 12:
        raise HTTPException(400, "period month must be 01–12")
    return f"{int(y):04d}-{mi:02d}"


def _parse_iso(d: str, name: str) -> date:
    if not _ISO_RE.match(d):
        raise HTTPException(400, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(d)
    except ValueError as e:
        raise HTTPException(400, f"invalid {name}: {e}") from e


def _window_days(window_days: int) -> int:
    if window_days < 1 or window_days > 366:
        raise HTTPException(400, "window_days must be 1–366")
    return window_days


class VerificationSettingsBody(BaseModel):
    enabled: Optional[bool] = None
    deviation_threshold: Optional[float] = Field(default=None, ge=0.01, le=0.5)


def _settings_dict(tenant: Tenant) -> dict[str, Any]:
    thr = getattr(tenant, "verification_deviation_threshold", None)
    return {
        "enabled": bool(getattr(tenant, "verification_reports_enabled", True)),
        "deviation_threshold": (
            float(thr) if thr is not None else DEFAULT_DEVIATION_THRESHOLD
        ),
        "deviation_threshold_is_default": thr is None,
        "standards_note": IEC_ALIGNMENT_NOTE,
    }


@router.get("/v1/array-owners/verification/summary")
def verification_summary(
    window_days: int = 30,
    authorization: str | None = Header(default=None),
):
    tenant = _tenant_from_bearer(authorization)
    wd = _window_days(window_days)
    return build_portfolio_verification(tenant, window_days=wd)


@router.get("/v1/array-owners/verification/arrays/{array_id}")
def verification_array(
    array_id: int,
    window_days: int = 30,
    authorization: str | None = Header(default=None),
):
    tenant = _tenant_from_bearer(authorization)
    wd = _window_days(window_days)
    thr = getattr(tenant, "verification_deviation_threshold", None)
    threshold = float(thr) if thr is not None else DEFAULT_DEVIATION_THRESHOLD
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(
                Array.id == array_id,
                Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if arr is None:
            raise HTTPException(404, "array not found")
        return build_array_verification(
            db, arr, window_days=wd, threshold=threshold
        )


@router.get("/v1/array-owners/verification/report")
def verification_report_json(
    period: str | None = None,
    authorization: str | None = Header(default=None),
):
    tenant = _tenant_from_bearer(authorization)
    p = _parse_period(period)
    return build_month_verification(tenant, period=p)


@router.get("/v1/array-owners/verification/report.pdf")
def verification_report_pdf(
    period: str | None = None,
    authorization: str | None = Header(default=None),
):
    tenant = _tenant_from_bearer(authorization)
    p = _parse_period(period)
    snap = build_month_verification(tenant, period=p)
    try:
        pdf = render_verification_pdf(snap)
    except RuntimeError as e:
        return JSONResponse(
            status_code=501,
            content={"detail": str(e), "period": snap.get("period")},
        )
    except Exception as e:
        return JSONResponse(
            status_code=501,
            content={
                "detail": f"PDF generation unavailable: {e}",
                "period": snap.get("period"),
            },
        )
    filename = f"verification-{snap.get('period', 'report')}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/v1/array-owners/verification/auditor-export")
def verification_auditor_export(
    start: str,
    end: str,
    authorization: str | None = Header(default=None),
):
    from datetime import timedelta

    tenant = _tenant_from_bearer(authorization)
    start_d = _parse_iso(start, "start")
    end_d = _parse_iso(end, "end")
    if end_d < start_d:
        raise HTTPException(400, "end must be on or after start")
    if (end_d - start_d).days > 400:
        raise HTTPException(400, "range too large (max ~13 months)")
    window_days = (end_d - start_d).days + 1
    synth_today = end_d + timedelta(days=1)
    snap = build_portfolio_verification(
        tenant, window_days=window_days, today=synth_today
    )
    zbytes = auditor_zip_bytes(snap)
    filename = f"verification-auditor-{start_d.isoformat()}_{end_d.isoformat()}.zip"
    return Response(
        content=zbytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/v1/array-owners/verification/method")
def verification_method(
    authorization: str | None = Header(default=None),
):
    _tenant_from_bearer(authorization)
    return {
        "method": METHOD_SUMMARY,
        "footer": REPORT_FOOTER,
        "alignment": IEC_ALIGNMENT_NOTE,
        "default_deviation_threshold": DEFAULT_DEVIATION_THRESHOLD,
    }


@router.get("/v1/array-owners/verification/settings")
def verification_settings_get(
    authorization: str | None = Header(default=None),
):
    tenant = _tenant_from_bearer(authorization)
    return _settings_dict(tenant)


@router.put("/v1/array-owners/verification/settings")
def verification_settings_put(
    body: VerificationSettingsBody,
    authorization: str | None = Header(default=None),
):
    tenant = _tenant_from_bearer(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        if t is None:
            raise HTTPException(404, "tenant not found")
        if body.enabled is not None:
            t.verification_reports_enabled = bool(body.enabled)
        if body.deviation_threshold is not None:
            t.verification_deviation_threshold = float(body.deviation_threshold)
        db.commit()
        db.refresh(t)
        return _settings_dict(t)


@router.get("/v1/array-owners/verification/interventions/{repair_ticket_id}")
def verification_intervention(
    repair_ticket_id: int,
    authorization: str | None = Header(default=None),
):
    tenant = _tenant_from_bearer(authorization)
    result = build_intervention_verification(tenant, repair_ticket_id)
    if result.get("reason") == "ticket_not_found":
        raise HTTPException(404, "repair ticket not found")
    return result
