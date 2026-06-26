"""Tests for the bring-your-own generation-spreadsheet auto-updater.

Covers the pure logic that does NOT need a DB:
  * heuristic column detection on an ARBITRARY-column sheet (xlsx + csv)
  * CSV normalization to xlsx preserving cell addressing
  * idempotent monthly append (never double-appends the same period)
"""
import io

import openpyxl

from api.billing import sheet_tracker as st


def _make_xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MyLedger"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# An operator's OWN, idiosyncratic layout: a title row, then headers that don't
# match any template — "Billing Month", "Solar Produced (kWh)", "Home Usage",
# "Credit $/kWh", "Total Credit".
SAMPLE_ROWS = [
    ["Maple Farm Solar — Generation Log", None, None, None, None],
    ["Billing Month", "Solar Produced (kWh)", "Home Usage", "Credit $/kWh", "Total Credit"],
    ["2026-03", 1820.5, 940, 0.2576, 469.0],
    ["2026-04", 2010.0, 880, 0.2576, 517.8],
    ["2026-05", 2310.2, 810, 0.2576, 595.1],
]


def test_detect_arbitrary_columns():
    blob = _make_xlsx(SAMPLE_ROWS)
    m = st.detect_structure(blob, "maple.xlsx")
    assert m["ok"], m
    assert m["sheet"] == "MyLedger"
    assert m["header_row"] == 1            # 0-based: title row 0, header row 1
    cols = m["columns"]
    assert cols["period"] == 0
    assert cols["generation"] == 1
    assert cols["consumption"] == 2
    assert cols["rate"] == 3
    assert cols["amount"] == 4
    assert m["last_period"] == "2026-05"
    assert m["data_rows"] == 3


def test_detect_csv_and_ingest_normalizes_to_xlsx():
    csv = (
        "Period,Generation kWh,Amount Due\n"
        "2026-04,1500,386.40\n"
        "2026-05,1600,412.16\n"
    ).encode("utf-8")
    res = st.ingest_upload(csv, "ledger.csv")
    assert res["ok"], res
    m = res["mapping"]
    assert m["kind"] == "xlsx"           # normalized
    assert m["sheet"] == "Generation"
    assert m["columns"]["period"] == 0
    assert m["columns"]["generation"] == 1
    assert m["columns"]["amount"] == 2
    # the normalized workbook actually opens + has the rows
    wb = openpyxl.load_workbook(io.BytesIO(res["workbook"]))
    ws = wb["Generation"]
    assert ws.cell(row=1, column=2).value == "Generation kWh"
    assert ws.cell(row=2, column=1).value == "2026-04"


def test_append_writes_into_mapped_columns():
    blob = _make_xlsx(SAMPLE_ROWS)
    m = st.detect_structure(blob, "maple.xlsx")
    new = st.append_period_row(blob, m, {
        "period": "2026-06", "generation": 2450.0,
        "consumption": 770, "rate": 0.2576, "amount": 631.1,
    })
    wb = openpyxl.load_workbook(io.BytesIO(new))
    ws = wb["MyLedger"]
    # appended onto row 6 (1-based): title(1) header(2) data(3,4,5) → new=6
    assert ws.cell(row=6, column=1).value == "2026-06"
    assert ws.cell(row=6, column=2).value == 2450.0
    assert ws.cell(row=6, column=5).value == 631.1
    # existing rows untouched
    assert ws.cell(row=5, column=1).value == "2026-05"
    assert ws.cell(row=5, column=2).value == 2310.2


def test_period_idempotency_matching():
    assert st._period_matches("2026-05", "2026-05")
    assert st._period_matches("May 2026", "2026-05")
    assert st._period_matches("5/31/2026", "2026-05")
    assert not st._period_matches("2026-04", "2026-05")
    assert not st._period_matches(None, "2026-05")


def test_detect_rejects_unmappable():
    blob = _make_xlsx([["Notes", "Misc"], ["hello", "world"]])
    m = st.detect_structure(blob, "junk.xlsx")
    assert not m["ok"]
    assert m["warnings"]
