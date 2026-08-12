"""A partial (period-less) bill pull must never append a duplicate empty row.

Ford, 2026-08-12 (Glover / VEC): the account grew one junk row per month — a
`partial` bill with null period, null kWh, no PDF — sitting beside every real
parsed bill. Cause: `_upsert_bill` only looked for an existing row to update when
`period_end` was set. A partial parse has no period_end, and `period_end = NULL`
is never true in SQL, so it could match nothing and every re-pull inserted a fresh
empty duplicate.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_partial_dedup_test")

import secrets as _secrets
from datetime import datetime

import pytest

from api.db import SessionLocal
from api.models import Tenant, Array, UtilityAccount, Bill
from api.worker import _upsert_bill


@pytest.fixture
def acct():
    tid = "ten_pd_" + _secrets.token_hex(4)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="Dedup Op",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Roaring Brook", region="VT")
        db.add(arr); db.flush()
        a = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="vec",
                           account_number="VEC-" + _secrets.token_hex(3),
                           nickname="Glover VEC")
        db.add(a); db.commit()
        return tid, a.id


def _bills(account_id):
    with SessionLocal() as db:
        return db.query(Bill).filter(Bill.account_id == account_id).all()


def _good_metrics():
    return dict(bill_date=datetime(2026, 6, 24), period_start=datetime(2026, 5, 21),
                period_end=datetime(2026, 6, 21), billing_days=31,
                kwh_generated=10200, parse_status="parsed",
                document_number="VEC-2026-06", raw_text="")


def _empty_partial(bill_date=datetime(2026, 6, 24)):
    return dict(bill_date=bill_date, period_start=None, period_end=None,
                billing_days=None, kwh_generated=None, parse_status="partial",
                document_number=None, raw_text="")


def test_empty_partial_beside_a_good_row_updates_not_duplicates(acct):
    tid, aid = acct
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, aid)
        assert _upsert_bill(db, tid, acc, _good_metrics()) == "created"
        db.commit()
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, aid)
        # The re-pull that used to append a junk row — matched by bill_date now.
        action = _upsert_bill(db, tid, acc, _empty_partial())
        db.commit()
    assert action == "updated"
    rows = _bills(aid)
    assert len(rows) == 1, f"expected the good row to survive alone, got {len(rows)}"
    # The good row's real values are intact — the None-guard protected them.
    assert rows[0].period_end is not None
    assert rows[0].kwh_generated == 10200


def test_empty_partial_with_no_existing_row_is_not_stored(acct):
    """A content-free partial with nothing to match is pure noise — never persist it."""
    tid, aid = acct
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, aid)
        action = _upsert_bill(db, tid, acc, _empty_partial())
        db.commit()
    assert action == "skipped"
    assert _bills(aid) == []


def test_repeated_partial_pulls_never_accumulate(acct):
    """The actual Glover shape: pull the good bill, then the same partial ten times."""
    tid, aid = acct
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, aid)
        _upsert_bill(db, tid, acc, _good_metrics()); db.commit()
    for _ in range(10):
        with SessionLocal() as db:
            acc = db.get(UtilityAccount, aid)
            _upsert_bill(db, tid, acc, _empty_partial()); db.commit()
    assert len(_bills(aid)) == 1


def test_a_partial_that_actually_carries_a_pdf_is_still_kept(acct):
    """We only DROP empty partials. One with a PDF is a real unparsed bill worth
    keeping for a later re-parse — it must still be stored."""
    tid, aid = acct
    m = _empty_partial(bill_date=datetime(2026, 7, 24))
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, aid)
        action = _upsert_bill(db, tid, acc, m, pdf_bytes=b"%PDF-1.4 stub")
        db.commit()
    assert action == "created"
    rows = _bills(aid)
    assert len(rows) == 1 and rows[0].pdf_bytes is not None


def test_good_period_bills_are_unaffected(acct):
    """The period_end path is untouched: two DIFFERENT periods both persist."""
    tid, aid = acct
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, aid)
        _upsert_bill(db, tid, acc, _good_metrics()); db.commit()
    m2 = _good_metrics()
    m2.update(bill_date=datetime(2026, 5, 26), period_start=datetime(2026, 4, 18),
              period_end=datetime(2026, 5, 21), kwh_generated=9840,
              document_number="VEC-2026-05")
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, aid)
        assert _upsert_bill(db, tid, acc, m2) == "created"; db.commit()
    assert len(_bills(aid)) == 2
