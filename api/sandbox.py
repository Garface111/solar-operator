"""
Sandbox canvas API — client graph visualization with persisted positions.

GET  /v1/sandbox/canvas     → full client graph (clients → accounts → arrays)
PATCH /v1/sandbox/positions → persist dragged node positions
POST /v1/sandbox/merge      → merge client B into A (thin convenience wrapper)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from .account import tenant_from_session, require_not_demo
from .db import SessionLocal
from .fuels import normalize_fuel
from .models import Array, Bill, Client, UtilityAccount, now
from .email_templates import quarter_context

HARD_DELETE_GRACE_DAYS = 30

router = APIRouter()


# ── GET /v1/sandbox/canvas ───────────────────────────────────────────────────

def _fmt_account(acc: UtilityAccount, arr: Optional[Array], mwh_per_qtr: Optional[float] = None) -> dict:
    return {
        "id": acc.id,
        "provider": acc.provider,
        "account_number": acc.account_number,
        "customer_number": getattr(acc, "customer_number", None),
        "service_address": acc.service_address,
        "canvas_x": getattr(acc, "canvas_x", None),
        "canvas_y": getattr(acc, "canvas_y", None),
        "canvas_pinned": getattr(acc, "canvas_pinned", False) or False,
        "array_id": arr.id if arr else None,
        "array_name": arr.name if arr else None,
        "nepool_gis_id": arr.nepool_gis_id if arr else None,
        # V2 fuel so the canvas FuelBadge can show non-solar arrays (it was
        # never emitted, so every array rendered as solar — June 2026 fix).
        "fuel_type": (getattr(arr, "fuel_type", None) or "solar") if arr else None,
        # MWh/qtr — most recent complete quarter, summed across all Bill rows
        # whose account_id points at this UtilityAccount. None when no
        # generation data has landed yet (clean dash in the UI, not "0 MWh").
        "mwh_per_qtr": mwh_per_qtr,
        "login_origin_client_id": getattr(acc, "login_origin_client_id", None),
        "array_reassigned_at": arr.reassigned_at.isoformat() if arr and arr.reassigned_at else None,
        "array_deleted_at": arr.deleted_at.isoformat() if arr and arr.deleted_at else None,
    }


@router.get("/v1/sandbox/canvas")
def get_canvas(authorization: Optional[str] = Header(default=None)):
    """Return the tenant's full client → account → array graph with saved positions.

    Deleted arrays are excluded entirely — once an operator deletes an array
    out of the sandbox it disappears immediately (no "ghost" purge-countdown
    row). The soft-delete row still lives in the DB so the in-session undo
    button (Cmd+Z, token-based) can restore it, but the canvas never surfaces
    deleted arrays or their accounts. (Ford, Jun 9 '26: deleted arrays must be
    permanently gone from the sandbox; undo still works.)
    """
    tenant = tenant_from_session(authorization)
    with SessionLocal() as db:
        clients = db.execute(
            select(Client).where(
                Client.tenant_id == tenant.id,
                Client.deleted_at.is_(None),
            ).order_by(Client.created_at)
        ).scalars().all()

        # Live arrays only — soft-deleted arrays are never shown in the sandbox.
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id,
                Array.deleted_at.is_(None),
            )
        ).scalars().all()
        array_map: dict[int, Array] = {a.id: a for a in arrays}

        # Live accounts only — no ghost rows for deleted arrays.
        accounts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant.id,
                UtilityAccount.deleted_at.is_(None),
            ).order_by(UtilityAccount.id)
        ).scalars().all()

        # ── MWh/qtr per UtilityAccount (Bruce June 6: "0 MWh/qtr" bug) ────
        # Bills land per UtilityAccount keyed by document_number. The card
        # footer shows the most-recent COMPLETE quarter's generation in MWh.
        # We sum Bill.kwh_generated where period_start (or bill_date as fallback)
        # falls inside that quarter, then divide by 1000. If no bills have
        # landed for an account, mwh_per_qtr stays None — the UI renders
        # "— MWh/qtr" instead of a misleading 0.
        qc = quarter_context()
        q_start_dt = datetime.combine(qc["_start_date"], datetime.min.time())
        q_end_dt = datetime.combine(qc["_end_date"], datetime.max.time())
        bills_in_q = db.execute(
            select(Bill).where(
                Bill.tenant_id == tenant.id,
                Bill.kwh_generated.is_not(None),
            )
        ).scalars().all()
        kwh_by_account: dict[int, int] = defaultdict(int)
        bills_seen: set[int] = set()
        for b in bills_in_q:
            # Use period_start if present, else bill_date — both are nullable
            # but at least one is set on parsed bills.
            ref_dt = b.period_start or b.bill_date
            if ref_dt is None:
                continue
            if not (q_start_dt <= ref_dt <= q_end_dt):
                continue
            if not b.kwh_generated or b.kwh_generated <= 0:
                continue
            kwh_by_account[b.account_id] += b.kwh_generated
            bills_seen.add(b.account_id)
        # Accounts with bills landed but 0 generation in-quarter still get 0.0
        # (real signal: "we have data, this quarter was zero"). Accounts with
        # no bills at all stay None ("no data yet, don't claim zero").
        mwh_per_acct: dict[int, float] = {
            acc_id: round(k / 1000.0, 3) for acc_id, k in kwh_by_account.items()
        }

        # Group accounts by which array they belong to.
        # Orphan UAs (no array / array soft-deleted / array with no client) are
        # NEVER surfaced as free-floating "GMP · …" cards (Ford 2026-07-15 —
        # Bruce cleanup: those little cards kept reappearing and cluttering the
        # sandbox). They remain in the DB for undo/history, but the canvas only
        # shows accounts attached to a live client-owned array.
        accs_by_array: dict[int, list[UtilityAccount]] = defaultdict(list)

        for acc in accounts:
            if acc.array_id is None:
                continue
            arr = array_map.get(acc.array_id)
            if arr is None or arr.client_id is None:
                continue
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
                "contact_email": c.contact_email,
                "canvas_x": getattr(c, "canvas_x", None),
                "canvas_y": getattr(c, "canvas_y", None),
                "canvas_pinned": getattr(c, "canvas_pinned", False) or False,
                "accounts": c_accs,
                # Per-utility login credentials so the sandbox can show
                # "Login GMP · marie@…" when the operator expands the login row.
                "logins": {
                    "GMP": c.gmp_email or c.gmp_username or None,
                    "VEC": c.vec_email or c.vec_username or None,
                },
            })

        # Origin client lookup — any client referenced by a non-null
        # login_origin_client_id, even if soft-deleted, so the sandbox can
        # label a moved login with "from <origin client name>".
        origin_ids: set[int] = set()
        for c in clients_out:
            for a in c["accounts"]:
                if a.get("login_origin_client_id") is not None:
                    origin_ids.add(a["login_origin_client_id"])
        clients_index: dict[int, dict] = {}
        if origin_ids:
            for c in db.execute(
                select(Client).where(
                    Client.tenant_id == tenant.id,
                    Client.id.in_(origin_ids),
                )
            ).scalars().all():
                clients_index[c.id] = {
                    "id": c.id,
                    "name": c.name,
                    "deleted": c.deleted_at is not None,
                    "logins": {
                        "GMP": c.gmp_email or c.gmp_username or None,
                        "VEC": c.vec_email or c.vec_username or None,
                    },
                }

    return {
        "clients": clients_out,
        # Always empty — orphan floaters retired (see above). Key kept so older
        # frontends that read `unclassified` don't break.
        "unclassified": [],
        "clients_index": clients_index,
    }


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
    require_not_demo(tenant)
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


# ── POST /v1/sandbox/client/pin ───────────────────────────────────────────────

class PinClientBody(BaseModel):
    client_id: int
    pinned: bool


@router.post("/v1/sandbox/client/pin")
def pin_client(
    body: PinClientBody,
    authorization: Optional[str] = Header(default=None),
):
    """Toggle a client's pinned/starred state. Pinned clients sort to the top
    of any list, render with a gold star, and survive bulk operations more
    visibly. Reuses the existing clients.canvas_pinned column."""
    tenant = tenant_from_session(authorization)
    require_not_demo(tenant)
    with SessionLocal() as db:
        c = db.get(Client, body.client_id)
        if not c or c.tenant_id != tenant.id or c.deleted_at is not None:
            raise HTTPException(404, "client not found")
        c.canvas_pinned = body.pinned
        db.commit()
    return {"ok": True, "client_id": body.client_id, "pinned": body.pinned}


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
    require_not_demo(tenant)
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


# ── POST /v1/sandbox/account/reassign ────────────────────────────────────────

class AccountReassignBody(BaseModel):
    account_id: int
    # When provided, attach to this client (auto-creates a holder array if the
    # account has none yet). When null/absent, detach (unclassify) the account.
    client_id: Optional[int] = None


@router.post("/v1/sandbox/account/reassign")
def sandbox_account_reassign(
    body: AccountReassignBody,
    authorization: Optional[str] = Header(default=None),
):
    """Move a UtilityAccount between clients (or unclassify it).

    Accounts hang off Arrays which hang off Clients. To "move an account to
    another client" we re-point its Array to the target client; if the
    account currently has no array (or shares one with siblings staying put)
    we create a new holder Array under the target client.

    Reassigning here always creates/uses a per-account array — the assumption
    is the operator wants to organize at the account level. If multiple
    accounts share one physical array (Bruce's Starlake = 3 sub-meters), they
    stay grouped only if the operator drags the *array* not individual
    accounts; v2 (array-level drag) implemented in feat/array-drag — see
    POST /v1/sandbox/array/reassign.
    """
    tenant = tenant_from_session(authorization)
    require_not_demo(tenant)
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, body.account_id)
        if not acc or acc.tenant_id != tenant.id or acc.deleted_at is not None:
            raise HTTPException(404, "account not found")

        # Resolve target client (if any)
        target_client: Optional[Client] = None
        if body.client_id is not None:
            target_client = db.execute(
                select(Client).where(
                    Client.tenant_id == tenant.id,
                    Client.id == body.client_id,
                    Client.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if not target_client:
                raise HTTPException(404, "client not found")

        # Capture the account's CURRENT client (before any reassignment) — used
        # below to decide whether to stamp/clear login_origin_client_id so the
        # sandbox can render a moved login as its own group.
        prior_client_id: Optional[int] = None
        if acc.array_id is not None:
            prior_arr = db.get(Array, acc.array_id)
            if prior_arr is not None and prior_arr.tenant_id == tenant.id:
                prior_client_id = prior_arr.client_id

        # Detach path: soft-delete the utility account so it does NOT reappear
        # as a free-floating "GMP · …" card on the canvas (Ford 2026-07-15).
        if target_client is None:
            from datetime import datetime as _dt
            acc.deleted_at = _dt.utcnow()
            acc.login_origin_client_id = None
            db.commit()
            return {"ok": True, "account_id": acc.id, "client_id": None, "array_id": None,
                    "soft_deleted": True}

        # Attach path: ensure the account has an array owned by target_client.
        # Strategy: if the account currently has its own array (1:1 holder) that
        # belongs to THIS tenant, just re-point it. Otherwise create a fresh
        # holder array under target_client with a tenant-unique name (the
        # arrays table has a UNIQUE (tenant_id, name) constraint, so we must
        # avoid name collisions including with soft-deleted siblings).
        cur_array: Optional[Array] = None
        if acc.array_id is not None:
            cur_array = db.get(Array, acc.array_id)
            if cur_array is not None and cur_array.tenant_id != tenant.id:
                cur_array = None  # safety: never touch another tenant's array

        if cur_array is not None:
            sibling_count = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.tenant_id == tenant.id,
                    UtilityAccount.array_id == cur_array.id,
                    UtilityAccount.deleted_at.is_(None),
                    UtilityAccount.id != acc.id,
                )
            ).scalars().all()
        else:
            sibling_count = []

        if cur_array is not None and len(sibling_count) == 0:
            # Solo holder array — reuse it, just reparent
            cur_array.client_id = target_client.id
            new_array_id = cur_array.id
        else:
            # Create new holder array under target client. The arrays table
            # has UNIQUE (tenant_id, name) — including soft-deleted rows — so
            # we de-dupe the name against any existing array with the same
            # base, suffixing " (2)", " (3)", etc. until we find a free slot.
            base_name = acc.nickname or f"{acc.provider.upper()} {acc.account_number}"
            candidate = base_name
            attempt = 2
            while db.execute(
                select(Array).where(
                    Array.tenant_id == tenant.id,
                    Array.name == candidate,
                )
            ).scalar_one_or_none() is not None:
                candidate = f"{base_name} ({attempt})"
                attempt += 1
                if attempt > 50:
                    raise HTTPException(500, "could not allocate a unique array name")

            new_array = Array(
                tenant_id=tenant.id,
                client_id=target_client.id,
                name=candidate,
                nepool_gis_id=None,
                bill_offset_months=1,
                # Inherit the holder client's fuel so splitting an account into a
                # fresh array under a non-solar client doesn't silently make solar.
                fuel_type=normalize_fuel(
                    None, getattr(target_client, "default_fuel_type", None)),
            )
            db.add(new_array)
            db.flush()
            acc.array_id = new_array.id
            new_array_id = new_array.id

        # Stamp the origin tag so the sandbox can render this account's login
        # as a SEPARATE group from any same-utility login the target client
        # already has. Rules:
        # - First-ever move away from home → stamp prior_client_id
        # - Move back to the tagged origin → clear the tag (it's home again)
        # - Already-tagged account moving to a third client → keep the
        #   ORIGINAL tag intact (so undo always returns it to its true home)
        if acc.login_origin_client_id is None:
            if prior_client_id is not None and prior_client_id != target_client.id:
                acc.login_origin_client_id = prior_client_id
        else:
            if acc.login_origin_client_id == target_client.id:
                acc.login_origin_client_id = None

        db.commit()

    return {
        "ok": True,
        "account_id": acc.id,
        "client_id": target_client.id,
        "array_id": new_array_id,
    }


# ── POST /v1/sandbox/array/reassign ──────────────────────────────────────────

class ArrayReassignBody(BaseModel):
    array_id: int
    client_id: Optional[int] = None


@router.post("/v1/sandbox/array/reassign")
def sandbox_array_reassign(
    body: ArrayReassignBody,
    authorization: Optional[str] = Header(default=None),
):
    """Move an Array to a different client (or unclassify it when client_id is null).

    Moving an array re-points Array.client_id to the target client. The array's
    UtilityAccount rows are NOT touched — their array_id FK stays the same, so
    the billing data link is preserved. Only the org-chart assignment changes.

    Sub-meter arrays (Bruce's Starlake = 3 UtilityAccounts sharing one Array)
    are handled naturally: since all sub-meter accounts already point at the
    same Array, moving the Array via this endpoint moves all of them together.
    The caller is responsible for confirming this with the operator when
    sub-meter accounts are present (subMeterCount > 1 in the drag payload).

    Stamps Array.reassigned_at server-side so the canvas can show a
    'Moved just now' badge for ~10s after the move.
    """
    tenant = tenant_from_session(authorization)
    require_not_demo(tenant)
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id,
                Array.id == body.array_id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not arr:
            raise HTTPException(404, "array not found")

        prior_client_id: Optional[int] = arr.client_id

        # Detach/unclassify path: soft-delete the array + its utility accounts
        # instead of leaving orphan "GMP · …" floaters on the canvas.
        if body.client_id is None:
            from datetime import datetime as _dt
            now_ts = _dt.utcnow()
            arr.deleted_at = now_ts
            arr.client_id = None
            arr.reassigned_at = now()
            attached = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.tenant_id == tenant.id,
                    UtilityAccount.array_id == arr.id,
                    UtilityAccount.deleted_at.is_(None),
                )
            ).scalars().all()
            for acct in attached:
                acct.deleted_at = now_ts
                acct.login_origin_client_id = None
            db.commit()
            return {
                "ok": True,
                "array_id": arr.id,
                "client_id": None,
                "prior_client_id": prior_client_id,
                "soft_deleted": True,
            }

        target_client = db.execute(
            select(Client).where(
                Client.tenant_id == tenant.id,
                Client.id == body.client_id,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not target_client:
            raise HTTPException(404, "client not found")

        arr.client_id = target_client.id
        arr.reassigned_at = now()

        # Propagate the same stamp/clear logic as sandbox_account_reassign so the
        # canvas renders the moved array's login as its own group (not the target's).
        attached = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant.id,
                UtilityAccount.array_id == arr.id,
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all()
        for acct in attached:
            if acct.login_origin_client_id is None:
                if prior_client_id is not None and prior_client_id != target_client.id:
                    acct.login_origin_client_id = prior_client_id
            else:
                if acct.login_origin_client_id == target_client.id:
                    acct.login_origin_client_id = None

        db.commit()

    return {
        "ok": True,
        "array_id": arr.id,
        "client_id": target_client.id,
        "prior_client_id": prior_client_id,
    }
