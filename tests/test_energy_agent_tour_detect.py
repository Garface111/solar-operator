"""Preset tour id detection — never freehand walkthroughs for named tabs."""
from api.energy_agent import _detect_tour_id


def test_invoices_phrases():
    for t in (
        "walk me through invoices",
        "show me around the invoices tab",
        "how does invoices work",
        "explain the offtaker page",
        "give me a tour of invoices",
    ):
        assert _detect_tour_id(t) == "reports", t


def test_other_tabs():
    assert _detect_tour_id("walk me through account") == "master_account"
    assert _detect_tour_id("tour fleet triage") == "dashboard"
    assert _detect_tour_id("show me the inverters tab") == "arrays"
    assert _detect_tour_id("walk me through analysis") == "analysis"
    assert _detect_tour_id("resources walkthrough") == "resources"


def test_not_a_tour():
    assert _detect_tour_id("what are the tabs") is None
    assert _detect_tour_id("set the share to 15%") is None
