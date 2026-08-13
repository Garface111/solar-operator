"""The grounding gate must recognize every store a REAL figure lives in — and
still catch a fresh fabrication. Pinned from the 2026-08-13 refusal spiral."""
from datetime import date

from bankai import grounding
from bankai.agent import chat as agent_chat, verify
from bankai import config, pending
from bankai.ingest import TxnIn, ingest_transactions, upsert_account


def _seed(session):
    acct = upsert_account(session, source="manual", name="Checking 2724",
                          kind="checking", balance=561.57)
    session.flush()
    ingest_transactions(session, acct, [
        TxnIn(posted=date(2026, 8, 1), amount=-95.38, description="VZWRLSS"),
    ])
    pending.note(session, amount=8.81, description="Zyns", account_hint="Apple Card")
    return acct


def test_a_balance_is_grounded_even_though_it_is_not_a_transaction(session):
    _seed(session)
    problems = grounding.check_reply(session, "Your checking (2724) holds $561.57 right now.")
    assert problems == []


def test_a_pending_expense_figure_is_grounded(session):
    _seed(session)
    # the exact live failure: $8.81 Zyns lives in pending_expenses, was refused
    problems = grounding.check_reply(session, "The Zyns run came to $8.81, logged as pending.")
    assert problems == []


def test_a_transaction_figure_still_grounds(session):
    _seed(session)
    assert grounding.check_reply(session, "Verizon hit for $95.38 on the 1st.") == []


def test_a_fresh_fabrication_is_still_caught(session):
    _seed(session)
    # Tinder-class lie: a specific charge that exists nowhere
    problems = grounding.check_reply(
        session, 'A $12.50 charge at "Tinder Gold" posted on the 4771.')
    assert any(p.text == "$12.50" for p in problems)


def test_conversation_continuity_exempts_previously_stated_figures(session):
    _seed(session)
    history = [
        {"role": "user", "content": "when does the redemption land?"},
        {"role": "assistant", "content": "The ~$9,989 redemption lands on the 15th."},
        {"role": "user", "content": "and how does that affect the mortgage?"},
    ]
    # $9,989 is in no table (it is a planning-sheet figure) but the copilot has
    # already stated it in this conversation — repeating it is continuity.
    problems = grounding.check_reply(
        session, "With the $9,989 landing on the 15th, the mortgage clears.", history)
    assert problems == []


def test_a_tool_computed_aggregate_is_ground_truth(session):
    """The last live failure: $1,078.37 (a spending_summary total) exists in no
    table — it is the OUTPUT of a tool. execute_tool records returned figures;
    the gate accepts them for the turn."""
    from bankai.agent.tools import _record_tool_figures

    _seed(session)
    _record_tool_figures(session, '{"spend": 1078.37, "income": 9989.0}')
    assert grounding.check_reply(
        session, "You spent a painful $1,078.37 this window against $9,989 in.") == []


def test_a_fabricated_figure_is_still_caught_with_the_ledger_active(session):
    from bankai.agent.tools import _record_tool_figures

    _seed(session)
    _record_tool_figures(session, '{"spend": 1078.37}')
    # no tool ever returned 12.50 — the Tinder-class lie stays caught
    problems = grounding.check_reply(session, 'A $12.50 charge at "Tinder Gold" posted.')
    assert any(p.text == "$12.50" for p in problems)


def test_stale_tool_figures_expire(session):
    from datetime import datetime, timedelta
    from bankai.models import ToolFigure

    _seed(session)
    session.add(ToolFigure(figure=444.44,
                           created_at=datetime.utcnow() - timedelta(hours=3)))
    session.flush()
    problems = grounding.check_reply(session, "That mystery charge was $444.44.")
    assert any(p.text == "$444.44" for p in problems)  # 3h-old figure no longer vouches


def test_execute_tool_records_its_figures(session):
    import json as _json
    from bankai.agent.tools import execute_tool
    from bankai.models import ToolFigure

    _seed(session)
    out = _json.loads(execute_tool(session, "get_accounts", {}))
    assert out["total"] is not None
    figures = {f.figure for f in session.query(ToolFigure).all()}
    assert round(abs(561.57), 2) in figures  # the balance it returned is logged


def test_the_refusal_message_no_longer_lists_figures():
    msg = grounding.refusal_message(
        [grounding.Unverified("amount", "$561.57", "ctx")])
    assert "$561.57" not in msg          # never hand the imitation template real numbers
    assert "stop myself" not in msg      # and never the old viral phrasing


# --- the draft-imitation guard in run_turn ---

def test_a_refusal_shaped_draft_is_retried_then_ships_the_real_answer(session, monkeypatch):
    _seed(session)
    monkeypatch.setattr(config, "LLM_BACKEND", "fake")
    monkeypatch.setattr(config, "ROUTER_ENABLED", False)
    monkeypatch.setattr(config, "CLAUDE_CLI_MODEL", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_EFFORT", "")
    monkeypatch.setattr(verify, "VERIFY_REPLIES", False)
    calls = {"n": 0}

    class Fake:
        @staticmethod
        def run(s, sys_, msgs, *, model=None, effort=None):
            calls["n"] += 1
            if calls["n"] == 1:  # imitation of the poisoned history
                return ("I need to stop myself here. I was about to state figures "
                        "that I cannot find in your data ($561.57).")
            return "Your checking (2724) holds $561.57."

    monkeypatch.setattr(agent_chat, "_backend", lambda name: Fake)
    out = agent_chat.run_turn(session, [{"role": "user", "content": "balance?"}])
    assert out == "Your checking (2724) holds $561.57."
    assert calls["n"] == 2  # one corrective retry, then the real answer


def test_a_backend_stuck_on_refusals_hands_the_turn_to_the_next(session, monkeypatch):
    _seed(session)
    monkeypatch.setattr(config, "LLM_BACKEND", "fake,fake2")
    monkeypatch.setattr(config, "ROUTER_ENABLED", False)
    monkeypatch.setattr(config, "CLAUDE_CLI_MODEL", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_EFFORT", "")
    monkeypatch.setattr(verify, "VERIFY_REPLIES", False)

    class Stuck:
        @staticmethod
        def run(s, sys_, msgs, *, model=None, effort=None):
            return "I need to stop myself here — I cannot state these figures."

    class Healthy:
        @staticmethod
        def run(s, sys_, msgs, *, model=None, effort=None):
            return "Checking is $561.57."

    impls = {"fake": Stuck, "fake2": Healthy}
    monkeypatch.setattr(agent_chat, "_backend", lambda name: impls[name])
    out = agent_chat.run_turn(session, [{"role": "user", "content": "balance?"}])
    assert out == "Checking is $561.57."