# Parallel CC-agent feature builds in this repo (survey-first + integration seams)

When Ford hands a multi-feature request ("build all these", often from a meeting
transcript like Paul's onboarding summary) and says "use cc agents", the failure
mode is NOT the agents — it's launching them before grounding. Hard-won rhythm:

## 1. SURVEY THE CODEBASE BEFORE DELEGATING ANYTHING (the biggest lever)
Most of what a customer "asks for" in this product is ALREADY BUILT. Real case
(Jun 2026, Paul's asks): the request looked like 3 net-new features —
percentage-allocation invoicing, AI template reproduction, YoY/trailing-12-month
reporting. Grounding the code first revealed:
- `api/billing/` already had the WHOLE invoicing engine: `matcher.py`
  (AI-parses a customer .xlsx → `allocation_pct` + `billing_model`
  percent_of_array/fixed_budget/flat_rate), `invoice.py` (PDF+XLSX mirroring the
  workbook's Template sheet), `delivery.py` (attachments + the
  to_me/to_client/to_both "drafted email for approval" slider + scheduling),
  `summary.py` (already emits `yoy_delta_kwh/pct`, `ttm_kwh`, `ttm_points`).
- `BillingReportSubscription` model already stores the uploaded workbook bytes,
  `parsed_map`, cadence, formats. The 95%/5% landowner split = arbitrary
  `allocation_pct` already supported.
So ~90% of the ask was live. Firing 3 agents to "build all these" would have
REBUILT live billing code and produced duplicate/conflicting modules + wasted
~$15-40 of agent spend. PROCESS: grep the repo for the feature's nouns
(`allocation|percent|invoice|yoy|trailing|trend`) and READ the hits BEFORE
writing any contract or launching any agent. Report the gap-analysis to Ford and
scope DOWN to the genuine net-new gaps. He values this honesty over volume.
The real gaps here were small: one net-new UI (macro multi-year trend-line tab)
+ two extensions (seasonal multi-year YoY, a dormant GMP-invoice-PDF attach hook
that lights up when Paul's own GMP-detection backend feeds it).

## 2. Worktree + contracts mechanics that worked
- `git worktree add -b feat/<name> /tmp/wt-<name> origin/main` (branch off the
  EXPLICIT `origin/main` ref, never local HEAD).
- Backend agent needs pytest deps: `ln -s /root/solar-operator/.venv
  /tmp/wt-backend/.venv` (reuse the main checkout's venv — same python/deps).
- Frontend agent: `ln -s /root/solar-operator/web/app/node_modules
  /tmp/wt-frontend/web/app/node_modules` (skip a fresh npm ci).
- Split agents on DISJOINT file sets: BACKEND owns `api/**`, FRONTEND owns
  `web/app/src/**` ONLY. They merged with ZERO conflicts because of this.
- Author `CONTRACTS.md` yourself first pinning the exact endpoint JSON shape; tell
  the frontend agent to consume it DEFENSIVELY (read response fields tolerantly)
  since the backend PR isn't merged when it builds.
- Launch: `claude -p "...read TASK.md+CONTRACTS.md...open PR...print URL" \
  --model opus --fallback-model sonnet --allowedTools "Read,Edit,Write,Bash,Glob,Grep"
  --permission-mode acceptEdits --max-turns 120 --output-format json` via
  terminal(background=true, notify_on_complete=true). Do NOT shell-wrap with
  nohup/disown/& at the tool level — Hermes rejects shell-level backgrounding; use the
  tool's background flag so lifecycle is tracked. To run N agents in parallel under ONE
  tracked process, write a launcher .sh that backgrounds each `claude -p` with `&`,
  `wait`s on all, then prints each `_out.json` subtype/turns/cost; run THAT via
  terminal(background, notify_on_complete).
  ALWAYS `--fallback-model sonnet` (opus dies on Overloaded mid-build). One liveness
  check at ~25-60s (procs alive + clean stderr) to catch dead-on-launch early.
- **RUNNING AS ROOT? `--dangerously-skip-permissions` IS REJECTED** — claude exits
  instantly with "cannot be used with root/sudo privileges for security reasons" (this
  killed a whole launch cycle Jun 2026; the dead agents wrote empty `_out.json` → "NO
  JSON" in the launcher summary = dead-on-launch, NOT a real run). FIX: grant tools
  explicitly instead — `--allowedTools "Read,Edit,Write,Bash,Glob,Grep" --permission-mode
  acceptEdits`. Print mode (`-p`) already skips the trust dialog, so the bypass flag is
  unnecessary anyway. `whoami` before launching to anticipate this.
- **Stale completion-notification trap:** if a first launch dies and you kill it +
  relaunch, the killed run can STILL fire its own notify_on_complete later. When a
  completion alert arrives, match its session_id/pid against the LIVE run before acting —
  a corpse reporting "NO JSON" / the failed command is not your real batch finishing.

## 3. THE CROSS-AGENT INTEGRATION SEAM ALWAYS NEEDS RECONCILING (don't trust "success")
Both agents self-reported `subtype:success`. The frontend agent LOUDLY flagged
(in its result) the seam: it linked trends by **client id** (the reports UI is
client-centric — `ClientRow`, keyed by `client.id`), but the backend
`/subscriptions/{id}/trends` resolved a `BillingReportSubscription.id`. Those are
DIFFERENT ids → feature broken end-to-end. This is the seam the orchestration
discipline warns about — neither agent owns it; the integrator does.
FIX PATTERN (reusable): make the endpoint accept EITHER id. Added
`_resolve_trends_target(db, tenant_id, ident)`: try sub-id first, else treat
`ident` as a Client.id → that client's newest non-deleted subscription; a VALID
client with no workbook yet returns the honest EMPTY state (200), not 404; only
an id matching neither sub nor client → 404. Reports UI in this repo is
client-centric throughout, so client-keyed routing + graceful-empty is the right
default for any new per-customer report surface.

## 4. VERIFY WITH YOUR OWN EYES, then integrate
- Read each agent's `_out.json` result, then RUN the proof yourself: backend
  `.venv/bin/python -m pytest tests/ -q` (full suite — this repo runs ~960 green;
  don't trust the agent's "tests pass"), frontend `cd web/app && npm run build`.
- Merge `--no-ff` each branch into main; disjoint files → clean.
- Add an integration test for the reconciled seam (client-id resolution,
  empty-client 200, unknown-id 404).
- Rebuild the served bundle: `bash build_app.sh` refreshes `api/app_dist/` from
  `web/app/dist/` (the backend serves app_dist, NOT web/app/dist — a stale
  app_dist ships the old UI even after a good frontend build).
- Migration: additive nullable column → `create_all` covers FRESH DBs, but an
  EXISTING table needs the explicit `column_exists()`-guarded `ALTER TABLE ... ADD
  COLUMN` block in `api/migrate.py` `main()` (BYTEA type name is fine on sqlite
  too). Prove on a CLEAN temp DB; a polluted local `storage/solar.db` can crash
  migrate `main()` on an UNRELATED legacy NOT-NULL backfill — that's a dev-DB
  artifact, not a prod issue (prod is clean Postgres). `mv storage/solar.db
  storage/solar.db.bak` to get a fresh dev DB for UI screenshots.

## 5. Test-isolation flakes this repo has
- Tests share an in-memory/file DB with no per-test truncation; some older tests
  do `select(BillingReportSubscription).scalar_one()` assuming exactly ONE row in
  the whole DB. New tests that leave rows can break those IN ARTIFICIAL REVERSE
  ORDER — but `pytest-randomly` is NOT installed, so deterministic file order
  (alphabetical) holds and CI is safe. Verify a suspected isolation break by
  running the two files in normal order before "fixing" the other test.
- UTC/local midnight flake: the capture handler stamps today's row by
  `now().date()` (UTC). A test using `datetime.date.today()` (local) queries the
  wrong day across the UTC/local boundary and fails `95.0 == 120.0`. Tests that
  assert on "today" MUST use `from api.models import now; now().date()`.

## 6. WHICH FRONTEND does an owner-facing feature belong in? (Jun 2026 — I got this wrong once)
Two owner-facing frontends, NOT the same product:
- NEPOOL Operator = `solar-operator/web/app` (React/Vite, basename `/accounts`). Its Arrays tab is
  RETIRED (redirects to Clients). For NEPOOL agents/verifiers.
- Array Operator = `/root/array-operator/public` (vanilla JS, dark solarpunk, hash-routed tabs via
  sandbox.js `TABS` map). What OWNERS (Paul/Bruce) actually use. Top nav:
  Master Account · Arrays · Claims · Reports.
I first built Paul's trends view in the NEPOOL React app — the WRONG product he never sees. The tell:
Ford said "make it a tab next to Arrays," but that app has NO Arrays tab. When an owner-facing request
mentions Arrays/Claims, it's the Array Operator site. CONFIRM which frontend before building.
Adding a tab to the Array Operator site (the SHIPPED Trends tab is the template):
1. `<a id="tabX" href="#x">` in index.html tabbar + a `<section id="panelX" class="panel">`.
2. add `x:{panel,tab}` to sandbox.js `TABS`, a branch in `tabFromHash()`, a loader in `applyView()`.
3. self-contained `x.js` exposing `window.__aoLoadX` (auth via `localStorage.so_session`).
4. `.x-*` CSS in command-center.css using `--ink/--good/--good2/--muted/--faint/--line/--bad` tokens.
5. `<script src="x.js">` in index.html.
DEPLOY IS MANUAL: `netlify deploy --prod --dir=public` from /root/array-operator (git push only
updates GitHub; live stays stale).
SHIPPED Trends tab = portfolio-wide multi-year. Backend `GET /v1/array-owners/fleet-trends` sums
DailyGeneration across ALL the tenant's arrays by (year,month) → monthly_by_year + seasonal_yoy +
ttm/lifetime + by_array. VISUAL-QA PITFALL fixed: the headline YoY must compare ONLY months present
in BOTH years — a partial current year (Jan–Jun) vs a full prior year reads a scary false -47%;
compare overlapping months for an honest +5.6%.
LOCAL visual-QA of the Array Operator site (it proxies same-origin /v1/* → Railway via _redirects):
boot uvicorn + a tiny Python http.server that forwards /v1/* to it, seed multi-array/multi-year
DailyGeneration, Playwright localStorage.setItem('so_session', <mint_session_for_tenant>) then goto
/#trends, screenshot + vision_analyze desktop AND mobile.

## 6b. "Make all four / go crazy" — fan agents at ONE frontend via a view-registry keystone (Jun 2026)
When Ford loves multiple design concepts and says "make all of them, spawn a CC agent for
each" (e.g. the Trends tab's 4 animated chart concepts), the parallel-agent disjoint-files
rule still applies — but here all N features live in the SAME vanilla-JS frontend
(`/root/array-operator/public`). The pattern that made 4 agents merge with only a trivial
conflict ("spider mode": build the keystone yourself, fan the dependent builds):
- **I (orchestrator) build the KEYSTONE first, commit it, branch agents off that commit.**
  Keystone = the seam every concept shares so they physically can't collide:
  - `trends-core.js` — shared helpers + brand tokens (read live from styles.css `:root`)
    + a responsive hi-DPI auto-animating `<canvas>` helper (`createCanvas(container,{aspect})`
    → `.start(draw)`/`.stop()`, auto-stops when detached from DOM) + a VIEW REGISTRY
    (`AOTrends.registerView(key,{label,badge,order,describe,mount(container,prepped,core)})`).
  - `trends.js` orchestrator — fetch + stat band + a segmented switcher + mount/unmount the
    active view (persist choice in localStorage) + the shared table. Knows NOTHING about how
    any chart draws.
  - Each concept = ONE self-contained `trends-view-<key>.js` that calls `registerView`.
    ONE AGENT OWNS ONE VIEW FILE. That single-file ownership is what prevents JS conflicts.
- **CSS rule that prevents 90% of conflicts:** each agent may only APPEND to `trends.css`,
  prefixed `.trv-<key>-*`. They still ALL append to the same end-of-file region, so the
  ONE predictable merge conflict is the CSS-append block (the JS files merge clean).
  RESOLUTION: don't hand-merge — rebuild trends.css = keystone base + concat each branch's
  diff-added lines (`git diff HEAD~1 HEAD -- public/trends.css | grep '^+'`), then `git
  checkout trends-<key> -- public/trends-view-<key>.js` for each clean view file.
- **Ship a standalone harness for the agents to self-test** (`trends-concepts-live.html`):
  loads the REAL core + all view files against a MOCK `/fleet-trends` payload (stub
  `window.fetch` + `localStorage.so_session`), with dataset TOGGLES for edge cases (3 years
  / single year / 2 thin months / down-month gap). Tell agents to test their view on ALL
  datasets + at narrow width. They can run their own throwaway server on a HIGH port — tell
  them explicitly NOT to touch the orchestrator's preview port (8899) or any running proc.
- **Per-agent TASK.md asks for the same production-hardening every view needs:** hover
  tooltip (one `.tr-tip` div in the position:relative container, removed in cleanup),
  responsive 360→1200px, `prefers-reduced-motion` → calm static frame, edge-case
  rendering, brand polish. Restraint > spectacle (Ford's taste rule) — literal, calm, on-brand.
- **VERIFY before integrating (don't trust subtype:success):** confirm each agent touched
  ONLY its own view file + scoped CSS (`git show --stat`), `node --check` every JS file,
  `grep -oE '\.trv-(key)-[a-z-]+'` to confirm CSS is scoped not clobbering shared rules,
  then merge `--no-ff` onto an integration branch off the keystone commit, then re-QA every
  view with Playwright hover + vision on all edge-case datasets BEFORE showing Ford.
  Cost: 4 opus agents ≈ $5.40 here. Deploy stays MANUAL + gated on Ford's eyeball.

## 7. Reports DRAFT→APPROVE→SEND inbox (Paul's full transcript, Jun 2026)
The SUMMARY undersold it; the full transcript (`Paul_s Onboarding Transcript.txt`) spelled out his
real workflow: GMP invoice arrives → pull TOTAL array generation → split by each customer's FIXED %
(Danville 95% customer / 5% landowner) → DRAFT an email (customer invoice PDF + the GMP utility
invoice PDF) → "I go over it and approve it or modify it and then send" (NOT auto-send) → save a
record. The old Reports tab was upload→schedule→auto-send — missing the human approval gate.
SHIPPED (backend `api/billing/routes.py` + `ReportDraft` model; frontend `array-operator/public/
reports.js` approval inbox):
- `ReportDraft` table (pending→sent/dismissed) snapshots period + array_total_kwh + allocation_pct +
  customer_kwh + amount. New table → `create_all` makes it on prod automatically (no ALTER needed).
- Endpoints under `/v1/array-operator/billing`: POST `/subscriptions/{id}/draft` (idempotent per
  invoice period — Paul's GMP-detection backend, which HE builds, calls this), GET `/drafts`
  (inbox), POST `/drafts/{id}/gmp-invoice` (attach utility PDF, validate head==b"%PDF"), PATCH
  `/drafts/{id}`, POST `/drafts/{id}/approve` (the human gate — copies the draft's GMP PDF onto the
  sub then calls existing `deliver_subscription`, so the GMP PDF rides the email via the
  generate_files attach hook already in place), POST `/drafts/{id}/dismiss`.
- KEY DERIVATION: the per-period ARRAY TOTAL isn't in `match.computed_invoice` (only the customer's
  share `kwh` + `allocation_pct`). Derive array_total = customer_kwh / allocation_pct — exactly
  Paul's "total array output × the customer's percentage" mental model. (project_totals has LIFETIME
  totals, not per-period.)
- This was a COHERENT backend+frontend slice (the draft-state seam is shared), so I built it
  DIRECTLY, NOT via parallel agents — splitting it would have fractured the seam. Use agents for
  disjoint feature sets; build shared-seam slices yourself.
- PER-CUSTOMER DELIVERY MODE (follow-up, Jun 2026): Paul wanted scheduled reports to NOT auto-send —
  land in his inbox to review/edit/send — but BOTH options available per customer. Added
  `BillingReportSubscription.delivery_mode` ("approval" DEFAULT | "auto"; migrate ALTER + create/patch
  + _sub_dict). `billing/delivery.draft_subscription()` creates the pending draft, stamps
  next_send_at forward (idempotent per period), and emails the OPERATOR a "Ready to review" note
  (NOT the customer). `scheduler.deliver_billing_reports` branches on delivery_mode: approval→draft,
  auto→deliver_subscription (old behavior). Frontend: a Draft/Auto toggle on the schedule form + each
  customer row (data-act="delivery" → PATCH). PITFALL: changing the scheduler DEFAULT broke an
  existing test (`test_scheduler_monthly_billing_delivers` asserted auto-send) — update such tests to
  opt into delivery_mode="auto" explicitly. Scheduler tests that count emails are fragile because the
  session DB accumulates subs from other tests (9+ fired) — assert on YOUR sub id / YOUR customer
  email, never global counts.
