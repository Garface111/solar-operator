"""Regression: local `Array` import inside _run_tool must not shadow module import.

Sentry PYTHON-FASTAPI (energy-agent chat-stream): UnboundLocalError
  cannot access local variable 'Array' where it is not associated with a value

Cause: `from .models import Array, ...` inside the portal_links branch of
_run_tool made Array local for the entire function. Earlier branches
(list_offtakers, fleet_trends_summary) used module-level Array and crashed
before the local import ever ran.

Array is imported at module scope in energy_agent.py — local re-import is
forbidden in _run_tool.
"""
from __future__ import annotations

import ast
import secrets
from pathlib import Path

import pytest

from api.db import SessionLocal, init_db
from api.models import Tenant


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _run_tool_node() -> ast.FunctionDef:
    src = Path(__file__).resolve().parents[1] / "api" / "energy_agent.py"
    tree = ast.parse(src.read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_run_tool":
            return node
    raise AssertionError("_run_tool not found in api/energy_agent.py")


def test_run_tool_does_not_locally_import_array():
    """AST guard: re-importing Array inside _run_tool reintroduces the Sentry bug."""
    fn = _run_tool_node()
    nested: list[ast.AST] = [
        c
        for c in ast.walk(fn)
        if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and c is not fn
    ]

    def _in_nested(n: ast.AST) -> bool:
        for nest in nested:
            for child in ast.walk(nest):
                if child is n:
                    return True
        return False

    local_array_imports: list[int] = []
    for child in ast.walk(fn):
        if child is fn or _in_nested(child):
            continue
        if isinstance(child, ast.ImportFrom):
            for alias in child.names:
                if alias.name == "Array" or alias.asname == "Array":
                    local_array_imports.append(child.lineno)

    assert local_array_imports == [], (
        f"_run_tool must not import Array locally (lines {local_array_imports}); "
        "Array is module-level — a local import makes it UnboundLocalError on every "
        "earlier use (list_offtakers, fleet_trends_summary, etc.)."
    )


def test_list_offtakers_uses_array_without_unbound_local():
    """Runtime path: list_offtakers queries Array before portal_links branch."""
    from api.energy_agent import _run_tool

    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(
            Tenant(
                id=tid,
                name="Shadow Owner",
                contact_email=f"{key}@owner.test",
                tenant_key=key,
                plan="comped",
                active=True,
                product="array_operator",
            )
        )
        db.commit()
        t = db.get(Tenant, tid)

        class _S:
            id = "sess_test"
            tenant_id = tid

        # Would raise UnboundLocalError if Array were local to _run_tool.
        out = _run_tool("list_offtakers", {}, t, _S(), db, user_text="list offtakers")
        assert isinstance(out, dict)
        assert "offtakers" in out
        assert isinstance(out["offtakers"], list)
