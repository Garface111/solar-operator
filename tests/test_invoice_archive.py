"""Invoice archive / monthly directory (api/billing/invoice_archive.py).

Proves the archive assembly Anna/Bruce asked for: the manifest groups
month → array → offtaker with honest availability flags, and the .zip lays files
out as <month>/<array>/{invoice, offtaker bill, array bill}. The heavy invoice
render + bill parse are covered by their own tests, so here we monkeypatch
build_match/generate_files and assert the ARCHIVE structure (grouping, bill
inclusion from stored pdf_bytes, zip paths).
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_arch_test")

import io
import zipfile
import pathlib
import secrets
from datetime import date, datetime

from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, Client,
                        BillingReportSubscription)
from api.billing import invoice_archive as ar


class _FakePeriod:
    end = date(2026, 6, 30)


class _FakeMatch:
    matched = True
    latest_period = _FakePeriod()
    computed_invoice = {"period_end": "2026-06-30"}
    customer = {"name": "St. J Muni"}


def _seed():
    tid = "ten_arch_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Arch",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Timberworks", region="VT"); db.add(arr); db.flush()
        acc_arr = UtilityAccount(tenant_id=tid, provider="gmp", account_number="ARR",
                                 array_id=arr.id)
        acc_off = UtilityAccount(tenant_id=tid, provider="gmp", account_number="OFF")
        db.add_all([acc_arr, acc_off]); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acc_arr.id, period_start=datetime(2026, 6, 1),
                    period_end=datetime(2026, 6, 30), kwh_generated=28788,
                    pdf_bytes=b"%PDF-ARRAYBILL", pdf_content_type="application/pdf"))
        db.add(Bill(tenant_id=tid, account_id=acc_off.id, period_start=datetime(2026, 6, 1),
                    period_end=datetime(2026, 6, 30), kwh_generated=7343,
                    pdf_bytes=b"%PDF-OFFTAKERBILL", pdf_content_type="application/pdf"))
        c = Client(tenant_id=tid, name="St. J Muni", active=True); db.add(c); db.flush()
        db.add(BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="St. J Muni",
            array_id=arr.id, allocation_pct=0.2553, utility_account_id=acc_off.id,
            billing_model="percent_of_array", cadence="monthly"))
        db.commit()
        return tid


def _patch(monkeypatch):
    monkeypatch.setattr(ar, "build_match", lambda sub: _FakeMatch())

    def fake_generate(match, formats, include_summary, out_dir, sub=None, **kw):
        p = pathlib.Path(out_dir) / "St._J_Muni_2026-06_invoice.pdf"
        p.write_bytes(b"%PDF-INVOICE")
        return [p]
    monkeypatch.setattr(ar, "generate_files", fake_generate)


def test_manifest_groups_month_array_offtaker(monkeypatch):
    tid = _seed()
    _patch(monkeypatch)
    with SessionLocal() as db:
        man = ar.list_archive(db, tid)
    assert man["ok"] and man["month_count"] == 1
    m = man["months"][0]
    assert m["month"] == "2026-06"
    assert m["invoice_count"] == 1
    arr = m["arrays"][0]
    assert arr["array_name"] == "Timberworks"
    assert arr["array_bill_available"] is True
    off = arr["offtakers"][0]
    assert off["customer_name"] == "St. J Muni"
    assert off["invoice_available"] is True
    assert off["offtaker_bill_available"] is True


def test_zip_layout_month_array_invoice_and_bills(monkeypatch):
    tid = _seed()
    _patch(monkeypatch)
    with SessionLocal() as db:
        data, fname, count = ar.build_archive_zip(db, tid)
    assert fname == "offtaker-invoices-2026-06.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "2026-06/Timberworks/St-J-Muni_invoice.pdf" in names, names
    assert "2026-06/Timberworks/St-J-Muni_offtaker-bill.pdf" in names, names
    assert "2026-06/Timberworks/_Master-Array-Bill_Timberworks_2026-06.pdf" in names, names
    assert count == 3


def test_empty_archive_has_readme_not_silent_empty(monkeypatch):
    tid = "ten_arch_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Empty",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.commit()
    with SessionLocal() as db:
        data, fname, count = ar.build_archive_zip(db, tid)
    assert count == 0
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert any(n.endswith("README.txt") for n in names), names
