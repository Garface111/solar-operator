"""Adaptive model routing: quick asks go fast, real analysis goes deep."""
import pytest

from bankai import config, router
from bankai.agent import chat as agent_chat


def msg(text):
    return [{"role": "user", "content": text}]


@pytest.fixture(autouse=True)
def routing_on(monkeypatch):
    monkeypatch.setattr(config, "ROUTER_ENABLED", True)
    monkeypatch.setattr(config, "ROUTER_COMPLEX_MODEL", "claude-fable-5")
    monkeypatch.setattr(config, "ROUTER_COMPLEX_EFFORT", "max")
    monkeypatch.setattr(config, "ROUTER_FAST_MODEL", "claude-opus-5")
    monkeypatch.setattr(config, "ROUTER_FAST_EFFORT", "low")
    monkeypatch.setattr(config, "ROUTER_QUICK_MODEL", "claude-sonnet-5")
    monkeypatch.setattr(config, "ROUTER_QUICK_EFFORT", "low")


# --- the three tiers ---

def test_explicit_urgency_picks_sonnet_and_skips_verify():
    d = router.choose(msg("[Ford] what did we spend on groceries, real quick"))
    assert d.tier == "quick" and d.model == "claude-sonnet-5" and d.verify is False
    assert router.choose(msg("quick q: checking balance?")).tier == "quick"
    assert router.choose(msg("tldr our net worth")).tier == "quick"


def test_a_simple_lookup_picks_opus_fast():
    d = router.choose(msg("[Ford] what's my checking balance?"))
    assert d.tier == "fast" and d.model == "claude-opus-5" and d.verify is False


def test_a_net_worth_lookup_is_fast_not_a_deep_think():
    # regression: 'net worth' as a bare word wrongly routed to Fable-max (169s)
    assert router.choose(msg("[Gaurav] what's our net worth right now")).tier == "fast"
    assert router.choose(msg("[Ford] how much cash do we have?")).tier == "fast"


def test_a_long_horizon_projection_still_goes_deep():
    assert router.choose(msg("what will our net worth be in 10 years?")).tier == "complex"
    assert router.choose(msg("project our retirement savings")).tier == "complex"


def test_analysis_picks_fable_max_and_verifies():
    d = router.choose(msg("[Ford] should we refinance the mortgage given rates?"))
    assert d.tier == "complex" and d.model == "claude-fable-5"
    assert d.effort == "max" and d.verify is True
    assert router.choose(msg("analyze our spending and tell me where to cut")).tier == "complex"


def test_a_long_ask_is_treated_as_complex():
    long = "[Ford] " + "here is a lot of context about our situation " * 8 + " what do you think?"
    assert router.choose(msg(long)).tier == "complex"


def test_urgency_wins_even_over_a_complex_question():
    # Ford's rule: if they say real quick, honor speed even for a hard question
    d = router.choose(msg("real quick — should we cancel the Amex?"))
    assert d.tier == "quick" and d.model == "claude-sonnet-5"


def test_two_questions_reads_as_complex():
    assert router.choose(msg("what's the balance? and are we on track?")).tier == "complex"


def test_quicken_is_not_mistaken_for_quick():
    # "quicken" must not trip the bare-quick rule
    assert router.choose(msg("did the Quicken import run?")).tier != "quick"


# --- background + disabled ---

def test_background_tending_is_always_complex():
    assert router.choose(msg("(scheduled tending prompt)"), channel="tending").tier == "complex"


def test_disabled_router_falls_back_to_fixed_config(monkeypatch):
    monkeypatch.setattr(config, "ROUTER_ENABLED", False)
    monkeypatch.setattr(config, "CLAUDE_CLI_MODEL", "claude-fable-5")
    monkeypatch.setattr(config, "CLAUDE_CLI_EFFORT", "max")
    d = router.choose(msg("anything"))
    assert d.tier == "default" and d.model == "claude-fable-5" and d.effort == "max"


# --- run_turn actually uses the decision ---

def test_run_turn_routes_model_and_skips_verify_on_fast(session, monkeypatch):
    seen = {}

    class Fake:
        @staticmethod
        def run(s, sys_, msgs, *, model=None, effort=None):
            seen["model"], seen["effort"] = model, effort
            return "Your balance is $561.57."

    monkeypatch.setattr(agent_chat, "_backend", lambda name: Fake)
    monkeypatch.setattr(config, "LLM_BACKEND", "fake")
    verified = {"n": 0}
    monkeypatch.setattr(
        agent_chat.verify, "verified_turn",
        lambda *a, **k: (verified.__setitem__("n", 1) or ("x", {})),
    )
    agent_chat.run_turn(session, msg("[Ford] what's my balance?"), channel="web")
    assert seen["model"] == "claude-opus-5" and seen["effort"] == "low"
    assert verified["n"] == 0  # fast tier does not verify


def test_force_tier_overrides_the_classifier():
    # a trivial message forced to complex still gets Fable-max + verify
    d = router.for_tier("complex")
    assert d.tier == "complex" and d.model == "claude-fable-5" and d.verify is True
    assert router.for_tier("quick").model == "claude-sonnet-5"


def test_run_turn_honors_force_tier_for_the_report(session, monkeypatch):
    seen = {}

    class Fake:
        @staticmethod
        def run(s, sys_, msgs, *, model=None, effort=None):
            seen["model"], seen["effort"] = model, effort
            return "The week's synthesis."

    monkeypatch.setattr(agent_chat, "_backend", lambda name: Fake)
    monkeypatch.setattr(config, "LLM_BACKEND", "fake")
    monkeypatch.setattr(agent_chat.verify, "verified_turn", lambda *a, **k: ("x", {}))
    # even a one-word prompt runs Fable-max when the caller forces complex
    agent_chat.run_turn(session, msg("hi"), channel="web", force_tier="complex")
    assert seen["model"] == "claude-fable-5" and seen["effort"] == "max"


def test_run_turn_verifies_on_complex(session, monkeypatch):
    class Fake:
        @staticmethod
        def run(s, sys_, msgs, *, model=None, effort=None):
            return "You should refinance; you'd save $300/mo."

    monkeypatch.setattr(agent_chat, "_backend", lambda name: Fake)
    monkeypatch.setattr(config, "LLM_BACKEND", "fake")
    verified = {"n": 0}
    monkeypatch.setattr(
        agent_chat.verify, "verified_turn",
        lambda *a, **k: (verified.__setitem__("n", 1) or ("checked reply", {})),
    )
    out = agent_chat.run_turn(
        session, msg("[Ford] should we refinance? analyze it."), channel="web"
    )
    assert verified["n"] == 1 and out == "checked reply"