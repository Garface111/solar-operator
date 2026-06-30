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


def test_capture_revives_soft_deleted_array_instead_of_500(client):
    """REGRESSION: the 'couldn't grab your GMP account' HTTP 500.

    uq_array_per_tenant spans (tenant_id, name) including soft-deleted rows, so a
    soft-deleted array still RESERVES its name. The capture path used to build its
    name map from non-deleted arrays only — so a re-capture whose name matched a
    soft-deleted array tried to INSERT a colliding name → psycopg2 UniqueViolation
    → 500. It must instead REVIVE the soft-deleted array (no duplicate, no crash).
    """
    from datetime import datetime
    tid, key = _make_tenant()
    # An array with this exact capture-derived name exists but is SOFT-DELETED.
    # The meter-capture name for a nickname-less GMP account = "GMP <acct>".
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="GMP 770099", client_id=None,
                    fuel_type="solar", deleted_at=datetime.utcnow())
        db.add(arr)
        db.flush()
        deleted_id = arr.id
        db.commit()

    resp = client.post(
        "/v1/array-owners/utility-meter-capture",
        json={
            "provider": "gmp",
            "accounts": [{
                "account_number": "770099",
                "nickname": "",                       # → name becomes "GMP 770099"
                "summary": {},
                "daily": [{"date": "2026-05-10", "generated_kwh": 250.0}],
            }],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    # Must NOT 500 — the soft-deleted array is revived and reused.
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        # Same row revived (deleted_at cleared), no duplicate created.
        arrs = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.name == "GMP 770099")
        ).scalars().all()
        assert len(arrs) == 1
        assert arrs[0].id == deleted_id
        assert arrs[0].deleted_at is None
        dg = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == deleted_id)
        ).scalars().all()
        assert len(dg) == 1 and dg[0].kwh == 250.0


def test_gmp_capture_creates_linkable_utility_account_and_bill(client):
    """The offtaker dropdown lists GMP UtilityAccounts and bills them from
    Bill.kwh_generated. A GMP meter capture with a billing-period summary must
    therefore create BOTH a UtilityAccount (so the account is linkable) and a
    Bill (so the offtaker invoice has real utility-bill kWh) — not just
    DailyGeneration. This is what makes 'Link GMP utility bills' actually
    populate the add-offtaker picker."""
    from api.models import Bill, UtilityAccount
    tid, key = _make_tenant()
    resp = client.post(
        "/v1/array-owners/utility-meter-capture",
        json={
            "provider": "gmp",
            "accounts": [{
                "account_number": "5551212",
                "nickname": "Maple Field",
                "summary": {
                    "accountNumber": "5551212",
                    "isNetMetered": True,
                    "billingPeriodStartDate": "2026-05-01",
                    "billingPeriodEndDate": "2026-05-31",
                    "totalGrossGenerated": 1800,
                    "totalGenerationSentToGrid": 1500,
                },
                "daily": [],
            }],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        ua = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tid,
                UtilityAccount.provider == "gmp",
                UtilityAccount.account_number == "5551212",
            )
        ).scalar_one()
        assert ua.array_id is not None          # linked to the array
        bills = db.execute(
            select(Bill).where(Bill.account_id == ua.id)
        ).scalars().all()
        assert len(bills) == 1
        assert bills[0].kwh_generated == 1800    # the paper-bill total
        assert bills[0].period_end is not None


def test_gmp_capture_open_cycle_creates_no_future_dated_bill(client):
    """REGRESSION (Bruce's 83-vs-803): GMP's /usage summary reports the CURRENT,
    OPEN cycle — billingPeriodEndDate is the NEXT bill date (in the FUTURE) and the
    generation is only the partial accrual so far. That must NOT be recorded as a
    settled paper bill (it would be future-dated and prorate a partial total across
    a partly-future window). The account stays linkable, but no Bill and no
    today/future DailyGeneration rows are written until the cycle closes."""
    from datetime import timedelta
    from api.models import Bill, UtilityAccount, now
    tid, key = _make_tenant()
    today = now().date()
    resp = client.post(
        "/v1/array-owners/utility-meter-capture",
        json={
            "provider": "gmp",
            "accounts": [{
                "account_number": "5557777",
                "nickname": "Open Cycle Field",
                "summary": {
                    "accountNumber": "5557777", "isNetMetered": True,
                    "billingPeriodStartDate": (today - timedelta(days=6)).isoformat(),
                    "billingPeriodEndDate": (today + timedelta(days=24)).isoformat(),
                    "totalGrossGenerated": 2574,     # partial accrual, open cycle
                },
                "daily": [],
            }],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        ua = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.account_number == "5557777")).scalar_one_or_none()
        if ua is not None:
            bills = db.execute(select(Bill).where(Bill.account_id == ua.id)).scalars().all()
            assert bills == [], f"open cycle must not create a bill: {[b.period_end for b in bills]}"
        fut = db.execute(select(DailyGeneration).where(
            DailyGeneration.tenant_id == tid,
            DailyGeneration.day > today)).scalars().all()
        assert fut == [], f"open cycle must not write future daily rows: {len(fut)}"


