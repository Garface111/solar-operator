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


# --- the scheduled household check-in (every CHECKIN_INTERVAL_DAYS) ---
#
# Tending stays quiet unless something matters; the check-in is the opposite
# contract — Ford asked for a copilot that reaches out on a fixed cadence, so
# arriving IS the point. It is emailed to both spouses when email is up.

def test_checkin_marker_logic(session):
    from datetime import date
    from bankai.models import MemoryNote

    today = date(2026, 8, 9)
    assert scheduler.checkin_action(session, today) == "init"
    session.add(MemoryNote(title="Last household check-in", content="2026-08-09"))
    session.flush()
    assert scheduler.checkin_action(session, today) == "skip"
    assert scheduler.checkin_action(session, date(2026, 8, 11)) == "skip"
    assert scheduler.checkin_action(session, date(2026, 8, 12)) == "run"
    # an unreadable marker must fail toward contact, not silence
    session.query(MemoryNote).filter_by(title="Last household check-in").one().content = "garbled"
    session.flush()
    assert scheduler.checkin_action(session, today) == "run"


def test_first_checkin_tick_initializes_without_mailing(session, monkeypatch):
    """A fresh deploy plants the marker; the first real check-in comes a full
    interval later — never a surprise email the moment the service boots."""
    from bankai.messaging import email_thread

    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="web", force_tier=None: pytest.fail("no turn on the init tick"),
    )
    monkeypatch.setattr(
        email_thread, "start_thread",
        lambda *a, **k: pytest.fail("no email on the init tick"),
    )
    assert scheduler.run_checkin_once() == {"status": "initialized"}
    assert scheduler.run_checkin_once()["status"] == "skip"


def _age_checkin_marker(days: int) -> None:
    from datetime import date, timedelta
    from bankai.db import session_scope
    from bankai.models import MemoryNote

    stale = (date.today() - timedelta(days=days)).isoformat()
    with session_scope() as s:
        note = s.query(MemoryNote).filter_by(title="Last household check-in").first()
        if note:
            note.content = stale
        else:
            s.add(MemoryNote(title="Last household check-in", content=stale))


def test_checkin_emails_the_household_when_configured(session, monkeypatch):
    from datetime import date
    from bankai.db import session_scope
    from bankai.messaging import email_thread
    from bankai.models import MemoryNote

    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    seen = {}
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="web", force_tier=None: "Net worth held steady; nothing needs you.",
    )
    monkeypatch.setattr(email_thread, "configured", lambda: True)
    monkeypatch.setattr(
        email_thread, "start_thread",
        lambda s, subject, body: seen.update(subject=subject, body=body) or {"sent": True},
    )
    result = scheduler.run_checkin_once()
    assert result["status"] == "sent"
    assert "check-in" in seen["subject"].lower()
    assert "Net worth held steady" in seen["body"]
    with session_scope() as s:
        marker = s.query(MemoryNote).filter_by(title="Last household check-in").one()
        assert marker.content == date.today().isoformat()


def test_checkin_falls_back_to_the_thread_while_email_is_dark(session, monkeypatch):
    from bankai.db import session_scope
    from bankai.messaging import email_thread

    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="web", force_tier=None: "All quiet this week.",
    )
    monkeypatch.setattr(email_thread, "configured", lambda: False)
    monkeypatch.setattr(
        email_thread, "start_thread",
        lambda *a, **k: pytest.fail("must not email while unconfigured"),
    )
    assert scheduler.run_checkin_once()["status"] == "sent"
    with session_scope() as s:
        stored = s.query(ChatMessage).all()
    assert any("All quiet this week." in m.content for m in stored)


def test_checkin_prompt_is_never_stored_as_something_they_said(session, monkeypatch):
    from bankai.db import session_scope
    from bankai.messaging import email_thread

    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    seen = {}

    def fake_turn(s, history, channel="web", force_tier=None):
        seen["history"] = history
        return "Checking in: the picture is unchanged."

    monkeypatch.setattr(scheduler.agent_chat, "run_turn", fake_turn)
    monkeypatch.setattr(email_thread, "configured", lambda: False)
    scheduler.run_checkin_once()
    # the prompt reached the model...
    assert "scheduled check-in" in seen["history"][-1]["content"]
    # ...but was never persisted as a household message
    with session_scope() as s:
        stored = s.query(ChatMessage).all()
    assert not any("scheduled check-in" in m.content for m in stored)


