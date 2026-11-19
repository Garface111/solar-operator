from datetime import date, datetime, timedelta

import pytest

from bankai import goals
from bankai.models import Account, BalanceSnapshot

TODAY = date(2026, 6, 1)


def mk_account(session, name="Rivera Savings", balance=0.0, kind="savings"):
    account = Account(source="csv", name=name, kind=kind, balance=balance, institution="Cascadia CU")
    session.add(account)
    session.flush()
    return account


def snap(session, account, days_ago, balance, today=TODAY):
    row = BalanceSnapshot(
        account_id=account.id, date=today - timedelta(days=days_ago), balance=balance
    )
    session.add(row)
    session.flush()
    return row


def mk_goal(session, *, age_days=0, today=TODAY, **kw):
    """create_goal, then backdate created_at so pace windows are testable."""
    kw.setdefault("name", "Emergency fund")
    kw.setdefault("target_amount", 12_000.0)
    goal = goals.create_goal(session, today=today, **kw)
    goal.created_at = datetime(today.year, today.month, today.day) - timedelta(days=age_days)
    session.flush()
    return goal


# --- direction of travel ---

def test_savings_progress_counts_up_from_starting_amount(session):
    account = mk_account(session, balance=6_000.0)
    goal = mk_goal(
        session, target_amount=10_000.0, category="savings",
        linked_account_id=account.id, starting_amount=2_000.0,
    )
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["current_amount"] == 4_000.0
    assert p["remaining"] == 6_000.0
    assert p["percent_complete"] == 40.0
    assert p["current_balance"] == 6_000.0
    assert "savings: balance" in p["basis"]


def test_debt_payoff_progress_counts_down_the_liability(session):
    account = mk_account(session, name="Blue Card", balance=-5_000.0, kind="credit")
    goal = mk_goal(
        session, name="Kill the Blue Card", target_amount=8_000.0, category="debt_payoff",
        linked_account_id=account.id, starting_amount=8_000.0,
    )
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["current_amount"] == 3_000.0  # 8000 owed originally, 5000 left
    assert p["percent_complete"] == 37.5
    assert "debt payoff" in p["basis"]


def test_debt_payoff_ignores_the_sign_convention_of_the_balance(session):
    """Some sources store card balances positive, some negative — same answer."""
    positive = mk_account(session, name="Card A", balance=5_000.0, kind="credit")
    negative = mk_account(session, name="Card B", balance=-5_000.0, kind="credit")
    a = mk_goal(session, name="A", target_amount=8_000.0, category="debt_payoff",
                linked_account_id=positive.id, starting_amount=8_000.0)
    b = mk_goal(session, name="B", target_amount=8_000.0, category="debt_payoff",
                linked_account_id=negative.id, starting_amount=8_000.0)
    assert goals.goal_progress(session, a, today=TODAY)["current_amount"] == \
        goals.goal_progress(session, b, today=TODAY)["current_amount"]


def test_backwards_progress_is_reported_not_hidden(session):
    account = mk_account(session, balance=1_500.0)
    goal = mk_goal(session, target_amount=10_000.0, linked_account_id=account.id,
                   starting_amount=2_000.0)
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["current_amount"] == -500.0
    assert p["percent_complete"] == -5.0


# --- undeterminable cases return None, never a guess ---

def test_goal_without_a_linked_account_is_unmeasurable(session):
    goal = mk_goal(session, target_amount=5_000.0)
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["current_amount"] is None
    assert p["percent_complete"] is None
    assert p["on_track"] is None
    assert "no linked account" in p["basis"]


def test_linked_account_without_a_balance_is_unmeasurable(session):
    account = mk_account(session, balance=None)
    goal = mk_goal(session, target_amount=5_000.0, linked_account_id=account.id)
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["current_amount"] is None and p["on_track"] is None


def test_no_target_date_means_no_on_track_verdict(session):
    account = mk_account(session, balance=6_000.0)
    goal = mk_goal(session, target_amount=10_000.0, linked_account_id=account.id,
                   starting_amount=0.0, age_days=90)
    snap(session, account, 90, 2_000.0)
    snap(session, account, 0, 6_000.0)
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["observed_monthly"] is not None  # pace is known...
    assert p["required_monthly"] is None      # ...but there is nothing to require
    assert p["on_track"] is None
    assert "no target date" in p["basis"]


