"""Mind steers the voice mouth — interim lines while deep tools run."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.energy_agent_mind import (
    EaEvent,
    EaPlan,
    EaTask,
    EaWorldState,
    interim_voice_steer,
    push_voice_steer,
    voice_steer_turn,
    _guess_intent,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    EaWorldState.__table__.create(engine)
    EaPlan.__table__.create(engine)
    EaTask.__table__.create(engine)
    EaEvent.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_interim_rates_line():
    assert interim_voice_steer("what is my solar credit rate?", None) == "Pulling your rates now."
    assert interim_voice_steer("set my offtaker discount", None) == "Pulling your rates now."


def test_interim_short_skip():
    assert interim_voice_steer("hi", None) is None
    assert interim_voice_steer("ok", None) is None


def test_interim_by_plan_kind():
    assert interim_voice_steer("anything", "fleet_concern") == "Checking the fleet picture now."
    assert interim_voice_steer("yes do it", "ux_proposal_execute") == (
        "On it — opening the builder with a prompt ready."
    )


def test_guess_intent_rate_and_improve():
    assert _guess_intent("what are my net rates per kWh?") == "rate_or_billing"
    assert _guess_intent("can we improve the invoice pipeline with energy balls") == "ux_friction"


def test_voice_steer_turn_emits_event(db):
    out = voice_steer_turn(
        db, "ten_test", "sess_1", "What is my solar credit rate right now?"
    )
    assert out["ok"] is True
    assert out["speak"] == "Pulling your rates now."
    assert out["mode"] == "interim"
    assert out["intent"] == "rate_or_billing"
    assert out["principles"]["mind_steers_voice"] is True

    evs = db.query(EaEvent).filter_by(tenant_id="ten_test", kind="voice_steer").all()
    assert len(evs) == 1
    assert evs[0].speak_as_mind == "Pulling your rates now."


def test_voice_steer_turn_trivial_no_speak(db):
    out = voice_steer_turn(db, "ten_test", None, "yo")
    assert out["ok"] is True
    assert out["speak"] is None
    assert db.query(EaEvent).filter_by(tenant_id="ten_test").count() == 0


def test_push_voice_steer_empty():
    # No db needed — empty rejected before write
    class _Fake:
        pass
    assert push_voice_steer(_Fake(), "t", speak="  ") == {"ok": False, "reason": "empty"}
