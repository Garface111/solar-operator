"""Tests for durable GMP bill-PDF capture + the auto-attach read seam.

Covers the end-to-end ingestion half built into worker.py:
  _capture_current_bill_pdf persists the PDF bytes in-row, and
  gmp_bill_pdf_read.get_bill_pdf_for_period returns them for the array+period.
"""
from __future__ import annotations

import secrets
from datetime import datetime, date

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, Bill


def _tenant_array_account_bill():
    """Seed a tenant → array → GMP account → one bill row (no PDF yet)."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="PDF Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(10),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="PDF Co", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="PDF Array", client_id=c.id, fuel_type="solar")
        db.add(arr); db.flush()
        ua = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                            account_number="GMP-1", enabled=True,
                            extra={"currentBillUrlBinary": "https://gmp.test/bill"})
        db.add(ua); db.flush()
        db.add(Bill(tenant_id=tid, account_id=ua.id,
                    bill_date=datetime(2026, 5, 31), period_start=datetime(2026, 5, 1),
                    period_end=datetime(2026, 5, 31), kwh_generated=1000,
                    parse_status="parsed"))
        db.commit()
        return tid, arr.id, ua.id


def test_capture_persists_pdf_bytes_and_read_seam_returns_them(monkeypatch, tmp_path):
    from api import worker
    from api.reports import gmp_bill_pdf_read as gbp

    tid, array_id, acct_id = _tenant_array_account_bill()

    # Fake the GMP adapter's fetch_bill_pdf: write a real PDF to the path.
    class FakeAdapter:
        def fetch_bill_pdf(self, url, out_path):
            out_path.write_bytes(b"%PDF-1.4\nGMP bill bytes\n")
            return out_path, "application/pdf"

    # Before capture: read seam finds no durable PDF.
    assert gbp.get_bill_pdf_for_period(array_id) is None
    assert gbp.has_capturable_gmp_account(array_id) is True

    # Run the capture (the JSON-path helper) against the seeded account.
    with SessionLocal() as db:
        acct = db.get(UtilityAccount, acct_id)
        res = worker._capture_current_bill_pdf(db, tid, acct, FakeAdapter())
        db.commit()
    assert res["saved"] is True, res
    assert res["bytes"] > 0

    # After capture: the read seam returns the durable bytes for the array.
    found = gbp.get_bill_pdf_for_period(array_id)
    assert found is not None
    assert found["bytes"].startswith(b"%PDF")
    assert found["content_type"] == "application/pdf"
    assert found["account_id"] == acct_id


def test_capture_rejects_non_pdf_auth_redirect(monkeypatch):
    """An auth redirect returns HTML, not a PDF — must NOT be stored as one."""
    from api import worker
    tid, array_id, acct_id = _tenant_array_account_bill()

    class HtmlAdapter:
        def fetch_bill_pdf(self, url, out_path):
            out_path.write_bytes(b"<html>login required</html>")
            return out_path, "text/html"

    with SessionLocal() as db:
        acct = db.get(UtilityAccount, acct_id)
        res = worker._capture_current_bill_pdf(db, tid, acct, HtmlAdapter())
        db.commit()
    assert res["saved"] is False
    assert "not a PDF" in res["reason"]
    # Nothing persisted.
    from api.reports import gmp_bill_pdf_read as gbp
    assert gbp.get_bill_pdf_for_period(array_id) is None
