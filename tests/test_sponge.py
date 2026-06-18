"""Tests for the GMP full-record extraction (data sponge), grounded on the REAL
GMP bill JSON structure verified against live prod raw_json (2026-06-18)."""
from api.adapters import gmp


def _bill(gen=None, consumed=None, excess=None, calc_dollars=None, **top):
    """Build a bill mirroring real GMP shape: KWH line items by unitCode +
    segmentCalcs carrying dollarAmount (the bill's money)."""
    li = []
    if gen is not None:
        li.append({"unitOfMeasure": "KWH", "unitCode": "GENERATE", "unitCount": gen, "dollarAmount": None})
    if consumed is not None:
        li.append({"unitOfMeasure": "KWH", "unitCode": "CONSUMED", "unitCount": consumed, "dollarAmount": None})
    if excess is not None:
        li.append({"unitOfMeasure": "KWH", "unitCode": "EXCESS", "unitCount": excess, "dollarAmount": None})
    seg = {"startDate": "2024-01-01", "endDate": "2024-01-31", "segmentLineItems": li}
    if calc_dollars is not None:
        seg["segmentCalcs"] = [{"rate": "G01", "dollarAmount": d, "billText": "x"} for d in calc_dollars]
    b = {"billSegments": [seg], "billNumber": "B1", "billDate": "2024-02-01"}
    b.update(top)
    return b


def test_real_codes_extract_generation_consumption_excess():
    m = gmp.bill_json_to_metrics(_bill(gen=1833.0, consumed=66.0, excess=300.0,
                                       calc_dollars=[18.15, -41.03, -12.0, 5.0]))
    assert m["kwh_generated"] == 1833
    assert m["kwh_consumed"] == 66
    assert m["kwh_sent_to_grid"] == 300.0
    assert m["kwh_gross_generated"] == 1833.0
    # total cost = sum of segmentCalcs dollarAmount = 18.15-41.03-12+5 = -29.88 (a credit)
    assert m["total_cost"] == round(18.15 - 41.03 - 12.0 + 5.0, 2)
    assert m["total_cost"] < 0
    # negative total = net credit earned
    assert m["net_credit"] == -m["total_cost"]
    assert m["is_net_metered"] is True
    assert m["supplier"] == "Green Mountain Power"


def test_blended_rate_uses_abs_cost_over_consumption():
    m = gmp.bill_json_to_metrics(_bill(consumed=100.0, calc_dollars=[21.46]))
    # |21.46| / 100 kWh * 100 = 21.46 cents/kWh
    assert abs(m["avg_rate_cents_kwh"] - 21.46) < 0.01


def test_raw_json_always_kept():
    b = _bill(gen=100.0, calc_dollars=[5.0], weirdFutureField={"x": 1})
    m = gmp.bill_json_to_metrics(b)
    assert m["raw_json"]["weirdFutureField"] == {"x": 1}


def test_consumption_only_bill_absorbed_with_cost():
    m = gmp.bill_json_to_metrics(_bill(consumed=350.0, calc_dollars=[88.0]))
    assert m["kwh_generated"] is None       # no solar that period
    assert m["kwh_consumed"] == 350         # consumption kept
    assert m["total_cost"] == 88.0          # cost kept
    assert m["net_credit"] is None          # positive bill, no credit
    assert m["is_net_metered"] is False


def test_calc_dollars_preferred_over_line_item_dollars():
    # When both segmentCalcs and line-item dollarAmounts exist, use calcs ONLY
    # (they sum to the same total — never double-count).
    b = _bill(consumed=66.0, calc_dollars=[10.0, 20.0])
    b["billSegments"][0]["segmentLineItems"][0]["dollarAmount"] = 30.0  # mirror total
    m = gmp.bill_json_to_metrics(b)
    assert m["total_cost"] == 30.0   # from calcs (10+20), not 30+30


def test_sponge_status_idle_shape():
    from api.sponge import sponge_status
    s = sponge_status("nonexistent_tenant", "gmp")
    assert s["status"] == "idle"
    assert s["pct"] == 0
