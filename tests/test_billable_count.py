"""Regression tests for the billable-array-count fix (June 2026).

Bug cluster: every reconcile_subscription_quantity callsite counted EXCLUDED
(and sometimes soft-deleted) arrays, so the Stripe quantity overbilled the
customer and disagreed with the dashboard "next charge" estimate. Toggling an
array to excluded — the one action whose purpose is to stop billing it — never
reconciled at all, and neither did merging duplicate arrays.

These prove: (1) the canonical billable_array_count helper, (2) the
billing-summary estimate matches it, (3) the PATCH exclude-toggle reconciles
with the billable (not total) count, (4) deleting an array reconciles with the
billable count, and (5) a no-op edit does NOT reconcile.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

import api.account as account
from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant, now
from api.stripe_helpers import billable_array_count


def _active_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Bill Test", contact_email=f"{tid}@test.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(18),
            plan="standard", active=True,
            stripe_subscription_id="sub_test_123",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _seed_arrays(tid: str, n_normal: int, n_excluded: int = 0, n_deleted: int = 0) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="C-" + secrets.token_hex(3), active=True)
        db.add(c); db.flush()
        cid = c.id
        for i in range(n_normal):
            db.add(Array(tenant_id=tid, client_id=cid, name=f"A{i}-{secrets.token_hex(2)}"))
        for i in range(n_excluded):
            db.add(Array(tenant_id=tid, client_id=cid,
                         name=f"X{i}-{secrets.token_hex(2)}", excluded=True))
        for i in range(n_deleted):
            db.add(Array(tenant_id=tid, client_id=cid,
                         name=f"D{i}-{secrets.token_hex(2)}", deleted_at=now()))
        db.commit()
    return cid


def test_billable_count_excludes_excluded_and_deleted():
    tid, _ = _active_tenant()
    _seed_arrays(tid, n_normal=3, n_excluded=2, n_deleted=1)
    with SessionLocal() as db:
        assert billable_array_count(db, tid) == 3


def test_billing_summary_matches_billable_count(client):
    """The dashboard 'next charge' estimate must report the billable count."""
    tid, auth = _active_tenant()
    _seed_arrays(tid, n_normal=4, n_excluded=2)
    r = client.get("/v1/account/billing-summary", headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["billable_arrays"] == 4


def test_exclude_toggle_reconciles_with_billable_count(client, monkeypatch):
    tid, auth = _active_tenant()
    cid = _seed_arrays(tid, n_normal=3)
    with SessionLocal() as db:
        aid = db.execute(
            select(Array).where(Array.tenant_id == tid)
        ).scalars().first().id

    calls: list[int] = []
    monkeypatch.setattr(account, "reconcile_subscription_quantity",
                        lambda sub, qty, t, email: calls.append(qty))

    # Excluding an array drops the billable count 3 → 2 and MUST reconcile.
    r = client.patch(f"/v1/account/clients/{cid}/arrays/{aid}",
                     headers={"Authorization": auth}, json={"excluded": True})
    assert r.status_code == 200, r.text
    assert calls == [2], "exclude toggle should reconcile with billable count 2"

    # A no-op edit (notes only, excluded unchanged) must NOT reconcile.
    calls.clear()
    r = client.patch(f"/v1/account/clients/{cid}/arrays/{aid}",
                     headers={"Authorization": auth}, json={"notes": "hello"})
    assert r.status_code == 200, r.text
    assert calls == [], "editing notes must not touch billing"


def test_delete_array_reconciles_with_billable_count(client, monkeypatch):
    """3 normal + 2 excluded; deleting one normal → reconcile with 2, not 4."""
    tid, auth = _active_tenant()
    cid = _seed_arrays(tid, n_normal=3, n_excluded=2)
    with SessionLocal() as db:
        aid = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.excluded.is_(False))
        ).scalars().first().id

    calls: list[int] = []
    monkeypatch.setattr(account, "reconcile_subscription_quantity",
                        lambda sub, qty, t, email: calls.append(qty))

    r = client.delete(f"/v1/account/clients/{cid}/arrays/{aid}",
                      headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    # Billable after delete = 2 remaining normal. The excluded pair never counts.
    assert calls == [2], f"expected reconcile qty 2, got {calls}"