def test_gmp_capture_bill_is_idempotent_no_dupe_bill(client):
    """Re-capturing the same GMP billing period upserts the Bill (climbs only),
    never duplicating it."""
    from api.models import Bill, UtilityAccount
    tid, key = _make_tenant()
    payload = {
        "provider": "gmp",
        "accounts": [{
            "account_number": "5559000",
            "nickname": "Birch Field",
            "summary": {
                "accountNumber": "5559000", "isNetMetered": True,
                "billingPeriodStartDate": "2026-05-01",
                "billingPeriodEndDate": "2026-05-31",
                "totalGrossGenerated": 1000,
            },
            "daily": [],
        }],
    }
    client.post("/v1/array-owners/utility-meter-capture", json=payload,
                headers={"Authorization": f"Bearer {key}"})
    # Second capture, generation climbed within the same period.
    payload["accounts"][0]["summary"]["totalGrossGenerated"] = 1850
    client.post("/v1/array-owners/utility-meter-capture", json=payload,
                headers={"Authorization": f"Bearer {key}"})

    with SessionLocal() as db:
        ua = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tid,
                UtilityAccount.account_number == "5559000")
        ).scalar_one()
        bills = db.execute(select(Bill).where(Bill.account_id == ua.id)).scalars().all()
        assert len(bills) == 1                   # no duplicate Bill for the period
        assert bills[0].kwh_generated == 1850    # climbed to max
        # And exactly one UtilityAccount (no dupe account either).
        accts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tid,
                UtilityAccount.account_number == "5559000")
        ).scalars().all()
        assert len(accts) == 1


def test_vec_capture_creates_linkable_account_even_with_no_generation(client):
    """REGRESSION (Ford: 'I linked VEC but don't see Glover in the picker').

    VEC net-metering 'credit' accounts routinely read 0 kWh in SmartHub's usage
    explorer, so the meter capture lands with no generation. The path used to skip
    ANY account with no detected generation → a freshly-linked VEC account never
    became a pickable UtilityAccount. For SmartHub providers we now create the
    bindable UtilityAccount + Array anyway (generation attaches later; the offtaker
    invoice waits for it). No pre-existing array."""
    tid, key = _make_tenant()
    resp = _capture(client, key, account_number="6578300",
                    nickname="52 County RD, Glover, VT", daily=[])  # NO generation
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        ua = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid, UtilityAccount.provider == "vec",
            UtilityAccount.account_number == "6578300")).scalar_one_or_none()
        assert ua is not None, "VEC account must be created so it's pickable"
        assert ua.array_id is not None              # linked to an array to bill from
        arrs = db.execute(select(Array).where(
            Array.tenant_id == tid, Array.deleted_at.is_(None))).scalars().all()
        assert len(arrs) == 1                        # the bindable array exists


def test_gmp_no_generation_account_is_pickable_but_has_no_array(client):
    """Ford, for Bruce: 'pull ALL the utility bills so I can see them in the choose-
    utility-account dropdown.' A GMP account with no detected solar generation (a
    home/pump/meter, OR a solar account whose generation hasn't loaded yet) now
    creates a bindable UtilityAccount — so it appears in the offtaker picker — but
    still NO Array, so the bury-prevention gate on the inverter dashboard holds."""
    tid, key = _make_tenant()
    resp = client.post(
        "/v1/array-owners/utility-meter-capture",
        json={"provider": "gmp", "accounts": [{
            "account_number": "4040404", "nickname": "No Solar Home",
            "summary": {}, "daily": []}]},
        headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        ua = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.account_number == "4040404")).scalar_one_or_none()
        assert ua is not None, "GMP no-generation account must be pickable in the dropdown"
        assert ua.array_id is None, "but NO array (don't bury real solar arrays)"
        arrs = db.execute(select(Array).where(
            Array.tenant_id == tid, Array.deleted_at.is_(None))).scalars().all()
        assert len(arrs) == 0, "a non-solar GMP account must not spawn an array"


def test_meter_capture_appends_offtaker_generation_sheet(client, monkeypatch):
    """REGRESSION (Ford: 'refresh on UTILITY bill pull, not just GMP'): the
    generation-spreadsheet append was wired ONLY into the GMP server pull
    (worker.py), so a bill that lands via the EXTENSION meter capture — GMP (whose
    server pull is flaky) or any SmartHub utility (VEC/WEC, which have no server
    pull at all) — never updated each offtaker's BYO spreadsheet. The capture path
    must now call the provider-agnostic tracker append for the touched account."""
    from api.models import UtilityAccount
    import api.billing.sheet_tracker as st
    calls: list[tuple] = []
    monkeypatch.setattr(st, "maybe_append_for_account",
                        lambda _db, _tid, _aid: calls.append((_tid, _aid)))
    tid, key = _make_tenant()
    resp = client.post(
        "/v1/array-owners/utility-meter-capture",
        json={"provider": "gmp", "accounts": [{
            "account_number": "8675309", "nickname": "Solar Barn",
            "summary": {
                "accountNumber": "8675309", "isNetMetered": True,
                "billingPeriodStartDate": "2026-05-01",
                "billingPeriodEndDate": "2026-05-31",
                "totalGrossGenerated": 1800, "totalGenerationSentToGrid": 1500,
            }, "daily": []}]},
        headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        ua = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.account_number == "8675309")).scalar_one()
    assert (tid, ua.id) in calls, "meter-capture bill-land must append the offtaker sheet"


