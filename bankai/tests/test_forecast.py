from datetime import date, timedelta

from bankai.ingest import TxnIn, ingest_transactions, upsert_account
from bankai.intelligence.forecast import cash_flow_forecast, spending_anomalies


def seed_series(session, account, desc, amount, *, step_days, count, end_days_ago=2):
    last = date.today() - timedelta(days=end_days_ago)
    txns = [
        TxnIn(posted=last - timedelta(days=step_days * i), amount=amount, description=desc)
        for i in range(count)
    ]
    ingest_transactions(session, account, txns)


def test_forecast_projects_recurring_and_finds_low_point(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=3000.0
    )
    # biweekly paycheck +2000, monthly rent -2500
    seed_series(session, checking, "ACME PAYROLL", 2000.0, step_days=14, count=8, end_days_ago=3)
    seed_series(session, checking, "SUNSET APARTMENTS RENT", -2500.0, step_days=30, count=6,
                end_days_ago=8)
    fc = cash_flow_forecast(session, days=45)
    assert fc["starting_liquid"] == 3000.0
    assert fc["events"], "recurring events should project into the horizon"
    kinds = {e["merchant"][:4] for e in fc["events"]}
    assert len(kinds) == 2  # both series present
    assert fc["lowest_point"]["balance"] <= fc["starting_liquid"]
    # running balances are cumulative and end where projected_end says
    assert fc["events"][-1]["projected_balance"] == fc["projected_end_balance"]


def test_forecast_rolls_forward_missed_predictions(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=1000.0
    )
    # last charge long enough ago that next_date is already past
    seed_series(session, checking, "OLD GYM MEMBERSHIP", -50.0, step_days=30, count=6,
                end_days_ago=40)
    fc = cash_flow_forecast(session, days=60)
    for e in fc["events"]:
        assert e["date"] >= date.today().isoformat()


def test_forecast_ignores_investment_balances(session):
    upsert_account(session, source="manual", name="Home", kind="property", balance=1_000_000.0)
    upsert_account(session, source="csv", name="Brokerage", kind="investment", balance=500_000.0)
    upsert_account(session, source="csv", name="Checking", kind="checking", balance=2_000.0)
    fc = cash_flow_forecast(session, days=30)
    assert fc["starting_liquid"] == 2_000.0


def test_spending_anomalies_flags_spike_and_new_merchant(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=0.0
    )
    today = date.today()
    this_month = today.replace(day=1)
    txns = []
    # steady $100/month of groceries in each of the 3 prior months
    for months_back in (1, 2, 3):
        d = (this_month - timedelta(days=1)).replace(day=15)
        for _ in range(months_back - 1):
            d = (d.replace(day=1) - timedelta(days=1)).replace(day=15)
        txns.append(TxnIn(posted=d, amount=-100.0, description="WHOLE FOODS MARKET"))
    # this month: $450 groceries spike + a large first-time merchant
    txns.append(TxnIn(posted=this_month, amount=-450.0, description="WHOLE FOODS MARKET"))
    txns.append(TxnIn(posted=this_month, amount=-900.0, description="BAY AREA ROOFING LLC"))
    ingest_transactions(session, checking, txns)

    result = spending_anomalies(session)
    assert result["month"] == today.strftime("%Y-%m")
    spikes = {c["category"]: c for c in result["category_spikes"]}
    assert spikes, "expected at least one category spike"
    top = result["category_spikes"][0]
    assert top["this_month"] > top["monthly_baseline"]
    new = result["large_new_merchants"]
    assert any("ROOFING" in m["description"] for m in new)
    # the recurring grocery merchant is not "new"
    assert not any("WHOLE FOODS" in m["description"] for m in new)
