"""QuickBooks/Xero invoice-export layout (api/billing/qb_export.py).

Locks the CSV column layout to Anna's sample (NRC Invoices April 2026.CSV) so a
future refactor can't silently shift a column and break her bookkeeping import.
"""
from api.billing import qb_export as q


def test_header_matches_nrc_sample():
    hdr = q._header_row()
    assert len(hdr) == 19
    assert hdr[0] == "Customer"
    assert hdr[10] == "Num"
    assert hdr[12] == "Date"
    assert hdr[13] == "Due Date"
    assert hdr[15] == "Description"
    assert hdr[16] == "Qty"
    assert hdr[17] == "Open Balance"
    # Byte-identical to the sample's header line.
    import csv, io
    buf = io.StringIO(); csv.writer(buf).writerow(hdr)
    assert buf.getvalue().strip() == "Customer,,,,,,,,,,Num,,Date,Due Date,,Description,Qty,Open Balance,"


def test_invoice_row_placement_and_dates():
    inv = {"customer_name": "St. J Muni", "invoice_number": "2026-06",
           "invoice_date": "2026-06-30", "due_date": "2026-07-28",
           "month": "2026-06", "amount_owed": 1351.07}
    row = q._invoice_row(inv, account_code="400")
    assert row[0] == "St. J Muni"
    assert row[10] == "2026-06"
    assert row[12] == "6/30/2026"      # M/D/YYYY, no leading zeros
    assert row[13] == "7/28/2026"
    assert row[16] == 1
    assert row[17] == 1351.07
    assert row[18] == "400"


def test_zero_and_missing_amount_skipped_never_fabricated():
    assert q._invoice_row({"customer_name": "X", "amount_owed": 0}, "") is None
    assert q._invoice_row({"customer_name": "X", "amount_owed": None}, "") is None


def test_budget_override_is_the_billed_amount():
    row = q._invoice_row({"customer_name": "B", "invoice_number": "9",
                          "amount_owed": 10.0, "budget_override": True,
                          "budgeted_amount": 88.5, "invoice_date": "2026-06-01",
                          "due_date": "2026-06-29"}, "")
    assert row[17] == 88.5
