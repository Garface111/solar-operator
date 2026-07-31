from datetime import date, timedelta

from bankai.intelligence.recurring import detect_recurring_from_rows


def _series(start: date, step: int, count: int, amount: float, name: str, jitter=0):
    rows = []
    d = start
    for i in range(count):
        rows.append(("acct_1", name, d, amount))
        d = d + timedelta(days=step + (jitter if i % 2 else -jitter))
    return rows


def test_detects_monthly_bill():
    rows = _series(date(2026, 1, 5), 30, 6, -120.0, "GREEN MOUNTAIN POWER", jitter=2)
    found = detect_recurring_from_rows(rows)
    assert len(found) == 1
    s = found[0]
    assert s.cadence == "monthly" and s.is_bill
    assert s.next_date > s.last_date


def test_detects_biweekly_paycheck():
    rows = _series(date(2026, 1, 2), 14, 8, 2500.0, "ACME PAYROLL")
    found = detect_recurring_from_rows(rows)
    assert len(found) == 1
    assert found[0].cadence == "biweekly" and not found[0].is_bill


def test_ignores_irregular_spending():
    rows = [
        ("acct_1", "RANDOM STORE", date(2026, 1, 1), -20.0),
        ("acct_1", "RANDOM STORE", date(2026, 1, 4), -35.0),
        ("acct_1", "RANDOM STORE", date(2026, 2, 20), -12.0),
        ("acct_1", "RANDOM STORE", date(2026, 3, 2), -99.0),
    ]
    assert detect_recurring_from_rows(rows) == []


def test_ignores_wildly_varying_amounts():
    rows = _series(date(2026, 1, 5), 30, 5, -100.0, "SHOP")
    rows[2] = ("acct_1", "SHOP", rows[2][2], -400.0)  # one 4x outlier breaks stability
    assert detect_recurring_from_rows(rows) == []


def test_two_txns_not_enough():
    rows = _series(date(2026, 1, 5), 30, 2, -50.0, "NETFLIX")
    assert detect_recurring_from_rows(rows) == []
