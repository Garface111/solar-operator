"""
Sandbox canvas API — client graph visualization with persisted positions.

GET  /v1/sandbox/canvas     → full client graph (clients → accounts → arrays)
PATCH /v1/sandbox/positions → persist dragged node positions
POST /v1/sandbox/merge      → merge client B into A (thin convenience wrapper)
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from .account import tenant_from_session
from .db import SessionLocal
from .models import Array, Client, UtilityAccount, now

router = APIRouter()


# ── GET /v1/sandbox/canvas ───────────────────────────────────────────────────

def _fmt_account(acc: UtilityAccount, arr: Optional[Array]) -> dict:
    return {
        "id": acc.id,
        "provider": acc.provider,
        "account_number": acc.account_number,
        "service_address": acc.service_address,
        "canvas_x": getattr(acc, "canvas_x", None),
        "canvas_y": getattr(acc, "canvas_y", None),
        "canvas_pinned": getattr(acc, "canvas_pinned", False) or False,
        "array_id": arr.id if arr else None,
        "array_name": arr.name if arr else None,
        "nepool_gis_id": arr.nepool_gis_id if arr else None,
    }


@router.get("/v1/sandbox/canvas")
def get_canvas(authorization: Optional[str] = Header(default=None)):
    """Return the tenant's full client → account → array graph with saved positions."""
    tenant = tenant_from_session(authorization)
    with SessionLocal() as db:
        clients = db.execute(
            select(Client).where(
                Client.tenant_id == tenant.id,
                Client.deleted_at.is_(None),
            ).order_by(Client.created_at)
        ).scalars().all()

        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
            )
        ).scalars().all()
        array_map: dict[int, Array] = {a.id: a for a in arrays}

        accounts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant.id,
                UtilityAccount.deleted_at.is_(None),
            ).order_by(UtilityAccount.id)
        ).scalars().all()

        # Group accounts by which array they belong to
        accs_by_array: dict[int, list[UtilityAccount]] = defaultdict(list)
        unclassified: list[UtilityAccount] = []

        for acc in accounts:
            if acc.array_id is None:
                unclassified.append(acc)
                continue
            arr = array_map.get(acc.array_id)
            if arr is None or arr.client_id is None:
                unclassified.append(acc)
            else:
                accs_by_array[arr.id].append(acc)

        # Group arrays by client
        arrays_by_client: dict[int, list[Array]] = defaultdict(list)
        for arr in arrays:
            if arr.client_id is not None:
                arrays_by_client[arr.client_id].append(arr)

        # Build client output
        clients_out = []
        for c in clients:
            c_accs = []
            for arr in arrays_by_client.get(c.id, []):
                for acc in accs_by_array.get(arr.id, []):
                    c_accs.append(_fmt_account(acc, arr))
            clients_out.append({
                "id": c.id,
                "name": c.name,
                "canvas_x": getattr(c, "canvas_x", None),
                "canvas_y": getattr(c, "canvas_y", None),
                "canvas_pinned": getattr(c, "canvas_pinned", False) or False,
                "accounts": c_accs,
            })

        # Build unclassified output
        unclassified_out = [
            _fmt_account(acc, array_map.get(acc.array_id) if acc.array_id else None)
            for acc in unclassified
        ]

    return {"clients": clients_out, "unclassified": unclassified_out}


# ── PATCH /v1/sandbox/positions ──────────────────────────────────────────────

class PositionUpdate(BaseModel):
    node_type: str  # 'client' | 'account'
    node_id: int
    x: float
    y: float


@router.patch("/v1/sandbox/positions")
def patch_positions(
    updates: list[PositionUpdate],
    authorization: Optional[str] = Header(default=None),
):
    """Persist dragged node positions. Silently ignores unknown/foreign IDs."""
    tenant = tenant_from_session(authorization)
    with SessionLocal() as db:
        for u in updates:
            if u.node_type == "client":
                obj = db.get(Client, u.node_id)
                if obj and obj.tenant_id == tenant.id and obj.deleted_at is None:
                    obj.canvas_x = u.x
                    obj.canvas_y = u.y
            elif u.node_type == "account":
                obj = db.get(UtilityAccount, u.node_id)
                if obj and obj.tenant_id == tenant.id and obj.deleted_at is None:
                    obj.canvas_x = u.x
                    obj.canvas_y = u.y
        db.commit()
    return {"ok": True}


# ── POST /v1/sandbox/merge ────────────────────────────────────────────────────

class SandboxMergeBody(BaseModel):
    src_client_id: int  # will be soft-deleted
    dst_client_id: int  # survives, inherits all arrays


@router.post("/v1/sandbox/merge")
def sandbox_merge(
    body: SandboxMergeBody,
    authorization: Optional[str] = Header(default=None),
):
    """Merge src into dst: reparent all arrays, soft-delete src.
    For full login-credential merging use POST /v1/account/clients/{src}/merge-into."""
    tenant = tenant_from_session(authorization)
    if body.src_client_id == body.dst_client_id:
        raise HTTPException(400, "src and dst must differ")

    with SessionLocal() as db:
        src = db.execute(
            select(Client).where(
                Client.tenant_id == tenant.id,
                Client.id == body.src_client_id,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        dst = db.execute(
            select(Client).where(
                Client.tenant_id == tenant.id,
                Client.id == body.dst_client_id,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not src or not dst:
            raise HTTPException(404, "client not found")

        for arr in db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id,
                Array.client_id == src.id,
            )
        ).scalars().all():
            arr.client_id = dst.id

        src.deleted_at = now()
        db.commit()

    return {"ok": True, "dst_client_id": dst.id, "merged_from_id": src.id}
