# Editing solar-operator backend safely: cron auto-commit + multi-author tree

How to make a clean change to a shared backend file in `/root/solar-operator`
(e.g. `api/array_owners.py`) WITHOUT clobbering another agent's in-flight work,
and how to reason about commit/push when a cron job is also touching the tree.
Distilled from the June 2026 prune of the dead `smarthub-meter-capture` endpoint.

## The two hazards (always both in play on this repo)

1. **A cron job auto-commits AND pushes the working tree.** A scheduled job on
   this repo periodically runs, and on its tick it can `git add` + commit + push
   whatever is currently uncommitted — including YOUR half-finished edits — under
   a commit message about ITS task, then push to `origin/main` (Railway
   auto-deploys from main). Net effect you'll observe: you finish editing, run
   `git diff` to stage your hunks, and the diff is EMPTY while the file on disk
   still shows your change — because the cron already committed it. `git log`
   shows a recent commit (often authored "Ford Genereaux") whose message does
   NOT mention your change, but `git show HEAD:<file>` contains it. This is not
   corruption; it's the cron sweeping the tree. (Memory note: this cron also
   sometimes leaves its own branch checked out; historically `git push` could say
   "up to date" while origin lagged — if that happens, `git push origin HEAD:main`.)

   **The cron sweeps the WHOLE tree, not just `api/`.** Confirmed June 2026 (post
   power-outage recovery): the cron committed `extension/background.js` +
   `manifest.json` on its tick, authored "Ford Genereaux." This is MORE dangerous
   than a backend sweep, because `git push` does NOT ship an extension change
   (Railway only redeploys the backend; the extension needs a manual Chrome reload
   / Store push). So a committed extension edit looks "landed" in git but is NOT
   live, and the disconnect is easy to miss.

   **PITFALL — the cron will promote an UNVERIFIED probe/harness to permanent.**
   If you leave a temporary verification block in the tree (e.g. a one-shot
   `[AO HISTORY PROBE]` that logs GMP history depth to the console, meant to be run
   on the live portal BEFORE trusting a widened pull), the cron can commit it — or,
   worse, a later cron tick can DELETE it while leaving the widened behavior in
   place, silently promoting the unverified change to permanent. This directly
   defeats Ford's "verify on the live console FIRST" discipline. Mitigation: do NOT
   leave a verification harness sitting uncommitted in this tree expecting to run it
   later. Either (a) run the probe immediately in the same session and act on the
   result, or (b) keep the probe OUTSIDE the repo (e.g. a scratch file in /tmp or a
   gist), never in `extension/`. If you find the harness already gone and the
   widened behavior committed (a commit that DELETES the probe lines), treat the
   widened behavior as UNVERIFIED and say so loudly — offer to restore the probe
   temporarily and run one live pass before pushing.

2. **The file frequently has another agent's UNCOMMITTED edits.** Multi-agent:
   another agent sometimes owns the backend. Before you edit, `git status --short`
   may show the target file already `M` (modified) with substantive work you did
   not make. You MUST preserve it.

## The safe-edit procedure

1. **Orient first.** `git rev-parse --abbrev-ref HEAD` (expect `main`),
   `git status --short`. If your target file is already `M`, inspect the
   in-flight diff: `git diff <file>` — understand what the other agent changed so
   you don't overwrite it. Save it as a safety net: `git diff <file> > /tmp/x.patch`
   and `cp <file> /tmp/<file>.bak`.
2. **Make ONLY your surgical change.** When removing a large block (e.g. a dead
   route), don't hand-delete line ranges in a paginated file — that risks
   off-by-one against the other agent's shifted line numbers. Use a Python
   truncate keyed on a UNIQUE anchor string (the section comment), so the edit is
   independent of line numbers:
   ```python
   src = open(path).read()
   marker = "\n\n# \u2500\u2500 SmartHub server-side-pull meter capture ..."  # exact unique header
   idx = src.find(marker)
   open(path,"w").write(src[:idx].rstrip()+"\n")  # keeps everything before, incl. the other agent's edit
   ```
   For small surgical edits, `patch` with enough surrounding context is fine.
3. **Remove now-orphaned imports too**, but verify they're truly orphaned for
   THIS file only — an adapter import (`from .adapters import smarthub`) may still
   be used by `app.py`, `jobs/`, `adapters/vec.py`, tests. Grep the whole repo
   (`from .adapters import smarthub|adapters.smarthub`) before deleting the module;
   only drop the IMPORT line in the file that no longer references it. Keep
   imports that carry a `# noqa: F401` (often kept so tests can monkeypatch, e.g.
   `import httpx` in array_owners.py).
