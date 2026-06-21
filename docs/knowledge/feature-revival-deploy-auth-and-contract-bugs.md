# Feature revival, AO deploy auth, and frontend↔backend contract bugs

Durable patterns from a long AO frontend/Reports/Trends session (Jun 2026).

## 1. AO (Netlify) deploy — CLI auth is UNRELIABLE; use the API fallback
The `netlify` CLI caches a login in `~/.config/netlify/config.json` that
OVERRIDES env auth. When that session expires, `netlify deploy --prod` returns
`JSONHTTPError: Unauthorized` even with a valid `NETLIFY_AUTH_TOKEN` AND with
`--auth <tok>`. `netlify login` is interactive browser OAuth → not headless-able.
The TOKEN is usually fine (curl `https://api.netlify.com/api/v1/user` → 200);
only the CLI is broken.

FIX — deploy via the REST API, bypassing the CLI entirely:
`python3 scripts/netlify_api_deploy.py` (in THIS skill's scripts/). It walks
`/root/array-operator/public`, POSTs the file-digest manifest, uploads only the
changed files, polls to `ready`. Site = array-operator-ea
(`966cb1f5-944e-41fd-855b-10053edc5d18`) → arrayoperator.com. Make this the
DEFAULT AO deploy path; don't keep fighting the CLI.

Token: chmod600 `~/.hermes/secrets/netlify_token`. Ford re-pastes a fresh
`nfp_...` token when the old one dies — store it, warn him it's now in the
transcript (suggest rotating), and verify validity with the API (not the CLI).

Secret-masker trap: it mangles inline `$(cat token)` / `"$TOK"` in BOTH terminal
commands and tool-call echoes. Read the token from a file inside a PYTHON script
(as the deploy script does), never inline in bash.

## 2. "Whatever happened to feature X?" → it was built but never WIRED/COMMITTED
When Ford asks where a feature went, the answer is often NOT "lost." This session
the GMP multi-year daily backfill was fully built (job + sponge + read-contract +
verified endpoint) yet 100% inert. Diagnosis order that found it fast:
  1. `git ls-files <path>` / `git status --short` — the entire logic layer
     (`api/jobs/gmp_daily_backfill.py`, all `api/reports/*_read.py`) was
     UNTRACKED (`??`). Parallel agents built it and never committed. Prod never
     had it. (The DB *models* WERE committed → tables existed but empty, which
     masks the gap.)
  2. Grep for a trigger: not in `api/scheduler.py`, no admin endpoint → nothing
     could run it even if deployed.
  3. Check the adapter it imports: `api/adapters/gmp.py` was missing
     `fetch_usage_csv` / `parse_usage_csv_to_daily` / `GmpUsageNotFound` /
     `GmpUsageTimeout` → `ImportError` at runtime (and broke
     `test_gmp_daily_sponge` collection). The bills adapter existed; the
     daily-USAGE-CSV adapter was specced in the contract doc but never landed.
  4. Verify the historical blocker is gone: probe prod via railway-ssh base64
     stdin — 22 live GMP sessions, fresh non-expired tokens. The original "dead
     token" blocker had cleared.
LESSON: a feature needs MODEL + LOGIC committed + a TRIGGER (scheduler/endpoint)
+ the adapter functions it imports, and the consuming surface must actually READ
it. Audit all four layers before declaring it built or lost.

### Activation recipe (reused here, generic to any "turn on a built job")
- Admin trigger: `POST /admin/<job>/...` guarded by `Depends(_require_admin)`
  (mirror `admin_refresh_rate_schedule` in app.py). `_require_admin` fails CLOSED
  on Railway (503 when ADMIN_API_KEY unset), falls OPEN locally — so a local test
  must `monkeypatch.setattr(appmod, "ADMIN_API_KEY", "k")` then assert 403, NOT
  assert 403 with no key (local returns 200).
- Scheduler: add a `_run_<job>()` wrapper (try/except + `send_internal_alert`) and
  `scheduler.add_job(CronTrigger(...), max_instances=1, coalesce=True)` in
  `start()`. railway-ssh introspection of live jobs shows 0 (the SSH shell never
  calls `start()`); proof is "registered in start() source" + app health 200.
- Connecting a new data source to Trends: `fleet-trends` only read the CSV
  `DailyGeneration` table. To surface GMP daily, merge `gmp_daily_read`
  `get_daily_series(array_id)` into the per-array day map, PREFERRING the CSV
  value on overlapping days (no double-count), GMP fills gaps. Wrap the read in
  try/except so a contract hiccup can't sink trends.

## 3. Frontend↔backend body-key 422 ("Couldn't save (HTTP 422)")
A 422 on a save almost always = the JS POST body key ≠ the Pydantic model field.
Here the Master Account "Company" field sent `{company_name}` but
`UpdateCompanyName` expects `{name}` → 422. Email worked because `{email}` matched
`UpdateEmail`. To prove: hit the prod endpoint with the OLD shape (expect 422)
and the NEW shape (expect 401 with a bogus token = body parsed, auth-gated). Fix
is the client key; the endpoint was always right → no backend change/migration.

## 4. CSS `[hidden]` overridden by `display:flex` (always-expanded editor)
A class rule like `.acct-pw-edit{display:flex}` BEATS the UA `[hidden]` rule
(class specificity > attribute default), so JS toggling the `hidden` attribute
does nothing — the panel stays open. Symptom this session: the password editor
was permanently expanded, so "Current password" showed even for accounts with no
password yet. FIX: add `.<cls>[hidden]{display:none}`. Whenever you hide via the
`hidden` attribute on an element that also has an explicit `display`, add the
`[hidden]` guard.

## 5. Sitewide LABEL renames (Ford's recurring ask) — display strings ONLY
"Rename X to Y sitewide" = user-facing display text only (labels, headings,
placeholders, button text, error messages, PDF/xlsx invoice line labels). NEVER
touch field names / API keys / element IDs / `data-*` values / function names —
that breaks the contract for zero benefit. Confirmed renames: "Net rate"→"Solar
credit rate", "Customers/customer"→"Offtakers/offtaker" (kept `customer_name`,
`has_customers`, `#rbqCustomer`, `data-sub="customers"`, `renderCustomers` etc.
all intact). Verify with grep: new label present, old label gone from DISPLAY,
remaining hits are comments/identifiers. State the split back to Ford.

