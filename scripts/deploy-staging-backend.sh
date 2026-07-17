#!/usr/bin/env bash
# Deploy the Array Operator / Solar Operator STAGING backend — the Railway
# `web-staging` service in the `staging` environment — from the committed
# `staging` branch. This is the backend half of the dev -> preprod -> prod
# pipeline (the frontend half lives in array-operator/scripts/deploy-preprod.sh).
#
# web-staging runs the SAME code as prod but is inert toward the outside world
# BY CONSTRUCTION:
#   * staging has NO worker and NO cloud-capture-harvester service, so the
#     scheduler, the Sovereign, and headless bill-capture simply never run;
#   * RUN_SCHEDULER=0 and SOVEREIGN_ENABLED=0 belt-and-suspenders;
#   * RESEND_API_KEY / STRIPE_SECRET_KEY are unset (email + billing are no-ops),
#     and EMAIL_SINK_TO redirects any stray mail to the operator;
#   * it has its OWN empty Postgres and its own session/admin/config secrets.
#
# Deploys are explicit (this script), not GitHub auto-deploy — deterministic,
# no surprise redeploys. It uploads the committed `staging` tree via `railway up`.
#
#   Usage:  scripts/deploy-staging-backend.sh
#   Env:    STAGING_BRANCH (default: staging)
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
PROJECT="7451f2d4-6d29-41de-b8f4-a7461052a578"
SERVICE="web-staging"
ENVN="staging"
HEALTH="https://web-staging-staging-3671.up.railway.app/health"
BRANCH="${STAGING_BRANCH:-staging}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
git fetch origin -q
SHA="$(git rev-parse --short "origin/$BRANCH")"
echo "[staging-backend] deploying $BRANCH @ $SHA -> $SERVICE ($ENVN)"

TMP="$(mktemp -d)"
trap 'git worktree remove --force "$TMP" 2>/dev/null || true; rm -rf "$TMP"' EXIT
git worktree add -q "$TMP" "origin/$BRANCH"
cd "$TMP"
railway link -p "$PROJECT" -e "$ENVN" -s "$SERVICE" >/dev/null 2>&1 || true
railway up --detach --service "$SERVICE" --environment "$ENVN"

echo "[staging-backend] triggered. Poll health (~2-4 min for build+migrate+boot):"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' $HEALTH   # expect 200"
