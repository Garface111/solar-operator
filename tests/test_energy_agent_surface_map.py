"""Surface mental model loads into product_map for page-level orientation."""
from api.energy_agent import load_product_map, _product_map_tool


def test_surface_topics_present():
    m = load_product_map(force=True)
    for key in (
        "surface",
        "product_spine",
        "surface_invoices",
        "surface_inverters",
        "surface_fleet_triage",
        "surface_analysis",
        "surface_account",
        "surface_resources",
        "orientation_playbook",
    ):
        assert key in m, key
        assert len(m[key]) > 100, key


def test_surface_tool_returns_macro():
    out = _product_map_tool({"topic": "surface_invoices"})
    assert out["topic"] == "surface_invoices"
    assert "MACRO" in out["map"]
    assert "MESO" in out["map"]
    assert "offtaker" in out["map"].lower()
    assert "surface" in out["source"]
