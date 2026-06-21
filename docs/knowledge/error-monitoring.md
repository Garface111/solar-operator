# Whole-System Error Monitoring (launch readiness — June 2026)

ONE backend Sentry DSN covers the entire EnergyAgent system, because both products
(NEPOOL Operator + Array Operator) share one FastAPI backend.

**STATUS (Jun 2026): LIVE.** `SENTRY_DSN` + `SENTRY_ENVIRONMENT=production` are set
in Railway; `GET /health` → `sentry_configured: true`; a test event was accepted.

## Backend
- `api/observability.py`: optional Sentry init — **SILENT NO-OP unless `SENTRY_DSN`
  env is set**, so dev / tests / prod-without-DSN are completely unaffected.
  `before_send` + recursive `_scrub()` redact sensitive keys (authorization,
  cookie, password, access_token, id_token, refresh_token, tenant_key,
  stripe-signature, secret, client_secret) before anything leaves the process.
  Exposes `is_enabled()`, `init_sentry()`, `capture_exception()` (never raises).
- `app.py`: `init_sentry()` immediately after `app = FastAPI(...)`. Global
  `@app.exception_handler(Exception)`:
    1. forward to Sentry (no-op if disabled),
    2. THROTTLED internal email via `notify.send_internal_alert` — 1 per
       `path|exc_type` per 5 min (`_LAST_ALERT` dict, `_ALERT_COOLDOWN_S=300`),
    3. clean JSON 500 `{ok:false, error:"Something went wrong..."}` — NO stack leak.
  `HTTPException` / Starlette HTTP errors are re-raised (normal 401/404 control flow,
  not turned into 500s).

## Browser + extension (client errors)
- `POST /v1/client-error` (in app.py): receives a browser/extension JS error and
  routes it through the SAME pipeline (Sentry capture_message + throttled email).
  Unauthenticated (errors happen pre-login), rate-limited 30/5min/IP via
  `ratelimit.allow("client_error", ip, ...)`, payload capped (message 500 chars,
  stack 4000), `source` tag ∈ `arrayoperator | nepool | extension`. Reuses the
  `_LAST_ALERT` cooldown table so a client error storm can't spam the inbox.
- `array-operator/public/error-reporter.js`: loaded FIRST in `<head>` of index.html
  so it sees errors from every later script. Listens for `error` +
  `unhandledrejection`, dedupes (60s), caps (20/session), uses `navigator.sendBeacon`
  (falls back to fetch keepalive). Exposes `window.AOReportError(msg, stack)` for
  manual reporting of caught errors.
- Extension `background.js` (v1.9.10): `self` `error` + `unhandledrejection`
  listeners POST to `/v1/client-error` (source=extension, deduped, capped 25).
  The SMA debugging saga (v1.9.2→1.9.8) would have been near-instant with this.
  Manifest already has `web-production-49c83.up.railway.app/*` host permission.
- Netlify `array-operator/public/_redirects` already proxies `/v1/*` → Railway, so
  the same-origin POST from arrayoperator.com works without a new rule.

## Activation (already done, kept for reference)
1. Create a free sentry.io project (Python / FastAPI), copy the **DSN**.
2. `railway variables --set "SENTRY_DSN=<dsn>"` (redeploys; starts capturing).
   Optional: `SENTRY_ENVIRONMENT` (default production), `SENTRY_TRACES_SAMPLE_RATE`
   (default 0.0 = errors only), `SENTRY_RELEASE`.
3. Verify: `GET /health` → `sentry_configured: true`.
- **Even with NO DSN**, every unhandled 500 AND every client error still emails
  Ford via the internal-alert fallback — so prod is never fully silent.

## Auto-fix subsystem (COMMITTED + SCHEDULED Jun 2026 — dormant until token added)
SAFE mode (mode A). Cron job `f1c8dc829a25` runs HOURLY in `no_agent` mode (script
IS the job, zero LLM token cost; delivers to origin chat; empty output = silent, so
Ford is only pinged when a PR opens). Verified end-to-end: a probe ZeroDivisionError
produced a correct one-line guard + 2 regression tests in PR #1 (since closed).
- `scripts/sentry_fetch.py`: reads new unresolved issues + latest-event stack via the
  Sentry API. Token from `$SENTRY_AUTH_TOKEN` or `/root/.hermes/secrets/sentry_auth_token`
  (read-only scopes event:read/project:read/org:read). No-ops cleanly with no token.
  Dedupe state in `/root/.hermes/secrets/sentry_processed.json` (`--mark` records seen).
