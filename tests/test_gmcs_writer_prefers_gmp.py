"""_daily_generation_by_month must return only the UTILITY's measured sub-monthly
generation — the real GMP interval meter (GmpDailyGeneration, near-full coverage).
It must NOT surface inverter/vendor telemetry (solaredge/fronius/…) or the
redundant bill_prorate smear (the real bill kWh is applied separately in
build_workbook via per_group). Ford 2026-07-16: NEPOOL reports settle on GMP,
not vendor data — London_SE was reporting SolarEdge because inverter rows
displaced the bill and then won the merge. The bill→report path (no undercount
when there's no meter) is covered end-to-end in test_gmcs_writer.py.
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


def test_gmp_real_meter_gives_the_month(db):
    _seed(db)
    _fill_daily(db, 2025, 6, 10.0, "bill_prorate")   # redundant smear — must be ignored
    _fill_gmp(db, 2025, 6, 20.0, 30)                 # real, full 30-day coverage = 600
    out = gmcs_writer._daily_generation_by_month(db, 1, date(2025, 6, 1), date(2025, 6, 30))
    assert out[(2025, 6)] == pytest.approx(600.0)    # GMP interval meter


def test_partial_gmp_month_is_rejected_not_undercounted(db):
    _seed(db)
    _fill_daily(db, 2025, 7, 10.0, "bill_prorate")   # redundant smear — ignored here
    _fill_gmp(db, 2025, 7, 99.0, 5)                  # only 5 days — coverage guard rejects
    out = gmcs_writer._daily_generation_by_month(db, 1, date(2025, 7, 1), date(2025, 7, 31))
    # No genuine meter for the month → the month is absent from the daily map, so
    # build_workbook uses the real bill kWh (per_group) instead of a 5-day fragment.
    assert (2025, 7) not in out


def test_vendor_and_bill_prorate_are_excluded_from_the_base(db):
    _seed(db)
    # One row per (array, day): bill_prorate on days 1-15, solaredge on 16-31.
    for dd in range(1, 16):
        db.add(DailyGeneration(tenant_id="ten_t", array_id=1, day=date(2025, 8, dd),
                               kwh=12.0, source="bill_prorate"))
    for dd in range(16, 32):
        db.add(DailyGeneration(tenant_id="ten_t", array_id=1, day=date(2025, 8, dd),
                               kwh=500.0, source="solaredge"))
    db.commit()
    out = gmcs_writer._daily_generation_by_month(db, 1, date(2025, 8, 1), date(2025, 8, 31))
    # Both excluded and no GMP meter → empty; the bill kWh is applied by build_workbook.
    assert (2025, 8) not in out
