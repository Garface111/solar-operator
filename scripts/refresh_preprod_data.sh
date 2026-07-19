#!/usr/bin/env bash
# Refresh PREPROD (staging) with a fresh copy of Ford's real prod tenant.
#
# Preprod deliberately does NOT pull live vendor/utility data itself: SMA rotates
# OAuth refresh tokens (a staging refresh would invalidate PROD's token), GMP/
# SmartHub sessions are cookie-bound + dedup'd (two environments fight and can
# lock the real utility accounts), and SolarEdge API keys are quota'd. So prod
# stays the only capturer, and preprod MIRRORS it. That gives real, minutes-old
# fleet/bill/inverter data with zero risk to production capture.
#
# Read-only on prod. Idempotent: wipes the tenant subtree in staging, reloads it.
#
# Usage:  scripts/refresh_preprod_data.sh [tenant_id]
# Pause:  touch ~/.preprod_refresh_pause     (skips runs — use while mid-test,
#         since each refresh WIPES preprod edits to this tenant)
# Resume: rm ~/.preprod_refresh_pause
set -euo pipefail

TID="${1:-ten_ford_demo_100}"
PAUSE="$HOME/.preprod_refresh_pause"
cd "$HOME/solar-operator"
umask 077

ts() { date "+%Y-%m-%d %H:%M:%S"; }

if [ -f "$PAUSE" ]; then
  echo "$(ts) [refresh] PAUSED ($PAUSE exists) — skipping"
  exit 0
fi

echo "$(ts) [refresh] fetching DB endpoints from Railway"
railway variables --service Postgres --environment production --kv \
  | grep '^DATABASE_PUBLIC_URL=' | cut -d= -f2- > /tmp/prod_db_url
railway variables --service Postgres-YSP8 --environment staging --kv \
  | grep '^DATABASE_PUBLIC_URL=' | cut -d= -f2- > /tmp/stg_db_url

if [ ! -s /tmp/prod_db_url ] || [ ! -s /tmp/stg_db_url ]; then
  echo "$(ts) [refresh] ERROR: could not resolve both DATABASE_PUBLIC_URLs (railway auth?)" >&2
  exit 1
fi

echo "$(ts) [refresh] copying $TID prod -> staging"
.venv/bin/python scripts/copy_tenant_to_staging.py "$TID"
echo "$(ts) [refresh] done"
