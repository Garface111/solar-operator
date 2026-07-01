"""QuickBooks + Xero invoice-export layouts (api/billing/qb_export.py).

QuickBooks and Xero need DIFFERENT import layouts — lock both so a refactor can't
silently shift a column and break an operator's bookkeeping import.
"""
from api.billing import qb_export as q


INV = {"customer_name": "St. J Muni", "invoice_number": "2026-06",
       "invoice_date": "2026-06-30", "due_date": "2026-07-28",
       "month": "2026-06", "amount_owed": 1351.07}


class _Sub:
    client_email = "billing@stjmuni.example"


def test_format_normalization():
    assert q.normalize_format("quickbooks") == "quickbooks"
    assert q.normalize_format("QB") == "quickbooks"
    assert q.normalize_format("qbo") == "quickbooks"
    assert q.normalize_format("xero") == "xero"
    assert q.normalize_format("") == "xero"          # default
    assert q.normalize_format(None) == "xero"


def test_xero_row_layout():
    assert q.XERO_HEADER[0] == "ContactName"
    assert q.XERO_HEADER[7] == "UnitAmount"
    assert q.XERO_HEADER[8] == "AccountCode"
    assert q.XERO_HEADER[9] == "TaxType"
    f = q._invoice_fields(INV, _Sub())
    row = q._xero_row(f, account_code="200", tax_type="Tax Exempt")
    assert row[0] == "St. J Muni"                     # ContactName
    assert row[1] == "billing@stjmuni.example"        # EmailAddress
    assert row[2] == "2026-06"                        # InvoiceNumber
    assert row[3] == "6/30/2026"                      # InvoiceDate M/D/YYYY
    assert row[6] == 1 and row[7] == 1351.07          # Quantity, UnitAmount
    assert row[8] == "200" and row[9] == "Tax Exempt"


def test_quickbooks_row_layout():
    assert q.QB_HEADER[0] == "InvoiceNo"
    assert q.QB_HEADER[1] == "Customer"
    assert q.QB_HEADER[4] == "Item(Product/Service)"
    assert q.QB_HEADER[8] == "ItemAmount"
    f = q._invoice_fields(INV, _Sub())
    row = q._qb_row(f, item_name="Solar Credit")
    assert row[0] == "2026-06"                        # InvoiceNo
    assert row[1] == "St. J Muni"                     # Customer
    assert row[4] == "Solar Credit"                   # Item
    assert row[6] == 1 and row[7] == 1351.07 and row[8] == 1351.07  # qty/rate/amount


def test_zero_or_missing_amount_skipped():
    assert q._invoice_fields({"customer_name": "X", "amount_owed": 0}, _Sub()) is None
    assert q._invoice_fields({"customer_name": "X", "amount_owed": None}, _Sub()) is None


def test_budget_override_is_the_billed_amount():
    f = q._invoice_fields({"customer_name": "B", "invoice_number": "9",
                           "amount_owed": 10.0, "budget_override": True,
                           "budgeted_amount": 88.5, "invoice_date": "2026-06-01",
                           "due_date": "2026-06-29"}, _Sub())
    assert f["amount"] == 88.5
