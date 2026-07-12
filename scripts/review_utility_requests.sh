#!/usr/bin/env bash
# Agent that researches operator utility-add requests and wires the easy ones in.
# SAFE by default: UR_AUTOADD unset => research + draft + email only, no repo write.
set -uo pipefail
REPO="/root/solar-operator"
cd "$REPO" || { echo "ur-review: repo missing"; exit 0; }
export PATH="/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1
if [ -f "$REPO/.ur_review_disabled" ]; then echo "ur-review: disabled — skipping"; exit 0; fi
# Single-flight: never overlap (a research pass can take minutes; auto-add writes the repo).
exec 9>/tmp/ur_review.lock
flock -n 9 || { echo "ur-review: another run holds the lock — skipping"; exit 0; }
if [ -z "${ADMIN_API_KEY:-}" ]; then
  export ADMIN_API_KEY="$(railway variables --service web --json 2>/dev/null | python3 -c 'import sys,json;
try:
    print(json.load(sys.stdin).get("ADMIN_API_KEY",""))
except Exception:
    print("")' 2>/dev/null)"
fi
python3 scripts/review_utility_requests.py
exit 0
