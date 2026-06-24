"""Cross-tab template repro (Paul's templates pull from a data tab).

The invoice sheet must render without breaking references into other tabs. The
isolation step KEEPS the other sheets (hidden) so cross-tab formulas resolve on
the headless render — instead of deleting them (which turned `=+NFD!G6` into
#REF!) or nulling uncached cross-sheet refs (which silently dropped real data).
"""
import io
import openpyxl

from api.billing.repro.template_repro import _isolate_to_invoice_sheet, _autofit_columns


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


def test_autofit_widens_narrow_date_and_number_columns():
    """A date/number in a too-narrow column renders as '###' (it can't overflow into
    an empty neighbor the way text does). _autofit_columns must widen the column to
    fit — even when the cell is still a LIVE formula (=TODAY(), the Invoice/Due Date
    case) and even when an empty neighbor exists that text could have borrowed."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.column_dimensions["B"].width = 4.0          # too narrow for a date
    ws.column_dimensions["D"].width = 3.0          # too narrow for a big number
    d = ws["B2"]; d.value = "=TODAY()"; d.number_format = "m/d/yyyy"   # live date formula
    n = ws["D2"]; n.value = 1234567.89; n.number_format = "#,##0.00"   # wide number value
    # C2 / E2 left EMPTY on purpose — a date/number can't borrow them.
    _autofit_columns(ws)
    assert ws.column_dimensions["B"].width > 8.0, ws.column_dimensions["B"].width
    assert ws.column_dimensions["D"].width > 8.0, ws.column_dimensions["D"].width


def test_autofit_gives_substituted_font_dates_a_generous_width():
    """The actual Paul bug: a date in 'Chalkboard' (a font the renderer lacks) is
    substituted with a WIDER font, so it overflows a column that looks wide enough by
    Excel's metric and renders '###'. _autofit_columns gives a non-standard-font date a
    generous allowance — comfortably more than the same date in a standard font."""
    from openpyxl.styles import Font

    def width_after(font_name):
        wb = openpyxl.Workbook(); ws = wb.active
        ws.column_dimensions["E"].width = 13.5           # Paul's actual column width
        c = ws["E2"]; c.value = "=TODAY()"; c.number_format = "mm-dd-yy"
        c.font = Font(name=font_name, size=16.0)
        _autofit_columns(ws)
        return ws.column_dimensions["E"].width

    chalk = width_after("Chalkboard")     # substituted → generous
    calibri = width_after("Calibri")      # standard → tuned
    assert chalk > 25.0, chalk            # wide enough for a 16pt substituted date
    assert chalk > calibri                # more than the standard-font allowance