def test_pace_unknown_on_a_brand_new_goal(session):
    account = mk_account(session, balance=500.0)
    goal = mk_goal(session, target_amount=10_000.0, linked_account_id=account.id,
                   starting_amount=0.0, target_date=TODAY + timedelta(days=365))
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["required_monthly"] is not None
    assert p["observed_monthly"] is None
    assert p["on_track"] is None
    assert "no observed pace yet" in p["basis"]


# --- pace math ---

def test_on_track_when_snapshot_pace_clears_the_requirement(session):
    account = mk_account(session, balance=6_000.0)
    goal = mk_goal(session, target_amount=12_000.0, linked_account_id=account.id,
                   starting_amount=0.0, target_date=TODAY + timedelta(days=180),
                   age_days=120)
    snap(session, account, 90, 2_000.0)
    snap(session, account, 45, 4_000.0)
    snap(session, account, 0, 6_000.0)
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["months_remaining"] == 5.9
    assert p["required_monthly"] == pytest.approx(1014.7, abs=1.0)   # 6000 over 5.9 months
    assert p["observed_monthly"] == pytest.approx(1352.9, abs=1.0)   # 4000 over 90 days
    assert p["on_track"] is True
    assert "3 balance snapshots" in p["basis"]


def test_off_track_when_snapshot_pace_falls_short(session):
    account = mk_account(session, balance=6_000.0)
    goal = mk_goal(session, target_amount=12_000.0, linked_account_id=account.id,
                   starting_amount=0.0, target_date=TODAY + timedelta(days=180),
                   age_days=120)
    snap(session, account, 90, 5_000.0)
    snap(session, account, 0, 6_000.0)
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["observed_monthly"] == pytest.approx(338.2, abs=1.0)
    assert p["on_track"] is False
    assert "falls short of" in p["basis"]


def test_snapshots_shorter_than_the_minimum_window_are_not_used(session):
    account = mk_account(session, balance=2_000.0)
    goal = mk_goal(session, target_amount=12_000.0, linked_account_id=account.id,
                   starting_amount=0.0, target_date=TODAY + timedelta(days=365),
                   age_days=10)
    snap(session, account, 5, 1_000.0)
    snap(session, account, 0, 2_000.0)   # only a 5-day span — meaningless monthly rate
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["observed_monthly"] is None
    assert p["on_track"] is None


def test_coarse_pace_fallback_when_there_is_no_snapshot_history(session):
    account = mk_account(session, balance=2_000.0)
    goal = mk_goal(session, target_amount=12_000.0, linked_account_id=account.id,
                   starting_amount=0.0, target_date=TODAY + timedelta(days=365),
                   age_days=60)
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["observed_monthly"] == pytest.approx(1014.7, abs=1.0)  # 2000 over 60 days
    assert "coarse pace" in p["basis"]
    assert p["on_track"] is True


def test_debt_pace_is_positive_when_the_balance_shrinks(session):
    account = mk_account(session, name="Blue Card", balance=-7_000.0, kind="credit")
    goal = mk_goal(session, name="Kill the Blue Card", target_amount=10_000.0,
                   category="debt_payoff", linked_account_id=account.id,
                   starting_amount=10_000.0, target_date=TODAY + timedelta(days=210),
                   age_days=90)
    snap(session, account, 60, -10_000.0)
    snap(session, account, 0, -7_000.0)
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["current_amount"] == 3_000.0
    assert p["observed_monthly"] == pytest.approx(1522.0, abs=1.0)
    assert p["on_track"] is True


def test_reached_target_is_on_track_and_says_so(session):
    account = mk_account(session, balance=12_500.0)
    goal = mk_goal(session, target_amount=12_000.0, linked_account_id=account.id,
                   starting_amount=0.0, target_date=TODAY + timedelta(days=30))
    p = goals.goal_progress(session, goal, today=TODAY)
    assert p["on_track"] is True
    assert p["remaining"] == 0.0
    assert p["percent_complete"] > 100
    assert "already reached" in p["basis"]


