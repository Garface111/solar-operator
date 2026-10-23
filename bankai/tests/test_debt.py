"""The debt/APR optimizer: real balances in, a lowest-cost paydown plan out."""
import json

from bankai.agent.tools import execute_tool
from bankai.intelligence import debt
from bankai.ingest import upsert_account
from bankai import accounts_terms


def _card(session, name, balance, apr=None, minimum=None):
    acct = upsert_account(session, source="manual", name=name, kind="credit", balance=balance)
    session.flush()
    if apr is not None or minimum is not None:
        accounts_terms.set_terms(session, acct.id, apr=apr, minimum_payment=minimum,
                                 source="test")
    return acct


def test_snapshot_flags_estimated_aprs(session):
    _card(session, "High APR card", -5000, apr=25.0, minimum=150)
    _card(session, "Unknown card", -2000)  # no terms
    debts = debt.snapshot(session)
    assert len(debts) == 2
    by_name = {d.name: d for d in debts}
    assert by_name["High APR card"].apr == 25.0 and not by_name["High APR card"].apr_estimated
    assert by_name["Unknown card"].apr_estimated  # modeled at the default, flagged


def test_avalanche_beats_minimums_and_targets_the_highest_apr(session):
    _card(session, "Big cheap", -8000, apr=12.0, minimum=160)
    _card(session, "Small pricey", -2000, apr=20.0, minimum=60)
    out = debt.optimize(session, monthly_budget=800)
    # both retire the debt at this budget (APRs under the minimum-trap line)
    assert out["avalanche"]["feasible"] and out["minimums_only"]["feasible"]
    # avalanche clears the 20% card first despite it being the smaller balance
    assert out["avalanche"]["payoff_order"][0] == "Small pricey"
    # and it costs less interest than paying only minimums
    assert out["avalanche"]["total_interest"] < out["minimums_only"]["total_interest"]
    assert out["interest_saved_vs_minimums"] > 0
    assert out["recommended"] == "avalanche"


def test_a_minimum_below_the_interest_is_the_debt_trap(session):
    # 27% APR with a 2% minimum: the minimum never covers the interest, so
    # minimums-only never retires it — the optimizer must not claim it does.
    _card(session, "Trap card", -6000, apr=27.0)
    out = debt.optimize(session, monthly_budget=50)
    assert out["minimums_only"]["feasible"] is False
    assert out["minimums_only"]["months_to_debt_free"] is None


def test_a_budget_below_minimums_is_flagged(session):
    _card(session, "Card A", -10000, apr=24.0)
    _card(session, "Card B", -10000, apr=24.0)
    out = debt.optimize(session, monthly_budget=100)  # far below minimums
    assert "warning" in out and "below" in out["warning"]


def test_needs_real_apr_lists_the_gaps(session):
    _card(session, "Known", -3000, apr=22.0, minimum=90)
    _card(session, "Mystery", -4000)
    out = debt.optimize(session, monthly_budget=600)
    assert out["needs_real_apr"] == ["Mystery"]


def test_balance_transfer_models_fee_and_savings(session):
    _card(session, "BankAmericard", -11000, apr=24.0, minimum=300)
    out = debt.balance_transfer(
        session, account_name="bankamericard", promo_apr=0.0, promo_months=18,
        fee_pct=3.0, monthly_payment=700,
    )
    assert out["card"] == "BankAmericard"
    assert out["transfer_fee"] == 330.0  # 3% of 11,000
    assert out["interest_under_transfer"] == 0.0  # 0% promo, cleared in time
    assert out["cleared_within_promo"] is True
    # staying on a 24% card costs real interest, so the transfer nets a saving
    assert out["net_savings"] > 0


def test_mortgage_is_excluded_unless_asked(session):
    upsert_account(session, source="manual", name="Mortgage", kind="mortgage", balance=-500000)
    _card(session, "Card", -3000, apr=20.0)
    assert [d.name for d in debt.snapshot(session)] == ["Card"]
    assert any(d.name == "Mortgage" for d in debt.snapshot(session, include_mortgage=True))


def test_the_tool_round_trips(session):
    _card(session, "Card One", -6000, apr=26.0, minimum=180)
    _card(session, "Card Two", -1500, apr=15.0, minimum=45)
    out = json.loads(execute_tool(session, "debt_optimizer", {"monthly_budget": 700}))
    assert out["total_owed"] == 7500.0
    assert "avalanche" in out and "snowball" in out and "minimums_only" in out
    # with a transfer scenario
    out2 = json.loads(execute_tool(session, "debt_optimizer", {
        "monthly_budget": 700, "transfer_card": "Card One",
        "transfer_promo_apr": 0, "transfer_promo_months": 12,
        "transfer_fee_pct": 3, "transfer_monthly_payment": 550,
    }))
    assert "balance_transfer" in out2 and out2["balance_transfer"]["card"] == "Card One"
