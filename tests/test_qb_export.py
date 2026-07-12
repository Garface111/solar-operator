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
    # QuickBooks Desktop (IIF) aliases (Bruce 2026-07-07).
    assert q.normalize_format("iif") == "iif"
    assert q.normalize_format("IIF") == "iif"
    assert q.normalize_format("quickbooks-desktop") == "iif"
    assert q.normalize_format("qbd") == "iif"
    assert q.normalize_format("QuickBooks-Desktop") == "iif"


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


# ── memo passthrough ─────────────────────────────────────────────────────────

def test_memo_overrides_default_description():
    """An operator-set memo replaces the default 'Solar credit ({month})' in all
    three layouts; a blank memo keeps the default (Bruce 2026-07-07)."""
    default = q._invoice_fields(INV, _Sub())
    assert default["desc"] == "Solar credit (2026-06)"          # default
    blank = q._invoice_fields(INV, _Sub(), memo="   ")
    assert blank["desc"] == "Solar credit (2026-06)"            # blank → default
    custom = q._invoice_fields(INV, _Sub(), memo="June 2026 net-metering credit")
    assert custom["desc"] == "June 2026 net-metering credit"
    # It flows into every layout's description/memo column.
    assert q._xero_row(custom, "", "")[5] == "June 2026 net-metering credit"
    assert q._qb_row(custom, "Solar Credit")[5] == "June 2026 net-metering credit"


# ── QuickBooks Desktop IIF ───────────────────────────────────────────────────

def _iif_lines(text):
    return [ln for ln in text.replace("\r\n", "\n").split("\n") if ln != ""]


def test_iif_header_block_once_at_top():
    f = q._invoice_fields(INV, _Sub())
    text = q._iif_blocks([f], income_account="Solar Credit Income")
    lines = _iif_lines(text)
    # Exactly one !-header block at the top: !TRNS, !SPL, !ENDTRNS.
    assert lines[0].split("\t") == q.IIF_TRNS_HEADER
    assert lines[1].split("\t") == q.IIF_SPL_HEADER
    assert lines[2].split("\t") == q.IIF_ENDTRNS_HEADER
    assert lines[0].startswith("!TRNS\t") and lines[0].count("\t") == 7
    # Only one header block, no matter how many invoices.
    assert sum(1 for ln in lines if ln.startswith("!TRNS")) == 1


def test_iif_trns_spl_endtrns_per_invoice_and_balances():
    f = q._invoice_fields(INV, _Sub())              # amount 1351.07
    text = q._iif_blocks([f], income_account="Solar Credit Income")
    lines = _iif_lines(text)[3:]                     # skip the header block
    assert lines[0].split("\t") == [
        "TRNS", "INVOICE", "6/30/2026", "Accounts Receivable", "St. J Muni",
        "1351.07", "2026-06", "Solar credit (2026-06)"]
    assert lines[1].split("\t") == [
        "SPL", "INVOICE", "6/30/2026", "Solar Credit Income", "St. J Muni",
        "-1351.07", "2026-06", "Solar credit (2026-06)"]
    assert lines[2] == "ENDTRNS"
    # The AR debit and income credit NET TO ZERO — QuickBooks Desktop rejects an
    # unbalanced transaction.
    trns_amt = float(lines[0].split("\t")[5])
    spl_amt = float(lines[1].split("\t")[5])
    assert round(trns_amt + spl_amt, 2) == 0.0
    # DOCNUM = invoice number; MEMO = description; both on TRNS and SPL.
    assert lines[0].split("\t")[6] == "2026-06" and lines[1].split("\t")[6] == "2026-06"


def test_iif_is_tab_delimited_and_multi_invoice():
    a = q._invoice_fields(INV, _Sub())
    b = q._invoice_fields(
        {"customer_name": "Brooks House", "invoice_number": "2026-01",
         "invoice_date": "2026-07-07", "due_date": "2026-08-04",
         "month": "2026-01", "amount_owed": 721.54}, _Sub())
    text = q._iif_blocks([a, b], income_account="")   # blank → default income acct
    # Tab-delimited (every data line has 7 tabs for the 8 columns; ENDTRNS has 0).
    for ln in _iif_lines(text):
        if ln.startswith(("TRNS", "SPL", "!TRNS", "!SPL")):
            assert ln.count("\t") == 7, ln
    # Two balanced blocks → 3 header + 3+3 body = 9 non-empty lines.
    assert len(_iif_lines(text)) == 9
    assert "Brooks House" in text and "Solar Credit Income" in text  # default income acct
    # Custom income account is honored when provided.
    text2 = q._iif_blocks([a], income_account="4200 · Solar Income")
    assert "4200 · Solar Income" in text2 and "Solar Credit Income" not in text2


def test_iif_memo_flows_to_docnum_and_memo():
    f = q._invoice_fields(INV, _Sub(), memo="Q2 solar settlement")
    text = q._iif_blocks([f], income_account="")
    body = _iif_lines(text)[3:]
    assert body[0].split("\t")[7] == "Q2 solar settlement"   # TRNS MEMO
    assert body[1].split("\t")[7] == "Q2 solar settlement"   # SPL MEMO


def test_iif_field_strips_tabs_and_newlines():
    # A stray tab/newline in a customer name must not break the delimiting.
    f = q._invoice_fields({"customer_name": "Bad\tName\nInc", "invoice_number": "1",
                           "invoice_date": "2026-06-30", "due_date": "2026-07-28",
                           "month": "2026-06", "amount_owed": 5.0}, _Sub())
    text = q._iif_blocks([f], income_account="")
    body = _iif_lines(text)[3:]
    assert body[0].count("\t") == 7                          # still 8 fields
    assert "Bad Name Inc" in body[0]
