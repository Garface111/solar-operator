"""Surface mental models load into product_map for page-level orientation."""
from api.energy_agent import (
    load_product_map,
    _product_map_tool,
    _access_surface_block,
)


def test_surface_topics_present():
    m = load_product_map(force=True)
    for key in (
        "surface",
        "product_spine",
        "surface_invoices",
        "surface_inverters",
        "surface_fleet_triage",
        "surface_fleet",
        "surface_marketplace",
        "surface_analysis",
        "surface_account",
        "surface_resources",
        "orientation_playbook",
    ):
        assert key in m, key
        assert len(m[key]) > 100, key


def test_mobile_surface_topics_present():
    m = load_product_map(force=True)
    for key in (
        "surface_mobile",
        "product_spine_mobile",
        "surface_mobile_fleet",
        "surface_mobile_agent",
        "surface_mobile_marketplace",
        "orientation_playbook_mobile",
    ):
        assert key in m, key
        assert len(m[key]) > 80, key


def test_desktop_spine_has_marketplace_and_fleet_not_inverters_top_tab():
    m = load_product_map(force=True)
    spine = m["product_spine"].lower()
    assert "marketplace" in spine
    assert "fleet" in spine
    assert "repairs" in spine
    # Current shell: Fleet owns Sandbox; Marketplace is a top tab
    assert "sandbox" in spine
    assert "no separate top tab named **inverters**" in spine or "inverters** or **fleet triage**" in spine


def test_surface_tool_returns_macro():
    out = _product_map_tool({"topic": "surface_invoices"})
    assert out["topic"] == "surface_invoices"
    assert "MACRO" in out["map"]
    assert "MESO" in out["map"]
    assert "offtaker" in out["map"].lower()
    assert "surface" in out["source"]


def test_mobile_surface_tool_source():
    out = _product_map_tool({"topic": "surface_mobile"})
    assert out["topic"] == "surface_mobile"
    assert "mobile" in out["source"]
    assert len(out["map"]) > 100


def test_access_surface_owner_web():
    block = _access_surface_block(
        {"client": "owner-web", "surface": "agent_sheet_mobile", "mobile": True}
    )
    assert "MOBILE WEB" in block
    assert "surface_mobile" in block
    assert "owner-web" in block


def test_access_surface_owner_native():
    block = _access_surface_block({"client": "owner-native", "surface": "rn_agent"})
    assert "REACT NATIVE" in block
    assert "surface_mobile" in block


def test_access_surface_desktop():
    block = _access_surface_block({"client": "desktop", "hash": "#marketplace"})
    assert "DESKTOP" in block
    assert "Marketplace" in block or "marketplace" in block.lower()
