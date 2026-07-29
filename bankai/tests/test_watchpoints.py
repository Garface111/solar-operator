"""Pure-logic tests for watchpoints: arming, firing exactly once, validation,
and the wake prompt the scheduler injects into the shared thread."""
from datetime import date, datetime

import pytest

from bankai.ingest import upsert_account
from bankai.watchpoints import (
    Watchpoint,
    build_wake_prompt,
    cancel_watchpoint,
    create_watchpoint,
    evaluate_watchpoints,
    list_watchpoints,
    validate_condition,
)

NOW = datetime(2026, 7, 29, 9, 0)


def _wp(session, **kwargs) -> Watchpoint:
    kwargs.setdefault("title", "Revisit the mortgage rate")
    kwargs.setdefault("note", "We locked at 7.1%; refi math works under 6%.")
    return create_watchpoint(session, **kwargs)


# --- on_date ---------------------------------------------------------------


def test_on_date_does_not_fire_before_the_date(session):
    _wp(session, kind="on_date", params={"date": "2026-09-01"})
    assert evaluate_watchpoints(session, NOW) == []


def test_on_date_fires_on_and_after_the_date_once(session):
    watchpoint = _wp(session, kind="on_date", params={"date": "2026-07-29"})
    fired = evaluate_watchpoints(session, NOW)
    assert [w.id for w in fired] == [watchpoint.id]
    assert watchpoint.status == "fired"
    assert watchpoint.fired_at == NOW
    # later ticks (even far past the date) never re-fire it
    assert evaluate_watchpoints(session, datetime(2026, 12, 1)) == []


def test_on_date_accepts_a_date_object_and_normalizes(session):
    watchpoint = _wp(session, kind="on_date", params={"date": date(2026, 7, 1)})
    assert watchpoint.params == {"date": "2026-07-01"}
    assert len(evaluate_watchpoints(session, NOW)) == 1


# --- net worth -------------------------------------------------------------


def test_net_worth_below_fires_only_when_under_threshold(session):
    upsert_account(session, source="csv", name="Joint Checking", balance=40_000.0)
    session.flush()
    _wp(session, title="Cash buffer eroding", kind="net_worth_below", params={"threshold": 30_000})
    assert evaluate_watchpoints(session, NOW) == []

    account = upsert_account(session, source="csv", name="Joint Checking", balance=25_000.0)
    assert account.balance == 25_000.0
    session.flush()
    fired = evaluate_watchpoints(session, NOW)
    assert len(fired) == 1
    assert "25,000.00" in fired[0].trigger_detail
    assert evaluate_watchpoints(session, NOW) == []


def test_net_worth_above_fires_when_over_threshold(session):
    upsert_account(session, source="csv", name="Brokerage", kind="investment", balance=250_000.0)
    session.flush()
    _wp(session, title="Time to rebalance", kind="net_worth_above", params={"threshold": 200_000})
    fired = evaluate_watchpoints(session, NOW)
    assert len(fired) == 1
    assert "above" in fired[0].trigger_detail
    assert evaluate_watchpoints(session, NOW) == []


def test_net_worth_below_stays_armed_with_no_accounts(session):
    """An empty finance model is unmeasurable, not a crisis — never fire on it."""
    watchpoint = _wp(session, kind="net_worth_below", params={"threshold": 30_000})
    assert evaluate_watchpoints(session, NOW) == []
    assert watchpoint.status == "armed"


# --- account balance -------------------------------------------------------


def test_account_balance_below_fires_for_that_account_only(session):
    watched = upsert_account(session, source="csv", name="Escrow", balance=900.0)
    upsert_account(session, source="csv", name="Vacation Fund", kind="savings", balance=100.0)
    session.flush()
    _wp(
        session,
        title="Escrow shortfall",
        kind="account_balance_below",
        params={"account_id": watched.id, "threshold": 1_000},
    )
    fired = evaluate_watchpoints(session, NOW)
    assert len(fired) == 1
    assert "Escrow" in fired[0].trigger_detail
    assert evaluate_watchpoints(session, NOW) == []