def test_passed_target_date_with_money_left_is_off_track(session):
    account = mk_account(session, balance=6_000.0)
    goal = mk_goal(session, target_amount=12_000.0, linked_account_id=account.id,
                   starting_amount=0.0, target_date=TODAY + timedelta(days=30),
                   age_days=120)
    p = goals.goal_progress(session, goal, today=TODAY + timedelta(days=60))
    assert p["on_track"] is False
    assert "has passed" in p["basis"]


# --- validation ---

def test_create_goal_validates_its_inputs(session):
    account = mk_account(session, balance=100.0)
    with pytest.raises(ValueError, match="name is required"):
        goals.create_goal(session, name="   ", target_amount=100.0, today=TODAY)
    with pytest.raises(ValueError, match="greater than zero"):
        goals.create_goal(session, name="x", target_amount=0.0, today=TODAY)
    with pytest.raises(ValueError, match="greater than zero"):
        goals.create_goal(session, name="x", target_amount=-5.0, today=TODAY)
    with pytest.raises(ValueError, match="must be a number"):
        goals.create_goal(session, name="x", target_amount="lots", today=TODAY)
    with pytest.raises(ValueError, match="category must be one of"):
        goals.create_goal(session, name="x", target_amount=10.0, category="vibes", today=TODAY)
    with pytest.raises(ValueError, match="must be in the future"):
        goals.create_goal(session, name="x", target_amount=10.0, target_date=TODAY, today=TODAY)
    with pytest.raises(ValueError, match="no account"):
        goals.create_goal(session, name="x", target_amount=10.0,
                          linked_account_id="acct_nope", today=TODAY)
    assert goals.create_goal(session, name="ok", target_amount=10.0,
                             linked_account_id=account.id, today=TODAY).id.startswith("goal_")


def test_starting_amount_defaults_to_todays_balance(session):
    savings = mk_account(session, balance=4_000.0)
    card = mk_account(session, name="Blue Card", balance=-9_000.0, kind="credit")
    s = goals.create_goal(session, name="House down payment", target_amount=50_000.0,
                          category="savings", linked_account_id=savings.id, today=TODAY)
    d = goals.create_goal(session, name="Card payoff", target_amount=9_000.0,
                          category="debt_payoff", linked_account_id=card.id, today=TODAY)
    assert s.starting_amount == 4_000.0        # progress counts from today, not from zero
    assert d.starting_amount == 9_000.0        # absolute value for a liability
    assert goals.goal_progress(session, s, today=TODAY)["current_amount"] == 0.0
    assert goals.goal_progress(session, d, today=TODAY)["current_amount"] == 0.0


def test_starting_amount_defaults_to_zero_without_a_linked_account(session):
    g = goals.create_goal(session, name="Sabbatical", target_amount=20_000.0, today=TODAY)
    assert g.starting_amount == 0.0
    assert g.status == "active" and g.category == "savings"


def test_update_goal_status(session):
    goal = mk_goal(session, target_amount=1_000.0)
    assert goals.update_goal_status(session, goal.id, "achieved").status == "achieved"
    assert goals.update_goal_status(session, goal.id, "abandoned").status == "abandoned"
    with pytest.raises(ValueError, match="status must be one of"):
        goals.update_goal_status(session, goal.id, "kinda")
    with pytest.raises(ValueError, match="no goal"):
        goals.update_goal_status(session, "goal_nope", "achieved")


def test_list_goals_with_progress_filters_by_status(session):
    account = mk_account(session, balance=3_000.0)
    a = mk_goal(session, name="Active one", target_amount=5_000.0,
                linked_account_id=account.id, starting_amount=0.0)
    b = mk_goal(session, name="Done one", target_amount=1_000.0)
    goals.update_goal_status(session, b.id, "achieved")

    active = goals.list_goals_with_progress(session, today=TODAY)
    assert [g["name"] for g in active] == ["Active one"]
    assert active[0]["percent_complete"] == 60.0
    assert active[0]["goal_id"] == a.id
    assert active[0]["linked_account"] == "Rivera Savings"

    assert {g["name"] for g in goals.list_goals_with_progress(session, status=None, today=TODAY)} \
        == {"Active one", "Done one"}
    assert [g["name"] for g in
            goals.list_goals_with_progress(session, status="achieved", today=TODAY)] == ["Done one"]
    with pytest.raises(ValueError, match="status must be one of"):
        goals.list_goals_with_progress(session, status="nope")
