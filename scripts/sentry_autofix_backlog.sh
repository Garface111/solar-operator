#!/usr/bin/env bash
# Full-backlog Sentry auto-fix — process EVERY unresolved issue sequentially
# with the Grok Build agent. One issue at a time so a failure never blocks the rest.
#
# Usage:
#   bash scripts/sentry_autofix_backlog.sh            # full sweep
#   bash scripts/sentry_autofix_backlog.sh --dry-run  # list only
#
# Logs: /tmp/sentry-autofix-backlog.log  +  /tmp/sentry-autofix-backlog-results.jsonl
set -uo pipefail
# deliberately no `set -e` — a single issue failure must not abort the sweep

REPO="/root/solar-operator"
cd "$REPO" || { echo "autofix: repo missing"; exit 1; }

export PATH="/root/.grok/bin:/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
# Default PR-ONLY (Ford 2026-07-19). Pass SENTRY_AUTOFIX_AUTOMERGE=1 explicitly
# AND remove .autofix_nomerge to run an unattended merging sweep.
export SENTRY_AUTOFIX_AUTOMERGE="${SENTRY_AUTOFIX_AUTOMERGE:-0}"
export SENTRY_AUTOFIX_MAX="${SENTRY_AUTOFIX_MAX:-1}"
# Generous caps for backlog autopilot — still bounded, but large enough for a
# real fix + regression test without thrashing on blocked-toolarge.
export SENTRY_AUTOFIX_MAX_LINES="${SENTRY_AUTOFIX_MAX_LINES:-250}"
export SENTRY_AUTOFIX_MAX_FILES="${SENTRY_AUTOFIX_MAX_FILES:-6}"
export SENTRY_AUTOFIX_TIMEOUT="${SENTRY_AUTOFIX_TIMEOUT:-900}"

LOG="${SENTRY_AUTOFIX_LOG:-/tmp/sentry-autofix-backlog.log}"
RESULTS="${SENTRY_AUTOFIX_RESULTS:-/tmp/sentry-autofix-backlog-results.jsonl}"
LOCK="${SENTRY_AUTOFIX_LOCK:-/tmp/sentry-autofix-backlog.lock}"
INPUT="/tmp/sentry-autofix-backlog-input.json"
STATE_FILE="/root/.hermes/secrets/sentry_processed.json"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

if [ -f "$REPO/.autofix_disabled" ]; then
  echo "autofix: disabled (.autofix_disabled present) — aborting"
  exit 0
fi

