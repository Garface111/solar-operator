"""Pure-logic tests for the long-horizon projection. No network, no LLM, no clock
dependence beyond date.today() (all fixtures are anchored relative to it)."""
import json
from datetime import date

import pytest

from bankai.ingest import TxnIn, ingest_transactions, upsert_account
from bankai.intelligence.horizon import (
    _months_back,
    affordability,
    derive_baseline,
    mortgage_payment,
    project_wealth,
)


def seed_months(session, account, *, months, income=5000.0, spend=-3500.0):
    """One paycheck + one spend in each of the last `months` COMPLETE months."""
    today = date.today()
    txns = []
    for back in range(1, months + 1):
        start = _months_back(today, back)
        txns.append(
            TxnIn(posted=start.replace(day=10), amount=income, description="GLOBEX PAYROLL")
        )
        txns.append(
            TxnIn(posted=start.replace(day=20), amount=spend, description="CITY MARKET GROCERY")
        )
    ingest_transactions(session, account, txns)


def household(session, *, months=8, liquid=60_000.0, investments=200_000.0):
    checking = upsert_account(
        session, source="csv", name="Everyday Checking", kind="checking", balance=liquid
    )
    upsert_account(
        session, source="csv", name="Index Brokerage", kind="investment", balance=investments
    )
    seed_months(session, checking, months=months)
    return checking


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #


def test_baseline_derives_median_monthly_savings(session):
    household(session, months=8, liquid=60_000.0, investments=200_000.0)
    base = derive_baseline(session)

    assert base["months_observed"] == 8
    assert base["monthly_savings"] == 1500.0  # 5000 in, 3500 out
    assert base["monthly_income"] == 5000.0
    assert base["monthly_spend"] == 3500.0
    assert base["savings_rate_pct"] == 30.0
    assert base["balances"]["liquid"] == 60_000.0
    assert base["balances"]["investments"] == 200_000.0
    assert base["balances"]["net_worth"] == 260_000.0
    assert base["data_thin"] is False
    assert base["confidence"] == "high"
    assert base["assumptions"]


def test_baseline_excludes_transfers_and_partial_month(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=10_000.0
    )
    seed_months(session, checking, months=4)
    today = date.today()
    # A big internal transfer must not read as income...
    ingest_transactions(
        session,
        checking,
        [
            TxnIn(
                posted=_months_back(today, 1).replace(day=11),
                amount=25_000.0,
                description="TRANSFER FROM SAVINGS",
            ),
            # ...and the in-progress month must not enter the median at all.
            TxnIn(posted=today, amount=99_000.0, description="GLOBEX PAYROLL"),
        ],
    )
    base = derive_baseline(session)

    assert base["monthly_savings"] == 1500.0
    assert all(m["month"] < today.strftime("%Y-%m") for m in base["months"])


def test_baseline_is_honest_about_thin_data(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=5_000.0
    )
    seed_months(session, checking, months=1)
    base = derive_baseline(session)

    assert base["months_observed"] == 1
    assert base["data_thin"] is True
    assert base["confidence"] == "low"
    assert any("complete month" in c for c in base["caveats"])


def test_baseline_on_empty_household_does_not_explode(session):
    base = derive_baseline(session)
    assert base["months_observed"] == 0
    assert base["monthly_savings"] == 0.0
    assert base["data_thin"] is True
    assert base["confidence"] == "low"
    assert base["caveats"]


def test_baseline_reports_income_cadence(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=1_000.0
    )
    seed_months(session, checking, months=6)
    income = derive_baseline(session)["income"]
    assert income["dominant_cadence"] == "monthly"
    assert income["implied_monthly_income"] == pytest.approx(5000.0, abs=1.0)


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #


def test_projection_is_deterministic_for_a_given_seed(session):
    household(session)
    a = project_wealth(session, 10, seed=1234, simulations=120)
    b = project_wealth(session, 10, seed=1234, simulations=120)
    assert a == b
    assert a["seed"] == 1234
    # a different seed moves the sampled bands
    c = project_wealth(session, 10, seed=99, simulations=120)
    assert c["bands"] != a["bands"]


def test_projection_bands_are_ordered_and_start_at_today(session):
    household(session)
    result = project_wealth(session, 15, seed=7, simulations=200)

    assert len(result["bands"]) == 16  # year 0 through 15
    for band in result["bands"]:
        assert band["p10"] <= band["p50"] <= band["p90"]
    year0 = result["bands"][0]
    assert year0["p10"] == year0["p50"] == year0["p90"] == 260_000.0
    assert result["deterministic"][0]["net_worth"] == 260_000.0
    assert result["starting"]["net_worth"] == 260_000.0


def test_more_savings_produces_a_higher_median(session):
    household(session)
    base = project_wealth(session, 10, seed=5, simulations=150)
    more = project_wealth(
        session, 10, monthly_savings_delta=1000.0, seed=5, simulations=150
    )
    assert more["monthly_savings_used"] == 2500.0
    assert more["summary"]["median_net_worth"] > base["summary"]["median_net_worth"]
    assert more["bands"][-1]["p10"] > base["bands"][-1]["p10"]


def test_projection_liquid_grows_by_savings_only(session):
    household(session)
    result = project_wealth(session, 5, seed=3, simulations=50, inflation=0.0)
    # inflation 0 => real == nominal, so liquid is exactly savings * 12 per year
    points = result["deterministic"]
    assert points[1]["liquid"] == pytest.approx(60_000.0 + 1500.0 * 12, abs=0.01)
    assert points[5]["liquid"] == pytest.approx(60_000.0 + 1500.0 * 60, abs=0.01)
    # investments compound at the deterministic rate
    assert points[5]["investments"] == pytest.approx(200_000.0 * 1.06**5, abs=0.05)


