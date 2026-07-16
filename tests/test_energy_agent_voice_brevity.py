"""Energy Agent spoken replies stay short so Realtime voice doesn't cut off."""
from __future__ import annotations

from api.energy_agent import _is_voice_turn, _mouth_line


def test_voice_turn_detection():
    assert _is_voice_turn("voice", None) is True
    assert _is_voice_turn("text", {"voice_source": True}) is True
    assert _is_voice_turn("text", {"channel": "voice"}) is True
    assert _is_voice_turn("text", {}) is False
    assert _is_voice_turn(None, None) is False


def test_mouth_line_voice_caps_words():
    long = (
        "Town of Glover's solar credit rate is about $0.18 per kWh. "
        "That comes from the offtaker's net rate, not the Resources tab. "
        "If you want, I can also walk through every offtaker and every "
        "utility bill binding and then explain the full invoice pipeline "
        "including auto-send and Stripe Connect step by step in detail."
    )
    spoken = _mouth_line(long, voice=True)
    assert "Glover" in spoken
    assert len(spoken.split()) <= 50
    # Should not dump the entire monologue
    assert "Stripe Connect" not in spoken


def test_mouth_line_strips_markdown_noise():
    raw = "**Londonderry** is healthy. See [docs](https://example.com/x) for more."
    spoken = _mouth_line(raw, voice=True)
    assert "**" not in spoken
    assert "http" not in spoken
    assert "Londonderry" in spoken
