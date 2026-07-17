"""Option D: Realtime weave — create_response + consult_deep_brain tool."""
from __future__ import annotations

import os

from api import energy_agent as ea


def test_voice_weave_enabled_by_default(monkeypatch):
    monkeypatch.delenv("EA_VOICE_WEAVE", raising=False)
    assert ea._voice_weave_enabled() is True


def test_realtime_config_weave_has_tool_and_create_response(monkeypatch):
    monkeypatch.setenv("EA_VOICE_WEAVE", "1")
    cfg = ea._realtime_session_config()
    turn = cfg["audio"]["input"]["turn_detection"]
    assert turn["create_response"] is True
    assert turn["interrupt_response"] is True
    assert cfg["tools"][0]["name"] == "consult_deep_brain"
    assert "consult_deep_brain" in cfg["instructions"]
    assert cfg["tool_choice"] == "auto"


def test_realtime_config_legacy_mouth_only(monkeypatch):
    monkeypatch.setenv("EA_VOICE_WEAVE", "0")
    cfg = ea._realtime_session_config()
    turn = cfg["audio"]["input"]["turn_detection"]
    assert turn["create_response"] is False
    assert turn["interrupt_response"] is False
    assert "tools" not in cfg
    assert "MOUTH only" in cfg["instructions"] or "mouth" in cfg["instructions"].lower()