def test_account_balance_below_stays_armed_for_missing_account(session):
    watchpoint = _wp(
        session,
        kind="account_balance_below",
        params={"account_id": "acct_does_not_exist", "threshold": 500},
    )
    assert evaluate_watchpoints(session, NOW) == []
    assert watchpoint.status == "armed"


# --- liquid ----------------------------------------------------------------


def test_liquid_below_sums_checking_and_savings_only(session):
    upsert_account(session, source="csv", name="Everyday Checking", balance=3_000.0)
    upsert_account(session, source="csv", name="Emergency Savings", kind="savings", balance=4_000.0)
    # investments and property are NOT liquid: they must not lift the total
    upsert_account(session, source="csv", name="Brokerage", kind="investment", balance=500_000.0)
    session.flush()
    _wp(session, title="Runway check", kind="liquid_below", params={"threshold": 10_000})
    fired = evaluate_watchpoints(session, NOW)
    assert len(fired) == 1
    assert "7,000.00" in fired[0].trigger_detail
    assert evaluate_watchpoints(session, NOW) == []


def test_liquid_below_does_not_fire_when_cash_is_ample(session):
    upsert_account(session, source="csv", name="Everyday Checking", balance=25_000.0)
    session.flush()
    _wp(session, kind="liquid_below", params={"threshold": 10_000})
    assert evaluate_watchpoints(session, NOW) == []


# --- lifecycle -------------------------------------------------------------


def test_cancelled_watchpoint_never_fires(session):
    watchpoint = _wp(session, kind="on_date", params={"date": "2026-01-01"})
    cancel_watchpoint(session, watchpoint.id)
    assert watchpoint.status == "cancelled"
    assert evaluate_watchpoints(session, NOW) == []
    # cancelling twice is a no-op, not an error
    assert cancel_watchpoint(session, watchpoint.id).status == "cancelled"


def test_cancelling_a_fired_watchpoint_raises(session):
    watchpoint = _wp(session, kind="on_date", params={"date": "2026-01-01"})
    evaluate_watchpoints(session, NOW)
    with pytest.raises(ValueError, match="already fired"):
        cancel_watchpoint(session, watchpoint.id)


def test_cancel_unknown_id_raises(session):
    with pytest.raises(ValueError, match="no watchpoint"):
        cancel_watchpoint(session, "wp_nope")


def test_list_watchpoints_filters_by_status(session):
    due = _wp(session, title="Due now", kind="on_date", params={"date": "2026-01-01"})
    later = _wp(session, title="Due later", kind="on_date", params={"date": "2027-01-01"})
    cancelled = _wp(session, title="Dropped", kind="on_date", params={"date": "2027-01-01"})
    cancel_watchpoint(session, cancelled.id)
    evaluate_watchpoints(session, NOW)

    assert [w.id for w in list_watchpoints(session, status="fired")] == [due.id]
    assert [w.id for w in list_watchpoints(session, status="armed")] == [later.id]
    assert [w.id for w in list_watchpoints(session, status="cancelled")] == [cancelled.id]
    assert len(list_watchpoints(session)) == 3


def test_multiple_watchpoints_fire_independently(session):
    upsert_account(session, source="csv", name="Everyday Checking", balance=1_200.0)
    session.flush()
    _wp(session, title="Date flag", kind="on_date", params={"date": "2026-07-01"})
    _wp(session, title="Cash flag", kind="liquid_below", params={"threshold": 5_000})
    _wp(session, title="Future flag", kind="on_date", params={"date": "2030-01-01"})
    fired = evaluate_watchpoints(session, NOW)
    assert sorted(w.title for w in fired) == ["Cash flag", "Date flag"]
    assert evaluate_watchpoints(session, NOW) == []
    assert len(list_watchpoints(session, status="armed")) == 1


