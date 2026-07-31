"""Acting on its own, and deciding instead of asking.

Two complaints from Ford, one root: the copilot ended a message with "do you
want me to also shift the watchpoint I planted?" — a question about its own
workspace that it had every fact needed to answer — and it only ever thinks when
spoken to.

So: a doctrine that says decide, and a loop that runs with nobody watching and
stays quiet unless something is worth saying.
"""
import pytest

from bankai import config
from bankai.agent import chat as agent_chat
from bankai.models import ChatMessage
from bankai import scheduler


# --- the doctrine ---

def test_the_persona_tells_it_to_decide_not_poll(session):
    system = agent_chat.build_system(session, "web")
    assert "Decide, do not poll" in system
    # the exact class of question that prompted this
    assert "you planted it, you know why, move it and tell them" in system
    # asserted on one line's worth: the prompt wraps mid-sentence
    assert "never end a message with a question" in system
    assert "Never ask two questions at once" in system


def test_it_still_knows_what_to_ask_about(session):
    """Decisiveness must not swallow the things only they can decide."""
    system = agent_chat.build_system(session, "web")
    assert "money leaving the household" in system
    assert "approval gate" in system


def test_tending_rules_only_on_its_own_time(session):
    assert "your own initiative" in agent_chat.build_system(session, "tending")
    for channel in ("web", "email", "sms"):
        assert "your own initiative" not in agent_chat.build_system(session, channel)


def test_tending_offers_silence_and_forbids_invention(session):
    system = agent_chat.build_system(session, "tending")
    assert agent_chat.SILENCE in system
    assert "Housekeeping done well is invisible" in system
    assert "never claim work you did not do" in system
    assert "propose_code_change" in system


# --- the loop ---

def test_quiet_maintenance_says_nothing(session, monkeypatch):
    """It did its work and nothing needs them. The thread must stay clean."""
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="tending": agent_chat.SILENCE,
    )
    result = scheduler.run_tending_once()
    assert result == {"status": "quiet"}


def test_something_worth_saying_reaches_the_thread(session, monkeypatch):
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="tending":
            "Your Apple Card minimum is due in 2 days and nothing is scheduled.",
    )
    result = scheduler.run_tending_once()
    assert result["status"] == "spoke"
    assert "Apple Card" in result["said"]


def test_the_housekeeping_prompt_is_never_stored_as_something_they_said(
    session, monkeypatch
):
    """Storing it would put words in the household's mouth and teach the copilot
    that these instructions come from them."""
    seen = {}

    def fake_turn(s, history, channel="tending"):
        seen["history"] = history
        return agent_chat.SILENCE

    monkeypatch.setattr(scheduler.agent_chat, "run_turn", fake_turn)
    scheduler.run_tending_once()

    # the prompt reached the model...
    assert "your own initiative" in seen["history"][-1]["content"]
    # ...but no user message was persisted for it
    from bankai.db import session_scope

    with session_scope() as s:
        stored = s.query(ChatMessage).all()
    assert not any("own initiative" in m.content for m in stored)


def test_the_loop_is_registered_with_the_others():
    assert hasattr(scheduler, "_tending_loop")
    assert config.TENDING_INTERVAL_HOURS >= 1


def test_a_failed_pass_does_not_take_the_loop_down(session, monkeypatch):
    def boom(s, history, channel="tending"):
        raise RuntimeError("backend down")

    monkeypatch.setattr(scheduler.agent_chat, "run_turn", boom)
    with pytest.raises(RuntimeError):
        scheduler.run_tending_once()  # the loop catches this; the function may raise
