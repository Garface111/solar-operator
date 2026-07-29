"""Minimal MCP stdio server exposing the finance tools to the Claude Code CLI.

Newline-delimited JSON-RPC 2.0 over stdin/stdout. Spawned by the CLI itself (see
backends/claude_cli.py); opens its own database connection, so it stays read-only in
exactly the same way as the in-process tools.
"""
from __future__ import annotations

import json
import sys

from sqlalchemy.orm import Session

from ..db import init_db, session_scope
from .tools import TOOLS, execute_tool

SERVER_INFO = {"name": "bankai", "version": "1.0.0"}


def handle_request(request: dict, session: Session | None = None) -> dict | None:
    """Return the JSON-RPC response dict for one request (None for notifications)."""
    method = request.get("method")
    rid = request.get("id")
    if method == "initialize":
        client_version = (request.get("params") or {}).get("protocolVersion", "2025-06-18")
        return _result(rid, {
            "protocolVersion": client_version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return _result(rid, {
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["input_schema"],
                }
                for t in TOOLS
            ]
        })
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if session is not None:
            text = execute_tool(session, name, args)
        else:
            with session_scope() as s:
                text = execute_tool(s, name, args)
        return _result(rid, {"content": [{"type": "text", "text": text}]})
    if rid is not None:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None


def _result(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> None:
    init_db()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
