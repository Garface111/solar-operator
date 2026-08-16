"""Two live failures, pinned: the verifier must never ship a refusal in place of
a real answer, and daily reminders must actually fire."""
from datetime import datetime

import pytest

from bankai.agent import verify
from bankai.models import Rule
from bankai.rules.engine import evaluate_rules

ORIGINAL = (
    "Your checking (2724) is $561.57, the Apple Card owes $7,600.17, and the "
    "trust redemption of $9,989 lands on the 15th. Net worth: $1,277,709.72."
)


# --- the refusal guard ---

def test_a_refusal_shaped_revision_is_detected():
    refusal = (
        "I need to stop myself here. I was about to state figures that I cannot "
        "find in your data ($561.57, $9,989), and I could not rewrite the answer "
        "without them."
    )
    assert verify.revision_is_refusal(ORIGINAL, refusal) is True


def test_a_number_stripped_gutting_is_detected():
    gutted = "I'd rather not quote balances right now; ask me again later."
    assert verify.revision_is_refusal(ORIGINAL, gutted) is True


def test_a_genuine_fix_passes():
    fixed = ORIGINAL.replace("$1,277,709.72", "$1,277,809.72")  # figure corrected
    assert verify.revision_is_refusal(ORIGINAL, fixed) is False


def test_verified_turn_keeps_the_original_when_revision_refuses(session, monkeypatch):
    monkeypatch.setattr(verify, "VERIFY_REPLIES", True)
    calls = {"n": 0}

    def backend(s, system, messages):
        calls["n"] += 1
        if system == verify.CRITIC_SYSTEM:
            return '{"verdict": "revise", "problems": ["$561.57 is unsupported"], "severity": "high"}'
        return (
            "I need to stop myself here. I was about to state figures that I "
            "cannot find in your data."
        )

    final, report = verify.verified_turn(
        session, [{"role": "user", "content": "balances?"}], ORIGINAL, backend
    )
    assert final == ORIGINAL                       # the real answer shipped
    assert report["revised"] is False
    assert report["reason"] == "revision_rejected_refusal"


def test_a_real_revision_still_ships(session, monkeypatch):
    monkeypatch.setattr(verify, "VERIFY_REPLIES", True)

    def backend(s, system, messages):
        if system == verify.CRITIC_SYSTEM:
            return '{"verdict": "revise", "problems": ["net worth arithmetic"], "severity": "high"}'
        return ORIGINAL.replace("$1,277,709.72", "$1,277,809.72")

    final, report = verify.verified_turn(
        session, [{"role": "user", "content": "balances?"}], ORIGINAL, backend
    )
    assert "$1,277,809.72" in final and report["revised"] is True


# --- daily reminders actually fire ---

def _daily_rule(session, hour=8, tz="America/Los_Angeles"):
    rule = Rule(
        name="Morning spend summary", kind="reminder",
        params={"daily": True, "hour": hour, "minute": 0, "timezone": tz},
        message="Morning report", enabled=True, created_by="agent",
    )
    session.add(rule)
    session.flush()
    return rule


def test_daily_reminder_fires_after_the_local_hour(session):
    _daily_rule(session, hour=8)
    # 16:30 UTC = 08:30 or 09:30 PT depending on DST — either way past 8am PT
    fired = evaluate_rules(session, now=datetime(2026, 8, 13, 16, 30))
    assert len(fired) == 1
    assert fired[0].subject.startswith("Reminder: Morning spend summary")


def test_daily_reminder_does_not_fire_before_the_local_hour(session):
    _daily_rule(session, hour=8)
    # 13:00 UTC = 06:00 PT (PDT) — before the 8am target
    assert evaluate_rules(session, now=datetime(2026, 8, 13, 13, 0)) == []


def test_daily_reminder_fires_once_per_day_only(session):
    _daily_rule(session, hour=8)
    assert len(evaluate_rules(session, now=datetime(2026, 8, 13, 16, 30))) == 1
    # the every-15-minutes scheduler re-evaluates all day; dedupe holds
    assert evaluate_rules(session, now=datetime(2026, 8, 13, 17, 0)) == []
    assert evaluate_rules(session, now=datetime(2026, 8, 13, 23, 45)) == []
    # and it fires again the next local day
    assert len(evaluate_rules(session, now=datetime(2026, 8, 14, 16, 30))) == 1


def test_weekly_and_monthly_reminders_still_work(session):
    session.add(Rule(name="rent", kind="reminder", params={"day_of_month": 1},
                     message="pay rent", enabled=True, created_by="agent"))
    session.flush()
    assert len(evaluate_rules(session, now=datetime(2026, 9, 1, 12, 0))) == 1
    assert evaluate_rules(session, now=datetime(2026, 9, 2, 12, 0)) == []