def test_a_failed_send_leaves_the_marker_so_the_next_tick_retries(session, monkeypatch):
    from bankai.db import session_scope
    from bankai.messaging import email_thread
    from bankai.models import MemoryNote

    _age_checkin_marker(days := config.CHECKIN_INTERVAL_DAYS)
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="web", force_tier=None: "A reply that never arrives.",
    )
    monkeypatch.setattr(email_thread, "configured", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("resend down")

    monkeypatch.setattr(email_thread, "start_thread", boom)
    with pytest.raises(RuntimeError):
        scheduler.run_checkin_once()  # the loop catches this; the function may raise
    from datetime import date, timedelta
    with session_scope() as s:
        marker = s.query(MemoryNote).filter_by(title="Last household check-in").one()
        assert marker.content == (date.today() - timedelta(days=days)).isoformat()


def test_zero_interval_disables_the_checkin(monkeypatch):
    monkeypatch.setattr(config, "CHECKIN_INTERVAL_DAYS", 0)
    assert scheduler.run_checkin_once() == {"status": "disabled"}


def test_the_checkin_loop_is_registered_with_the_others():
    assert hasattr(scheduler, "_checkin_loop")
    assert config.CHECKIN_INTERVAL_DAYS >= 1


# --- the sync wake (mind activates when new bank data arrives) ---

def test_sync_wake_skips_without_new_data(session, monkeypatch):
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="tending": pytest.fail("no turn without new data"),
    )
    assert scheduler.run_sync_wake_once({"status": "ok", "added": 0}) == {"status": "skip"}


def test_sync_wake_can_be_disabled(session, monkeypatch):
    monkeypatch.setattr(config, "SYNC_WAKE", False)
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="tending": pytest.fail("disabled means no turn"),
    )
    assert scheduler.run_sync_wake_once({"added": 9}) == {"status": "skip"}


def test_sync_wake_stays_quiet_when_the_data_is_boring(session, monkeypatch):
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="tending": agent_chat.SILENCE,
    )
    result = scheduler.run_sync_wake_once({"added": 3})
    assert result == {"status": "quiet", "added": 3}


def test_sync_wake_speaks_and_the_prompt_is_never_stored(session, monkeypatch):
    from bankai.db import session_scope

    seen = {}

    def fake_turn(s, history, channel="tending"):
        seen["history"] = history
        return "The trust redemption just posted — you are covered into September."

    monkeypatch.setattr(scheduler.agent_chat, "run_turn", fake_turn)
    result = scheduler.run_sync_wake_once({"added": 2})
    assert result["status"] == "spoke"
    assert "trust redemption" in result["said"]
    # the wake prompt reached the model with the count...
    assert "2 new transaction(s)" in seen["history"][-1]["content"]
    # ...but was never persisted as something the household said
    with session_scope() as s:
        stored = s.query(ChatMessage).all()
    assert not any("fresh bank data just synced" in m.content for m in stored)
    assert any("trust redemption" in m.content for m in stored)


# --- a reply that is not a review is not a finished review ---
#
# 2026-11-19: the monthly review came back twice as something else, once as the
# grounding gate's stand-in and once as a request for permission to start. Both
# were stored in the thread and both marked the month as reviewed.

from bankai import grounding as _grounding  # noqa: E402

GATE_STAND_IN = _grounding.refusal_message([])  # the gate's own words, not a copy
GAVE_UP = (
    "I cannot write an accurate monthly review with the data currently on file. "
    "Most accounts show balances from last week or older. May I proceed with "
    "current data before writing the check-in?"
)
GAVE_UP_QUOTING_A_FIGURE = (
    "I can see the Initech card at $912.40, but I cannot write an accurate "
    "monthly review with the data on file. May I proceed?"
)
A_REVIEW = (
    "Net worth closed the month at $48,210.55, up $1,204.10. The Initech card "
    "is the one to watch: $912.40 against a $1,500.00 limit."
)
A_CHECKIN = "Net worth held at $48,210.55; nothing needs you."


def test_the_stand_in_under_test_is_the_one_the_gate_writes():
    assert GATE_STAND_IN.startswith("I couldn't verify part of that answer")
    assert "$" not in GATE_STAND_IN


@pytest.fixture()
def loop_db():
    """The scheduler loops write through session_scope to one throwaway database
    that lives for the whole test run. Start, and leave, with an empty thread,
    no tries on the books, no cycle markers and no flags planted."""
    from bankai.db import session_scope
    from bankai.models import MemoryNote
    from bankai.watchpoints import Watchpoint

    def wipe():
        with session_scope() as s:
            s.query(ChatMessage).delete()
            s.query(Watchpoint).delete()
            s.query(MemoryNote).filter(MemoryNote.title.in_([
                "Monthly review attempts", "Check-in attempts", "Life review attempts",
                "Last monthly review", "Last household check-in", "Last life review",
            ])).delete(synchronize_session=False)
        scheduler._unusable_wakes.clear()
        scheduler._unanswered_prompts.clear()

    wipe()
    yield
    wipe()