def test_created_by_defaults_to_agent_and_can_be_user(session):
    assert _wp(session, kind="on_date", params={"date": "2030-01-01"}).created_by == "agent"
    manual = _wp(
        session, kind="on_date", params={"date": "2030-01-01"}, created_by="user"
    )
    assert manual.created_by == "user"
    assert manual.id.startswith("wp_")


# --- validation ------------------------------------------------------------


def test_unknown_kind_raises(session):
    with pytest.raises(ValueError, match="unknown watchpoint kind"):
        _wp(session, kind="when_i_feel_like_it", params={})


def test_on_date_requires_a_parseable_date(session):
    with pytest.raises(ValueError, match="requires an ISO 'date'"):
        _wp(session, kind="on_date", params={})
    with pytest.raises(ValueError, match="not an ISO date"):
        _wp(session, kind="on_date", params={"date": "next tuesday"})


def test_thresholds_must_be_numeric(session):
    with pytest.raises(ValueError, match="numeric 'threshold'"):
        _wp(session, kind="net_worth_below", params={})
    with pytest.raises(ValueError, match="numeric 'threshold'"):
        _wp(session, kind="liquid_below", params={"threshold": "a lot"})
    with pytest.raises(ValueError, match="numeric 'threshold'"):
        _wp(
            session,
            kind="account_balance_below",
            params={"account_id": "acct_1", "threshold": None},
        )


def test_account_kind_requires_account_id(session):
    with pytest.raises(ValueError, match="requires an 'account_id'"):
        _wp(session, kind="account_balance_below", params={"threshold": 100})


def test_title_is_required(session):
    with pytest.raises(ValueError, match="requires a title"):
        _wp(session, title="   ", kind="on_date", params={"date": "2030-01-01"})


def test_validate_condition_normalizes_thresholds():
    assert validate_condition("net_worth_below", {"threshold": "30000"}) == {"threshold": 30000.0}
    assert validate_condition("on_date", {"date": "2026-07-29T00:00:00"}) == {"date": "2026-07-29"}


def test_list_watchpoints_rejects_unknown_status(session):
    with pytest.raises(ValueError, match="unknown status"):
        list_watchpoints(session, status="pending")


# --- wake prompt -----------------------------------------------------------


def test_wake_prompt_carries_title_note_and_trigger(session):
    watchpoint = _wp(
        session,
        title="Reconsider the HELOC",
        note="Rate was 9.4% and not worth it; revisit if rates drop or cash gets tight.",
        kind="liquid_below",
        params={"threshold": 8_000},
    )
    upsert_account(session, source="csv", name="Everyday Checking", balance=2_500.0)
    session.flush()
    fired = evaluate_watchpoints(session, NOW)
    prompt = build_wake_prompt(fired[0])

    assert "[watchpoint fired]" in prompt
    assert "Reconsider the HELOC" in prompt
    assert "Rate was 9.4%" in prompt
    assert watchpoint.created_at.date().isoformat() in prompt
    assert "2,500.00" in prompt  # the live reading that tripped it
    assert "Reassess" in prompt


def test_wake_prompt_falls_back_to_condition_description(session):
    """Rebuilt from a fresh DB row (no transient detail) the prompt still says
    what the flag was waiting for."""
    watchpoint = _wp(session, kind="net_worth_below", params={"threshold": 30_000})
    session.commit()
    session.expunge_all()
    reloaded = session.get(Watchpoint, watchpoint.id)
    prompt = build_wake_prompt(reloaded)
    assert "net worth falls below $30,000.00" in prompt


def test_wake_prompt_handles_a_missing_note(session):
    watchpoint = _wp(session, title="Bare flag", note="", kind="on_date", params={"date": "2026-01-01"})
    fired = evaluate_watchpoints(session, NOW)
    assert fired == [watchpoint]
    assert "(no note recorded)" in build_wake_prompt(watchpoint)
