"""Tests for the Array Operator billing-workbook matcher.

Runs against the three real HCT sample workbooks (committed as fixtures). These
lock in that "match any spreadsheet" recognizes each family and extracts the
right customer, billing model, current period, and invoice math.
"""
import pathlib
from datetime import date

import pytest

from api.billing.matcher import match_billing_workbook, compute_invoice

FIX = pathlib.Path(__file__).parent / "fixtures" / "billing"


def _load(name: str):
    return match_billing_workbook((FIX / name).read_bytes(), allow_llm=False)


def test_fairlee_fixed_budget():
    m = _load("fairlee.xlsx")
    assert m.matched
    assert m.source == "schema"
    assert m.confidence >= 0.8
    assert m.customer["name"] == "Town of Fairlee"
    assert m.billing_model == "fixed_budget"
    assert m.billing_rate == pytest.approx(0.9)
    assert m.allocation_pct == pytest.approx(0.34)
    lp = m.latest_period
    assert lp is not None and lp.customer_kwh == pytest.approx(9121)
    # Fixed budget → amount owed is the flat budget figure, not the metered value.
    assert m.computed_invoice["amount_owed"] == pytest.approx(1250.0)
    # The metered solar value still matches the ledger's own Value column.
    assert m.computed_invoice["solar_value"] == pytest.approx(lp.value, rel=1e-3)


def test_norwich_percent_of_array():
    m = _load("norwich.xlsx")
    assert m.matched
    assert m.customer["name"] == "Norwich Fire District"
    assert m.billing_model == "percent_of_array"
    assert m.allocation_pct == pytest.approx(0.16)
    lp = m.latest_period
    assert lp.customer_kwh == pytest.approx(4292)
    # percent_of_array → amount owed equals the billed value (value × rate).
    assert m.computed_invoice["amount_owed"] == pytest.approx(lp.bill, rel=1e-3)
    assert m.computed_invoice["billed_value"] == pytest.approx(865.19, abs=0.05)


def test_valley_cares_flat_rate_picks_current_sheet():
    m = _load("valley_cares.xlsx")
    assert m.matched
    # Must pick the live "Valley Cares Data" ledger, NOT the stale "SAMPLE" sheet.
    assert m.data_sheet == "Valley Cares Data"
    assert m.customer["name"] == "Valley Cares, Inc."
    assert m.billing_model == "flat_rate"
    # Flat rate → amount owed is the flat figure ($2,150), not the metered value.
    assert m.computed_invoice["amount_owed"] == pytest.approx(2150.0)
    assert m.latest_period.customer_kwh == pytest.approx(17252)


def test_field_map_has_core_columns():
    m = _load("fairlee.xlsx")
    for key in ("month", "start", "end", "array_kwh", "customer_kwh",
                "tariff", "value", "bill"):
        assert key in m.field_map


def test_invoice_math_matches_ledger_for_every_period():
    """Our compute_invoice must reproduce the ledger's own Value/Bill columns."""
    m = _load("norwich.xlsx")
    checked = 0
    for p in m.periods:
        if not (p.customer_kwh and p.tariff and p.value):
            continue
        inv = compute_invoice(p.customer_kwh, p.tariff, p.adder,
                              m.billing_rate, "percent_of_array", None)
        assert inv["solar_value"] == pytest.approx(p.value, rel=1e-3)
        if p.bill:
            assert inv["billed_value"] == pytest.approx(p.bill, rel=1e-3)
        checked += 1
    assert checked > 12  # exercised across years of data


def test_garbage_file_does_not_crash():
    m = match_billing_workbook(b"not a spreadsheet", allow_llm=False)
    assert not m.matched
    assert m.confidence == 0.0
    assert m.warnings


def test_template_header_lifted():
    m = _load("fairlee.xlsx")
    assert "HCT Sun Enterprises" in (m.template.get("operator") or "")
    assert m.template.get("fixed_amount") == pytest.approx(1250.0)


def test_compute_invoice_models():
    # percent → billed = value × rate
    inv = compute_invoice(1000, 0.18, 0.04, 0.9, "percent_of_array", None)
    assert inv["solar_value"] == pytest.approx(220.0)
    assert inv["billed_value"] == pytest.approx(198.0)
    assert inv["amount_owed"] == pytest.approx(198.0)
    # fixed → amount owed is the fixed figure
    inv2 = compute_invoice(1000, 0.18, 0.04, 0.9, "fixed_budget", 1250)
    assert inv2["amount_owed"] == pytest.approx(1250.0)