def test_projection_flags_negative_savings_and_cash_burn(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=6_000.0
    )
    seed_months(session, checking, months=6, income=3000.0, spend=-4000.0)
    result = project_wealth(session, 10, seed=11, simulations=60)

    # -$1,000/month against $6,000 of cash: underwater within the first year
    assert result["monthly_savings_used"] == -1000.0
    assert result["summary"]["cash_runs_out_year"] == 1
    assert any("NEGATIVE" in c for c in result["caveats"])


def test_projection_output_is_json_serializable(session):
    household(session)
    result = project_wealth(session, 8, seed=2, simulations=40)
    assert result["assumptions"]
    json.dumps(result)  # must not raise


# --------------------------------------------------------------------------- #
# Affordability
# --------------------------------------------------------------------------- #


def test_mortgage_payment_matches_known_values():
    # $300k, 6%, 30yr -> $1,798.65/mo (standard amortization table value)
    assert mortgage_payment(300_000.0, 0.06, 30) == pytest.approx(1798.65, abs=0.01)
    # $400k, 6%, 30yr -> $2,398.20/mo
    assert mortgage_payment(400_000.0, 0.06, 30) == pytest.approx(2398.20, abs=0.01)
    # 0% degrades to straight-line principal
    assert mortgage_payment(240_000.0, 0.0, 20) == pytest.approx(1000.0, abs=0.001)
    assert mortgage_payment(0.0, 0.06, 30) == 0.0


def test_affordability_math_and_cash_impact(session):
    household(session, months=8, liquid=200_000.0, investments=300_000.0)
    result = affordability(
        session,
        purchase_price=500_000.0,
        down_payment=100_000.0,
        annual_rate=0.06,
        term_years=30,
        extra_monthly_costs=600.0,
        years=10,
        seed=42,
        simulations=120,
    )
    purchase = result["purchase"]
    assert purchase["loan_principal"] == 400_000.0
    assert purchase["down_payment_pct"] == 20.0
    assert purchase["monthly_principal_interest"] == pytest.approx(2398.20, abs=0.01)
    assert purchase["total_monthly_cost"] == pytest.approx(2998.20, abs=0.01)
    assert purchase["total_interest_over_term"] == pytest.approx(463_352.8, abs=5.0)

    cash = result["cash_impact"]
    assert cash["liquid_after"] == 100_000.0
    assert cash["monthly_surplus_after"] == pytest.approx(1500.0 - 2998.20, abs=0.01)
    assert cash["reserve_months_after"] == pytest.approx(100_000.0 / 3500.0, abs=0.01)

    assert result["assumptions"]
    assert "Confidence" in result["verdict"]
    json.dumps(result)


def test_affordability_bands_ordered_and_paired(session):
    household(session, months=8, liquid=400_000.0, investments=300_000.0)
    result = affordability(
        session,
        purchase_price=300_000.0,
        down_payment=150_000.0,
        annual_rate=0.05,
        term_years=30,
        years=10,
        seed=8,
        simulations=150,
    )
    for key in ("bands_without", "bands_with"):
        for band in result["projection"][key]:
            assert band["p10"] <= band["p50"] <= band["p90"]
    without = result["projection"]["without_purchase"]
    with_buy = result["projection"]["with_purchase"]
    assert without["p10"] <= without["p50"] <= without["p90"]
    assert with_buy["p10"] <= with_buy["p50"] <= with_buy["p90"]
    assert result["projection"]["delta_p50"] == pytest.approx(
        with_buy["p50"] - without["p50"], abs=0.01
    )


def test_affordability_refuses_when_down_payment_exceeds_cash(session):
    household(session, months=8, liquid=10_000.0, investments=50_000.0)
    result = affordability(
        session,
        purchase_price=500_000.0,
        down_payment=100_000.0,
        annual_rate=0.06,
        term_years=30,
        years=10,
        seed=1,
        simulations=50,
    )
    assert result["signals"]["down_payment_exceeds_liquid"] is True
    assert result["verdict"].startswith("No")


def test_affordability_flags_negative_surplus(session):
    household(session, months=8, liquid=300_000.0, investments=100_000.0)
    result = affordability(
        session,
        purchase_price=900_000.0,
        down_payment=180_000.0,
        annual_rate=0.07,
        term_years=30,
        extra_monthly_costs=1_200.0,
        years=10,
        seed=4,
        simulations=60,
    )
    assert result["signals"]["monthly_surplus_goes_negative"] is True
    assert result["verdict"].startswith("Stretch")


def test_affordability_is_deterministic_for_a_given_seed(session):
    household(session, months=8, liquid=250_000.0, investments=250_000.0)
    kwargs = dict(
        purchase_price=400_000.0,
        down_payment=80_000.0,
        annual_rate=0.06,
        term_years=30,
        years=10,
        seed=2026,
        simulations=100,
    )
    assert affordability(session, **kwargs) == affordability(session, **kwargs)


def test_affordability_carries_thin_data_confidence(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=150_000.0
    )
    seed_months(session, checking, months=2)
    result = affordability(
        session,
        purchase_price=300_000.0,
        down_payment=60_000.0,
        annual_rate=0.06,
        term_years=30,
        years=10,
        seed=6,
        simulations=50,
    )
    assert result["data_thin"] is True
    assert result["confidence"] == "low"
    assert "LOW" in result["verdict"]
