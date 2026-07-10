#!/usr/bin/env bash
# IMMEDIATE feature-suggestion trigger (Ford 2026-07-10: "the sublime version
# triggers immediately when someone submits"). Long-polls the backend's
# /admin/feature-suggestions/wait — which blocks until a NEW suggestion lands —
# then fires the review/judge/auto-ship pipeline within seconds. The 3-hourly
# cron stays as a backstop; this is the fast path. Single-instance (flock), and
# it never overlaps a build (the pipeline itself drains ALL 'new' rows).
set -uo pipefail
REPO="/root/solar-operator"
cd "$REPO" || exit 1
export PATH="/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

# One watcher only.
exec 9>/tmp/fs_watch.lock
if ! flock -n 9; then echo "fs_watch: already running"; exit 0; fi

BASE="${AO_API_BASE:-https://web-production-49c83.up.railway.app}"
if [ -z "${ADMIN_API_KEY:-}" ]; then
  export ADMIN_API_KEY="$(railway variables --service web --json 2>/dev/null | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("ADMIN_API_KEY",""))
except Exception: print("")' 2>/dev/null)"
fi
[ -z "${ADMIN_API_KEY:-}" ] && { echo "fs_watch: no ADMIN_API_KEY — exiting"; exit 0; }

echo "fs_watch: watching $BASE for new suggestions ($(date))"
while true; do
  [ -f "$REPO/.fs_review_disabled" ] && { sleep 60; continue; }
  # Blocks up to ~25s; returns the instant a 'new' suggestion exists.
  RESP="$(curl -s --max-time 35 "$BASE/admin/feature-suggestions/wait?timeout=25&key=$ADMIN_API_KEY" 2>/dev/null || true)"
  HIT="$(printf '%s' "$RESP" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("suggestion_id") or "")
except Exception: print("")' 2>/dev/null)"
  if [ -n "$HIT" ]; then
    echo "fs_watch: suggestion #$HIT landed — firing pipeline ($(date))"
    bash "$REPO/scripts/review_feature_suggestions.sh" >> /root/fs_review.log 2>&1 || true
  else
    # timeout / transient error → immediately re-arm; brief backoff on hard error
    printf '%s' "$RESP" | grep -q '"timeout"' || sleep 3
  fi
done
