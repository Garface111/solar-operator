"""resolve_offtaker_excess_credit — option B (Ford, 2026-06-22).

Bill the offtaker for the EXCESS sent to grid every month. Cashed months use the
bill's own credit rate; BANKED months fall back to a reference (account history →
fleet median → DEFAULT_CREDIT_RATE) so a perpetual-banker (e.g. Londonderry) still
bills monthly for the solar received, instead of $0 until the annual true-up.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_offtaker_ref_test")

from datetime import datetime
import secrets

from api.db import SessionLocal
from api.models import Tenant, Array, UtilityAccount, Bill
from api.rate_schedule import resolve_offtaker_excess_credit, DEFAULT_CREDIT_RATE


def _mk(*, bills, provider="gmp"):
    """bills: list of (month, excess_kwh, solar_credit_usd|None)."""
    tid = "ten_ref_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Ref",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        a = Array(tenant_id=tid, name="Ref Array", region="VT")
        db.add(a); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=a.id, provider=provider,
                              account_number="R" + secrets.token_hex(3))
        db.add(acct); db.flush()
        for month, excess, credit in bills:
            db.add(Bill(tenant_id=tid, account_id=acct.id,
                        period_start=datetime(2026, month, 1),
                        period_end=datetime(2026, month, 28),
                        kwh_generated=int(excess), kwh_sent_to_grid=excess,
                        solar_credit_usd=credit))
        db.commit()
        return acct.id


def test_cashed_month_uses_bill_rate():
    aid = _mk(bills=[(5, 1000.0, 257.60)])
    with SessionLocal() as db:
        excess, credit, rate, ps, pe, label, source = resolve_offtaker_excess_credit(db, aid)
    assert source == "bill_cash"
    assert excess == 1000.0
    assert abs(rate - 0.2576) < 1e-4
    assert credit == 257.60


def test_banked_latest_uses_account_history():
    # latest (June) banked; an earlier (May) month cashed at $0.20/kWh.
    aid = _mk(bills=[(5, 1000.0, 200.0), (6, 2000.0, None)])
    with SessionLocal() as db:
        excess, credit, rate, ps, pe, label, source = resolve_offtaker_excess_credit(db, aid)
    assert source == "reference"
    assert excess == 2000.0            # bills the latest (banked) month's excess
    assert abs(rate - 0.20) < 1e-4     # at the account's own historical rate
    assert credit == 400.0             # 2000 × 0.20


def test_banked_no_history_uses_default():
    # Perpetual banker, never cashed, unique provider → no fleet → DEFAULT_CREDIT_RATE.
    aid = _mk(bills=[(6, 5000.0, None)], provider="zzz_no_fleet")
    with SessionLocal() as db:
        excess, credit, rate, ps, pe, label, source = resolve_offtaker_excess_credit(db, aid)
    assert source == "reference"
    assert excess == 5000.0
    assert abs(rate - DEFAULT_CREDIT_RATE) < 1e-6
    assert credit == round(5000.0 * DEFAULT_CREDIT_RATE, 2)
