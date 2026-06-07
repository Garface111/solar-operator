"""
Dev-only sandbox helpers — seed/wipe fake clients, logins, accounts.

ALL routes here are gated by the SO_DEV_ENABLED env var (default off in prod).
Routes are tenant-scoped: they only touch data owned by the calling session's
tenant, never anyone else's. The seeded entities are tagged with name prefix
`[DEV] ` so they're trivially distinguishable from real data, and the
/v1/dev/wipe endpoint only removes rows matching that prefix.

Mounted from api/app.py only when the env var is truthy; otherwise the import
itself works but no endpoints register.
"""
from __future__ import annotations

import os
import random
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from .account import tenant_from_session, require_not_demo, require_not_demo
from .db import SessionLocal
from .models import Array, Client, UtilityAccount, now

DEV_ENABLED = os.environ.get("SO_DEV_ENABLED", "").lower() in ("1", "true", "yes", "on")
DEV_PREFIX = "[DEV] "

router = APIRouter(prefix="/v1/dev")


def _require_dev():
    if not DEV_ENABLED:
        raise HTTPException(403, "Dev endpoints disabled. Set SO_DEV_ENABLED=1.")


@router.get("/status")
def status(authorization: Optional[str] = Header(default=None)):
    """Returns whether dev mode is active for this server + how many [DEV]
    rows already exist for the calling tenant. Used by the floating DevPanel
    to decide whether to render itself at all."""
    tenant = tenant_from_session(authorization)
    with SessionLocal() as db:
        n_clients = db.execute(
            select(Client).where(
                Client.tenant_id == tenant.id,
                Client.name.like(f"{DEV_PREFIX}%"),
                Client.deleted_at.is_(None),
            )
        ).scalars().all()
        return {
            "enabled": DEV_ENABLED,
            "tenant_id": tenant.id,
            "dev_clients": len(n_clients),
            "dev_prefix": DEV_PREFIX,
        }


# ── Random fixture data ─────────────────────────────────────────────────────

_FAKE_CLIENT_NAMES = [
    "Maple Ridge LLC", "Pine Valley Co-op", "Birch Hill Farm",
    "Willow Creek Holdings", "Cedar Brook Solar", "Granite State Properties",
    "River Bend Trust", "Stone Wall Schools", "Sugar Maple Inn",
    "Liberty Hill Cooperative", "Lakeview Estates", "Hillside Mills",
    "Brookside Properties", "Quarry Hill LLC", "Fox Run Holdings",
]

_FAKE_ARRAY_NAMES = [
    "North Field", "South Pasture", "Barn Roof", "Riverside",
    "Hilltop", "Meadow", "East Wing", "West Lot", "Garage Roof",
    "Old Orchard", "Lower Field", "Upper Bank",
]


def _rand_account_number() -> str:
    return f"{random.randint(1000, 9999)}-{random.randint(1000, 9999)}"


def _rand_customer_number() -> str:
    return f"C{random.randint(10000000, 99999999)}"


def _rand_nepool_gis_id() -> str:
    return f"NE-{random.randint(100000, 999999)}"


# ── Seed: clients ───────────────────────────────────────────────────────────

class SeedClientsBody(BaseModel):
    count: int = 3
    """Number of fake clients to create. Capped at 25 per request."""


@router.post("/seed/clients")
def seed_clients(
    body: SeedClientsBody,
    authorization: Optional[str] = Header(default=None),
):
    _require_dev()
    tenant = tenant_from_session(authorization)
    require_not_demo(tenant)
    count = max(1, min(25, body.count))
    created = []
    with SessionLocal() as db:
        # Find next free grid slot among existing tenant clients so seeded
        # clients drop into a tidy row instead of random scatter.
        from .models import Client as _C
        existing = db.execute(
            select(_C).where(
                _C.tenant_id == tenant.id,
                _C.deleted_at.is_(None),
                _C.canvas_x.isnot(None),
                _C.canvas_y.isnot(None),
            )
        ).scalars().all()
        COLS, COL_W, ROW_H, ORIGIN = 4, 330, 295, 40
        occupied = set()
        for c in existing:
            col = round((c.canvas_x - ORIGIN) / COL_W)
            row = round((c.canvas_y - ORIGIN) / ROW_H)
            occupied.add((col, row))

        def next_slot():
            idx = 0
            while True:
                col, row = idx % COLS, idx // COLS
                idx += 1
                if (col, row) not in occupied:
                    occupied.add((col, row))
                    return col * COL_W + ORIGIN, row * ROW_H + ORIGIN

        for _ in range(count):
            base = random.choice(_FAKE_CLIENT_NAMES)
            name = f"{DEV_PREFIX}{base} #{random.randint(100, 999)}"
            x, y = next_slot()
            c = Client(
                tenant_id=tenant.id,
                name=name,
                created_at=now(),
                canvas_x=x,
                canvas_y=y,
            )
            db.add(c)
            db.flush()
            created.append({"id": c.id, "name": c.name})
        db.commit()
    return {"ok": True, "created": created}


# ── Seed: login + arrays + accounts under a client ──────────────────────────

class SeedLoginBody(BaseModel):
    client_id: int
    utility: str = "GMP"  # GMP / VEC / WEC
    arrays: int = 3
    """Number of (array, account) pairs to create under one shared login."""


