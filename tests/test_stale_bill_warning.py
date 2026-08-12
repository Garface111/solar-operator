"""A stale utility bill must be flagged, not silently billed as current.

Ford, 2026-08-12 (Glover / VEC): after the VEC login broke, Glover kept drafting
its June invoice for weeks — same amount, no warning — because June genuinely was
the newest bill we had. The invoice must SAY when it's built on a stale period so
the operator knows to reconnect the utility, instead of quietly re-billing the
past.

The detector is self-calibrated to each account's own cadence, so it fires on a
stalled monthly account without nagging one that just bills infrequently.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_stale_bill_test")

import secrets as _secrets
from datetime import date, datetime, timedelta

import pytest

from api.db import SessionLocal
from api.models import Tenant, Array, UtilityAccount, Bill
from api.billing.delivery import _stale_bill_warning


@pytest.fixture
def acct():
    tid = "ten_sb_" + _secrets.token_hex(4)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="Stale Op",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Roaring Brook", region="VT")
        db.add(arr); db.flush()
        a = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="vec",
                           account_number="VEC-" + _secrets.token_hex(3))
        db.add(a); db.commit()
        return a.id


def _bill(db, aid, pstart, pend, kwh=1000):
    db.add(Bill(tenant_id=db.get(UtilityAccount, aid).tenant_id, account_id=aid,
                bill_date=pend, period_start=pstart, period_end=pend,
                kwh_generated=kwh, kwh_sent_to_grid=kwh, parse_status="parsed"))


def test_monthly_account_gone_quiet_is_flagged(acct):
    """Two monthly bills, newest ~2 months old → overdue → warn."""
    aid = acct
    with SessionLocal() as db:
        _bill(db, aid, datetime(2026, 4, 21), datetime(2026, 5, 21))
        _bill(db, aid, datetime(2026, 5, 21), datetime(2026, 6, 21))
        db.commit()
        # "today" fixed 52 days past the newest period_end
        w = _stale_bill_warning(db, aid, today=date(2026, 8, 12))
    assert w is not None
    assert "overdue" in w and "2026-06-21" in w


def test_fresh_bill_is_not_flagged(acct):
    aid = acct
    with SessionLocal() as db:
        _bill(db, aid, datetime(2026, 6, 21), datetime(2026, 7, 21))
        _bill(db, aid, datetime(2026, 7, 21), datetime(2026, 8, 21))
        db.commit()
        w = _stale_bill_warning(db, aid, today=date(2026, 8, 25))  # 4 days old
    assert w is None


def test_single_bill_never_warns_cadence_unknown(acct):
    """One bill → we can't tell 'slow' from 'stalled' → stay silent (no false alarm
    on a brand-new offtaker, and no flakiness for single-bill test fixtures)."""
    aid = acct
    with SessionLocal() as db:
        _bill(db, aid, datetime(2020, 1, 1), datetime(2020, 2, 1))
        db.commit()
        w = _stale_bill_warning(db, aid, today=date(2026, 8, 12))
    assert w is None


def test_infrequent_account_not_nagged(acct):
    """An account that genuinely bills ~quarterly isn't 'stalled' at 60 days."""
    aid = acct
    with SessionLocal() as db:
        _bill(db, aid, datetime(2026, 1, 1), datetime(2026, 4, 1))   # ~90-day cycle
        _bill(db, aid, datetime(2026, 4, 1), datetime(2026, 7, 1))
        db.commit()
        w = _stale_bill_warning(db, aid, today=date(2026, 8, 30))    # 60 days < 1.5×91
    assert w is None


def test_no_account_id_is_safe(acct):
    with SessionLocal() as db:
        assert _stale_bill_warning(db, None) is None
