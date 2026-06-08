"""
Verify-accuracy router.

Operators upload their own records (spreadsheet, PDF, etc.) and view them
side-by-side against the SO-generated workbook to confirm accuracy.

Auth: same Bearer session as the dashboard (account.tenant_from_session).
Storage: api/storage/verification/<tenant_id>/<uuid>.<ext>  (25 MB limit)
"""
from __future__ import annotations

import pathlib
import re
import uuid
import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from .account import tenant_from_session, require_not_demo
from .db import SessionLocal
from .models import Client, Array, VerificationCheck, now

logger = logging.getLogger(__name__)

router = APIRouter()

STORAGE_ROOT = pathlib.Path(__file__).parent / "storage" / "verification"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

ALLOWED_EXTENSIONS = {
    ".xlsx", ".xls", ".csv", ".pdf", ".png", ".jpg", ".jpeg",
}
MIME_MAP = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _safe_ext(filename: str) -> str:
    """Return lowercase extension or raise."""
    ext = pathlib.Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file type '{ext}'. Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    return ext


def _check_to_dict(c: VerificationCheck) -> dict:
    return {
        "id": c.id,
        "tenant_id": c.tenant_id,
        "client_id": c.client_id,
        "array_id": c.array_id,
        "uploaded_filename": c.uploaded_filename,
        "uploaded_mime": c.uploaded_mime,
        "period_label": c.period_label,
        "status": c.status,
        "operator_note": c.operator_note,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
    }


def _parse_period_label(label: str) -> Optional[date]:
    """Parse period_label into a reference_date for build_workbook.

    Accepted formats:
      'Q1 2026' | '2026-Q1'  -> first day of following quarter (so Q1 is the last shown)
      '2026-03'              -> same logic via the quarter that month belongs to
    Returns None if the label can't be parsed (caller falls back to today).
    """
    s = (label or "").strip()
    # 'Q1 2026' or '2026-Q1'
    m = re.match(r'(?:Q([1-4])\s+(\d{4})|(\d{4})-Q([1-4]))', s, re.I)
    if m:
        q = int(m.group(1) or m.group(4))
        yr = int(m.group(2) or m.group(3))
        nq = q + 1
        if nq > 4:
            return date(yr + 1, 1, 1)
        return date(yr, (nq - 1) * 3 + 1, 1)
    # 'YYYY-MM'
    m2 = re.match(r'^(\d{4})-(\d{2})$', s)
    if m2:
        yr, mo = int(m2.group(1)), int(m2.group(2))
        q = (mo - 1) // 3 + 1
        nq = q + 1
        if nq > 4:
            return date(yr + 1, 1, 1)
        return date(yr, (nq - 1) * 3 + 1, 1)
    return None


# ─── upload ──────────────────────────────────────────────────────────────────

