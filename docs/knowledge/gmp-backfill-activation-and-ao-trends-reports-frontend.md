# GMP backfill activation + AO Trends/Reports frontend evolution (Jun 2026)

Session learnings. The SKILL.md BODY is OVER the 100k hard limit (~106k) so it
can't be edited even to add a pointer — discover refs by listing the skill's
`references/` dir, not the body.

## 1. THE "BUILT-BUT-UNTRACKED SUBSYSTEM" LANDMINE (the big one)

When a feature "was lost / whatever happened to X", the cause is often that a
whole **logic layer was built but never `git add`ed** — it sits untracked in the
working tree, so prod never had it, even though its MODELS were committed (tables
exist, empty). This bit the GMP daily backfill HARD:

- `api/models.py` (committed) had `GmpUsageRaw` + `GmpDailyGeneration` → tables
  existed on prod, **0 rows**.
- `api/jobs/gmp_daily_backfill.py`, `api/reports/gmp_daily_read.py`, and the
  whole sponge/read layer were `?? ` UNTRACKED for days. `api/billing/delivery.py`
  (committed) even `import`ed `gmp_daily_read` and survived only via try/except —
  masking the gap.
- The adapter the job imports (`fetch_usage_csv`, `parse_usage_csv_to_daily`,
  `GmpUsageNotFound`, `GmpUsageTimeout`) was never landed in `adapters/gmp.py`
  at all → `ImportError` at runtime if it ever ran.

DIAGNOSIS RECIPE (do this BEFORE rebuilding when a feature seems missing):
```
git ls-files <path>          # empty output = UNTRACKED (the smoking gun)
git status --short | grep '^??'   # full untracked inventory
```
Then prove on the DEPLOYED image (a committed importer can hide an untracked
import behind try/except): base64 a probe script and run it against prod:
```
B64=$(base64 -w0 /tmp/probe.py)
railway ssh "cd /app && echo $B64 | base64 -d | python -"
```
`ModuleNotFoundError: No module named 'api.jobs.gmp_daily_backfill'` on prod
while the file imports clean locally == confirmed untracked.

FIX: commit the self-contained modules (verify no cross-vein imports first:
`grep -E '^from|^import' <file> | grep -iE 'solaredge|weather|ext_sponge'`),
push, wait for deploy, re-run the prod probe to confirm imports resolve.

## 2. GMP backfill is now LIVE-WIRED end-to-end

- `adapters/gmp.py`: added the daily 15-min USAGE CSV path —
  `fetch_usage_csv(acct, jwt, start, end)` (GET /api/v2/usage/{acct}/download?
  format=csv; 404→`GmpUsageNotFound`=below meter floor, 503/timeout→`GmpUsageTimeout`=
  window too big) + `parse_usage_csv_to_daily(csv)` (order-tolerant header detect;
  **NEVER fabricates** — blank/missing Quantity is skipped, not zero-filled).
- Admin triggers: `POST /admin/gmp-backfill/tenant/{id}` + `/account/{id}`
  (admin-key guarded; fails CLOSED with 503 on Railway when ADMIN_API_KEY unset —
  that 503 is CORRECT, not a bug).
- Scheduler: `_run_gmp_daily_backfill` daily 05:00 UTC across every tenant with
  enabled GMP accounts (full multi-year history per meter on first run, cheap
  incremental after). Registered in `api/scheduler.py` `start()`.
- GMP AUTH IS ALIVE (the old blocker is gone): 22 fresh GMP sessions, non-expired
  tokens. Probe via railway-ssh against `UtilitySession` rows before assuming a
  dead token.

## 3. Trends/Reports were BLIND to GMP data — the data-source union

`array_owners.py` fleet-trends + the daily-bar graph read the **`DailyGeneration`
CSV table only** — they did NOT see `gmp_daily_generation`. So even a full
backfill wouldn't show in Trends until you union the two. Pattern applied in the
per-array loop:
```python
from .reports import gmp_daily_read as _gdr
per_day = {}                      # CSV first
for d, kwh in <DailyGeneration rows>: per_day[d] = float(kwh)
for pt in _gdr.get_daily_series(arr.id, db=db):   # GMP fills GAPS only
    if pt["day"] not in per_day: per_day[pt["day"]] = float(pt["kwh"] or 0)
```
**CSV WINS on overlapping days — never sum both (double-count).** Wrap the GMP
read in try/except so a read-contract hiccup can't sink Trends.

## 4. AO Trends-tab frontend (vanilla JS, /root/array-operator/public/trends*.js)

- The 4 visualizations are SEPARATE files (`trends-view-*.js`) self-registering on
  `window.AOTrends` via `registerView(key, {label, badge, order, describe, mount})`.
  `trends-core.js` is the keystone (prep/colors/canvas helper/registry).
- **`order:0` is FALSY** → `(a.order||99)` sorts it LAST. Use `0.5` for "first".
  (bars=0.5, monthly=0.7 etc.) This bit twice.
