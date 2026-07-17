"""Option B: brain authors its own [SPOKEN] line — parser + fallback.

The one Claude brain produces dual output on voice-active turns:
panel text above the marker, spoken line after. No Haiku middleman.
"""
from __future__ import annotations

from api.energy_agent import _make_spoken, _split_spoken


def test_split_spoken_extracts_marker_and_keeps_panel():
    panel, spoken = _split_spoken(
        "Tannery Brook has three inverters flagged today.\n\n"
        "[SPOKEN] Tannery has three flagged — one dead, two lagging. Want the rundown?"
    )
    assert panel.startswith("Tannery Brook has three")
    assert "[SPOKEN]" not in panel
    assert spoken is not None
    assert spoken.startswith("Tannery has three flagged")
    assert "[SPOKEN]" not in spoken
    assert "rundown" in spoken


def test_split_spoken_no_marker_returns_none_spoken():
    text = "Only a panel answer. Nothing for the mouth marker."
    panel, spoken = _split_spoken(text)
    assert panel == text
    assert spoken is None
    # Fallback is the brain's own lead — still no Haiku.
    assert "panel answer" in _make_spoken(panel, voice_active=True).lower()


def test_split_spoken_last_marker_wins():
    panel, spoken = _split_spoken(
        "Intro\n[SPOKEN] first attempt\nmore panel\n[SPOKEN] last spoken wins"
    )
    assert spoken == "last spoken wins"
    assert "[SPOKEN]" not in panel
    assert "Intro" in panel


def test_split_spoken_marker_only_shows_and_speaks_same():
    panel, spoken = _split_spoken("[SPOKEN] Just this one short line.")
    assert spoken is not None
    assert "short line" in spoken
    assert panel == spoken  # no separate panel text → show the spoken line


def test_split_spoken_tolerant_of_spacing_and_case():
    panel, spoken = _split_spoken("Full answer.\n[ spoken ]:  Hello there.")
    assert panel == "Full answer."
    assert spoken == "Hello there."


def test_split_spoken_strips_markdown_from_spoken():
    panel, spoken = _split_spoken(
        "**Bold panel** answer.\n\n[SPOKEN] **Londonderry** is healthy — see the card."
    )
    assert "Bold panel" in panel or "**Bold" in panel  # panel may keep md until tidy
    assert spoken is not None
    assert "**" not in spoken
    assert "Londonderry" in spoken
