from bankai.intelligence.categorize import categorize


def test_known_merchants():
    assert categorize("HANNAFORD #8123 BURLINGTON VT", -54.20) == "groceries"
    assert categorize("GREEN MOUNTAIN POWER BILLPAY", -120.0) == "utilities"
    assert categorize("NETFLIX.COM", -15.49) == "subscriptions"
    assert categorize("ACME PAYROLL DIRECT DEP", 2500.0) == "income"
    assert categorize("ZELLE TRANSFER TO SAVINGS", -500.0) == "transfer"


def test_unknown_outflow_uncategorized_inflow_income():
    assert categorize("SOME RANDOM PLACE", -10.0) == "uncategorized"
    assert categorize("SOME RANDOM REFUND", 10.0) == "income"
