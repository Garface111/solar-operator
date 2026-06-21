# Array Operator + NEPOOL Operator — Claude Code Handoff

> Written by Hermes on 2026-06-21 for the transition to Claude Code (CC) as the
> primary operator of these two products. This file is the single source of truth
> for what you need to manage and continue both projects. Read it fully before
> touching prod.

Owner: **Ford Genereaux** (Garface111 on GitHub, ford.genereaux@gmail.com).
His dad **Bruce Genereaux** (bruce.genereaux@gmail.com) is the live NEPOOL pilot —
real production data, deletion-safety care.

---

## 0. THE TWO PRODUCTS (one shared backend)

Both products run off ONE FastAPI backend + ONE Postgres DB on Railway
(project **Solar-Operator**, `7451f2d4-6d29-41de-b8f4-a7461052a578`). They are
isolated by `product` field, not by separate infra.

| | NEPOOL Operator | Array Operator (AO) |
|---|---|---|
| Who | VT community-solar operators (Bruce) | Solar array OWNERS (fleet owners) |
| Does | Quarterly net-metering credit reports (GMCS Excel) | Owner dashboard: live inverter feeds, trends, offtaker billing reports |
| Frontend | `solar-operator/web/app` (React SPA, no inverter buttons) | `/root/array-operator` (separate repo, Netlify) — "Log in with <vendor>" buttons + canvas |
| Domain | solaroperator.org | (AO owner site on Netlify, served via landing proxy) |
| Backend | shared `/root/solar-operator/api` | same |

**Critical:** the two frontends are TWO repos. When debugging one, `git log` the
other too. Product isolation is STRICT — Ford has corrected "appears in both" /
"show-all fallback" multiple times. One bucket per item, clean either/or.

---

## 1. REPOS & PATHS

| Path | Repo | Deploys to |
|---|---|---|
| `/root/solar-operator` | github.com/Garface111/solar-operator | Railway (push to main auto-deploys, ~70s) |
| `/root/array-operator` | github.com/Garface111/array-operator | Netlify site `966cb1f5-944e-41fd-855b-10053edc5d18` |
| `/root/vt-solar-intel` | (settlement-auditing venture, separate) | — |

Both git remotes use HTTPS via `gh auth git-credential` (already configured in
`~/.gitconfig`). `gh` is logged in as Garface111 (scopes: gist, read:org, repo).

`git push` on solar-operator → Railway auto-deploys backend.
On array-operator → deploy with `cd /root/array-operator && netlify deploy --prod`
(or build+deploy per its README/netlify.toml).

---

## 2. SECRETS — WHERE THEY LIVE

### A. Production runtime secrets = Railway env (source of truth)
Link once, then read:
```
cd /root/solar-operator && railway link   # project Solar-Operator if not linked
railway variables                          # full list with values
```
Key env var NAMES currently set on prod (read live VALUES with `railway variables`
— values are intentionally NOT committed to this repo):
- `DATABASE_URL` — Postgres (Railway internal)
- `STRIPE_SECRET_KEY` — LIVE Stripe (sk_live_)
- `STRIPE_PUBLISHABLE_KEY` — note: publishable is test-keyed (pk_test_)
- `STRIPE_WEBHOOK_SECRET` — whsec_
- `STRIPE_AO_KWH_PRICE_ID` ← **AO active price** (NOT the legacy `STRIPE_ARRAY_PRICE_ID`)
- `STRIPE_ARRAY_PRICE_ID` (legacy)
- `STRIPE_SETUP_PRICE_ID`
- `SESSION_SECRET`
- `SENTRY_DSN` — Sentry project `o4511571621707776` / `4511571625639936`
- `RESEND_API_KEY` — FROM: admin@solaroperator.org
- `ANTHROPIC_API_KEY` (for in-product AI)

### B. Local secrets on disk (`~/.hermes/secrets/`, chmod 600 — read the file, don't echo)
- `sentry_auth_token` — Sentry READ token (for the auto-fix cron + manual issue triage)
- `netlify_token` — Netlify deploy/CLI token
- `firecrawl_api_key`, `google_places_key`, `resend_full_key` (mostly Master Control, not AO/NEPOOL)

