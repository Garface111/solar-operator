"""Gauntlet: offtaker bulk-import power preparer + detector on messy spreadsheets.

Builds many deliberately hostile roster shapes (multi-sheet, section banners,
European CSV, phones next to accounts, totals, junk titles, multi-row headers)
and asserts we still recover offtaker_name + allocation + array identity.
"""
from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from openpyxl import Workbook

from api.billing.roster_detector import detect_roster_columns as detect
from api.billing.roster_prepare import prepare_roster_grid, is_phone_like
from api.billing.routes import _bulk_pct


# ── fixture fleet ────────────────────────────────────────────────────────────

ARRAYS = [
    {"id": 1, "name": "Maple Street Solar (53984)"},
    {"id": 2, "name": "Route 7 Community Array"},
    {"id": 3, "name": "Hilltop Farm"},
    {"id": 4, "name": "Londonderry"},
]
UACCTS = [
    {"utility_account_id": 101, "array_id": 1, "array_name": "Maple Street Solar",
     "nickname": "Maple St", "provider": "gmp", "account_number": "10001", "has_bill": True},
    {"utility_account_id": 102, "array_id": 2, "array_name": "Route 7 Community",
     "nickname": None, "provider": "gmp", "account_number": "20002", "has_bill": True},
    {"utility_account_id": 103, "array_id": 3, "array_name": "Hilltop Farm",
     "nickname": None, "provider": "vec", "account_number": "30003", "has_bill": True},
    {"utility_account_id": 104, "array_id": 4, "array_name": "Londonderry",
     "nickname": "Londo", "provider": "gmp", "account_number": "40004", "has_bill": True},
]


def _csv(grid: list[list[Any]], delim: str = ",") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim)
    for row in grid:
        w.writerow(["" if c is None else c for c in row])
    return buf.getvalue().encode("utf-8")


def _xlsx(sheets: dict[str, list[list[Any]]]) -> bytes:
    wb = Workbook()
    # remove default
    wb.remove(wb.active)
    for name, grid in sheets.items():
        ws = wb.create_sheet(title=name[:31])
        for row in grid:
            ws.append(["" if c is None else c for c in row])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _mapped(res: dict, field: str):
    cm = (res.get("column_map") or {}).get(field)
    return cm["index"] if cm else None


def _assert_core(res: dict, label: str):
    assert res.get("ok"), f"{label}: not ok — {res.get('warnings')}"
    assert _mapped(res, "offtaker_name") is not None, f"{label}: missing offtaker_name"
    assert _mapped(res, "allocation_pct") is not None, f"{label}: missing allocation_pct"
    has_array = (
        _mapped(res, "array_name") is not None
        or _mapped(res, "account_number") is not None
        or _mapped(res, "master_account_number") is not None
    )
    assert has_array, f"{label}: missing array identity — map={res.get('column_map')}"


# ── unit helpers ──────────────────────────────────────────────────────────────

def test_phone_not_account():
    assert is_phone_like("802-555-1212")
    assert is_phone_like("(802) 555-1212")
    assert not is_phone_like("10001")
    assert not is_phone_like("ACCT-40004")


def test_bulk_pct_european():
    v, err = _bulk_pct("25,5%")
    assert err is None and abs(v - 0.255) < 1e-6
    v, err = _bulk_pct("25,5")
    assert err is None and abs(v - 0.255) < 1e-6
    v, err = _bulk_pct("50")
    assert err is None and abs(v - 0.5) < 1e-9


# ── clean + classic messy ─────────────────────────────────────────────────────

def test_clean_labeled():
    grid = [
        ["Offtaker Name", "Array Name", "Allocation %", "Email"],
        ["Alice Cooper", "Maple Street Solar", "50%", "alice@example.com"],
        ["Bob Dylan", "Route 7 Community Array", "30%", "bob@example.com"],
        ["Carol King", "Hilltop Farm", "20%", "carol@example.com"],
    ]
    res = detect(_csv(grid), "clean.csv", ARRAYS, UACCTS)
    _assert_core(res, "clean")
    assert _mapped(res, "email") == 3