def _put_review_marker_a_month_back() -> str:
    from datetime import date, timedelta
    from bankai.db import session_scope
    from bankai.models import MemoryNote

    last_month = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    with session_scope() as s:
        note = s.query(MemoryNote).filter_by(title="Last monthly review").first()
        if note:
            note.content = last_month
        else:
            s.add(MemoryNote(title="Last monthly review", content=last_month))
    return last_month


def _note(title: str):
    from bankai.db import session_scope
    from bankai.models import MemoryNote

    with session_scope() as s:
        note = s.query(MemoryNote).filter_by(title=title).first()
        return note.content if note else None


def _thread():
    from bankai.db import session_scope

    with session_scope() as s:
        rows = s.query(ChatMessage).order_by(ChatMessage.created_at, ChatMessage.id).all()
        return [(m.role, m.speaker, m.content) for m in rows]


def _replies(monkeypatch, *replies):
    """Make each scheduled turn return the next of `replies`; record what it saw."""
    seen = {"calls": 0}
    queue = list(replies)

    def fake_turn(s, history, channel="web", force_tier=None):
        seen["calls"] += 1
        seen["history"] = history
        seen["tier"] = force_tier
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(scheduler.agent_chat, "run_turn", fake_turn)
    return seen


def _email(monkeypatch, sent=None):
    """Email is up. Bodies land in `sent`; with no list, any send fails the test."""
    from bankai.messaging import email_thread

    monkeypatch.setattr(email_thread, "configured", lambda: True)
    if sent is None:
        monkeypatch.setattr(
            email_thread, "start_thread",
            lambda *a, **k: pytest.fail("this reply must never be emailed"),
        )
    else:
        monkeypatch.setattr(
            email_thread, "start_thread",
            lambda s, subject, body: sent.append(body) or {"sent": True},
        )


@pytest.mark.parametrize("reply, usable", [
    (A_REVIEW, True),
    (GATE_STAND_IN, False),
    (GAVE_UP, False),
    (GAVE_UP_QUOTING_A_FIGURE, False),
    (agent_chat.SILENCE, False),
    ("", False),
    ("   ", False),
    # a real limit, stated with its figures, is still a review
    ("Net worth is $48,210.55. The Hooli feed has been stale since March, so "
     "spending there is not in this figure.", True),
    ("I can't write a complete review while the Hooli feed is stale. What I can "
     "stand behind: net worth $48,210.55, up $1,204.10, and the Initech card at "
     "$912.40.", True),
    # asking for one missing document is part of the job, not a give-up
    ("Net worth is $48,210.55. Could you upload the Hooli statement before next "
     "month's review?", True),
])
def test_what_counts_as_a_review(reply, usable):
    assert (scheduler.review_problem(reply) is None) is usable


@pytest.mark.parametrize("reply, sendable", [
    (A_CHECKIN, True),
    ("Nothing has moved since Monday and nothing needs you.", True),  # no figure is fine
    (GATE_STAND_IN, False),
    ("I can't write this check-in properly with the data on file. Shall I proceed?", False),
    ("", False),
    ("  \n ", False),                # an empty email is not a check-in
    (agent_chat.SILENCE, True),      # a deliberate silence is the quiet path's to handle
    # a real question about a real bill is a check-in, not a give-up
    ("The Hooli bill of $312.40 is due on the 1st. Shall I go ahead and schedule it?", True),
])
def test_what_may_go_out_as_a_checkin(reply, sendable):
    assert (scheduler.checkin_problem(reply) is None) is sendable


def test_a_good_review_lands_in_the_thread_and_marks_the_month(loop_db, monkeypatch):
    from datetime import date

    _put_review_marker_a_month_back()
    seen = _replies(monkeypatch, A_REVIEW)
    assert scheduler.run_monthly_review_once() == {"status": "ran"}
    assert seen["tier"] == "review"
    assert "scheduled monthly review" in seen["history"][-1]["content"]
    # the prompt, then the review under it, exactly as the thread showed before
    assert _thread() == [
        ("user", "monthly review (scheduled)", scheduler.MONTHLY_REVIEW_PROMPT),
        ("assistant", "copilot", A_REVIEW),
    ]
    assert _note("Last monthly review") == date.today().strftime("%Y-%m")
    assert scheduler.run_monthly_review_once() == {"status": "skip"}
    assert seen["calls"] == 1


