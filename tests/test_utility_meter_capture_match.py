"""Tests for utility-meter generation capture array MATCHING (the VEC/NEPOOL fix).

The NEPOOL VEC failure: bills landed but generation never did, because the kWh
lives only in the usage API. The extension now pulls daily generation and POSTs
it to /v1/array-owners/utility-meter-capture (dual-auth, tenant_key). The risk
when that capture lands on the NEPOOL side: the existing array (created by the
bill-capture path, named by service address + linked to a UtilityAccount) must
be MATCHED by its account number — not duplicated because the capture's nickname
(addr1, city, state — no zip) doesn't string-match the array name.
"""
from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, DailyGeneration, Tenant, UtilityAccount


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Meter Cap Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _make_vec_array(tid: str, *, name: str, account_number: str) -> int:
    """An existing VEC array as the bill-capture path leaves it: named by the
    full service address, linked to a vec UtilityAccount."""
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name=name, client_id=None, fuel_type="solar")
        db.add(arr)
        db.flush()
        db.add(UtilityAccount(
            tenant_id=tid, array_id=arr.id, provider="vec",
            account_number=account_number,
        ))
        db.commit()
        return arr.id


def _capture(client, key: str, *, account_number: str, nickname: str,
             daily: list[tuple[str, float]]):
    return client.post(
        "/v1/array-owners/utility-meter-capture",
        json={
            "provider": "vec",
            "accounts": [{
                "account_number": account_number,
                "nickname": nickname,
                "summary": {},
                "daily": [{"date": d, "generated_kwh": k} for d, k in daily],
            }],
        },
        headers={"Authorization": f"Bearer {key}"},
    )


def test_capture_matches_existing_array_by_account_number(client):
    """Generation capture for VEC acct 6578300 must attach to the EXISTING array
    (matched by its linked account number) even though the capture nickname
    ('52 County RD, Glover, VT' — no zip) differs from the array name
    ('52 County RD, Glover, VT, 05839'). No duplicate array."""
    tid, key = _make_tenant()
    arr_id = _make_vec_array(
        tid, name="52 County RD, Glover, VT, 05839", account_number="6578300")

    resp = _capture(
        client, key,
        account_number="6578300",
        nickname="52 County RD, Glover, VT",   # no zip — would NOT name-match
        daily=[("2026-05-10", 300.0), ("2026-05-11", 320.0)],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accounts_captured"] == 1
    # CRITICAL: matched the existing array, did NOT create a new one.
    assert body["arrays_created"] == 0

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid,
                                Array.deleted_at.is_(None))
        ).scalars().all()
        assert len(arrays) == 1  # no duplicate
        dg = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalars().all()
        assert len(dg) == 2
        assert sum(r.kwh for r in dg) == 620.0
        assert all(r.source == "utility_meter" for r in dg)


def test_capture_is_idempotent_no_duplicate_days(client):
    """Re-capturing the same days upserts (max-kWh) rather than duplicating."""
    tid, key = _make_tenant()
    arr_id = _make_vec_array(tid, name="Addr A, VT, 05000", account_number="999111")

    _capture(client, key, account_number="999111", nickname="Addr A, VT",
             daily=[("2026-05-10", 100.0)])
    _capture(client, key, account_number="999111", nickname="Addr A, VT",
             daily=[("2026-05-10", 100.0)])

    with SessionLocal() as db:
        dg = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalars().all()
        assert len(dg) == 1  # one day, not two rows