def test_junk_title_weird_headers():
    grid = [
        ["Community Solar Roster — Q3 2026", None, None, None, None],
        ["Generated 2026-07-01. Confidential.", None, None, None, None],
        [None, None, None, None, None],
        ["Solar Site", "% of System", "Customer", "Contact", "Discount"],
        ["Maple Street Solar", "0.50", "Alice Cooper", "alice@example.com", "0.10"],
        ["Route 7 Community", "0.30", "Bob Dylan", "bob@example.com", "0.05"],
        ["Hilltop Farm", "0.20", "Carol King", "carol@example.com", "0.00"],
    ]
    res = detect(_csv(grid), "messy.csv", ARRAYS, UACCTS)
    _assert_core(res, "junk_title")
    assert res["header_row"] == 3
    assert _mapped(res, "array_name") == 0


# ── multi-sheet: instructions + sample + real data ────────────────────────────

def test_multi_sheet_picks_real_data_not_sample():
    sample = [
        ["Offtaker", "Array", "Share %"],
        ["EXAMPLE PERSON", "Example Array", "100"],
    ]
    instructions = [
        ["How to fill this"],
        ["Put offtaker names in column A"],
    ]
    data = [
        ["Subscriber", "Site", "Ownership %", "Email"],
        ["Alice Cooper", "Maple Street Solar", "40", "alice@example.com"],
        ["Bob Dylan", "Londonderry", "60", "bob@example.com"],
    ]
    raw = _xlsx({
        "Instructions": instructions,
        "SAMPLE": sample,
        "Members": data,
    })
    prep = prepare_roster_grid(raw, "multi.xlsx", ARRAYS, UACCTS)
    assert prep["ok"]
    assert prep["sheet"] == "Members", prep["sheets"]
    res = detect(raw, "multi.xlsx", ARRAYS, UACCTS)
    _assert_core(res, "multi_sheet")
    assert res["sheet"] == "Members"


# ── section banners (no array column) ─────────────────────────────────────────

def test_section_banner_arrays():
    grid = [
        ["Community Solar Members"],
        ["Name", "Share %", "Email"],
        ["Maple Street Solar"],  # section banner
        ["Alice Cooper", "25%", "alice@example.com"],
        ["Bob Dylan", "25%", "bob@example.com"],
        ["Hilltop Farm"],
        ["Carol King", "50%", "carol@example.com"],
        ["Total", "100%", ""],
    ]
    res = detect(_csv(grid), "sections.csv", ARRAYS, UACCTS)
    _assert_core(res, "section_banner")
    # synthetic array column should exist
    assert res.get("section_array_col") is not None or _mapped(res, "array_name") is not None
    # data rows recovered
    assert res["data_rows"] >= 3


# ── European semicolon + decimal comma ────────────────────────────────────────

def test_european_semicolon_csv():
    # semicolon delimiter, decimal comma shares
    text = (
        "Abonnent;Anlage;Anteil;E-Mail\n"
        "Alice Cooper;Maple Street Solar;50,0;alice@example.com\n"
        "Bob Dylan;Hilltop Farm;50,0;bob@example.com\n"
    )
    raw = text.encode("utf-8")
    res = detect(raw, "eu.csv", ARRAYS, UACCTS)
    _assert_core(res, "european")
    v, err = _bulk_pct("50,0")
    assert err is None and abs(v - 0.5) < 1e-9


def test_latin1_accents():
    text = (
        "Offtaker,Array,Share %,Email\n"
        "José García,Maple Street Solar,50%,jose@example.com\n"
        "François Dupont,Hilltop Farm,50%,francois@example.com\n"
    )
    raw = text.encode("latin-1")
    res = detect(raw, "accents.csv", ARRAYS, UACCTS)
    _assert_core(res, "latin1")


# ── phone vs account ──────────────────────────────────────────────────────────

def test_phone_column_not_chosen_as_account():
    grid = [
        ["Customer", "Array", "Share %", "Phone", "Account #", "Email"],
        ["Alice", "Maple Street Solar", "50", "802-555-0100", "10001", "a@x.com"],
        ["Bob", "Hilltop Farm", "50", "802-555-0199", "30003", "b@x.com"],
    ]
    res = detect(_csv(grid), "phone.csv", ARRAYS, UACCTS)
    _assert_core(res, "phone")
    acct = _mapped(res, "account_number")
    phone = 3
    # Account should be col 4, not phone col 3
    if acct is not None:
        assert acct != phone, f"phone column stolen account mapping: {res['column_map']}"


# ── multi-row header ──────────────────────────────────────────────────────────

