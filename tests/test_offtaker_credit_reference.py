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


def _mk_raw(*, excess_kwh, raw_json, provider="gmp"):
    """One bill where solar_credit_usd is NULL (group net metering shares it out at
    $0) but the raw bill states a credited excess rate — the screenshot's case."""
    tid = "ten_raw_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Raw",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        a = Array(tenant_id=tid, name="Raw Array", region="VT")
        db.add(a); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=a.id, provider=provider,
                              account_number="W" + secrets.token_hex(3))
        db.add(acct); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 16),
                    kwh_generated=int(excess_kwh), kwh_sent_to_grid=excess_kwh,
                    solar_credit_usd=None, raw_json=raw_json))
        db.commit()
        return acct.id


def test_group_net_metering_uses_bill_stated_rate():
    # GMP group net metering: 28,772 kWh shared out at $0; only a 9 kWh residual is
    # credited (@ $0.18398 → -$1.66). solar_credit_usd never captured → must price
    # the shared excess at the bill's OWN stated rate, NOT a fleet reference.
    raw = {"billSegments": [
        {"segmentLineItems": [
            {"unitCode": "EXCESS", "unitOfMeasure": "KWH", "unitCount": 9.0, "dollarAmount": -1.66}]},
        {"segmentLineItems": [
            {"unitCode": "GENERATE", "unitOfMeasure": "KWH", "unitCount": 28788.0, "dollarAmount": 0.0},
            {"unitCode": "EXCESS", "unitOfMeasure": "KWH", "unitCount": 28772.0, "dollarAmount": 0.0}]}]}
    aid = _mk_raw(excess_kwh=28772.0, raw_json=raw)
    with SessionLocal() as db:
        excess, credit, rate, ps, pe, label, source = resolve_offtaker_excess_credit(db, aid)
    assert source == "bill_cash"           # the bill's rate, not "reference"
    assert excess == 28772.0               # bills the shared excess
    assert abs(rate - 0.18444) < 1e-4      # 1.66 / 9 — the bill's printed credit rate


def test_truly_banked_raw_falls_back_to_reference():
    # All excess shown at $0 (no credited line) → genuinely banked → reference.
    raw = {"billSegments": [{"segmentLineItems": [
        {"unitCode": "EXCESS", "unitOfMeasure": "KWH", "unitCount": 4000.0, "dollarAmount": 0.0}]}]}
    aid = _mk_raw(excess_kwh=4000.0, raw_json=raw, provider="zzz_no_fleet2")
    with SessionLocal() as db:
        excess, credit, rate, ps, pe, label, source = resolve_offtaker_excess_credit(db, aid)
    assert source == "reference"
    assert abs(rate - DEFAULT_CREDIT_RATE) < 1e-6
