"""Tests for report_has_data() — the data-coverage guard that stops the
automatic scheduler from emailing a BLANK GMCS workbook.

Mirrors the three real prod situations found when diagnosing the "bogus
automatic reports" bug (all 'empty' clients on live NEPOOL tenants):
  1. client with arrays but NO utility accounts / bills / daily data → empty
  2. empty onboarding stub: client with NO arrays → empty
  3. client with bills in the reporting window → has data (must still send)

report_has_data must match build_workbook's rolling-quarter window + the same
two data sources (Bill calendar-day attribution + DailyGeneration), so it never
disagrees with what the rendered cells would show.
"""
from __future__ import annotations

import secrets
from datetime import datetime, date

from api.db import SessionLocal
from api.models import Array, Bill, Client, Tenant, UtilityAccount, DailyGeneration
from api.writers.gmcs_writer import report_has_data

# Q2 2024 in progress → last complete quarter = Q1 2024; window = Q4'22..Q1'24.
_REF = date(2024, 4, 1)
# A month inside the window and one well outside it.
_IN_WINDOW = (2024, 1)
_OUT_OF_WINDOW = (2020, 6)


def _new_tenant_client() -> tuple[str, int]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Coverage Co", contact_email=f"{tid}@test.com",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
        ))
        db.flush()
        c = Client(tenant_id=tid, name="Coverage Client", active=True)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    return tid, cid


def _add_array(tid: str, cid: int, *, excluded: bool = False) -> int:
    with SessionLocal() as db:
        a = Array(tenant_id=tid, client_id=cid, name="Arr",
                  nepool_gis_id="GIS1", excluded=excluded)
        db.add(a)
        db.flush()
        aid = a.id
        db.commit()
    return aid


def _add_account_with_bill(tid: str, array_id: int, *, year: int, month: int,
                           kwh: int = 1500) -> None:
    with SessionLocal() as db:
        ua = UtilityAccount(tenant_id=tid, array_id=array_id, provider="gmp",
                            account_number="ACC_" + secrets.token_hex(4))
        db.add(ua)
        db.flush()
        db.add(Bill(
            tenant_id=tid, account_id=ua.id,
            bill_date=datetime(year, month, 15),
            period_start=datetime(year, month, 1),
            kwh_generated=kwh,
            document_number=f"doc-{secrets.token_hex(4)}",
        ))
        db.commit()


def _add_daily(tid: str, array_id: int, *, day: date, kwh: float = 42.0) -> None:
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=array_id, day=day, kwh=kwh))
        db.commit()


# ── case 2: no arrays → empty (onboarding stub like prod clients 117/233/268) ──
def test_no_arrays_is_empty():
    tid, cid = _new_tenant_client()
    assert report_has_data(cid, reference_date=_REF) is False


# ── case 1: arrays but no accounts/bills/daily → empty (prod clients 171/267) ──
def test_arrays_but_no_data_is_empty():
    tid, cid = _new_tenant_client()
    _add_array(tid, cid)
    assert report_has_data(cid, reference_date=_REF) is False


# ── case 3: bills in window → has data (the reports that SHOULD send) ──────────
def test_bills_in_window_has_data():
    tid, cid = _new_tenant_client()
    aid = _add_array(tid, cid)
    _add_account_with_bill(tid, aid, year=_IN_WINDOW[0], month=_IN_WINDOW[1])
    assert report_has_data(cid, reference_date=_REF) is True


# ── bills only OUTSIDE the window → still empty for this report period ─────────
def test_bills_only_out_of_window_is_empty():
    tid, cid = _new_tenant_client()
    aid = _add_array(tid, cid)
    _add_account_with_bill(tid, aid, year=_OUT_OF_WINDOW[0], month=_OUT_OF_WINDOW[1])
    assert report_has_data(cid, reference_date=_REF) is False


# ── DailyGeneration in window (no bills) → has data ───────────────────────────
def test_daily_generation_in_window_has_data():
    tid, cid = _new_tenant_client()
    aid = _add_array(tid, cid)
    _add_daily(tid, aid, day=date(2024, 1, 15), kwh=100.0)
    assert report_has_data(cid, reference_date=_REF) is True


# ── excluded arrays don't count (matches the renderer's excluded filter) ──────
def test_excluded_array_with_data_is_empty():
    tid, cid = _new_tenant_client()
    aid = _add_array(tid, cid, excluded=True)
    _add_account_with_bill(tid, aid, year=_IN_WINDOW[0], month=_IN_WINDOW[1])
    assert report_has_data(cid, reference_date=_REF) is False


# ── zero-kWh daily rows don't count as data ───────────────────────────────────
def test_zero_kwh_daily_is_empty():
    tid, cid = _new_tenant_client()
    aid = _add_array(tid, cid)
    _add_daily(tid, aid, day=date(2024, 1, 15), kwh=0.0)
    assert report_has_data(cid, reference_date=_REF) is False
