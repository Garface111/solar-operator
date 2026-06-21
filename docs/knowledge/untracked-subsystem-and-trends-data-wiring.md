# Untracked-subsystem landmine + Trends data-wiring & frontend patterns

Two durable lessons from the "whatever happened to the GMP backfill?" session
(Jun 2026). Both generalize well beyond GMP.

NOTE: this skill's SKILL.md BODY is OVER the 100k hard limit, so it cannot be
edited to add a pointer here. Discover this file by LISTING the references/ dir.

---

## 1. The "BUILT BUT NEVER COMMITTED" landmine (why a feature "never happened")

Symptom: Ford asks "whatever happened to feature X? please build it" — and X
*looks* absent in prod, but the code is partly present locally.

What actually happened with the GMP daily backfill: the MODELS were committed
(`GmpUsageRaw`, `GmpDailyGeneration`) so the tables existed on prod — but EMPTY
(0 rows). The entire LOGIC layer (`api/jobs/gmp_daily_backfill.py`,
`api/reports/gmp_daily_read.py`, and other `api/reports/*_read.py` sponge
modules) was written by earlier parallel agents and left **untracked** in the
working tree for days. It never deployed. A committed file (`billing/delivery.py`)
even imported the untracked read module and survived only via a `try/except`,
masking the gap.

DIAGNOSIS SEQUENCE — run this BEFORE rebuilding anything when a feature is
"missing/lost":
1. `git ls-files <path>` — empty output = the file is UNTRACKED (the smoking gun).
2. `git status --short | grep '^??'` — lists ALL untracked files; scan for whole
   subsystems (jobs/, reports/, adapters/) sitting outside git.
3. Confirm on the deployed image, not just locally:
   `railway ssh "cd /app && echo <base64-of-probe.py> | base64 -d | python -"`
   where probe.py does `import api.jobs.x` / `importlib.import_module(...)` and
   prints which functions exist. A `ModuleNotFoundError` on prod for a file that
   exists locally == untracked-and-undeployed.
4. Check whether a COMMITTED file imports the untracked one inside try/except —
   that path is silently degraded on prod, and committing the dep fixes it too.

FIX: commit the untracked files (own hunks only — shared tree; see the deploy
ref). Verify they're self-contained first (`grep '^from\|^import'` for
cross-vein deps so you don't drag another agent's unfinished module along).

GENERAL RULE: "models committed but tables empty" + "logic untracked" is a
recurring shape here because parallel build agents are told NOT to commit. When
you inherit their work, the first job is to git-track + wire it, not rebuild.

A SECOND missing-keystone variant: the backfill job imported adapter functions
(`fetch_usage_csv`, `parse_usage_csv_to_daily`, `GmpUsageNotFound/Timeout`) that
were SPECCED in a contract doc but never landed in `adapters/gmp.py` at all — so
even locally it ImportError'd on first run. When a job imports from an adapter,
grep the adapter to confirm the functions actually exist before assuming the
job runs.

---

## 2. Wiring an inert subsystem ON (trigger + scheduler + verify)

Once the code is tracked, an "inert" subsystem usually needs three switch-ons:
- **A manual trigger**: admin POST endpoint guarded by `_require_admin`
  (`/admin/gmp-backfill/tenant/{id}` + `/account/{id}`). Pattern mirrors
  `/admin/rate-schedule/refresh`. Note: `_require_admin` FAILS CLOSED on Railway
  (503 when `ADMIN_API_KEY` unset) but FALLS OPEN in local/test — so a test that
  asserts "401/403 without key" must `monkeypatch ADMIN_API_KEY` to a value first.
