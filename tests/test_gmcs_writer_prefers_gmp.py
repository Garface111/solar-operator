"""The GMCS writer must prefer real GMP meter data (GmpDailyGeneration) over the
bill_prorate estimate for near-fully-covered months — this is what makes monthly
generation reconcile with Crown REC. Partial GMP coverage must NOT undercount.
"""
from __future__ import annotations

import calendar
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.models import Base, Tenant, Array, UtilityAccount, DailyGeneration, GmpDailyGeneration
from api.writers import gmcs_writer


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    import api.reports.gmp_daily_read as rd
    rd.SessionLocal = Session
    yield s
    s.close()


def _seed(db):
    db.add(Tenant(id="ten_t", name="T", contact_email="t@x.com", tenant_key="t_key"))
    db.add(Array(id=1, tenant_id="ten_t", name="Chester"))
    db.add(UtilityAccount(id=10, tenant_id="ten_t", array_id=1, provider="gmp", account_number="A1"))
    db.commit()


def _fill_daily(db, y, m, per_day, source):
    dim = calendar.monthrange(y, m)[1]
    for dd in range(1, dim + 1):
        db.add(DailyGeneration(tenant_id="ten_t", array_id=1, day=date(y, m, dd),
                               kwh=per_day, source=source))
    db.commit()


def _fill_gmp(db, y, m, per_day, days):
    for dd in range(1, days + 1):
        db.add(GmpDailyGeneration(tenant_id="ten_t", account_id=10, account_number="A1",
                                  array_id=1, day=date(y, m, dd), kwh=per_day,
                                  interval_count=96, source="gmp_api"))
    db.commit()


def test_gmp_real_wins_over_bill_prorate_when_month_fully_covered(db):
    _seed(db)
    _fill_daily(db, 2025, 6, 10.0, "bill_prorate")   # flat estimate: 30 * 10 = 300
    _fill_gmp(db, 2025, 6, 20.0, 30)                 # real, full 30-day coverage = 600
    out = gmcs_writer._daily_generation_by_month(db, 1, date(2025, 6, 1), date(2025, 6, 30))
    assert out[(2025, 6)] == pytest.approx(600.0)    # GMP meter wins


def test_partial_gmp_month_does_not_undercount(db):
    _seed(db)
    _fill_daily(db, 2025, 7, 10.0, "bill_prorate")   # 31 * 10 = 310
    _fill_gmp(db, 2025, 7, 99.0, 5)                  # only 5 days — guard must reject
    out = gmcs_writer._daily_generation_by_month(db, 1, date(2025, 7, 1), date(2025, 7, 31))
    assert out[(2025, 7)] == pytest.approx(310.0)    # bill estimate kept, not 5*99


def test_no_gmp_data_falls_back_to_daily(db):
    _seed(db)
    _fill_daily(db, 2025, 8, 12.0, "bill_prorate")
    out = gmcs_writer._daily_generation_by_month(db, 1, date(2025, 8, 1), date(2025, 8, 31))
    assert out[(2025, 8)] == pytest.approx(12.0 * 31)
