"""
Unit tests for api/adapters/vec.py.

Covers parse_usage (aria-label parsing), parse_bill (billing-history row
normalization), and parse_extension_payload (full POST body normalization).

Fixtures are in tests/fixtures/vec/ — independent of any specific real-world
tenant so these tests don't overfit to Bruce's VEC account.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

import pytest

from api.adapters.vec import parse_bill, parse_extension_payload, parse_usage

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vec"


# ─── parse_usage ─────────────────────────────────────────────────────────────


def test_parse_usage_normal():
    label = (
        "Jun 2023 Billing Period. Usage Dates: May 18 - June 17. "
        "Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 58 °F"
    )
    row = parse_usage(label)
    assert row is not None
    assert row["period_label"] == "Jun 2023"
    assert row["meter_id"] == "63698951"
    assert row["kwh"] == 0.0
    assert row["avg_temp_f"] == 58.0
    assert row["period_start"] == datetime(2023, 5, 18)
    assert row["period_end"] == datetime(2023, 6, 17)
    assert row["usage_dates_raw"] == "May 18 - June 17"


def test_parse_usage_no_temp():
    """Optional temperature field is absent."""
    label = (
        "Jan 2024 Billing Period. Usage Dates: Dec 18 - Jan 19. "
        "Meter 63698951 - Consumption - kWh: 12345 kWh."
    )
    row = parse_usage(label)
    assert row is not None
    assert row["kwh"] == 12345.0
    assert row["avg_temp_f"] is None
    # Jan 2024 billing: start is Dec 2023 (year before), end is Jan 2024
    assert row["period_start"] == datetime(2023, 12, 18)
    assert row["period_end"] == datetime(2024, 1, 19)


def test_parse_usage_year_wrap_dec():
    """Dec billing period — dates should stay in the same year (Nov → Dec)."""
    label = (
        "Dec 2022 Billing Period. Usage Dates: Nov 19 - Dec 18. "
        "Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 31 °F"
    )
    row = parse_usage(label)
    assert row is not None
    assert row["period_start"] == datetime(2022, 11, 19)
    assert row["period_end"] == datetime(2022, 12, 18)


def test_parse_usage_generation_type():
    """Meter type 'Generation' should parse identically to 'Consumption'."""
    label = (
        "Mar 2024 Billing Period. Usage Dates: Feb 20 - Mar 19. "
        "Meter 63698951 - Generation - kWh: 8760.5 kWh. Average Temperature: 41 °F"
    )
    row = parse_usage(label)
    assert row is not None
    assert row["kwh"] == pytest.approx(8760.5)
    assert row["period_start"] == datetime(2024, 2, 20)
    assert row["period_end"] == datetime(2024, 3, 19)


def test_parse_usage_from_fixture_file():
    """All lines in the fixture file should parse successfully."""
    lines = (FIXTURES / "aria_labels.txt").read_text().strip().splitlines()
    for line in lines:
        if not line.strip():
            continue
        row = parse_usage(line)
        assert row is not None, f"Failed to parse: {line}"
        assert row["meter_id"] == "63698951"
        assert row["kwh"] >= 0.0


def test_parse_usage_invalid():
    assert parse_usage("not a valid aria-label") is None
    assert parse_usage("") is None
    assert parse_usage("Jun 2023 Billing Period.") is None  # incomplete


# ─── parse_bill ──────────────────────────────────────────────────────────────


def test_parse_bill_normal():
    rows = json.loads((FIXTURES / "billing_rows.json").read_text())
    parsed = parse_bill(rows[0])
    assert parsed["account_id"] == "6578300"
    assert parsed["customer_name"] == "WEST GLOVER ROARING BROOK SOLAR LLC"
    assert parsed["billing_date"] == datetime(2023, 11, 15)
    assert parsed["bill_amount"] == pytest.approx(-245.67)
    assert parsed["adjustments"] == pytest.approx(0.0)
    assert parsed["total_due"] == pytest.approx(-245.67)
    assert parsed["bill_uuid"] == "abc123-def456-7890"
    assert "billPdfService" in (parsed["pdf_url"] or "")


def test_parse_bill_second_row():
    rows = json.loads((FIXTURES / "billing_rows.json").read_text())
    parsed = parse_bill(rows[1])
    assert parsed["billing_date"] == datetime(2023, 10, 16)
    assert parsed["bill_amount"] == pytest.approx(-198.40)


def test_parse_bill_missing_amounts():
    row = {
        "account_id": "9999999",
        "billing_date": "01/01/2024",
        "bill_amount": "",
        "adjustments": None,
        "total_due": "  ",
    }
    parsed = parse_bill(row)
    assert parsed["bill_amount"] is None
    assert parsed["adjustments"] is None
    assert parsed["total_due"] is None
    assert parsed["billing_date"] == datetime(2024, 1, 1)


def test_parse_bill_no_date():
    row = {"account_id": "9999999", "billing_date": ""}
    parsed = parse_bill(row)
    assert parsed["billing_date"] is None


# ─── parse_extension_payload ─────────────────────────────────────────────────


def _vec_payload(**overrides):
    base = {
        "provider": "vec",
        "capturedAt": "2024-01-01T00:00:00Z",
        "user": {"hostname": "vermontelectric.smarthub.coop"},
        "auth": {},
        "accounts": [
            {
                "accountNumber": "6578300",
                "customerName": "WGRBS LLC",
                "serviceAddress": "123 Main Rd, West Glover VT",
            }
        ],
        "bills": [],
        "usage": [],
    }
    base.update(overrides)
    return base


def test_parse_extension_payload_with_accounts():
    normalized = parse_extension_payload(_vec_payload())
    assert normalized["provider"] == "vec"
    assert normalized["auth"] == {}
    assert len(normalized["accounts"]) == 1
    assert normalized["accounts"][0]["account_number"] == "6578300"
    assert normalized["accounts"][0]["nickname"] == "WGRBS LLC"
    assert normalized["accounts"][0]["service_address"] == {
        "line1": "123 Main Rd, West Glover VT"
    }


def test_parse_extension_payload_dedupes_accounts():
    """Two payload accounts with the same accountNumber → one in output."""
    payload = _vec_payload(
        accounts=[
            {"accountNumber": "6578300", "customerName": "A"},
            {"accountNumber": "6578300", "customerName": "A (dup)"},
        ]
    )
    normalized = parse_extension_payload(payload)
    assert len(normalized["accounts"]) == 1


def test_parse_extension_payload_accounts_from_bills():
    """When accounts list is empty, derive accounts from bill rows."""
    bills = json.loads((FIXTURES / "billing_rows.json").read_text())
    payload = _vec_payload(accounts=[], bills=bills)
    normalized = parse_extension_payload(payload)
    assert len(normalized["accounts"]) == 1  # bills have same account_id
    assert normalized["accounts"][0]["account_number"] == "6578300"


def test_parse_extension_payload_bills_raw_passed_through():
    """Raw bills and usage are preserved for future processing."""
    bills = json.loads((FIXTURES / "billing_rows.json").read_text())
    payload = _vec_payload(bills=bills)
    normalized = parse_extension_payload(payload)
    assert len(normalized["bills_raw"]) == 2
    assert normalized["usage_raw"] == []


def test_parse_extension_payload_no_accounts_no_bills():
    """Empty payload → empty accounts, no crash."""
    payload = _vec_payload(accounts=[], bills=[], usage=[])
    normalized = parse_extension_payload(payload)
    assert normalized["accounts"] == []
    assert normalized["provider"] == "vec"