4. **Fix stale docstrings/comments** that referenced the removed thing (grep the
   file for the symbol name after the cut — a docstring like "used by BOTH … and
   the smarthub_meter_capture path" must be corrected).

## Verification (do all of these — "AST OK" alone is not enough)

- `python -c "import ast; ast.parse(open('<file>').read()); print('AST OK')"`
- **Import the app and assert routes** — this is the real test that the module
  loads and the route is gone/kept:
  ```python
  from api.app import app
  paths=[r.path for r in app.routes]
  assert "/v1/array-owners/smarthub-meter-capture" not in paths   # dead route gone
  assert "/v1/array-owners/utility-meter-capture" in paths        # live route kept
  ```
- Grep the repo for ALL spellings of the removed symbol (route string,
  function name, Pydantic body class) → expect 0 in live code (`.pyc` and doc
  mentions are noise).
- Run the related tests: `python -m pytest tests/test_smarthub_adapter.py
  tests/test_provider_registry.py -q`. Activate the venv first
  (`source venv/bin/activate 2>/dev/null || source .venv/bin/activate`).
- **Distinguish pre-existing test failures from your damage.** If a test errors
  with `sqlite3.OperationalError: no such table: tenants / utility_sessions`,
  that's a missing-migration/DB-fixture issue, NOT your edit. PROVE it: stash your
  version, restore the `.bak`, rerun the same `-k` selection — if it fails
  identically on the backup, it's pre-existing. Restore your version after. Never
  claim a green run you didn't get, and never blame your change for a failure that
  reproduces without it.

## Commit / push reasoning under the cron

- After verifying, check `git status` AGAIN. If your change is already committed
  (empty diff, recent commit contains it via `git show HEAD:<file>`), confirm
  BOTH your change AND the other agent's in-flight edit are present in HEAD
  (`git show HEAD:<file> | grep -c <your-marker>` and `| grep -c <their-marker>`),
  and that `git rev-parse HEAD == git rev-parse origin/main` (already pushed).
  Then you're done — don't re-commit.
- If it is NOT yet committed and you must commit yourself: stage ONLY your file
  (`git add <file>`), write your own message, and be aware the cron may still
  bundle other untracked files. Prefer `git push origin HEAD:main` per the memory
  note about the lagging-origin trap.

### PITFALL — another agent's change shares the SAME file as yours (`git add <file>` over-commits)
"Stage only your file" is NOT enough when one file (classically `api/models.py`,
also `api/migrate.py`) has BOTH your hunk and another agent's uncommitted hunk
(e.g. you added `BillingReportSubscription.auto_attach_gmp` while they added a
whole `class WeatherLocation(Base)` block to the same file). `git add api/models.py`
stages THEIR work too and you'll commit it under your message. Procedure to stage
ONLY your hunk:
1. `git diff <file> | grep "^@@"` — list the hunk headers. If there's >1 hunk and
   one isn't yours, you must split.
2. Files that are 100% yours (single clean hunk, or every hunk is your feature) →
   plain `git add`.
3. Mixed file → build a patch of just your hunk(s) and apply to the index with
   `--recount` (stale `@@` line numbers are normal because the other agent's block
   shifts everything; `--recount` recomputes them):
   ```python
   # extract only the hunk(s) containing your unique marker, keep the 4 header lines
   diff = subprocess.run(['git','diff','api/models.py'],capture_output=True,text=True).stdout
   # ...assemble header(diff[:4 lines]) + the hunk whose body contains 'auto_attach_gmp'...
   open('/tmp/mine.patch','w').write(patch)
   ```
   then `git apply --cached --recount /tmp/mine.patch`.
4. VERIFY the split: `git diff --cached --name-only` (your files only) and
   `git diff --cached | grep -c "<their-marker>"` MUST be 0, `| grep -c "<your-marker>"`
   MUST be >0. Only then commit. This keeps their in-flight work in the working
   tree, unstaged, for them to commit.
- Confirm deploy: Railway auto-deploys main — `curl -s -o /dev/null -w "%{http_code}"
  https://web-production-49c83.up.railway.app/health` should be 200.

### PITFALL — adding an ORM column + pushing to main breaks prod until you migrate
Railway auto-deploys main. The moment a commit that adds a NEW column to a
SQLAlchemy model lands, the live code starts SELECTing that column — but the prod
Postgres does NOT have it until `api/migrate.py` runs. Result: `/health` stays 200
(it touches nothing), but every endpoint that queries the changed table 500s with
`(psycopg2.errors.UndefinedColumn)` / locally `sqlite3.OperationalError: no such
column: <table>.<col>`. This is the SAME failure mode as the stale-dev-DB error
(see the local-probe reference) — `Base.metadata.create_all` does NOT ALTER an
existing table, so neither dev nor prod gains the column for free.
- Always pair a model column-add with an idempotent ALTER in `api/migrate.py`
  (guarded by `column_exists(conn, table, col)`; nullable `DOUBLE PRECISION` /
  `BYTEA` etc. — additive, non-destructive). Follow the existing block style there.
- After pushing such a change, the migration MUST run on prod:
  `railway ssh "cd /app && python -m api.migrate"`. It is idempotent + additive, but
  it is a PROD DB write — confirm with Ford before running it yourself, and tell him
  plainly that the live Reports/billing tab 500s for existing users until it runs.
- `getattr(row, "new_col", None)` in serializers does NOT save you: SQLAlchemy still
  emits the column in the SELECT, so the query fails before your getattr runs.
- **TIMING TRAP — `railway ssh migrate` right after `git push` runs against the OLD
  code.** Railway takes ~30–60s+ to build/swap the new image. If you run
  `railway ssh "cd /app && python -m api.migrate"` immediately after pushing, you're
  executing the PREVIOUSLY-deployed `migrate.py`, which lacks your new ALTER block —
  so the log shows the old migrations and NOT your `+ <table>.<col>` line, and you'll
  wrongly think it failed. Two-part fix: (a) re-run migrate after the deploy lands
  (wait, or push then sleep ~45s), but (b) DON'T trust the migration stdout at all —
  VERIFY the column directly, which is authoritative regardless of which code ran:
  ```bash
  railway ssh "cd /app && python -c \"from api.db import engine; from sqlalchemy import inspect; \
  print('present:', 'auto_attach_gmp' in [c['name'] for c in inspect(engine).get_columns('billing_report_subscriptions')])\""
  ```
  Also confirm the changed table's endpoint returns 401 (healthy auth gate), NOT 500,
  before declaring it safe. (Note: a NOT-NULL-with-server_default column can get added
  by `create_all` on the new deploy's startup anyway, so the column may appear without
  your ALTER line ever printing — the `inspect()` check is what tells you the truth.)

## Resuming a feature you were mid-implementation on when a crash/new-session happened

When Ford says "you were doing X when you crashed," DON'T rely on session_search —
it frequently returns ZERO hits for in-flight work (the crash happened before the
transcript was indexed). The uncommitted working tree IS the recovery record:
1. `git status --short` in the likely repo(s) (/root/solar-operator AND
   /root/array-operator — the feature may span both). `M`/`??` files are your
   in-flight work.
2. `git diff <file>` to read exactly what you'd changed; `git log --oneline -15`
   for what already landed (so you don't redo committed work).
3. Cross-reference the matching skill reference (here, the GMP backfill lived in
   `gmp-meter-api-contract.md`) — the reference often already documents the PLAN,
   so the diff tells you how far you got against it.
4. VERIFY before continuing: `node --check <file>` for extension JS,
   `python -c "import ast; ast.parse(...)"` for backend — a crash can leave a
   half-written block. Read the full changed region, don't trust the diff summary.
5. Then finish + verify the way the feature demands; only commit YOUR files
   (`git add <specific files>`), never `git add -A` (the tree carries other agents'
   unrelated work — e.g. reports/billing redesign sitting uncommitted alongside).
   EXCEPTION: Ford may explicitly override with "just commit everything across all
   repos / we'll sort it later" (he did during June 2026 outage recovery). That's a
   deliberate user call, not a license to default to `git add -A` — honor it when
   he says it, but the default remains commit-only-your-files. When you do bulk
   commit on his say-so, still push each repo to origin so nothing lives only on the
   local disk, and note in the commit message that it's a recovery/wip sweep so the
   bundled-unrelated-work is findable later.

## Report honestly

Tell Ford plainly when the cron bundled your change into someone else's commit:
the change is correct and live, but the commit MESSAGE doesn't mention it, so the
cleanup is buried in history. He values that honesty over a tidy-looking summary.
