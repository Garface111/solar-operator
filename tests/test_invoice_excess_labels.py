"""The rendered invoice describes the GMP-credit basis: the kWh shown is the EXCESS
sent to grid (the net-metering credit basis), labeled customer-facing as "Solar
generation" (per Ford). A BANKED month flags its rate as a reference estimate.
Workbook/legacy invoices keep the "production" wording.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_inv_lbl_test")

import pathlib
import secrets
import tempfile
from datetime import datetime

import pdfplumber

from api.db import SessionLocal
from api.models import Tenant, Array, UtilityAccount, Bill, BillingReportSubscription
from api.billing import delivery, invoice as inv_mod


def _seed_banked():
    tid = "ten_invlbl_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="InvLbl",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        a = Array(tenant_id=tid, name="Big Array", region="VT")
        db.add(a); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=a.id, provider="zzz_lbl",
                              account_number="X" + secrets.token_hex(3))
        db.add(acct); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    period_start=datetime(2026, 5, 1), period_end=datetime(2026, 5, 31),
                    kwh_generated=50000, kwh_sent_to_grid=50000.0,
                    solar_credit_usd=None))      # banked → reference rate path
        db.commit()
        return tid, a.id, acct.id


def _pdf_text(match):
    with tempfile.TemporaryDirectory() as tmp:
        p = inv_mod.render_invoice_pdf(match, pathlib.Path(tmp) / "i.pdf")
        with pdfplumber.open(str(p)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)


def test_gmp_credit_invoice_labels_generation_and_flags_banked():
    tid, aid, acct_id = _seed_banked()
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Offtaker Co",
        utility_account_id=acct_id, array_id=aid, allocation_pct=0.5,
        billing_model="percent_of_array")
    m = delivery.build_manual_match(sub)
    assert m.computed_invoice["net_rate_source"] == "gmp_credit_reference"
    text = _pdf_text(m)
    # The array-total + share-% breakdown lines were removed per Ford.
    assert "Solar generation sent to grid" not in text
    assert "Your share of the array" not in text
    # The offtaker's own kWh is still shown, GMP-labeled (excess, not gross production).
    assert "Your share of the generation" in text
    assert "Your share of production" not in text           # GMP path uses the generation label
    assert "banked" in text.lower()                          # honest reference note
