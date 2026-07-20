#!/usr/bin/env bash
# Sentry auto-fix tick — run by cron. Chains: fetch new issues → orchestrate fixes.
# Prints a human summary to stdout (the cron delivers it to Ford). The orchestrator
# prints a short "no new issues" line when there's nothing to do.
#
# HUMAN-GATED mode (Ford, 2026-07-19): the orchestrator fixes, tests, and OPENS A PR
# — then stops. Nothing merges and nothing deploys without a human clicking merge.
# Detection and fixing stay fully autonomous; only the ship step is gated.
#
# WHY the change: the 2026-07-19 unattended sweep squash-merged 26 PRs to main in one
# night (each merge auto-deploys). Prod survived and the diffs sampled well, but the
# gate is thinner than it reads — the merge rail claims "FULL test suite GREEN" while
# the repo carries ~23 KNOWN-RED tests, so the backlog runner had to fall back to
# "the agent's own regression test passes". That is not a suite-green guarantee, and
# `gh pr merge --admin` bypasses review entirely. One reviewer in the morning is
# cheap; an unreviewed bad merge that auto-deploys is not (see the Sovereign outage:
# autonomous merges → constant Railway cutovers → prod down).
#
# TWO independent brakes, both engaged — either alone forces PR-only:
#   1. this export = 0
#   2. the `.autofix_nomerge` file in the repo root (wins over any env)
# To re-arm autonomous merging you must BOTH set this to 1 AND `rm .autofix_nomerge`.
# `touch .autofix_disabled` still stops everything, including detection.
export SENTRY_AUTOFIX_AUTOMERGE=0
#
# No-ops cleanly (exit 0) when the Sentry token isn't configured yet.
set -uo pipefail

REPO="/root/solar-operator"
cd "$REPO" || { echo "autofix: repo missing"; exit 0; }

# Make the gateway-installed Railway/gh + Grok Build CLI reachable in cron's bare env.
export PATH="/root/.grok/bin:/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

# Kill-switch: create this file to pause the system without touching cron.
if [ -f "$REPO/.autofix_disabled" ]; then
  echo "autofix: disabled (.autofix_disabled present) — skipping"
  exit 0
fi

# 1) Fetch new issues (marks them seen so they aren't re-filed). No-ops without token.
#    14d window catches issues that re-fire after a partial fix.
ISSUES="$(python3 scripts/sentry_fetch.py --mark --limit 15 --period 14d 2>/dev/null)"

# 2) Hand them to the orchestrator (reads issues on stdin). Sequential, Grok Build.
export SENTRY_AUTOFIX_MAX="${SENTRY_AUTOFIX_MAX:-5}"
printf '%s' "$ISSUES" | python3 scripts/sentry_autofix.py
exit 0