def test_the_prompt_is_filed_when_it_was_asked_and_the_review_when_it_came(
    loop_db, monkeypatch,
):
    """A turn can run for many minutes. Whatever the household says meanwhile
    belongs between the two rows, as it did when the prompt was stored first."""
    import time
    from bankai.db import session_scope
    from bankai.models import _utcnow

    _put_review_marker_a_month_back()
    during = {}

    def slow_turn(s, history, channel="web", force_tier=None):
        time.sleep(0.02)
        during["at"] = _utcnow()
        time.sleep(0.02)
        return A_REVIEW

    monkeypatch.setattr(scheduler.agent_chat, "run_turn", slow_turn)
    assert scheduler.run_monthly_review_once() == {"status": "ran"}
    with session_scope() as s:
        prompt, review = s.query(ChatMessage).order_by(ChatMessage.created_at).all()
        assert (prompt.role, review.role) == ("user", "assistant")
        assert prompt.created_at < during["at"] < review.created_at


@pytest.mark.parametrize("bad", [
    GATE_STAND_IN, GAVE_UP, GAVE_UP_QUOTING_A_FIGURE, agent_chat.SILENCE,
])
def test_an_unusable_review_stores_nothing_and_the_next_tick_tries_again(
    loop_db, monkeypatch, bad,
):
    from datetime import date

    last_month = _put_review_marker_a_month_back()
    seen = _replies(monkeypatch, bad, A_REVIEW)
    result = scheduler.run_monthly_review_once()
    assert result["status"] == "withheld" and result["attempt"] == 1
    assert _thread() == []                                   # nothing to re-read
    assert _note("Last monthly review") == last_month        # the month is still owed
    # the next tick asks again, and a real review ends it
    assert scheduler.run_monthly_review_once() == {"status": "ran"}
    assert seen["calls"] == 2
    assert [role for role, _, _ in _thread()] == ["user", "assistant"]
    assert _note("Last monthly review") == date.today().strftime("%Y-%m")


def test_a_review_that_never_comes_good_stops_at_the_limit(loop_db, monkeypatch):
    from datetime import date

    _put_review_marker_a_month_back()
    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 3)
    seen = _replies(monkeypatch, GATE_STAND_IN)
    statuses = [scheduler.run_monthly_review_once()["status"] for _ in range(3)]
    assert statuses == ["withheld", "withheld", "abandoned"]
    # no fourth model turn, this tick or any later one this month
    assert scheduler.run_monthly_review_once() == {"status": "skip"}
    assert seen["calls"] == 3
    assert _thread() == []
    month = date.today().strftime("%Y-%m")
    assert _note("Monthly review attempts") == f"{month}:3"


def test_a_review_its_own_guards_threw_away_is_a_spent_try(loop_db, monkeypatch):
    """The model was paid to answer and the turn's guards threw every draft
    away. Left uncounted, that is a paid turn every twelve hours for ever."""
    _put_review_marker_a_month_back()
    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 2)
    seen = _replies(monkeypatch, agent_chat.UnusableReply(
        "claude-cli: backend twice claimed its own tools were unavailable"))
    first = scheduler.run_monthly_review_once()
    assert first["status"] == "withheld" and "threw every draft away" in first["why"]
    assert scheduler.run_monthly_review_once()["status"] == "abandoned"
    assert scheduler.run_monthly_review_once() == {"status": "skip"}
    assert seen["calls"] == 2
    assert _thread() == []


def test_a_downed_backend_stores_nothing_and_uses_up_no_try(loop_db, monkeypatch):
    last_month = _put_review_marker_a_month_back()
    seen = _replies(monkeypatch, RuntimeError("all Claude models are out of credits/usage"))
    for _ in range(5):
        with pytest.raises(RuntimeError):
            scheduler.run_monthly_review_once()  # the loop catches this
    assert seen["calls"] == 5
    assert _thread() == []                       # not even the prompt
    assert _note("Monthly review attempts") is None
    assert _note("Last monthly review") == last_month


def test_last_cycles_failures_do_not_count_against_this_one(session):
    for _ in range(3):
        scheduler._record_failed_attempt(session, "Monthly review attempts", "2031-01")
    session.flush()
    assert scheduler._failed_attempts(session, "Monthly review attempts", "2031-01") == 3
    assert scheduler._failed_attempts(session, "Monthly review attempts", "2031-02") == 0
    assert scheduler._record_failed_attempt(session, "Monthly review attempts", "2031-02") == 1
    # a note someone mangled reads as no tries, never as a crash
    note = session.query(scheduler.MemoryNote).filter_by(title="Monthly review attempts").one()
    for mangled in ("garbled", "2031-02:", "2031-02:\u00b2", ":", ""):
        note.content = mangled
        session.flush()
        assert scheduler._failed_attempts(session, "Monthly review attempts", "2031-02") == 0


