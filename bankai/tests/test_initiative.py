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
    no tries on the books and no cycle markers."""
    from bankai.db import session_scope
    from bankai.models import MemoryNote

    def wipe():
        with session_scope() as s:
            s.query(ChatMessage).delete()
            s.query(MemoryNote).filter(MemoryNote.title.in_([
                "Monthly review attempts", "Check-in attempts",
                "Last monthly review", "Last household check-in",
            ])).delete(synchronize_session=False)

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
