#!/bin/bash
# Register the bankai finance MCP server with the Grok CLI, so grok-cli fallback
# turns get the same tools as claude-cli. Grok reads MCP servers from its own
# config (unlike Claude's inline --mcp-config), so this must run once on the box.
# Idempotent: `grok mcp add` updates an existing entry. Run as the user the
# bankai service runs as (root on FordBrain).
set -e
RUNTIME=/opt/bankai
PY="$RUNTIME/venv/bin/python"
DB=$(grep -E '^DATABASE_URL=' "$RUNTIME/.env" 2>/dev/null | cut -d= -f2-)
DB=${DB:-sqlite:////root/bankai-data/bankai.db}

grok mcp add bankai "$PY" \
  -e "DATABASE_URL=$DB" \
  -e "PYTHONPATH=$RUNTIME" \
  -- -m bankai.agent.mcp_server

echo "== registered MCP servers =="
grok mcp list
echo
echo "NOTE: the Grok CLI must be signed in for tools (and any turn) to work:"
echo "  grok login --device-code    # or set XAI_API_KEY"
