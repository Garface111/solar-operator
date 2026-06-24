"""Cross-tab template repro (Paul's templates pull from a data tab).

The invoice sheet must render without breaking references into other tabs. The
isolation step KEEPS the other sheets (hidden) so cross-tab formulas resolve on
the headless render — instead of deleting them (which turned `=+NFD!G6` into
#REF!) or nulling uncached cross-sheet refs (which silently dropped real data).
"""
import io
import openpyxl

from api.billing.repro.template_repro import _isolate_to_invoice_sheet


def _wb_with_crosstab():
    wb = openpyxl.Workbook()
    inv = wb.active
    inv.title = "Invoice"
    inv["A1"] = "Amount:"
    inv["B1"] = "=Data!A1"      # cross-tab ref, no cached value (fresh workbook)
    inv["B2"] = "=B1*2"         # intra-sheet formula
    data = wb.create_sheet("Data")
    data["A1"] = 500
    wb.create_sheet("Trend")["A1"] = 1
    return wb


def test_isolate_keeps_data_sheets_hidden_not_deleted():
    wb = _wb_with_crosstab()
    ws = wb["Invoice"]
    buf = io.BytesIO(); wb.save(buf)
    _isolate_to_invoice_sheet(wb, ws, buf.getvalue())

    # The data tabs are KEPT (so cross-tab formulas can still resolve), just hidden.
    assert "Data" in wb.sheetnames and "Trend" in wb.sheetnames
    assert wb["Data"].sheet_state == "hidden"
    assert wb["Trend"].sheet_state == "hidden"
    assert ws.sheet_state == "visible"
    # Active sheet is the visible invoice — never a hidden one.
    assert wb.active.title == "Invoice"


def test_isolate_does_not_null_uncached_cross_sheet_refs():
    """A cross-sheet formula with no Excel cache keeps its formula (resolves live
    against the hidden sheet) — it is NOT nulled, which was the data-loss bug."""
    wb = _wb_with_crosstab()
    ws = wb["Invoice"]
    buf = io.BytesIO(); wb.save(buf)
    _isolate_to_invoice_sheet(wb, ws, buf.getvalue())
    assert ws["B1"].value == "=Data!A1"   # preserved, not None