def test_a_withheld_checkin_is_never_emailed_and_the_next_tick_tries_again(
    loop_db, monkeypatch,
):
    from datetime import date, timedelta

    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    stale = (date.today() - timedelta(days=config.CHECKIN_INTERVAL_DAYS)).isoformat()
    sent = []
    _email(monkeypatch, sent)
    seen = _replies(monkeypatch, GATE_STAND_IN, A_CHECKIN)
    result = scheduler.run_checkin_once()
    assert (result["status"], result["attempt"]) == ("withheld", 1)
    assert sent == [] and _thread() == []
    assert _note("Last household check-in") == stale
    # the next tick runs again and the real one goes out
    assert scheduler.run_checkin_once()["status"] == "sent"
    assert seen["calls"] == 2
    assert sent == [A_CHECKIN]
    assert _note("Last household check-in") == date.today().isoformat()


def test_a_checkin_that_asks_leave_to_start_is_never_emailed(loop_db, monkeypatch):
    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    sent = []
    _email(monkeypatch, sent)
    _replies(
        monkeypatch,
        "I can't write this check-in properly with the data on file. Shall I proceed?",
        A_CHECKIN,
    )
    assert scheduler.run_checkin_once()["status"] == "withheld"
    assert sent == []
    assert scheduler.run_checkin_once()["status"] == "sent"
    assert sent == [A_CHECKIN]


def test_a_checkin_withheld_every_time_skips_the_cycle(loop_db, monkeypatch):
    from datetime import date

    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 3)
    _email(monkeypatch)  # any send fails the test
    seen = _replies(monkeypatch, GATE_STAND_IN)
    statuses = [scheduler.run_checkin_once()["status"] for _ in range(3)]
    assert statuses == ["withheld", "withheld", "abandoned"]
    assert scheduler.run_checkin_once()["status"] == "skip"   # until the next cycle
    assert seen["calls"] == 3
    assert _thread() == []
    assert _note("Last household check-in") == date.today().isoformat()


def test_a_checkin_its_own_guards_threw_away_is_a_spent_try(loop_db, monkeypatch):
    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 2)
    _email(monkeypatch)  # any send fails the test
    seen = _replies(monkeypatch, agent_chat.UnusableReply(
        "claude-cli: backend produced only refusal-template output twice"))
    assert [scheduler.run_checkin_once()["status"] for _ in range(2)] == [
        "withheld", "abandoned",
    ]
    assert scheduler.run_checkin_once()["status"] == "skip"
    assert seen["calls"] == 2


def test_a_downed_backend_uses_up_no_checkin_try(loop_db, monkeypatch):
    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    _email(monkeypatch)  # any send fails the test
    _replies(monkeypatch, RuntimeError("all Claude models are out of credits/usage"))
    for _ in range(4):
        with pytest.raises(RuntimeError):
            scheduler.run_checkin_once()
    assert _note("Check-in attempts") is None


def test_a_new_checkin_cycle_starts_with_a_full_set_of_tries(loop_db, monkeypatch):
    from bankai.messaging import email_thread

    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(email_thread, "configured", lambda: False)
    seen = _replies(monkeypatch, GATE_STAND_IN)
    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS)
    assert [scheduler.run_checkin_once()["status"] for _ in range(2)] == [
        "withheld", "abandoned",
    ]
    # the cycle after that one (a different last-check-in date) is owed its own
    _age_checkin_marker(config.CHECKIN_INTERVAL_DAYS + 1)
    assert scheduler.run_checkin_once()["status"] == "withheld"
    assert seen["calls"] == 3


# --- the other scheduled turns hold the same line ---
#
# The monthly review and the check-in stopped storing a guard's stand-in on
# 2026-11-19. The watchpoint wake, the sync wake, tending and the life review
# still took whatever came back: it went into the thread and the turn counted
# as done.

A_WAKE_NOTE = "The Initech card is at $912.40 against its $1,500.00 limit. Pay it this week."
GUARDS_THREW_IT_AWAY = agent_chat.UnusableReply(
    "claude-cli: backend produced only refusal-template output twice"
)
NOT_AN_ANSWER = [GATE_STAND_IN, "", "  \n ", GUARDS_THREW_IT_AWAY]


def _plant_due_flag(title: str = "Initech card payoff") -> str:
    """A date watchpoint whose date has passed. Returns its id."""
    from bankai import watchpoints
    from bankai.db import session_scope

    with session_scope() as s:
        flag = watchpoints.create_watchpoint(
            s, title=title, kind="on_date", note="check the Initech balance",
            params={"date": "2026-01-05"},
        )
        return flag.id


def _flag_status(flag_id: str) -> str:
    from bankai.db import session_scope
    from bankai.watchpoints import Watchpoint

    with session_scope() as s:
        return s.get(Watchpoint, flag_id).status


@pytest.mark.parametrize("reply, kept", [
    ("Your Initech minimum is due in 2 days and nothing is scheduled.", True),
    (agent_chat.SILENCE, True),      # a deliberate silence is the caller's to handle
    (GATE_STAND_IN, False),
    ("", False),
    ("  \n ", False),
])
def test_what_may_be_kept_from_a_scheduled_turn(reply, kept):
    assert (scheduler.reply_problem(reply) is None) is kept


