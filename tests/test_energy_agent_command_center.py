"""Command Center (fleet) mode — read-only sandbox for the Energy Agent.

When a turn arrives with context.mode == "command_center" the agent is fenced to
the fleet read allowlist: the LLM is only offered those tools, and the _run_tool
dispatch gate refuses anything else (including meta/skill tools) so nothing can
self-escalate into a write. These tests pin that boundary.
"""
from api.energy_agent import (
    COMMAND_CENTER_TOOLS,
    TOOL_DEFS,
    _run_tool,
    _tool_defs_for,
)


def _tool_names(defs) -> set[str]:
    return {(t.get("function") or {}).get("name") for t in defs}


class _StubTenant:
    """Enough of a Tenant for the dispatch gate, which returns before any DB use."""
    id = "ten_cc_test_0001"
    is_demo = False


def test_allowlist_tools_all_exist_in_registry():
    # No typos / dead entries — every allowlisted name is a real, registered tool.
    registered = _tool_names(TOOL_DEFS)
    missing = COMMAND_CENTER_TOOLS - registered
    assert not missing, f"allowlist references unknown tools: {missing}"


def test_allowlist_excludes_writes_and_meta():
    # The sandbox must never quietly include a write, a sender, or a self-grant.
    banned = {
        "enable_skill", "list_skills", "request_capability",  # self-escalation
        "set_email_copy", "schedule_email_copy",              # writes
        "patch_gen_client", "patch_gen_array",                # writes
        "upsert_service_contact",                             # writes
    }
    leaked = COMMAND_CENTER_TOOLS & banned
    assert not leaked, f"command center allowlist leaks non-read tools: {leaked}"


def test_tool_defs_for_command_center_narrows_registry():
    fleet = _tool_defs_for("command_center")
    names = _tool_names(fleet)
    # Exactly the allowlist, nothing more.
    assert names == set(COMMAND_CENTER_TOOLS)
    # A representative write tool is absent from what the model can even see.
    assert "set_email_copy" not in names
    # Core fleet reads are present.
    assert {"fleet_overview", "array_detail", "fleet_financial_health"} <= names


def test_tool_defs_for_default_is_full_registry():
    assert _tool_defs_for(None) is TOOL_DEFS
    assert _tool_defs_for("main") is TOOL_DEFS


def test_dispatch_gate_refuses_write_tool_in_command_center():
    # A write tool must be refused BEFORE any execution, without touching the DB
    # (db=None proves the gate returns first).
    out = _run_tool(
        "set_email_copy", {"body": "x"}, _StubTenant(), None, None,
        mode="command_center",
    )
    assert out.get("status") == "tool_unavailable"
    assert out.get("mode") == "command_center"


def test_dispatch_gate_refuses_meta_self_escalation():
    # The classic escape: talk the model into enable_skill to unlock writes.
    out = _run_tool(
        "enable_skill", {"skill": "fleet_money"}, _StubTenant(), None, None,
        mode="command_center",
    )
    assert out.get("status") == "tool_unavailable"


def test_dispatch_gate_allows_read_tool_in_command_center():
    # product_map is on the allowlist and is DB-free, so it runs end-to-end
    # through the gate and returns a real result — not a refusal.
    out = _run_tool(
        "product_map", {"topic": "tools"}, _StubTenant(), None, None,
        mode="command_center",
    )
    assert out.get("status") != "tool_unavailable"
    assert out.get("source") == "energy_agent_support_map.md"


def test_no_mode_does_not_trip_command_center_gate():
    # Outside command center, the gate is inert — product_map still runs.
    out = _run_tool("product_map", {"topic": "tools"}, _StubTenant(), None, None)
    assert out.get("status") != "tool_unavailable"
    assert out.get("source") == "energy_agent_support_map.md"
