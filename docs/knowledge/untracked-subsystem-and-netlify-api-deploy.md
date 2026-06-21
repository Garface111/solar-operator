# "Feature was lost" is often BUILT-BUT-UNTRACKED + the Netlify API deploy fallback

Two durable, cross-session lessons from the GMP-backfill revival (Jun 2026).

## 1. "Whatever happened to feature X? please build it" → FIRST check git tracking, not just behavior

When Ford says a feature seems "lost" or "we built this and I think it was lost,"
the most common root cause is **the code was written by a parallel/earlier agent
but never `git add`ed** — it sits untracked in the working tree, so it never
deployed and never ran. This bit us hard: the entire GMP daily-backfill logic
layer (the job + read-contract modules) was complete on disk but untracked for
days. The DB *tables* were committed (so a probe found them, empty), but the
logic that fills/reads them was invisible to prod.

DIAGNOSIS ORDER before rebuilding anything:
1. `git status --short api/ | grep '^??'` — list ALL untracked files. Parallel
   "spider-mode" builds leave whole subsystems untracked (jobs/, reports/,
   adapters/, models_*.py). Look for coherent clusters.
2. `git ls-files <file>` — confirm whether a specific module is tracked at all.
3. Check the deployed image, not just local:
   `railway ssh "cd /app && python -c 'import api.jobs.<mod>'"` — a
   `ModuleNotFoundError` on prod for a file that exists locally = untracked.
4. Grep for who already imports it: a committed module importing an UNtracked one
   (e.g. `billing/delivery.py` importing `reports/gmp_daily_read`) is only
   surviving via try/except and silently no-ops on prod. Committing the missing
   file un-breaks that path too.

SECOND failure mode layered on top: a job that imports adapter functions which
were *specced in a contract doc but never landed in the adapter*. The job would
`ImportError` the instant it ran. So "built" can be three-layers-inert:
tables committed but empty → logic untracked → adapter fns never written.
Verify the WHOLE import chain resolves (`python -c "import <job>"`) before
claiming a backfill/ingest feature works.

WHEN COMMITTING a rediscovered subsystem in a shared/multi-agent tree: stage ONLY
the self-contained files for THIS feature. Verify no cross-vein imports first
(`grep -E '^from|^import' <file> | grep -iE 'solaredge|weather|other_vein'`) so
you don't drag another agent's half-built vein onto prod. The other untracked
files (other agents' veins) stay untracked — not your call to ship them.

## 2. Netlify CLI auth dies constantly → deploy via the REST API (bypass the CLI)

The Netlify CLI's cached session (`~/.config/netlify/config.json`) expires
repeatedly and `netlify login` is interactive browser OAuth (can't do headless).
Worse: even a VALID `NETLIFY_AUTH_TOKEN` env var and `--auth <token>` flag get
IGNORED because the stale `config.json` session takes precedence → endless
"JSONHTTPError: Unauthorized" / "Failed retrieving user account: Unauthorized".

The token itself is usually FINE — prove it directly:
`curl -s -o /dev/null -w "%{http_code}" -H @hdrfile https://api.netlify.com/api/v1/user`
(200 = token good; the CLI is the broken part, not the token).

FIX — deploy through Netlify's REST API directly with a small Python script.
Saved at `scripts/netlify_api_deploy.py` in THIS skill. Flow:
1. POST /sites/{id}/deploys with `{files: {"/path": sha1, ...}}` for every file.
2. Netlify returns `required` = list of sha1s it still needs.
3. PUT each required file's bytes to /deploys/{id}/files{path}.
4. Poll GET /deploys/{id} until state == "ready".
It only uploads changed files (3 of 28 on a typical AO deploy) and reports the
live URL. This is now the PREFERRED AO deploy path when the CLI is flaky.

Find the array-operator site id once:
`curl -H @hdr "https://api.netlify.com/api/v1/sites?per_page=100"` → grep "array"
→ array-operator-ea = `966cb1f5-944e-41fd-855b-10053edc5d18` (live=arrayoperator.com).

### Secret-masker traps when scripting the token (recurring, painful)
The shell secret-masker mangles inline `$(cat token)` / `Bearer $TOK"` quoting in
BOTH terminal commands AND the *echo* of write_file tool calls — it replaces the
substitution with `***` and breaks quote balance ("unexpected EOF", "not a valid
identifier"). Workarounds that actually hold:
- Put the export + deploy in a `.sh` FILE and run it; the file BYTES are written
  correctly even though the tool-call echo shows `***`. Re-read the file to
  confirm before running.
- For curl auth headers, write the header to a file and use `curl -H @hdrfile`
  (build it: `printf 'Authorization: Bearer ' > h && cat tokenfile >> h`).
- Put any inline Python (json parsing) in its OWN .py file, never `python3 -c`
  with brackets — the masker garbles `[...]`/quotes there too.

## 3. Reusable: union a new daily source into fleet-trends WITHOUT double-counting
Trends + the daily bar graph read `DailyGeneration` (CSV table). To surface a new
per-day source (e.g. `gmp_daily_generation` via its read contract) merge per-day
into a `per_day: dict` keyed by date, loading the CSV table FIRST, then filling
only days the new source covers that CSV doesn't (`if d not in per_day`). Prefer
the existing/authoritative table on overlap so live report numbers never shift.
Wrap the new-source read in try/except so a read-contract hiccup never sinks
trends. Test the exact overlap arithmetic (CSV wins) + a pure-new-source array.
