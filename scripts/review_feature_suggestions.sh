#!/usr/bin/env bash
# Claude Code agent review of queued AO feature suggestions (cron or on-demand).
# SAFE: review-only — the agent runs in plan mode and never edits/deploys.
# No-ops cleanly when ADMIN_API_KEY can't be resolved.
set -uo pipefail
REPO="/root/solar-operator"
cd "$REPO" || { echo "review: repo missing"; exit 0; }
# Make gateway-installed railway/claude tools reachable in cron's bare env.
export PATH="/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
# Kill-switch.
if [ -f "$REPO/.fs_review_disabled" ]; then echo "review: disabled — skipping"; exit 0; fi
# Resolve ADMIN_API_KEY: prefer env, else pull from Railway (read-only).
if [ -z "${ADMIN_API_KEY:-}" ]; then
  export ADMIN_API_KEY="$(railway variables --service web --json 2>/dev/null | python3 -c 'import sys,json;
try:
    print(json.load(sys.stdin).get("ADMIN_API_KEY",""))
except Exception:
    print("")' 2>/dev/null)"
fi
python3 scripts/review_feature_suggestions.py
exit 0