@router.post("/v1/verification/upload")
async def upload_verification(
    file: UploadFile = File(...),
    client_id: int = Form(...),
    period_label: str = Form(...),
    array_id: Optional[int] = Form(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """Accept an operator-uploaded file for side-by-side comparison."""
    t = tenant_from_session(authorization)
    require_not_demo(t)

    ext = _safe_ext(file.filename or "upload.bin")
    mime = MIME_MAP.get(ext, "application/octet-stream")

    # Validate ownership
    with SessionLocal() as db:
        client = db.execute(
            select(Client).where(
                Client.id == client_id,
                Client.tenant_id == t.id,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not client:
            raise HTTPException(404, "Client not found")
        if array_id is not None:
            arr = db.execute(
                select(Array).where(
                    Array.id == array_id,
                    Array.tenant_id == t.id,
                    Array.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if not arr:
                raise HTTPException(404, "Array not found")

    # Read body + enforce size limit
    body = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds 25 MB limit")

    # Persist to disk
    dest_dir = STORAGE_ROOT / t.id
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = dest_dir / filename
    dest.write_bytes(body)

    with SessionLocal() as db:
        check = VerificationCheck(
            tenant_id=t.id,
            client_id=client_id,
            array_id=array_id,
            uploaded_filename=file.filename or filename,
            uploaded_mime=mime,
            storage_path=str(dest),
            period_label=period_label.strip()[:20],
            status="pending",
        )
        db.add(check)
        db.commit()
        db.refresh(check)
        return _check_to_dict(check)


# ─── list ─────────────────────────────────────────────────────────────────────

@router.get("/v1/verification")
def list_verifications(
    client_id: int,
    authorization: Optional[str] = Header(default=None),
):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        rows = db.execute(
            select(VerificationCheck)
            .where(
                VerificationCheck.tenant_id == t.id,
                VerificationCheck.client_id == client_id,
            )
            .order_by(VerificationCheck.created_at.desc())
        ).scalars().all()
        return {"checks": [_check_to_dict(r) for r in rows]}


# ─── detail ───────────────────────────────────────────────────────────────────

@router.get("/v1/verification/{check_id}")
def get_verification(
    check_id: int,
    authorization: Optional[str] = Header(default=None),
):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        check = db.execute(
            select(VerificationCheck).where(
                VerificationCheck.id == check_id,
                VerificationCheck.tenant_id == t.id,
            )
        ).scalar_one_or_none()
        if not check:
            raise HTTPException(404, "Verification check not found")
        return _check_to_dict(check)


# ─── stream uploaded file ─────────────────────────────────────────────────────

@router.get("/v1/verification/{check_id}/uploaded-file")
def get_uploaded_file(
    check_id: int,
    authorization: Optional[str] = Header(default=None),
):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        check = db.execute(
            select(VerificationCheck).where(
                VerificationCheck.id == check_id,
                VerificationCheck.tenant_id == t.id,
            )
        ).scalar_one_or_none()
        if not check:
            raise HTTPException(404, "Verification check not found")
        path = pathlib.Path(check.storage_path)
        if not path.exists():
            raise HTTPException(404, "Uploaded file not found on disk")
        # Safety check: must be under STORAGE_ROOT
        if not str(path.resolve()).startswith(str(STORAGE_ROOT.resolve())):
            raise HTTPException(403, "Access denied")
        return FileResponse(
            str(path),
            media_type=check.uploaded_mime,
            headers={
                "Content-Disposition": f'inline; filename="{check.uploaded_filename}"',
            },
        )


# ─── stream SO workbook ───────────────────────────────────────────────────────

@router.get("/v1/verification/{check_id}/so-workbook")
def get_so_workbook(
    check_id: int,
    authorization: Optional[str] = Header(default=None),
):
    """Generate and stream the SO workbook for the client+period in this check."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        check = db.execute(
            select(VerificationCheck).where(
                VerificationCheck.id == check_id,
                VerificationCheck.tenant_id == t.id,
            )
        ).scalar_one_or_none()
        if not check:
            raise HTTPException(404, "Verification check not found")
        client_id = check.client_id
        period_label = check.period_label

    reference_date = _parse_period_label(period_label)

    from .writers import build_workbook
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp:
        out_path = pathlib.Path(tmp) / "so_workbook.xlsx"
        try:
            built = build_workbook(
                client_id=client_id,
                out_path=out_path,
                reference_date=reference_date,
            )
        except ValueError as e:
            raise HTTPException(
                422,
                f"Cannot generate SO workbook: {e}. "
                "No bill data has been captured for this client yet.",
            ) from e
        except Exception as e:
            logger.exception("so-workbook build failed for check %s", check_id)
            raise HTTPException(500, f"Workbook generation failed: {e}") from e

        if not built.exists():
            raise HTTPException(422, "Workbook generation produced no output")

        data = built.read_bytes()

    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'inline; filename="so_workbook_{period_label}.xlsx"',
        },
    )


# ─── resolve ──────────────────────────────────────────────────────────────────

class ResolveBody(BaseModel):
    status: str  # 'confirmed' | 'flagged'
    note: Optional[str] = None


@router.post("/v1/verification/{check_id}/resolve")
def resolve_verification(
    check_id: int,
    body: ResolveBody,
    authorization: Optional[str] = Header(default=None),
):
    t = tenant_from_session(authorization)
    require_not_demo(t)

    if body.status not in ("confirmed", "flagged"):
        raise HTTPException(400, "status must be 'confirmed' or 'flagged'")

    with SessionLocal() as db:
        check = db.execute(
            select(VerificationCheck).where(
                VerificationCheck.id == check_id,
                VerificationCheck.tenant_id == t.id,
            )
        ).scalar_one_or_none()
        if not check:
            raise HTTPException(404, "Verification check not found")
        check.status = body.status
        check.operator_note = (body.note or "").strip() or None
        check.resolved_at = now()
        db.commit()
        db.refresh(check)
        return _check_to_dict(check)
