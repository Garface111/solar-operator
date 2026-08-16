#!/usr/bin/env bash
# One-command BankAI launcher: sets up venv + deps + .env on first run, then serves.
#   ./start.sh            start the portal (http://localhost:8300)
#   ./start.sh --demo     seed realistic demo data first, then start
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "First run: creating virtualenv + installing dependencies..."
  python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo ">>> Created .env from .env.example."
  echo ">>> Edit it now: set APP_PASSWORD and one LLM backend"
  echo ">>>   (ANTHROPIC_API_KEY, or LLM_BACKEND=claude-cli if Claude Code is"
  echo ">>>   installed and logged in, or LLM_BACKEND=grok with XAI_API_KEY)."
  echo ">>> Then re-run ./start.sh"
  exit 1
fi

if [ "${1:-}" = "--demo" ]; then
  ./venv/bin/python scripts/seed_demo.py
fi

echo "BankAI portal -> http://localhost:8300"
exec ./venv/bin/python run.py