if [ -f "$LOCK" ]; then
  oldpid=$(cat "$LOCK" 2>/dev/null || true)
  if [ -n "${oldpid:-}" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "autofix: another backlog run is live (pid $oldpid) — aborting"
    exit 0
  fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

{
  echo "============================================================"
  echo "Sentry backlog autofix START $(date -Is)"
  echo "  agent=Grok Build  automerge=$SENTRY_AUTOFIX_AUTOMERGE  dry=$DRY"
  echo "============================================================"
} | tee -a "$LOG"

# Sentry only accepts statsPeriod of '', '24h', or '14d'. Empty = full unresolved set.
python3 scripts/sentry_fetch.py --all --limit 100 --period "" > "$INPUT" 2>>"$LOG"
COUNT=$(python3 -c "import json; d=json.load(open('$INPUT')); print(len(d.get('new_issues') or []))")
echo "Backlog size: $COUNT unresolved issues" | tee -a "$LOG"

if [ "$COUNT" = "0" ] || [ -z "$COUNT" ]; then
  echo "Nothing to fix. ✅" | tee -a "$LOG"
  exit 0
fi

if [ "$DRY" = "1" ]; then
  python3 -c "
import json
d=json.load(open('$INPUT'))
for i,it in enumerate(d.get('new_issues') or [],1):
    print(f\"{i:3}. {it.get('short_id')}: {str(it.get('title'))[:100]}\")
"
  exit 0
fi

: > "$RESULTS"
# Prioritize: real code bugs first, infra/noise last — still processes ALL.
python3 -c "
import json, re
d=json.load(open('$INPUT'))
issues=list(d.get('new_issues') or [])

def score(it):
    t = (it.get('title') or '') + ' ' + (it.get('culprit') or '')
    tl = t.lower()
    # Highest value: clear application bugs
    if re.search(r'NameError|AttributeError|UnboundLocalError|TypeError|KeyError|IntegrityError|UniqueViolation|UndefinedColumn|is not defined', t):
        return 0
    if 'client-error' in tl or '[arrayoperator]' in tl:
        return 2  # frontend — may need array-operator repo; still try
    if 'LockNotAvailable' in t or 'DeadlockDetected' in t or 'QueuePool' in t or 'PendingRollback' in t:
        return 3
    if 'Vault decrypt is disabled' in t or 'SOVEREIGN_ENABLED' in t or 'not configured' in tl:
        return 4  # intentional config / expected
    if 'ClientDisconnect' in t or 'ResizeObserver' in t:
        return 5  # noise
    return 1

issues.sort(key=lambda it: (score(it), -(int(it.get('count') or 0))))
for it in issues:
    print(json.dumps(it, separators=(',',':')))
" > /tmp/sentry-autofix-issues.ndjson

i=0
while IFS= read -r issue_json; do
  i=$((i+1))
  short=$(printf '%s' "$issue_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('short_id') or '?')")
  title=$(printf '%s' "$issue_json" | python3 -c "import sys,json; print(str(json.load(sys.stdin).get('title') or '')[:90])")
  iid=$(printf '%s' "$issue_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id') or '')")
  {
    echo ""
    echo "---------- [$i/$COUNT] $short ----------"
    echo "$title"
  } | tee -a "$LOG"

  # Build single-issue payload via temp file (avoids shell quoting hell)
  printf '%s' "$issue_json" > /tmp/sentry-autofix-one-issue.json
  python3 -c "
import json
it=json.load(open('/tmp/sentry-autofix-one-issue.json'))
json.dump({'ok': True, 'new_issues': [it]}, open('/tmp/sentry-autofix-one-payload.json','w'))
"

  start=$(date +%s)
  timeout $((SENTRY_AUTOFIX_TIMEOUT + 300)) \
    python3 scripts/sentry_autofix.py \
    < /tmp/sentry-autofix-one-payload.json \
    > /tmp/sentry-autofix-last-out.txt 2>&1
  rc=$?
  cat /tmp/sentry-autofix-last-out.txt >> "$LOG"
  elapsed=$(( $(date +%s) - start ))

  if [ $rc -ne 0 ]; then
    outcome="orchestrator-fail"
    echo "  !! orchestrator exit=$rc on $short (${elapsed}s)" | tee -a "$LOG"
  else
    outcome=$(grep -E '→ (MERGED|PR-opened|skipped|blocked|tests-failed|merge-failed)' /tmp/sentry-autofix-last-out.txt \
      | tail -1 | sed 's/.*→ //' | cut -d: -f1 | tr -d ' ' || true)
    outcome=${outcome:-unknown}
  fi

  # Mark seen so hourly cron does not re-queue forever
  ISSUE_ID="$iid" python3 -c "
import json, os
from pathlib import Path
iid = os.environ.get('ISSUE_ID','')
sf = Path('$STATE_FILE')
try:
    d = json.loads(sf.read_text()) if sf.exists() else {'seen': []}
except Exception:
    d = {'seen': []}
seen = list(d.get('seen') or [])
if iid and iid not in seen:
    seen.append(iid)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({'seen': seen[-2000:]}))
"

  SHORT="$short" OUTCOME="$outcome" ELAPSED="$elapsed" RC="$rc" python3 -c "
import json, os, datetime
print(json.dumps({
  'short_id': os.environ['SHORT'],
  'outcome': os.environ['OUTCOME'],
  'elapsed_s': int(os.environ['ELAPSED']),
  'rc': int(os.environ['RC']),
  'ts': datetime.datetime.now().isoformat(timespec='seconds'),
}))
" >> "$RESULTS"

  echo "  done in ${elapsed}s → $outcome" | tee -a "$LOG"
done < /tmp/sentry-autofix-issues.ndjson

{
  echo ""
  echo "============================================================"
  echo "Sentry backlog autofix END $(date -Is)"
  echo "Results: $RESULTS"
  python3 - <<'SUM'
import json
from collections import Counter
from pathlib import Path
p = Path("/tmp/sentry-autofix-backlog-results.jsonl")
rows = []
if p.exists():
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
print(f"  attempted: {len(rows)}")
for k, v in Counter(r.get("outcome", "?") for r in rows).most_common():
    print(f"  {k}: {v}")
for r in rows:
    if r.get("outcome") in ("MERGED", "PR-opened"):
        print(f"  ✅ {r.get('short_id')}: {r.get('outcome')}")
SUM
  echo "============================================================"
} | tee -a "$LOG"