- `scripts/sentry_autofix.py`: per issue, branch → delegate fix to Claude Code (opus)
  → run FULL test suite → if green, push + open a PR. NEVER merges, NEVER deploys,
  NEVER touches main (resets to origin/main each run, cleans Claude's untracked adds).
  HARD RAILS (fix abandoned, issue left for humans): diff touches a sensitive path
  (auth/login/session/password, billing/stripe/payment/webhook, migrations, api/db.py,
  delete/teardown, secrets — regex `SENSITIVE_RE`, covers edited AND newly-created
  files); >60 lines or >3 files; tests fail; Claude reports UNSAFE/NOFIX. Max 3 PRs/run.
- `scripts/sentry_autofix_tick.sh` (repo) + `~/.hermes/scripts/sentry_autofix_tick.sh`
  (thin cron wrapper, required because cron scripts must live under ~/.hermes/scripts/).
- KILL-SWITCH: `touch /root/solar-operator/.autofix_disabled` (gitignored) pauses it
  without touching cron. To pause the cron itself: `cronjob action=pause job_id=f1c8dc829a25`.
- Ford initially asked for AGGRESSIVE (auto-merge to prod), then corrected to SAFE —
  honor SAFE unless he re-confirms aggressive.
- ACTIVATED (Jun 2026): read-only token (`sntryu_…`) stored at
  `/root/.hermes/secrets/sentry_auth_token` (chmod 600, off-repo). `sentry_fetch.py`
  confirmed reading the live project (org `dyson-swarm-technologies`, project
  `python-fastapi`). The cron is now fully live. The DSN only SENDS errors — reading
  issues back requires this SEPARATE API token (don't confuse the two).
- Token-file gotcha: `_token()` strips trailing newline, so a 71-byte file for a
  70-char token is fine. `railway ssh` / `gh ... | python3 -c` pipe-to-interpreter
  patterns can trip the shell security scanner — avoid piping curl/output into a
  python `-c`; use `gh --template`/`--json` flags or a temp file instead.

## Operating the auto-fixer MANUALLY (proven Jun 2026 — "is it working on it? + auto-merge if it passes")
When Ford asks whether the auto-repair is working on a specific Sentry error (and may
then say "auto-merge if it passes"), DON'T just describe the cron — drive it and prove it.
Proven end-to-end sequence (took one IntegrityError on /v1/array-owners/inverter-capture
from Sentry → live prod fix):

1. **Triage state first** (cheap, no side effects):
   - `cronjob action=list` → confirm `f1c8dc829a25` enabled + recent `last_status:ok`.
     ("ok" on a no_agent script only means exit 0 — it does NOT mean it acted on THIS issue.)
   - Kill switch: `ls /root/solar-operator/.autofix_disabled` (absent = active).
   - Dedupe state: read `/root/.hermes/secrets/sentry_processed.json` (`seen` array of
     issue ids). If the target issue id is NOT there, it hasn't been processed yet.
   - Open PRs: `gh pr list --repo Garface111/solar-operator --state open` (autofix branches
     are `autofix/sentry-<short_id>-<ts>`).
   - LIVE fetch (no `--mark`): `.venv/bin/python scripts/sentry_fetch.py` → shows
     `new_issues[]` with id/short_id/culprit/count/stack. This is the source of truth for
     what Sentry currently sees.
2. **Timing gap is the usual "not working yet" cause, NOT a failure:** the cron ticks
   hourly on the hour; an error that first appeared minutes AFTER the last tick simply
   waits for the next one. Detection (sentry_fetch) working + no PR + issue not in `seen`
   = it just hasn't run since the error appeared.
3. **`cronjob action=run` is unreliable for forcing a real PR** — observed it MARK the
   issue in `sentry_processed.json` (added to `seen`) yet open NO PR. To actually drive a
   fix, run the tick script DIRECTLY (it pipes sentry_fetch → sentry_autofix):
   `cd /root/solar-operator && bash scripts/sentry_autofix_tick.sh` (background +
   notify_on_complete — Claude opus + full pytest takes ~3–5 min; opus output is buffered
   so you see nothing until it exits, then a final summary line per issue with the PR URL).
4. **PROTECT WORKING-TREE WIP BEFORE RUNNING (critical).** `sentry_autofix.py`'s
   `ensure_clean_main()` does `git reset --hard origin/main`, which DESTROYS tracked
   uncommitted changes (untracked files survive). Before running: `git stash push -m
   autofix-protect-wip <paths>` (and `cp` a backup to /tmp), run the fixer, then `git stash
   pop` after. (Seen: an `extension/manifest.json` version bump would have been wiped.)
5. **Re-mark dedupe so the run picks up the issue:** if a prior `action=run` already added
   the issue id to `seen`, rewrite the file to REMOVE it before running directly, then ADD
   it back after a successful PR so the hourly cron doesn't re-fetch it.
6. **VERIFY THE PR YOURSELF — never trust Claude's self-report (Ford trust-checks):**
   `gh pr view <n> --json additions,deletions,changedFiles,mergeable,files` + `gh pr diff
   <n>`. Then run the NEW regression test AND the full suite ON THE PR's code:
   `git fetch origin <branch> && git checkout FETCH_HEAD` →
   `.venv/bin/python -m pytest tests/<file>::<new_test> -q` → `.venv/bin/python -m pytest -q
   | grep -E "passed|failed"`. (Checking out FETCH_HEAD guarantees you're testing the PR,
   not stale working-tree state.)
7. **Auto-merge ONLY when Ford explicitly authorizes it this session** (standing default is
   SAFE/PRs-only — see rail below). `git checkout main` → `gh pr merge <n> --squash
   --delete-branch`. Merge auto-deploys to Railway. Then restore the stash, poll
   `railway deployment list | sed -n 2p` until SUCCESS, and `curl .../health` (expect
   `sentry_configured:true`). Leave the Sentry issue OPEN — it auto-resolves once events stop.
8. The fix the autofixer produced here was textbook: matched arrays by name ACROSS
   soft-deleted rows + reactivated (mirroring the existing Inverter-undelete in the same
   function) — root cause was the match-or-create lookup filtering `deleted_at.is_(None)`
   while `uq_array_per_tenant` spans `(tenant_id, name)` with NO soft-delete awareness, so a
   re-capture of a soft-deleted array INSERTs a colliding name. (This is the same trap
   `create_array` already handles — see the main SKILL's persisted-inverters note.)

## Tests
`tests/test_observability.py` (11): no-DSN no-op, capture-disabled no-op, `_scrub`
redaction, `_before_send` header scrub, 500→clean JSON + capture + alert,
HTTPException-not-swallowed, alert throttling, `/health` flag, client-error
accept+alert, client-error ignores-empty, client-error caps-payload.
Full suite after this work: 923 passed, 3 xfailed.