- STACKED COLUMN, not tabbed: Ford wanted all views shown at once down a column
  (no switcher). `teardown()` must track EVERY mounted view's stop fn (array),
  not a single `_activeStop`.
- Multi-year art (liquid/spiral/heatfield) renders near-EMPTY with <2 years of
  data and READS AS BROKEN. Caption them honestly ("needs 2+ years", dimmed)
  rather than letting a single dot look broken. A real quantitative **Monthly
  Production** bar chart (latest year bold + prior years ghosted) is what owners
  actually read — the rest were decorative over the same monthly data.
- PER-ARRAY FILTER: `GET /fleet-trends?array_id=N` scopes ALL aggregates to one
  owned array; `by_array` STAYS full-fleet so the dropdown can switch; echo
  `selected_array_id`; unowned id → 404. Frontend keeps the full array list in a
  module var across scoped reloads so the dropdown never shrinks. Scoped-empty
  array shows an inline empty state that KEEPS the dropdown (never strands you).
- Stat-band honesty: TTM==Lifetime with one month of data is NOT a bug; a "LATEST
  YOY —" dash reads as broken → adaptively show BEST MONTH until 2+ years exist.
- CSV export of monthly+daily is a cheap, high-value power-user lever.

## 5. AO Reports-tab overdesign trims (Ford audit, "almost there")

Ford asked for a usability + overdesign audit. Functionality was solid (real
data, no console errors, discount model resolves correctly — the "your default"
badge is rate-SOURCE provenance, separate from the discount; not a bug). The wins
were de-cluttering:
- COLLAPSE dense rows: a per-offtaker row had ~11 always-on controls (2 sliders +
  discount input + 6 buttons + chips). Collapse to name + status chips + rate line
  + 3 buttons (Draft/Preview/Edit); Edit toggles a `.rb-sub-more` panel (hidden
  attr + `[hidden]{display:none}` since the row is flex). The edit toggle is
  LOCAL — no fetch, no list refresh (refresh rebuilds + re-collapses the row).
- Kill redundancy: the discount showed 3× per row (chip + rate line + input) →
  drop the chip.
- Remove DECORATIVE charts from CUSTOMER-FACING docs: the Solar Spiral in the
  Quarterly report conveyed nothing; keep only the Daily Generation bar chart.
- Thin eyebrow pills that just echo the heading/subtab name (1 per card max).

## 5b. CONSOLIDATION: merge-a-tab + prune-vs-fold + "maintain functionality"

As AO matures Ford repeatedly drives toward de-clutter: "delete the X tab and do
everything through Y", "how does Z dovetail... is it legacy or necessary? fold it
in". This is a recurring CLASS of task. The workflow he expects:

1. **AUDIT FIRST, don't restructure silently.** Render every state live (Playwright
   + vision_analyze over localhost), read the code, and report the honest verdict
   with evidence BEFORE touching anything. He picks the scope via clarify.
2. **PRUNE-vs-FOLD decision: trace what the feature UNIQUELY does.** Don't assume
   "old standalone thing" = legacy. Example: the spreadsheet-upload path LOOKED
   like a legacy orphan but was NECESSARY — it stores `source_workbook` bytes and
   `invoice_writer.populate_invoice_workbook` re-fills THAT exact .xlsx each cycle
   (bill in the owner's own format); manual subs fall back to a generic invoice.
   Pruning would silently downgrade every workbook customer. Verdict: **fold, not
   prune.** Always give him the trace + a prune-vs-fold recommendation, let him choose.
3. **MERGE A TAB without losing functionality.** Pattern that worked (Offtakers
   tab → Invoice Generator): the deleted tab edited fields (name/email/CC/array/
   share%/rate) the survivor didn't. Fold those into the survivor's per-row Edit
   panel and REUSE the existing save fn (`saveCustCard`) — pass it the row el; it
   reads `[data-f=...]` + `.rb-cust-status` which exist wherever you mount them.
   Relocate the tab's "＋ Add" button + its inline form to the survivor (set
   `MANUAL_HOST_ID`/`MANUAL_AFTER_ADD` to the new mount). Delete the now-dead
   render/card/wire fns + the subtab button/panel/routing; KEEP the save fn.