def test_a_named_document_can_be_given_up_on_and_nothing_else():
    gave_up = "I cannot complete the life review with the data on file. May I proceed?"
    assert scheduler.reply_problem(gave_up, "life review") is not None
    # with nothing named, a sentence about a review is just a sentence
    assert scheduler.reply_problem(gave_up) is None
    # and a real finding that mentions its own limits, with its figures, is kept
    finding = (
        "I couldn't complete the review of March while the Hooli feed is stale, "
        "but the Initech card moved from $612.40 to $912.40 and $300.00 of that "
        "is one charge."
    )
    assert scheduler.reply_problem(finding, "life review") is None


def test_the_line_under_an_unanswered_flag_never_reaches_the_model():
    from bankai.agent import verify

    assert verify.is_error_template(scheduler.WAKE_UNANSWERED)
    assert "$" not in scheduler.WAKE_UNANSWERED


def test_a_wake_that_answers_lands_under_its_prompt(loop_db, monkeypatch):
    flag = _plant_due_flag()
    seen = _replies(monkeypatch, A_WAKE_NOTE)
    result = scheduler.run_watchpoints_once()
    assert result == {"fired": 1, "woken": 1, "withheld": 0, "deferred": 0}
    asked, answered = _thread()
    assert asked[:2] == ("user", "watchpoint (scheduled)")
    assert asked[2].startswith("[watchpoint fired] 'Initech card payoff'")
    assert answered == ("assistant", "copilot", A_WAKE_NOTE)
    # the model was asked exactly what the thread now shows, and nobody forced a tier
    assert seen["history"][-1] == {
        "role": "user", "content": f"[watchpoint (scheduled)] {asked[2]}",
    }
    assert seen["tier"] is None
    assert _flag_status(flag) == "fired"
    # a flag fires once
    assert scheduler.run_watchpoints_once()["fired"] == 0
    assert seen["calls"] == 1


ONE_FLAG_ON_SHOW = [("user", "watchpoint (scheduled)")]


def _flags_and_replies() -> list[tuple[str, str]]:
    return [row[:2] for row in _thread()]


def _wake_books() -> tuple[dict, dict]:
    return scheduler._unusable_wakes, scheduler._unanswered_prompts


@pytest.mark.parametrize("bad", NOT_AN_ANSWER)
def test_a_wake_that_is_not_an_answer_files_no_reply_and_the_flag_fires_again(
    loop_db, monkeypatch, bad,
):
    flag = _plant_due_flag()
    seen = _replies(monkeypatch, bad, A_WAKE_NOTE)
    result = scheduler.run_watchpoints_once()
    assert (result["fired"], result["woken"], result["withheld"]) == (1, 0, 1)
    # the flag and its reading stay on show; nothing a guard wrote goes under them
    assert _flags_and_replies() == ONE_FLAG_ON_SHOW
    assert _flag_status(flag) == "armed"   # not counted as done
    # the next tick asks again in place of the unanswered prompt, and the note lands
    assert scheduler.run_watchpoints_once()["woken"] == 1
    assert seen["calls"] == 2
    assert [row[0] for row in _thread()] == ["user", "assistant"]
    assert _thread()[1][2] == A_WAKE_NOTE
    assert _flag_status(flag) == "fired"
    assert _wake_books() == ({}, {})


@pytest.mark.parametrize("bad", [GATE_STAND_IN, GUARDS_THREW_IT_AWAY])
def test_a_wake_that_never_comes_good_stops_and_the_thread_says_so(
    loop_db, monkeypatch, bad,
):
    from bankai.db import session_scope
    from bankai.messaging import thread as chat_thread

    flag = _plant_due_flag()
    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 3)
    seen = _replies(monkeypatch, bad)
    results = []
    for _ in range(5):
        results.append(scheduler.run_watchpoints_once())
        # one question in the thread, however many times it was asked
        assert [row[0] for row in _thread()].count("user") == 1
    assert [r["fired"] for r in results] == [1, 1, 1, 0, 0]
    assert all(r["woken"] == 0 for r in results)
    assert seen["calls"] == 3              # three paid turns, then it stops asking
    assert _flag_status(flag) == "fired"
    # the household is told the flag tripped, in words no guard wrote for a figure
    asked, answered = _thread()
    assert asked[2].startswith("[watchpoint fired] 'Initech card payoff'")
    assert answered == ("assistant", "copilot", scheduler.WAKE_UNANSWERED)
    # and the model reads the flag back, never the line under it
    with session_scope() as s:
        history = chat_thread.build_history(s)
    assert [m["role"] for m in history] == ["user"]
    assert _wake_books() == ({}, {})


