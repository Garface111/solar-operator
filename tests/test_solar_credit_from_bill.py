"""solar_credit_from_bill — Ford/Bruce's offtaker credit-rate model (Jun 2026).

An offtaker is billed for the EXCESS solar sent to the grid, at the credit the
utility ACTUALLY gave (EXCESS + SOLCRED summed), not the retail rate or a default.
SOLCRED is optional (default to excess alone). Banked months (excess credited at
~$0) are ignored. Numbers are the real GMP bills probed on prod (102154 / 98655).
"""
from api.rate_schedule import solar_credit_from_bill


def _bill(items):
    return {"billSegments": [{"segmentLineItems": items}]}


def test_excess_plus_solcred_real_bill_102154():
    # Real bill 102154: EXCESS 766 kWh -$164.36, SOLCRED 767 kWh -$32.98.
    r = solar_credit_from_bill(_bill([
        {"unitOfMeasure": "KWH", "unitCode": "EXCESS",  "unitCount": 766, "dollarAmount": -164.36},
        {"unitOfMeasure": "KWH", "unitCode": "SOLCRED", "unitCount": 767, "dollarAmount": -32.98},
        {"unitOfMeasure": "KWH", "unitCode": "CONSUMED","unitCount": 3080, "dollarAmount": 138.80},
    ]))
    assert r is not None
    assert r["excess_kwh"] == 766.0
    assert r["credit_usd"] == 197.34            # 164.36 + 32.98
    assert abs(r["credit_rate"] - 0.2576) < 0.001


def test_defaults_to_excess_when_no_solcred():
    r = solar_credit_from_bill(_bill([
        {"unitOfMeasure": "KWH", "unitCode": "EXCESS", "unitCount": 1000, "dollarAmount": -214.60},
    ]))
    assert r is not None
    assert r["credit_usd"] == 214.60
    assert abs(r["credit_rate"] - 0.2146) < 0.001


def test_banked_month_is_ignored():
    # Real bill 98655: 56,438 kWh excess but only -$25.32 + -$5.07 → ~$0.0005/kWh.
    r = solar_credit_from_bill(_bill([
        {"unitOfMeasure": "KWH", "unitCode": "EXCESS",  "unitCount": 56438, "dollarAmount": -25.32},
        {"unitOfMeasure": "KWH", "unitCode": "SOLCRED", "unitCount": 118,   "dollarAmount": -5.07},
    ]))
    assert r is None                            # banked → not billable


def test_no_excess_returns_none():
    assert solar_credit_from_bill(_bill([
        {"unitOfMeasure": "KWH", "unitCode": "CONSUMED", "unitCount": 500, "dollarAmount": 95.0},
    ])) is None
    assert solar_credit_from_bill(None) is None
    assert solar_credit_from_bill({}) is None
