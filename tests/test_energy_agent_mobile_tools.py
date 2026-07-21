"""Energy Agent mobile capability tools (offtaker create, vacancy, demand, Connect)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.energy_agent_mobile_tools import TOOL_DEFS_EXTRA, run_mobile_tool


def test_extra_tool_names():
    names = {t["function"]["name"] for t in TOOL_DEFS_EXTRA}
    assert "create_offtaker" in names
    assert "marketplace_vacancy" in names
    assert "create_exchange_demand" in names
    assert "payments_connect_status" in names
    assert "start_payments_connect" in names


def test_unknown_returns_none():
    assert run_mobile_tool("not_a_tool", {}, SimpleNamespace(id="t"), SimpleNamespace(id="s"), MagicMock()) is None


def test_create_offtaker_demo_blocked():
    tenant = SimpleNamespace(id="ten_demo", is_demo=True, product="array_operator", contact_email="d@x.com")
    out = run_mobile_tool(
        "create_offtaker",
        {"name": "Pat", "share_pct": 10},
        tenant,
        SimpleNamespace(id="ea"),
        MagicMock(),
        user_text="add Pat 10%",
    )
    assert out["error"] == "demo_blocked"


def test_create_demand_demo_blocked():
    tenant = SimpleNamespace(id="ten_demo", is_demo=True, product="array_operator")
    out = run_mobile_tool(
        "create_exchange_demand",
        {"contact_name": "Lee", "contact_email": "l@x.com"},
        tenant,
        SimpleNamespace(id="ea"),
        MagicMock(),
        user_text="add demand Lee",
    )
    assert out["error"] == "demo_blocked"