def test_multirow_header_merge():
    grid = [
        ["Export from Utility Portal"],
        ["Subscriber", "Solar", "Ownership", "Contact"],
        ["Name", "Site", "Interest %", "Email"],
        ["Alice Cooper", "Maple Street Solar", "40", "alice@example.com"],
        ["Bob Dylan", "Route 7 Community Array", "60", "bob@example.com"],
    ]
    res = detect(_csv(grid), "multirow.csv", ARRAYS, UACCTS)
    _assert_core(res, "multirow")


# ── account-first (no array name) ─────────────────────────────────────────────

def test_account_first_no_array_name():
    grid = [
        ["Member Name", "Share %", "Utility Account", "Email"],
        ["Alice Cooper", "50", "10001", "alice@example.com"],
        ["Bob Dylan", "50", "30003", "bob@example.com"],
    ]
    res = detect(_csv(grid), "acctfirst.csv", ARRAYS, UACCTS)
    # ok may be true with account_number standing in for array
    assert _mapped(res, "offtaker_name") is not None
    assert _mapped(res, "allocation_pct") is not None
    assert _mapped(res, "account_number") is not None
    assert res["ok"] is True


# ── wide sheet with junk columns ──────────────────────────────────────────────

def test_wide_sheet_junk_columns():
    grid = [
        ["ID", "Ignore", "Notes", "Customer Name", "Foo", "Site Name", "Bar",
         "Allocation %", "Baz", "Email", "Internal code", "Phone"],
        ["1", "x", "vip", "Alice Cooper", "q", "Maple Street Solar", "w",
         "25%", "e", "alice@example.com", "ZZ", "802-555-0001"],
        ["2", "x", "", "Bob Dylan", "q", "Londonderry", "w",
         "75%", "e", "bob@example.com", "YY", "802-555-0002"],
    ]
    res = detect(_csv(grid), "wide.csv", ARRAYS, UACCTS)
    _assert_core(res, "wide")
    assert _mapped(res, "email") is not None


# ── totals interspersed ───────────────────────────────────────────────────────

def test_totals_do_not_break_detection():
    grid = [
        ["Offtaker", "Array", "Share %"],
        ["Alice", "Maple Street Solar", "30"],
        ["Bob", "Maple Street Solar", "20"],
        ["Subtotal Maple", "", "50"],
        ["Carol", "Hilltop Farm", "50"],
        ["Grand Total", "", "100"],
    ]
    res = detect(_csv(grid), "totals.csv", ARRAYS, UACCTS)
    _assert_core(res, "totals")


# ── tab-separated ─────────────────────────────────────────────────────────────

def test_tsv():
    grid = [
        ["Offtaker", "Array", "Share %", "Email"],
        ["Alice", "Maple Street Solar", "50", "a@x.com"],
        ["Bob", "Hilltop Farm", "50", "b@x.com"],
    ]
    res = detect(_csv(grid, delim="\t"), "roster.tsv", ARRAYS, UACCTS)
    _assert_core(res, "tsv")


# ── two share columns (allocation + discount) ─────────────────────────────────

def test_allocation_and_discount_columns():
    grid = [
        ["Customer", "Project", "Share %", "Discount %", "Email"],
        ["Alice", "Maple Street Solar", "50", "10", "a@x.com"],
        ["Bob", "Hilltop Farm", "50", "5", "b@x.com"],
    ]
    res = detect(_csv(grid), "disc.csv", ARRAYS, UACCTS)
    _assert_core(res, "discount")
    # share should not be the discount col
    share = _mapped(res, "allocation_pct")
    disc = _mapped(res, "discount_pct")
    assert share is not None
    if disc is not None:
        assert share != disc


# ── Excel float accounts ──────────────────────────────────────────────────────

def test_xlsx_float_account_numbers():
    # openpyxl will store numbers as floats
    raw = _xlsx({
        "Roster": [
            ["Offtaker", "Array", "Share %", "Account"],
            ["Alice", "Maple Street Solar", 50, 10001],  # int
            ["Bob", "Hilltop Farm", 50, 30003.0],  # float-ish
        ]
    })
    res = detect(raw, "float_acct.xlsx", ARRAYS, UACCTS)
    _assert_core(res, "float_acct")


# ── ownership % + bill to ─────────────────────────────────────────────────────

def test_ownership_bill_to_headers():
    grid = [
        ["Bill To", "Facility", "Ownership Interest", "Email Address"],
        ["Green Grocer LLC", "Route 7 Community Array", "33.3%", "ap@greengrocer.com"],
        ["Town of Elsewhere", "Londonderry", "66.7%", "clerk@elsewhere.gov"],
    ]
    res = detect(_csv(grid), "own.csv", ARRAYS, UACCTS)
    _assert_core(res, "ownership")