def test_a_downed_backend_leaves_one_prompt_however_long_it_is_down(loop_db, monkeypatch):
    flag = _plant_due_flag()
    _replies(monkeypatch, RuntimeError("all Claude models are out of credits/usage"))
    for _ in range(5):
        result = scheduler.run_watchpoints_once()
        assert (result["fired"], result["woken"], result["withheld"]) == (1, 0, 0)
        # the flag shows, once: it used to leave one more prompt every tick
        assert _flags_and_replies() == ONE_FLAG_ON_SHOW
        assert _flag_status(flag) == "armed"
    assert scheduler._unusable_wakes == {}   # an outage uses up no try
    # the backend comes back: the flag still has all its tries and its note lands
    _replies(monkeypatch, A_WAKE_NOTE)
    assert scheduler.run_watchpoints_once()["woken"] == 1
    assert [row[0] for row in _thread()] == ["user", "assistant"]
    assert _thread()[1][2] == A_WAKE_NOTE


def _move_flag_date(flag_id: str, day: str) -> None:
    from bankai.db import session_scope
    from bankai.watchpoints import Watchpoint

    with session_scope() as s:
        s.get(Watchpoint, flag_id).params = {"date": day}


@pytest.mark.parametrize("bad", [GATE_STAND_IN, RuntimeError("backend down")])
def test_a_flag_that_tripped_stays_on_show_when_it_stops_firing(loop_db, monkeypatch, bad):
    """Its reading moves back before the next try, so no wake is coming. The
    trip must not vanish with it, and a later trip starts from nothing."""
    flag = _plant_due_flag()
    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 3)
    seen = _replies(monkeypatch, bad)
    scheduler.run_watchpoints_once()
    assert _flag_status(flag) == "armed"
    _move_flag_date(flag, "2099-01-01")             # the condition no longer holds
    assert scheduler.run_watchpoints_once()["fired"] == 0
    assert seen["calls"] == 1
    assert _flags_and_replies() == ONE_FLAG_ON_SHOW  # the record that it tripped
    assert _thread()[0][2].startswith("[watchpoint fired] 'Initech card payoff'")
    assert _wake_books() == ({}, {})
    # it trips again much later: that is a new event, with all its tries
    _move_flag_date(flag, "2026-01-05")
    seen = _replies(monkeypatch, GATE_STAND_IN)
    for _ in range(5):
        scheduler.run_watchpoints_once()
    assert seen["calls"] == 3
    assert [row[0] for row in _thread()] == ["user", "user", "assistant"]
    assert _thread()[2][2] == scheduler.WAKE_UNANSWERED


def test_a_cancelled_flag_keeps_the_record_that_it_tripped(loop_db, monkeypatch):
    from bankai import watchpoints
    from bankai.db import session_scope

    flag = _plant_due_flag()
    seen = _replies(monkeypatch, GATE_STAND_IN)
    scheduler.run_watchpoints_once()
    with session_scope() as s:
        watchpoints.cancel_watchpoint(s, flag)
    assert scheduler.run_watchpoints_once()["fired"] == 0
    assert seen["calls"] == 1
    assert _flags_and_replies() == ONE_FLAG_ON_SHOW
    assert _wake_books() == ({}, {})


def test_a_wake_answered_with_silence_files_no_marker(loop_db, monkeypatch):
    flag = _plant_due_flag()
    seen = _replies(monkeypatch, agent_chat.SILENCE)
    result = scheduler.run_watchpoints_once()
    assert (result["woken"], result["withheld"]) == (1, 0)
    assert _flags_and_replies() == ONE_FLAG_ON_SHOW  # the flag, and no "[[no reply]]" row
    assert _flag_status(flag) == "fired"
    assert scheduler.run_watchpoints_once()["fired"] == 0
    assert seen["calls"] == 1


def test_one_bad_wake_does_not_cost_the_flag_beside_it(loop_db, monkeypatch):
    first = _plant_due_flag("Initech card payoff")
    second = _plant_due_flag("Hooli renewal")
    _replies(monkeypatch, GATE_STAND_IN, A_WAKE_NOTE)   # the first wake is withheld
    result = scheduler.run_watchpoints_once()
    assert (result["fired"], result["woken"], result["withheld"]) == (2, 1, 1)
    assert {_flag_status(first), _flag_status(second)} == {"armed", "fired"}
    # both flags show; only the second has a note under it
    assert [row[0] for row in _thread()] == ["user", "user", "assistant"]
    assert GATE_STAND_IN not in [row[2] for row in _thread()]


