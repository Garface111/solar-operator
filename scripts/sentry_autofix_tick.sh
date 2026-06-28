#!/usr/bin/env bash
# Sentry auto-fix tick — run by cron. Chains: fetch new issues → orchestrate fixes.
# Prints a human summary to stdout (the cron delivers it to Ford). The orchestrator
# prints a short "no new issues" line when there's nothing to do.
#
# AUTONOMOUS mode is ARMED: the orchestrator fixes, tests, opens a PR, and — when
# every hard rail passes AND the diff carries a regression test — squash-merges it,
# which auto-deploys to prod. Each merge is still gated by ALL rails (FIXED verdict,
# no sensitive path [auth/billing/Stripe/migrations excluded], <=60 lines / <=3 files,
# FULL test suite GREEN, regression test present). Brakes: `touch .autofix_nomerge`
# reverts to PR-only; `touch .autofix_disabled` stops everything.
#
# Armed 2026-06-28 with Ford's explicit sign-off (he chose "Arm it" when asked whether
# to enable the unattended auto-merge loop). To revert without touching cron, drop the
# brake file above or comment the export below.
export SENTRY_AUTOFIX_AUTOMERGE=1
#
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
