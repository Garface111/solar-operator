"""EA Anthropic model id must not be a retired not_found model (Sentry PYTHON-FASTAPI-1H)."""
from __future__ import annotations

from api import energy_agent as ea


def test_default_anthropic_model_is_live(monkeypatch):
    monkeypatch.delenv("EA_ANTHROPIC_MODEL", raising=False)
    model = ea._anthropic_model()
    assert model == "claude-sonnet-4-5"
    assert model != "claude-sonnet-4-20250514"


def test_retired_anthropic_model_is_remapped(monkeypatch):
    monkeypatch.setenv("EA_ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    assert ea._anthropic_model() == "claude-sonnet-4-5"


def test_explicit_live_model_passthrough(monkeypatch):
    monkeypatch.setenv("EA_ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
    assert ea._anthropic_model() == "claude-sonnet-4-5-20250929"