# ── blank columns between fields ──────────────────────────────────────────────

def test_blank_columns_between():
    grid = [
        ["Offtaker", "", "", "Array", "", "Share %", "", "Email"],
        ["Alice", "", "", "Maple Street Solar", "", "50%", "", "a@x.com"],
        ["Bob", "", "", "Hilltop Farm", "", "50%", "", "b@x.com"],
    ]
    res = detect(_csv(grid), "blanks.csv", ARRAYS, UACCTS)
    _assert_core(res, "blanks")


# ── complex: multi-sheet + section + junk ─────────────────────────────────────

def test_complex_kitchen_sink_xlsx():
    raw = _xlsx({
        "README": [["This workbook has multiple tabs"], ["Use Members Export"]],
        "Pivot Summary": [["Array", "Count"], ["Maple", 2]],
        "Members Export": [
            ["Utility export generated 2026-07-01"],
            ["Confidential — do not distribute"],
            [],
            ["Participant", "Contact Info", "Allocation", "Notes", "Phone"],
            ["Name", "Email", "%", "Internal", "Mobile"],
            ["Maple Street Solar"],
            ["Alice Cooper", "alice@example.com", "25%", "vip", "802-555-1111"],
            ["Bob Dylan", "bob@example.com", "25%", "", "802-555-2222"],
            ["Subtotal", "", "50%", "", ""],
            ["Londonderry"],
            ["Carol King", "carol@example.com", "50%", "board", "802-555-3333"],
            ["Grand Total", "", "100%", "", ""],
        ],
    })
    prep = prepare_roster_grid(raw, "kitchen.xlsx", ARRAYS, UACCTS)
    assert prep["sheet"] == "Members Export", prep["sheets"]
    res = detect(raw, "kitchen.xlsx", ARRAYS, UACCTS)
    _assert_core(res, "kitchen_sink")
    assert res["data_rows"] >= 3


# ── master + offtaker account columns ─────────────────────────────────────────

def test_master_and_offtaker_accounts():
    grid = [
        ["Offtaker", "Master Utility Account Number", "Offtaker Account Number",
         "Share %", "Email"],
        ["Brooks House", "40004", "55555", "50", "bh@example.com"],
        ["Other Co", "40004", "55556", "50", "oc@example.com"],
    ]
    res = detect(_csv(grid), "master.csv", ARRAYS, UACCTS)
    assert _mapped(res, "offtaker_name") is not None
    assert _mapped(res, "allocation_pct") is not None
    # at least one account field
    assert (
        _mapped(res, "master_account_number") is not None
        or _mapped(res, "account_number") is not None
    )


# ── random-ish stress: many offtakers ─────────────────────────────────────────

def test_large_roster_still_detects():
    grid = [["Customer Name", "Site Name", "Pct", "Email"]]
    sites = ["Maple Street Solar", "Route 7 Community Array", "Hilltop Farm", "Londonderry"]
    for i in range(120):
        grid.append([
            f"Customer {i:03d}",
            sites[i % 4],
            str(1 if i < 100 else 0.5),  # mostly 1% each
            f"c{i:03d}@example.com",
        ])
    res = detect(_csv(grid), "large.csv", ARRAYS, UACCTS)
    _assert_core(res, "large")
    assert res["data_rows"] >= 100


def test_utf16_le_csv():
    text = (
        "Offtaker,Array,Share %,Email\n"
        "Alice,Maple Street Solar,50%,a@x.com\n"
        "Bob,Hilltop Farm,50%,b@x.com\n"
    )
    raw = text.encode("utf-16")  # with BOM
    res = detect(raw, "utf16.csv", ARRAYS, UACCTS)
    _assert_core(res, "utf16")


# ── prepare sheet scoring sanity ──────────────────────────────────────────────

def test_prepare_prefers_good_sheet_name():
    raw = _xlsx({
        "Summary": [["a", "b"], ["1", "2"]],
        "Offtaker Roster": [
            ["Name", "Array", "Share %"],
            ["Alice", "Maple Street Solar", "100"],
        ],
    })
    prep = prepare_roster_grid(raw, "n.xlsx", ARRAYS, UACCTS)
    assert prep["sheet"] == "Offtaker Roster"


# Remove accidental bad import if present
def test_no_bogus_import():
    # ensure detect_roster_columns is the real one
    assert callable(detect)