> The full plaintext values for everything above are in the local Desktop copy of
> this handoff (`/mnt/c/Users/fordg/Desktop/AO-NEPOOL-CC-HANDOFF.md`) and in
> `railway variables` / `~/.hermes/secrets/`. They are deliberately kept OUT of git
> (GitHub push protection blocks committed secrets, and it's correct to).

### C. CLI auth already on this machine
- `railway whoami` → Ford Genereaux (token in `~/.railway/config.json`)
- `gh auth status` → Garface111
- `netlify` CLI uses `netlify_token` above

> SECURITY NOTE for Ford: the live Stripe secret key, session secret, and webhook
> secret are now printed (partially) in this transcript and fully readable via
> `railway variables`. Consider rotating `STRIPE_WEBHOOK_SECRET` and
> `SESSION_SECRET` after the handoff if you want a clean break.

---

## 3. DEPLOY LOOP (memorize — this is where things bite)

### Backend (solar-operator → Railway)
1. Stage ONLY your hunks. **NEVER `git add -A`** — it's a shared tree with other agents' work.
2. `git push origin HEAD:main` → Railway auto-deploys (~70s).
3. If you added a DB column the ORM SELECTs: endpoints **500 until you migrate**.
   ORDER: push → WAIT for deploy → `railway ssh "cd /app && python -m api.migrate"`
   → VERIFY the column exists (migrate LOG runs OLD code and can no-op — NOT proof;
   confirm via `railway ssh` get_columns) → confirm route returns 401 not 500.
4. NOT-NULL columns need DEFAULT + NOT NULL or the migration fails on existing rows.

### Frontend NEPOOL (solar-operator/web/app)
React SPA. Build + deploy per repo scripts. Watch for **stale SPA shell-cache** —
if a fix "doesn't work" but your automated test passes against the live deploy,
tell Ford to HARD REFRESH (Cmd/Ctrl+Shift+R) FIRST.

### Frontend AO (array-operator → Netlify)
`netlify deploy --prod` from `/root/array-operator`. The landing `netlify.toml`
proxies `/v1`, `/admin/api`, `/onboard`, `/health` → Railway and SPA-routes `/app/*`.

### Local QA gotchas
- `uvicorn` WITHOUT `--reload` serves STALE code.
- Dev OOM (exit 137) surfaces in UI as "Session expired" — check `/health` first.
- A 401 on a noAuth request = BAD CREDENTIALS, not session expiry.

---

## 4. DATA MODEL (the spine)

`Tenant → Client → Array → UtilityAccount`. Plus `Bill`, `DailyGeneration`,
`BillingReportSubscription`, `Offtaker`.

- **NEPOOL pilot:** Bruce = tenant `ten_14b76982523a3b47` — 7 arrays, 9 GMP accounts, comped.
  Starlake array sums 3 sub-meters, `bill_offset_months=0` (same-month; others prior-month).
- **AO demo tenant:** `ten_a554c8e7a08f8cfa` (Ford's ford.genereaux login) — SolarEdge-only,
  0 GMP. Recurring test-fixture array names: Londonderry, Cover Catamount Building,
  Starlake (+ Starlake North/South/Center sub-meters), Tannery Brook, Timberworks,
  Waterford, Chester. GMP imports arrive prefixed "1a_"/"1b_" (positional code,
  stripped by `api/adapters/_gmp_clean.py`). These sub-meters are REAL distinct
  arrays, never dupes.
- Ford has **multiple tenants per email** (nepool + array_operator on
  ford.genereaux@gmail.com). Always diagnose the EXACT product tenant.

### GMCS Excel writer (NEPOOL — DO NOT BREAK)
`api/writers/gmcs_writer.py` pixel-matches Bruce's GMCS.xlsx. Rules in
`solar-operator/CLAUDE.md` §"GMCS writer format rules". Footnotes VERBATIM.
RECs = `int(mwh)` floor. One sheet per array. Rolling 6 quarters.

### Billing / rate model (AO)
Invoice = `kWh × net_rate × (1 − discount)`. Default 10% off, editable global
(`Tenant.default_net_rate_per_kwh` + `default_discount_pct`) and per-customer
(`BillingReportSubscription.discount_pct` + `net_rate_per_kwh`). Resolver:
`delivery.resolve_discount_pricing`. **Ford's HARD rule: never fabricate a rate** —
derive defaults from captured data (e.g. blended rate from 27k captured GMP bills),
skip cells with too few samples rather than guess.

### Offtaker reports (AO)
Bill EXCLUSIVELY from utility PAPER BILLS (`Bill.kwh_generated`) — never vendor/
inverter telemetry, never GMP hourly. NO fallback. Offtaker→GMP-bill picker lists
`UtilityAccount`s; `_persist_meter_accounts` upserts both UtilityAccount + Bill.

---

## 5. INVERTER / UTILITY VENDORS (the "whack-a-mole" surface)

Ford's ethos: **"not complete until we support ALL vendors people have."** Adding
vendors is critical-path.

- **SolarEdge** — server-side pollable. `/overview` `currentPower` LAGS; an old
  `lastUpdateTime` = SOURCE outage (vendor lost feed) → show ⚠ SOURCE OFFLINE,
  not our bug.
- **SMA, Fronius, Chint** — 0 pullable creds (no dev-app registration). The
  Chrome extension's hourly silent recapture is their ONLY live source. Server
  poller CANNOT refresh them.
- **SmartHub / VEC / WEC bills** — `totalUsage` = CONSUMPTION, not generation.
  Route to `kwh_consumed`, NEVER `kwh_generated`. Generation lives only in the
  usage API as negative-y net-export → client-side daily pull in extension
  `smarthub_content.js` → POST to `/v1/array-owners/utility-meter-capture`.

When a vendor feed breaks, Ford wants the **mole-CLASS killed** (durable fix),
not just the symptom. Trace capture → DB → endpoint → fleet-store → render.

### Chrome extension (`solar-operator/extension`)
MV3. Currently ~v1.0.1 pending Chrome Store push. It's the live-feed lifeline for
the un-pollable vendors. Build/release + email-Bruce flow documented in skill refs
(see §7). Sub-meter capture must build its name-map from ALL arrays incl.
soft-deleted + revive-on-reuse, else `uq_array_per_tenant` UniqueViolation → 500
("couldn't grab your GMP account").

---

## 6. MONITORING & CRON

- **Sentry auto-fix cron** (`f1c8dc829a25`, hourly) runs
  `/root/solar-operator/scripts/sentry_autofix_tick.sh` — SAFE mode (opens PRs only,
  no auto-merge). Uses the Sentry read token above. This is the one AO/NEPOOL cron;
  the other 9 Hermes crons are Master Control (a different product — leave them).
- Sentry project DSN above; READ issues via the auth token.
- `git push` to solar-operator main is the deploy trigger; watch Railway logs:
  `railway logs`.

> If you (CC) take over the Sentry auto-fix loop, you can drop the Hermes cron and
> run the repo's `scripts/sentry_autofix_tick.sh` from your own scheduler. It's
> self-contained and versioned in-repo.

---

## 7. DEEP KNOWLEDGE — the skill reference library

Hermes accumulated ~100 dense reference docs at:
`~/.hermes/skills/projects/solar-operator-energyagent/references/`

These are GOLD — each is a battle-tested writeup of a specific subsystem/bug-class.
You don't need to read them all up front, but grep them when you hit a topic.
The highest-value ones:

- `repo-topology.md` — full repo/dir map of both products
- `reports-billing-build-and-rate-model.md` — billing/rate model + ship-loop traps
- `backend-safe-edit-and-cron-commit.md` — shared-tree edit + deploy safety
- `offtaker-reports-and-sandbox-source-routing.md` — offtaker billing + strict source routing
- `two-product-isolation-and-spa-cache.md` — product isolation + SPA cache + 401 semantics
- `smarthub-vec-generation-pipeline-and-empty-report-skip.md` — SmartHub/VEC consumption-vs-generation
- `live-inverter-feed-debugging.md` + `live-inverter-feeds-whack-a-mole.md` — vendor feed root-causing
- `inverter-array-grouping-persistence.md` — array dedup / soft-delete revive (the 500 trap)
- `trends-analytics-and-multiyear-data-propagation.md` — Trends surface + multi-year backfill
- `adding-a-new-inverter-vendor.md` — the add-a-vendor playbook
- `prod-db-reconciliation-and-railway-ssh.md` — safe prod DB inspection via railway ssh
- `gmp-meter-api-contract.md`, `chint-portal-api-contract.md`, `alsoenergy-api-contract.md` — vendor API contracts

> Suggestion: `cp -r` that references/ dir into the repo (e.g.
> `solar-operator/docs/knowledge/`) so it lives with the code and you have it
> without Hermes. They're plain markdown.

---

## 8. WORKING STYLE FORD EXPECTS (carry this over)

- **Deletion-safety > obedience.** On any "just delete X" (even "delete all" /
  "override and approve"), REFUSE blind irreversible deletes until the exact target
  is confirmed via DRY-RUN. Twice "delete dad's account" turned out to be Bruce's
  LIVE prod. Map owners by `tenants.contact_email`, show data day-counts BEFORE
  deleting. CONSOLIDATE over delete. Soft > hard.
- **Never fabricate.** No fake data into a real user's account. Name the gap +
  safe options. Drop a feature honestly rather than build it on a guess.
- **Visual QA every UI change:** screenshot (Playwright) + look at it; fix
  clipping/overflow before calling done. Show UI via `localhost http.server`,
  never `file://`.
- **Commit AND push** — "done" = committed + pushed + deployed so Ford can test
  live. Never leave changes staged-but-unpushed.
- **Root-cause through the whole chain** (capture→DB→endpoint→store→render), not
  the symptom. When he says "get to the bottom," he means it.
- **Unit economics first.** Surface cost/margin tradeoffs BEFORE building.
- **He localizes bugs by comparison** (same-vendor/diff-array, same-array/diff-account).
  Mid-stream "stop"/"wait" reframes the diagnosis — re-anchor on his latest framing.
- **Stale-cache first** when a deployed fix "still doesn't work" but tests pass.
- **Any local artifact Ford grabs → `/mnt/c/Users/fordg/Desktop/`, NEVER OneDrive.**

---

## 9. FIRST MOVES FOR CC

1. `cd /root/solar-operator && git pull && git log --oneline -10` (orient on latest state)
2. `cd /root/array-operator && git pull && git log --oneline -10`
3. `railway link` (Solar-Operator) → `railway variables` to confirm prod secrets
4. `railway logs` to see current backend health; check `/health`
5. Skim `solar-operator/CLAUDE.md` (already in-repo) + this file
6. Copy the references/ knowledge dir into the repo if you want it durable
7. Ask Ford what the active priority is — don't assume.
