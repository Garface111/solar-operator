"""The Tinder test.

On 2026-08-12 the copilot told Ford he had a $12.50 "Tinder Gold" charge on the
4771 card, with a date and a transaction id. He had never used Tinder; the
charge did not exist; and when challenged it doubled down with an invented
merchant description. These tests exist so that exact reply cannot leave the
system again — checked against the database, not against a model's opinion of
itself.
"""
from datetime import date

import pytest

from bankai import grounding
from bankai.agent import chat as agent_chat
from bankai.models import Account, Transaction


@pytest.fixture()
def ledger(session):
    """The real week: three charges on the card, none of them $12.50."""
    acct = Account(name="Customized Cash Rewards Visa 4771", kind="credit", balance=-774.0)
    session.add(acct)
    session.flush()
    rows = [
        (date(2026, 7, 3), -10.00, "Patreon* Membership      Internet     CA"),
        (date(2026, 7, 4), -0.11, "INTEREST CHARGED ON PURCHASES"),
        (date(2026, 7, 14), -9.99, "PLAYSTATION              800-3457669  CA"),
    ]
    for i, (posted, amount, desc) in enumerate(rows):
        session.add(Transaction(
            id=f"txn_real{i}", account_id=acct.id, posted=posted, amount=amount,
            description=desc, category="entertainment", fingerprint=f"fp{i}",
        ))
    session.flush()
    return acct


THE_LIE = (
    '**"Tinder Gold *weekly* 12314185147" — −$12.50 on 2026-07-07**, on the '
    "BofA Premium Rewards 4771 card, auto-categorized as Entertainment."
)


def test_the_tinder_reply_is_caught(session, ledger):
    problems = grounding.check_reply(session, THE_LIE)
    assert problems, "the exact reply that went to Ford must not pass"
    assert any(p.text == "$12.50" for p in problems)


def test_a_real_charge_passes_untouched(session, ledger):
    ok = "Your Patreon membership charged $10.00 on July 3."
    assert grounding.check_reply(session, ok) == []


def test_an_invented_transaction_id_is_unambiguous(session, ledger):
    problems = grounding.check_reply(session, "See txn_abc123def456 for the charge.")
    assert [p.kind for p in problems] == ["transaction_id"]
    assert grounding.check_reply(session, "See txn_real0 for the charge.") == []


def test_computed_figures_are_not_treated_as_quoted_rows(session, ledger):
    """Totals, balances and projections are the model's arithmetic — demanding a
    matching transaction row for them would block honest answers."""
    for line in (
        "Your net worth is $1,261,489.81.",
        "That is about $4,847.22 per month across the year.",
        "The five-year projection lands near $2,104,880.50.",
        "Your card balance is $774.00.",
    ):
        assert grounding.check_reply(session, line) == [], line


def test_round_illustrative_figures_are_allowed(session, ledger):
    assert grounding.check_reply(session, "Set aside $500 a week and see.") == []


def test_a_grounded_reply_reaches_the_household_unchanged(session, ledger):
    def never_called(*a, **k):
        pytest.fail("a clean reply must not trigger a correction round")

    out = agent_chat._grounded(
        session, "Patreon charged $10.00 on July 3.", [], never_called
    )
    assert out == "Patreon charged $10.00 on July 3."


def test_a_fabrication_is_corrected_and_the_fix_is_what_ships(session, ledger):
    seen = {}

    def run(s, sys_, msgs):
        seen["prompt"] = msgs[-1]["content"]
        return "You have no Tinder charge. The card shows Patreon $10.00 on July 3."

    out = agent_chat._grounded(session, THE_LIE, [], run)
    assert "Tinder Gold" not in out
    assert "$10.00" in out
    # the model is shown the exact offending string, not a scolding
    assert "$12.50" in seen["prompt"]


def test_a_second_fabrication_is_refused_not_sent(session, ledger):
    """Doubling down is precisely what happened when Ford pushed back. If the
    correction round invents again, the household gets the truth instead."""
    def run(s, sys_, msgs):
        return "I checked again — it really is $12.50 from Tinder Gold."

    out = agent_chat._grounded(session, THE_LIE, [], run)
    assert "Tinder" not in out
    # the refusal must NOT echo the fabricated figure: quoting figures in the
    # error template is what fallback brains learned to imitate (2026-08-13
    # refusal spiral) — the refusal is short, figure-free, and offers a lookup
    assert "couldn't verify" in out and "$12.50" not in out


def test_a_dead_backend_during_correction_still_refuses(session, ledger):
    def boom(*a, **k):
        raise RuntimeError("backend down")

    out = agent_chat._grounded(session, THE_LIE, [], boom)
    assert "couldn't verify" in out
    assert "stop myself" not in out  # the viral template is retired


def test_the_gate_runs_regardless_of_router_tier(session, ledger, monkeypatch):
    """The lie went out on a turn the router called 'simple', with verification
    switched off. The gate must not be tier-dependent."""
    from bankai import router

    monkeypatch.setattr(
        agent_chat, "_backend",
        lambda name: type("B", (), {"run": staticmethod(lambda s, sys_, msgs, **k: THE_LIE)}),
    )
    monkeypatch.setattr(
        router, "choose",
        lambda msgs, channel: router.RouteDecision(
            tier="fast", model="claude-opus-5", effort="low",
            verify=False, reason="simple request",
        ),
    )
    out = agent_chat.run_turn(session, [{"role": "user", "content": "any subscriptions?"}])
    assert "Tinder" not in out


# --- the false positive that broke logging ---------------------------------
# Within an hour of shipping, the gate refused Ford twice while he was LOGGING
# a new expense over WhatsApp ("$147.42 on water bill"). The number was not in
# the ledger because he was the one supplying it. A guard that blocks the
# household's own figures is not caution, it is a broken assistant.

def test_a_figure_the_household_just_gave_us_is_grounded(session, ledger):
    convo = [{"role": "user", "content": "$147.42 on water bill"}]
    reply = "Logged $147.42 for the water bill."
    assert grounding.check_reply(session, reply, convo) == []


def test_still_blocked_when_nobody_said_it(session, ledger):
    """Same figure, no one mentioned it — back to being a fabrication."""
    assert grounding.check_reply(session, "You paid $147.42 for water.", []) != []


def test_the_household_figure_survives_the_whole_turn(session, ledger):
    def never(*a, **k):
        pytest.fail("logging an expense must not trigger a correction round")

    convo = [{"role": "user", "content": "no I'm logging it — $147.42 on water bill"}]
    out = agent_chat._grounded(session, "Logged $147.42 for water.", convo, never)
    assert "$147.42" in out


def test_a_fabrication_alongside_a_household_figure_is_still_caught(session, ledger):
    convo = [{"role": "user", "content": "$147.42 on water bill"}]
    reply = "Logged $147.42 for water. I also see a $12.50 Tinder charge."
    problems = grounding.check_reply(session, reply, convo)
    assert [p.text for p in problems] == ["$12.50"]
