#!/usr/bin/env bash
# Sentry auto-fix tick — run by cron. Chains: fetch new issues → orchestrate fixes.
# Prints a human summary to stdout (the cron delivers it to Ford). Stays SILENT-ish
# when there's nothing to do (the orchestrator prints a short "no new issues" line).
#
# SAFE mode: the orchestrator only ever opens PRs — it never merges or deploys.
# No-ops cleanly (exit 0) when the Sentry token isn't configured yet.
set -uo pipefail

REPO="/root/solar-operator"
cd "$REPO" || { echo "autofix: repo missing"; exit 0; }

# Make the gateway-installed Railway/gh/claude tools reachable in cron's bare env.
export PATH="/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

# Kill-switch: create this file to pause the system without touching cron.
if [ -f "$REPO/.autofix_disabled" ]; then
  echo "autofix: disabled (.autofix_disabled present) — skipping"
  exit 0
fi

# 1) Fetch new issues (marks them seen so they aren't re-filed). No-ops without token.
ISSUES="$(python3 scripts/sentry_fetch.py --mark --limit 10 2>/dev/null)"

# 2) Hand them to the orchestrator (reads issues on stdin).
printf '%s' "$ISSUES" | python3 scripts/sentry_autofix.py
exit 0