## 6. Stacked-views + per-view-canvas teardown
Converting a tabbed switcher to an all-at-once stacked column: change the single
`_activeStop` to an `_activeStops[]` array and stop ALL on teardown, mount each
view into its own `#trHost_<key>` host. Multi-year/decorative charts look BROKEN
with <2 years of real data (single dot / one bright cell) — gate them: dim +
tag "needs 2+ years" + caption, rather than letting a near-empty canvas read as
a bug. Pair with a real quantitative chart (Monthly Production bars) for the read
owners actually want.

## 7. Power-user dashboard upgrade checklist (Trends)
What a power user needs beyond pretty charts: (a) a data-FRESHNESS line ("through
<date> (Nd ago) · X/Y arrays reporting") so numbers earn trust; (b) Export CSV
(monthly + daily, real rows, no fabrication); (c) stat tiles that never show a
bare "—" — make the 4th tile adaptive (BEST MONTH until 2yrs of data, then true
YoY); (d) by-array table sorted by output with a share-of-fleet chip and an
explicit "no data yet" chip for 0-kWh arrays (so a dead-looking row is explained).

## Local QA recurring traps (all hit again this session)
- Stale uvicorn on :8788 serves OLD code even after edits (no --reload). Symptom:
  new route 404 / response missing a new field. FIX: `pkill -f "uvicorn api.app"`,
  confirm port free, restart; verify the route via `/openapi.json` or a probe.
- Two uvicorns can both "start" — the second fails to bind silently and the FIRST
  (stale) one keeps serving. Always kill + confirm `ss -ltnp | grep 8788` free.
- dev_proxy on :8089 dies between QA runs (restart it) AND returns 501/000 on the
  upstream when the backend is down — looks like a frontend bug, is a dead backend.
- Local dev backend OOM-kills (exit 137) mid-Playwright; "Session expired" in the
  UI often = dead/booting backend, re-check /health.
- Probe session tokens expire mid-run → re-mint with
  `mint_session_for_tenant('ten_paulbozuwa01')` before each Playwright pass.
- Shared-tree test isolation: a new test that seeds tenants/arrays into the shared
  SQLite leaks into other files' GLOBAL-count asserts (e.g. delivery's
  `scalar_one()` on all subscriptions). Add an autouse cleanup fixture that
  deletes the rows it seeded (Tenant by `id`, children by `tenant_id`).
- Pre-existing test failures from a sibling agent's uncommitted work: confirm by
  `git stash` your files → re-run → still fails ⇒ not yours. Don't chase it.
