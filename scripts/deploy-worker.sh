#!/usr/bin/env bash
# Deploy the Railway `worker` service (the ONLY scheduler host) to origin/main.
#
# WHY THIS EXISTS (2026-07-19): `worker` was observed NOT auto-deploying on push
# while `web` and `cloud-capture-harvester` — same repo, same branch `main`, empty
# Watch Paths — deployed fine. Every scheduled job lives ONLY on `worker`
# (api/scheduler.py, api/jobs/*: generation-report sends, morning digest,
# pre-send reviews, delivery receipts, co-op session warnings, alert sweeps), so
# a scheduler change can merge, "deploy", and silently never run. It bit twice in
# one evening — including a fix FOR the scheduler that never reached it.
#
# Use this whenever `verify` below says the worker is behind. Safe to re-run:
# the worker builds identically to web (railway.toml -> `sh start.sh`) and its
# role is env-driven (PROCESS_ROLE=worker), so a CLI upload preserves service vars.
#
#   scripts/deploy-worker.sh verify   # is the worker behind origin/main?
#   scripts/deploy-worker.sh deploy   # upload origin/main to the worker
#
set -euo pipefail

PROJECT="7451f2d4-6d29-41de-b8f4-a7461052a578"   # Solar-Operator
ENVIRONMENT="production"
SERVICE="worker"
REPO_DIR="${REPO_DIR:-$HOME/solar-operator}"
WT="${WT:-/tmp/so-worker-deploy}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 2; }; }
need git; need railway

fresh_worktree() {
  git -C "$REPO_DIR" fetch origin --quiet
  rm -rf "$WT"
  git -C "$REPO_DIR" worktree prune
  git -C "$REPO_DIR" worktree add --detach "$WT" origin/main >/dev/null
  git -C "$WT" rev-parse --short HEAD
}

case "${1:-verify}" in
  verify)
    git -C "$REPO_DIR" fetch origin --quiet
    echo "origin/main : $(git -C "$REPO_DIR" rev-parse --short origin/main)"
    echo "--- worker deployments (newest first) ---"
    railway deployment list --service "$SERVICE" --environment "$ENVIRONMENT" | head -4
    echo
    echo "A deployment must be NEWER than your push. If it is not, run:"
    echo "    scripts/deploy-worker.sh deploy"
    ;;
  deploy)
    sha="$(fresh_worktree)"
    echo "deploying origin/main ($sha) -> $SERVICE"
    cd "$WT"
    railway link --project "$PROJECT" --environment "$ENVIRONMENT" --service "$SERVICE" >/dev/null
    railway up --service "$SERVICE" --environment "$ENVIRONMENT" --detach
    echo
    echo "Build started. Confirm it lands, then verify the code is really running:"
    echo "    railway deployment list --service $SERVICE --environment $ENVIRONMENT | head -2"
    echo "    railway ssh --service $SERVICE --environment $ENVIRONMENT \\"
    echo "        \"python -c 'import os;print(os.getenv(\\\"PROCESS_ROLE\\\"), os.getenv(\\\"RUN_SCHEDULER\\\"))'\""
    echo "Expect: worker 1"
    ;;
  *)
    echo "usage: scripts/deploy-worker.sh [verify|deploy]" >&2
    exit 2
    ;;
esac
