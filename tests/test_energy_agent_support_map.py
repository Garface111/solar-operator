"""Energy Agent support map — single source of product truth for product_map."""
from api.energy_agent import (
    _parse_support_map_md,
    _product_map_tool,
    load_product_map,
)


REQUIRED_TOPICS = {
    "tabs", "fleet", "capture", "system", "vendors", "analysis",
    "offtakers", "billing", "status", "security", "tools",
}


def test_support_map_file_loads_all_topics():
    pmap = load_product_map(force=True)
    missing = REQUIRED_TOPICS - set(pmap)
    assert not missing, f"support map missing topics: {missing}"
    # Tabs must teach the six owner-facing labels
    tabs = pmap["tabs"].lower()
    for label in ("fleet triage", "inverters", "analysis", "invoices", "resources", "account"):
        assert label in tabs, f"tabs section missing {label!r}"
    # Tab is "Account" (not "Master Account"); offtakers still use "Master account" as a form field
    assert "master account" not in tabs or "was" in tabs  # legacy note OK


def test_parse_support_map_md_sections():
    raw = "# Title\n\n## alpha\nHello\n\n## beta\nWorld\n"
    assert _parse_support_map_md(raw) == {"alpha": "Hello", "beta": "World"}


def test_product_map_tool_topic_and_all():
    one = _product_map_tool({"topic": "capture"})
    assert one["topic"] == "capture"
    assert "cloud" in one["map"].lower() or "store it with us" in one["map"].lower()
    assert one.get("source") == "energy_agent_support_map.md"

    # "all" is a directory of topics + entry sections (not a full dump).
    all_ = _product_map_tool({"topic": "all"})
    assert all_["topic"] == "directory"
    assert "topics" in all_
    assert "capture" in all_["topics"]
    assert "system" in all_["map"].lower() or "tabs" in all_["map"].lower()


def test_product_map_unknown_topic_lists_directory():
    out = _product_map_tool({"topic": "not_a_real_topic"})
    assert out["topic"] == "unknown"
    assert set(REQUIRED_TOPICS).issubset(set(out["topics"]))