- **A scheduler job**: add a `_run_*` wrapper (try/except + `send_internal_alert`
  so it can't crash the scheduler thread) and register it in
  `api/scheduler.py::start()` via `scheduler.add_job(... CronTrigger(...),
  max_instances=1, coalesce=True)`. GMP backfill runs daily 05:00 UTC.
- **Verify on prod**: `railway ssh` import-probe (adapter fns + job module +
  `'<job_id>' in inspect.getsource(scheduler.start)`). NOTE: a fresh `railway
  ssh` python process never calls `start()`, so `scheduler.get_jobs()` returns 0
  there — that's expected, not a failure. Authoritative proof is (a) the job is
  registered in start()'s source and (b) `/health` 200 (web process booted it).

GMP auth was the ORIGINAL blocker on this feature; by this session it was ALIVE
(22 fresh non-expired sessions). Always re-probe auth state before assuming the
old blocker still holds — don't inherit a stale "it's blocked on tokens" belief.

---

## 3. Trends data-source DISCONNECT (the real gap behind "Trends has no data")

`/v1/array-owners/fleet-trends` historically aggregated ONLY the
`DailyGeneration` table (CSV uploads / billing meter). The GMP daily sponge
writes to a DIFFERENT table (`gmp_daily_generation`), so Trends + the daily bar
graph were BLIND to GMP data even after a backfill.

FIX pattern (per array, in the fleet-trends loop): build a `per_day` dict from
`DailyGeneration` first, then fold in `gmp_daily_read.get_daily_series(arr.id)`
ONLY for days the CSV table doesn't already cover — **CSV wins on overlap, no
double-count**. Wrap the GMP read in try/except so a read-contract hiccup can't
sink trends. This same merged `per_day` feeds fleet month×year, the 30-day
`daily_recent` bars, and per-array lifetime.

LESSON: when two data sources feed one chart, dedupe per-key with an explicit
precedence rule; never sum both blindly.

---

## 4. Array Operator Trends frontend upgrade patterns (vanilla-JS, public/trends*.js)

The Trends tab is a registry of view modules (`trends-view-*.js` self-register on
`window.AOTrends`); `trends.js` owns layout/data/which-view; `trends-core.js` is
the shared canvas + prep + registry keystone.

- **Stacked column instead of a switcher**: render ALL views at once, one block
  per visualization (each its own host `#trHost_<key>`, accent, title, desc).
  `teardown()` must track an ARRAY of stop fns (`_activeStops`), not a single
  one, or only the last canvas's RAF loop gets cancelled.
- **registry `order` is FALSY-bug-prone**: `(a.order||99)` makes `order:0` sort
  LAST. Use `order: 0.5` for "first".
- **Honest single-year captioning**: the decorative multi-year views
  (liquid/spiral/heatfield) render near-EMPTY with <2 years and read as
  "broken". Tag+dim them ("needs 2+ years" + italic note) when `years.length<2`.
  Also ship a real QUANTITATIVE monthly bar chart — the decorative art is not a
  substitute for the number an owner actually reads.
- **Per-array FILTER** (the power-user lever Ford asked for): add an optional
  `?array_id=N` query param to the backend endpoint that SCOPES the aggregates to
  one owned array, but keep `by_array` = FULL fleet (so the dropdown can switch)
  and echo `selected_array_id`. Unowned id → 404. Frontend keeps the full array
  list in a module var (`_fleetArrays`) across scoped reloads so the dropdown
  stays complete; a scoped array with no data shows an INLINE empty state that
  keeps the dropdown (never strand the user on the whole-tab empty screen).
- **Stat-band trust fixes**: don't show a bare "—" (reads as broken) — make the
  4th tile adaptive (real YoY when 2+ years, else "Best Month"). Add a
  data-freshness line ("Through <date> (Nd ago) · X/Y arrays reporting") and a
  one-click CSV export built client-side from the payload. Rename "savings" →
  "value" with a blended-rate tooltip.

DEPLOY: AO frontend is the Netlify REST-API script (CLI auth is wedged) —
`scripts/netlify_api_deploy.py`, site_id `array-operator-ea`. Backend = push to
`origin/main` (Railway auto-deploy ~70-80s) then verify route is 401 not 404/500.
See refs ao-deploy-and-frontend-debugging.md / solar-operator-deploy for detail.
