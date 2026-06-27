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


# ── Multi-kWh-column sheet (Paul's "Fairlee" shape): whole-array, the offtaker's
# share, and a cumulative running total. The offtaker's NAMED column must win, the
# cumulative one must never, and a split tariff+adder must beat a bare tariff.
_FAIRLEE = [
    ["", "", "", "", "Cumm"],                                          # sub-header over the cumulative col
    ["Month", "kWh whole array", "kWh Fairlee", "Tariff+Adder", "KwH"],
    ["2026-04", 26640, 9058, 0.19417, 51653],
    ["2026-05", 29700, 10098, 0.19417, 61751],
]


def test_cumulative_column_never_chosen_as_generation():
    m = st.detect_structure(_make_xlsx(_FAIRLEE), "f.xlsx")
    assert m["ok"]
    assert m["columns"]["generation"] != 4               # the cumulative "KwH" is demoted


def test_offtaker_name_picks_their_share_column():
    m = st.detect_structure(_make_xlsx(_FAIRLEE), "f.xlsx",
                            st.name_hint_tokens("Town of Fairlee"))
    assert m["columns"]["generation"] == 2               # "kWh Fairlee" (their share)
    assert m["columns"]["rate"] == 3                      # "Tariff+Adder", not a bare tariff
    assert m["columns"]["period"] == 0


def test_amount_detects_bill_column():
    m = st.detect_structure(_make_xlsx([["Month", "kWh Generated", "Bill"],
                                        ["2026-05", 1000, 257.6]]), "b.xlsx")
    assert m["columns"].get("amount") == 2


def test_name_hint_tokens_drops_filler():
    assert st.name_hint_tokens("Town of Fairlee") == ["fairlee"]
    assert st.name_hint_tokens(None) == []


def test_ai_unavailable_falls_back_to_heuristic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from api.billing import sheet_tracker_ai as ai
    assert ai.ai_available() is False
    res = st.ingest_upload(_make_xlsx(SAMPLE_ROWS), "maple.xlsx", "Maple Farm")
    assert res["ok"]
    assert res["mapping"]["via"] == "heuristic"
    assert res["mapping"]["columns"]["generation"] == 1


def test_ai_parser_validates_and_rejects():
    from api.billing import sheet_tracker_ai as ai
    good = ('{"header_row":1,"columns":{"period":0,"generation":1,"amount":4,'
            '"consumption":null},"confidence":0.9}')
    p = ai._parse(good, width=5, n_rows=5)
    assert p and p["columns"] == {"period": 0, "generation": 1, "amount": 4}
    assert ai._parse("prose " + good + " tail", 5, 5)                          # tolerant of surrounding text
    assert ai._parse('{"header_row":1,"columns":{"period":0},"confidence":0.9}', 5, 5) is None      # no generation
    assert ai._parse('{"header_row":1,"columns":{"generation":1},"confidence":0.2}', 5, 5) is None  # low confidence
    assert ai._parse('{"header_row":1,"columns":{"generation":99},"confidence":0.9}', 5, 5) is None # out of range
    assert ai._parse("not json", 5, 5) is None