@router.post("/seed/login")
def seed_login(
    body: SeedLoginBody,
    authorization: Optional[str] = Header(default=None),
):
    """Create N arrays + N utility accounts all sharing the same customer_number
    (i.e. one login → many accounts) attached to the target client.

    Lets you reproduce the multi-array-per-login UI without touching the
    real GMP scraper."""
    _require_dev()
    tenant = tenant_from_session(authorization)
    require_not_demo(tenant)
    n = max(1, min(15, body.arrays))
    utility = body.utility.upper()
    if utility not in ("GMP", "VEC", "WEC"):
        raise HTTPException(400, "utility must be GMP, VEC, or WEC")

    with SessionLocal() as db:
        client = db.get(Client, body.client_id)
        if not client or client.tenant_id != tenant.id or client.deleted_at is not None:
            raise HTTPException(404, "client not found")

        # One shared customer_number across this login's accounts so the
        # frontend grouping treats them as one login row.
        shared_customer = _rand_customer_number()
        login_label = f"{DEV_PREFIX}{utility} login {random.randint(100, 999)}"

        # Stamp the credential on the client so the login row renders
        # "Signed in as <email>".
        fake_email = f"dev{random.randint(1000, 9999)}@example.test"
        if utility == "GMP":
            client.gmp_email = fake_email
        elif utility == "VEC":
            # If your model has equivalent fields for VEC/WEC, set them here.
            pass

        created_arrays = []
        created_accounts = []
        for i in range(n):
            arr_name = f"{DEV_PREFIX}{random.choice(_FAKE_ARRAY_NAMES)} {random.randint(1, 99)}"
            arr = Array(
                tenant_id=tenant.id,
                client_id=client.id,
                name=arr_name,
                nepool_gis_id=_rand_nepool_gis_id(),
                created_at=now(),
            )
            db.add(arr)
            db.flush()
            created_arrays.append({"id": arr.id, "name": arr.name})

            acc = UtilityAccount(
                tenant_id=tenant.id,
                array_id=arr.id,
                provider=utility.lower(),
                account_number=_rand_account_number(),
                customer_number=shared_customer,
                nickname=login_label,
                last_seen=now(),
                enabled=True,
            )
            db.add(acc)
            db.flush()
            created_accounts.append({
                "id": acc.id,
                "account_number": acc.account_number,
                "customer_number": acc.customer_number,
                "array_id": arr.id,
            })
        db.commit()

    return {
        "ok": True,
        "client_id": body.client_id,
        "utility": utility,
        "customer_number": shared_customer,
        "arrays": created_arrays,
        "accounts": created_accounts,
    }


# ── Seed: unclassified account (floats on canvas, no client) ────────────────

class SeedUnclassifiedBody(BaseModel):
    count: int = 2
    utility: str = "GMP"


@router.post("/seed/unclassified")
def seed_unclassified(
    body: SeedUnclassifiedBody,
    authorization: Optional[str] = Header(default=None),
):
    _require_dev()
    tenant = tenant_from_session(authorization)
    require_not_demo(tenant)
    n = max(1, min(15, body.count))
    utility = body.utility.upper()
    if utility not in ("GMP", "VEC", "WEC"):
        raise HTTPException(400, "utility must be GMP, VEC, or WEC")

    created = []
    with SessionLocal() as db:
        for _ in range(n):
            acc = UtilityAccount(
                tenant_id=tenant.id,
                array_id=None,
                provider=utility.lower(),
                account_number=_rand_account_number(),
                customer_number=_rand_customer_number(),
                nickname=f"{DEV_PREFIX}unclassified",
                last_seen=now(),
                enabled=True,
                canvas_x=random.uniform(1300, 1700),
                canvas_y=random.uniform(0, 600),
            )
            db.add(acc)
            db.flush()
            created.append({
                "id": acc.id,
                "account_number": acc.account_number,
            })
        db.commit()
    return {"ok": True, "created": created}


# ── Wipe: remove all [DEV]-prefixed rows for this tenant ────────────────────

@router.post("/wipe")
def wipe(authorization: Optional[str] = Header(default=None)):
    """Soft-delete every [DEV]-prefixed client + array, and hard-delete any
    [DEV]-nicknamed utility account that's not under a real client."""
    _require_dev()
    tenant = tenant_from_session(authorization)
    require_not_demo(tenant)
    n_clients = 0
    n_arrays = 0
    n_accounts = 0
    with SessionLocal() as db:
        # Clients (soft delete to be safe)
        clients = db.execute(
            select(Client).where(
                Client.tenant_id == tenant.id,
                Client.name.like(f"{DEV_PREFIX}%"),
                Client.deleted_at.is_(None),
            )
        ).scalars().all()
        for c in clients:
            c.deleted_at = now()
            n_clients += 1

        # Arrays under those clients (soft delete)
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == tenant.id,
                Array.name.like(f"{DEV_PREFIX}%"),
                Array.deleted_at.is_(None),
            )
        ).scalars().all()
        for a in arrays:
            a.deleted_at = now()
            n_arrays += 1

        # Accounts: nickname starts with [DEV]
        accounts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant.id,
                UtilityAccount.nickname.like(f"{DEV_PREFIX}%"),
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all()
        for ac in accounts:
            ac.deleted_at = now()
            n_accounts += 1

        db.commit()
    return {
        "ok": True,
        "clients_removed": n_clients,
        "arrays_removed": n_arrays,
        "accounts_removed": n_accounts,
    }
