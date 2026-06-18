"""Tests for the GMP full-record extraction (data sponge)."""
from api.adapters import gmp


def _bill(gen=None, consume=None, sent=None, **top):
    segs = [{"startDate": "2024-01-01", "endDate": "2024-01-31", "segmentLineItems": []}]
    li = segs[0]["segmentLineItems"]
    if gen is not None:
        li.append({"unitOfMeasure": "KWH", "unitCode": "GENERATE", "unitCount": gen})
    if consume is not None:
        li.append({"unitOfMeasure": "KWH", "unitCode": "CONSUME", "unitCount": consume})
    if sent is not None:
        li.append({"unitOfMeasure": "KWH", "unitCode": "EXPORT", "unitCount": sent})
    b = {"billSegments": segs, "billNumber": "B1", "billDate": "2024-02-01"}
    b.update(top)
    return b


def test_full_record_extracts_generation_and_consumption():
    m = gmp.bill_json_to_metrics(_bill(gen=900.0, consume=400.0, sent=500.0,
                                        amountDue=120.50, isNetMetered=True))
    assert m["kwh_generated"] == 900
    assert m["kwh_consumed"] == 400
    assert m["kwh_sent_to_grid"] == 500.0
    assert m["kwh_gross_generated"] == 900.0
    assert m["is_net_metered"] is True
    assert m["total_cost"] == 120.50
    # blended rate = cost/consumed*100 = 120.5/400*100
    assert abs(m["avg_rate_cents_kwh"] - (120.50 / 400 * 100)) < 0.01


def test_raw_json_always_kept():
    b = _bill(gen=100.0, weirdFutureField={"x": 1})
    m = gmp.bill_json_to_metrics(b)
    # the ENTIRE bill is preserved so a field we don't model isn't lost
    assert m["raw_json"]["weirdFutureField"] == {"x": 1}


def test_consumption_only_bill_is_absorbed_not_skipped():
    # A no-generation bill must still carry its consumption/record (the sponge).
    m = gmp.bill_json_to_metrics(_bill(consume=350.0, amountDue=88.0))
    assert m["kwh_generated"] is None          # no solar that period
    assert m["kwh_consumed"] == 350            # but we KEEP the consumption
    assert m["total_cost"] == 88.0
    assert m["raw_json"] is not None


def test_sponge_status_idle_shape():
    from api.sponge import sponge_status
    s = sponge_status("nonexistent_tenant", "gmp")
    assert s["status"] == "idle"
    assert s["pct"] == 0