def test_a_flag_waiting_its_turn_keeps_its_count(loop_db, monkeypatch):
    """More flags trip than one tick may wake, so the overflow waits. A flag
    that already failed a try and then has to wait is still owed its wake,
    with the same count: waiting must not hand it a fresh set of tries."""
    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 2)
    pair = {_plant_due_flag("Initech card payoff"), _plant_due_flag("Hooli renewal")}
    seen = _replies(monkeypatch, GATE_STAND_IN)
    scheduler.run_watchpoints_once()                    # both fail their first try
    assert scheduler._unusable_wakes == dict.fromkeys(pair, 1)
    monkeypatch.setattr(scheduler, "MAX_WAKES_PER_TICK", 1)
    result = scheduler.run_watchpoints_once()           # one is abandoned, one waits
    assert (result["fired"], result["deferred"]) == (1, 1)
    (waiting,) = scheduler._unusable_wakes
    assert scheduler._unusable_wakes == {waiting: 1} and waiting in pair
    assert waiting in scheduler._unanswered_prompts
    scheduler.run_watchpoints_once()                    # its second try is its last
    assert _wake_books() == ({}, {})
    assert scheduler.run_watchpoints_once()["fired"] == 0
    assert seen["calls"] == 4
    assert [row[0] for row in _thread()].count("user") == 2


@pytest.mark.parametrize("bad", NOT_AN_ANSWER)
def test_tending_never_files_a_reply_that_is_not_one(loop_db, monkeypatch, bad):
    seen = _replies(monkeypatch, bad)
    result = scheduler.run_tending_once()
    assert result["status"] == "withheld" and result["why"]
    assert _thread() == []
    assert seen["calls"] == 1 and seen["tier"] is None


@pytest.mark.parametrize("bad", NOT_AN_ANSWER)
def test_the_sync_wake_never_files_a_reply_that_is_not_one(loop_db, monkeypatch, bad):
    monkeypatch.setattr(config, "SYNC_WAKE", True)
    _replies(monkeypatch, bad)
    result = scheduler.run_sync_wake_once({"added": 2})
    assert (result["status"], result["added"]) == ("withheld", 2)
    assert _thread() == []
    # the next sync with new data wakes it again, and real news still lands
    _replies(monkeypatch, "The Hooli refund of $64.20 posted this morning.")
    assert scheduler.run_sync_wake_once({"added": 1})["status"] == "spoke"
    assert [row[2] for row in _thread()] == ["The Hooli refund of $64.20 posted this morning."]


@pytest.mark.parametrize("bad", NOT_AN_ANSWER)
def test_a_life_review_that_is_not_one_stores_nothing_and_the_next_tick_tries_again(
    loop_db, monkeypatch, bad,
):
    from datetime import date

    monkeypatch.setattr(config, "LIFE_REVIEW_DAYS", 7)
    seen = _replies(monkeypatch, bad, agent_chat.SILENCE)
    result = scheduler.run_life_review_once()
    assert (result["status"], result["attempt"]) == ("withheld", 1)
    assert _thread() == []
    assert _note("Last life review") is None      # the week is not marked done
    # the next tick runs again; a quiet review is a finished one
    assert scheduler.run_life_review_once()["status"] == "quiet"
    assert seen["calls"] == 2
    assert _note("Last life review") == date.today().isoformat()
    assert scheduler.run_life_review_once()["status"] == "not_due"


def test_a_life_review_withheld_every_time_skips_the_cycle(loop_db, monkeypatch):
    from datetime import date

    monkeypatch.setattr(config, "LIFE_REVIEW_DAYS", 7)
    monkeypatch.setattr(config, "SCHEDULED_TURN_MAX_ATTEMPTS", 2)
    seen = _replies(monkeypatch, GATE_STAND_IN)
    statuses = [scheduler.run_life_review_once()["status"] for _ in range(3)]
    assert statuses == ["withheld", "abandoned", "not_due"]
    assert seen["calls"] == 2
    assert _thread() == []
    assert _note("Last life review") == date.today().isoformat()


def test_a_life_review_with_something_to_say_still_says_it(loop_db, monkeypatch):
    from datetime import date

    monkeypatch.setattr(config, "LIFE_REVIEW_DAYS", 7)
    said = "Hooli billed twice this month: $14.99 on the 3rd and $14.99 on the 17th."
    _replies(monkeypatch, said)
    assert scheduler.run_life_review_once()["status"] == "spoke"
    assert _thread() == [("assistant", "copilot", said)]
    assert _note("Last life review") == date.today().isoformat()


def test_a_downed_backend_uses_up_no_life_review_try(loop_db, monkeypatch):
    monkeypatch.setattr(config, "LIFE_REVIEW_DAYS", 7)
    _replies(monkeypatch, RuntimeError("all Claude models are out of credits/usage"))
    for _ in range(4):
        with pytest.raises(RuntimeError):
            scheduler.run_life_review_once()
    assert _note("Life review attempts") is None
    assert _note("Last life review") is None
