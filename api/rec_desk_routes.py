"""REC Desk HTTP routes — ownership + readiness (Marketplace RECs sub-tab).

No brokerage, no GIS transfer, no money movement.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from .db import SessionLocal
from .models import Array
from . import rec_desk as desk

router = APIRouter()


def _tenant(authorization: str | None):
    from .array_owners import _tenant_from_bearer
    return _tenant_from_bearer(authorization)


@router.get("/v1/array-owners/rec-desk")
def get_rec_desk(authorization: str | None = Header(default=None)) -> dict:
    """Fleet-wide REC ownership + readiness + expected inventory."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        return desk.tenant_rec_desk(db, tenant.id)


class RecPositionBody(BaseModel):
    ownership: str | None = None
    ownership_note: str | None = None
    verifier_name: str | None = None
    nepool_gis_id: str | None = None  # optional convenience write-through
    cert_registry: str | None = None


@router.patch("/v1/array-owners/arrays/{array_id}/rec-position")
def patch_rec_position(array_id: int, body: RecPositionBody,
                       authorization: str | None = Header(default=None)) -> dict:
    """Set REC ownership / verifier on one array. Never claims a sale."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant.id or arr.deleted_at is not None:
            raise HTTPException(404, "Array not found")
        if body.ownership is not None:
            own = body.ownership.strip().lower()
            if own not in desk.OWNERSHIP_VALUES:
                raise HTTPException(
                    422, f"ownership must be one of {sorted(desk.OWNERSHIP_VALUES)}")
            arr.rec_ownership = own
        if body.ownership_note is not None:
            arr.rec_ownership_note = body.ownership_note.strip() or None
        if body.verifier_name is not None:
            arr.rec_verifier_name = body.verifier_name.strip() or None
        if body.nepool_gis_id is not None:
            arr.nepool_gis_id = body.nepool_gis_id.strip() or None
        if body.cert_registry is not None:
            arr.cert_registry = body.cert_registry.strip() or None
        db.commit()
        db.refresh(arr)
        return {"ok": True, "array": desk.array_readiness(db, arr)}