4. **FOLD AN ORPHAN into one entry with internal tabs.** Unify two front-doors
   (typed form + spreadsheet upload) into ONE "Add" panel with `Type it in` /
   `Upload a spreadsheet` tabs (a module `ADD_MODE` var). Move the dropzone + live
   doc-preview INTO the upload tab; wire `wireUpload()`/`renderDoc()` when that tab
   opens (not at load — the elements don't exist until then). Mirror the close+
   refresh on save across both paths.
5. **Fold into the ONBOARDING wizard too** when asked: add an "or upload your
   spreadsheet" path to the wizard's add-offtakers step. Subtlety: the workbook
   sub is created IMMEDIATELY on upload (file bytes must POST), unlike typed
   entries deferred to Finish — so the Finish loop must SKIP already-created
   uploads (a `from_upload` flag) or it double-creates.
6. **PROVE functionality preserved — this is a HARD Ford requirement.** "Maintain
   functionality" means actually EXERCISE the new path end-to-end and verify the
   side effect: edit a field through the merged Edit panel and confirm it
   PERSISTED via the API (then revert the test value); upload through the new tab
   and confirm subs count went up by 1 (then delete the test sub by id). Describing
   it is NOT enough. Clean up every test artifact you create on the live demo data.

## 6. AO Netlify deploy: CLI auth is DEAD here — use the REST API

The Netlify CLI's cached session in `~/.config/netlify/config.json` is expired and
OVERRIDES `NETLIFY_AUTH_TOKEN` + `--auth` + `--site` → every `netlify deploy`
returns "Unauthorized"/"session has expired" even with a VALID token (confirm
token is fine via `curl -H "Authorization: Bearer <tok>" api.netlify.com/api/v1/user`
→ 200). `netlify login` is interactive browser OAuth — can't do headless.

PREFERRED DEPLOY: the file-digest REST API (bypasses the CLI entirely). Script at
`scripts/netlify_api_deploy.py` (this skill). Flow: POST /sites/{id}/deploys with
{files:{"/path":sha1}} → PUT each `required` sha's bytes → poll state=ready.
site_id array-operator-ea = `966cb1f5-944e-41fd-855b-10053edc5d18`; token at
`~/.hermes/secrets/netlify_token`.

SECRET-MASKER TRAP: the shell secret-masker mangles inline `$(cat token)` /
`$TOK` / `Bearer $TOK` (replaces with `***`, breaks quoting). Read the secret
INSIDE a python script, OR write token+cmd into a `.sh` file and `bash` it (the
masker garbles the tool-call ECHO, not the written file bytes — verify with
read_file then run). For curl headers, use `curl -H @headerfile`.

## 6b. AO frontend↔backend contract bugs (recurring class — check the SHAPE)

Several "Couldn't save / Couldn't recognize" errors this session were all the
same root cause: the JS sent/read a different key shape than the API. Always
diff the JS body/response-read against the Pydantic model / route shape.

- **Master Account company-name 422**: JS posted `{company_name: val}` to
  `/v1/account/company-name`, but `UpdateCompanyName` expects `{name}`. Key
  mismatch → Pydantic 422 "Couldn't save". Fix = `{ name: val }`. (Email path
  `{email}` already matched.) Verify the fix against the REAL backend, not just
  by reading code: old shape → 422, new shape → 200/401 (auth-gated = body
  parsed OK). NOTE a `.test`/reserved TLD email also 422s on EmailStr — use a
  `.com` when testing so you don't misdiagnose a working path as broken.
- **Wizard spreadsheet "Couldn't recognize that workbook"**: `/match` nests its
  result under a `match` key — `{ok, filename, match:{matched, customer, ...}}`.
  My wizard upload read `m.matched`/`m.customer` off the TOP level (undefined →
  always "not recognized"). Fix = `const m = mdata.match;` then `m.matched`
  (same as the tab's `matchFile`). When you copy a flow, copy how it READS the
  response too, not just the request.

## 6c. "Default ON" for an existing boolean toggle — gated one-time migration

Ford: "X should be on by default, remove the manual button below it." Doing
"default ON" right for an EXISTING column means three things, not one:
1. **Model**: `default=True, server_default="true"` (new rows).
2. **Frontend display default**: read `x !== false` (treat missing/old-false as
   on for the toggle's checked state) — but the real behavior is driven by the DB.
3. **Flip existing rows ONCE without clobbering deliberate opt-outs.** Do NOT run
   a bare `UPDATE ... SET x=true WHERE x=false` every migrate — it re-fires on
   every deploy and undoes anyone who later toggled it off. GATE it on the
   column's current default (Postgres `information_schema.columns.column_default`):
   only when the default is still the OLD `false`, flip the rows AND
   `ALTER COLUMN x SET DEFAULT true`. After that runs once, the default is `true`
   so the gate never fires again. Verify on prod by INSPECTING the live DB
   (`column_default` + a `GROUP BY x count(*)`), not the migrate log — the flip
   may have landed on an earlier deploy tick and correctly skipped re-running.
   When removing the paired manual UI button, also delete its event wiring + the
   now-unused handler fn, and update intro copy that referenced the manual step.

## 7. Shared-tree commit hygiene (sibling agents editing same files)

solar-operator is edited by sibling subagents concurrently. Before commit:
`git status --short | grep -v '^??'` to confirm ONLY your files are dirty; stage
explicit paths (never `git add -A`). Pre-existing test failures (e.g. a Chint
per-inverter live-power test) are NOT yours — prove by stashing your files and
re-running on clean HEAD. New test files need an autouse cleanup fixture that
deletes seeded rows (shared sqlite leaks rows across test files → spurious
`scalar_one()` failures in other agents' tests).
