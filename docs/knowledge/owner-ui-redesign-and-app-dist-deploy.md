# Owner-UI redesign method + the app_dist deploy trap (Jun 2026)

Two durable lessons from redesigning the Array Operator **Reports tab** for a real
owner-user (Paul Bozuwa). One is a deploy trap that cost a false "shipped"; one is
the design method Ford expects for owner-facing UI.

## 1. ⚠️ THE app_dist DEPLOY TRAP (lost real work to this — read first)

The account SPA (`web/app`, React, served at `/accounts/`) is served from a
**git-TRACKED prebuilt artifact `api/app_dist/`** — NOT from `web/app/dist`, and
Railway does **NOT** rebuild the SPA from source on deploy. `api/app.py` mounts
`/accounts` from `api/app_dist` (built by `build_app.sh`, committed into the repo).

CONSEQUENCE: committing only `web/app/src/*.tsx` ships the **OLD frontend** even
when the backend deployed, health is 200, and the full test suite is green. The
served JS is whatever `api/app_dist` contained at the last commit.

THE FIX (run before pushing ANY frontend change):
```
./build_app.sh          # cd web/app && npm ci && npm run build; rm -rf api/app_dist; cp -r web/app/dist api/app_dist
git add api/app_dist/    # the regenerated tracked artifact (git add -f if needed; assets are gitignored but the tracked set updates)
git commit && git push origin HEAD:main
```
`build_app.sh`'s own header says "Run before every commit that touches web/app/." Heed it.

VERIFY LIVE — do NOT trust the bundle hash or "tests pass". Grep the SERVED bundle
for a unique new string:
```
curl -s https://web-production-49c83.up.railway.app/accounts/ | grep -oE 'index-[A-Za-z0-9]+\.js'   # served hash
curl -s https://web-production-49c83.up.railway.app/accounts/assets/<YourChunk>-<hash>.js | grep -oE "Add customer|<your new string>"
```
If your new string isn't in the live bundle, app_dist wasn't rebuilt/committed. This
is the ONLY reliable proof the frontend shipped.

Backend note (unrelated but same session): new billing columns need the prod
migration after deploy — `railway ssh "cd /app && python -m api.migrate"` — though
the app also migrates on boot, so verify the columns exist rather than trusting the
ssh exit (the ssh channel can time out AFTER the migration ran).

## 2. OWNER-UI REDESIGN METHOD (what Ford expects for owner-facing UI)

Trigger: Ford says a UI is "far from ready to ship", "redesign for a user like X",
"really research UX", "most control with the least clicks". This is NOT a request
for "a card with form fields" — that reading got rejected this session. He wants
genuine product design.

The method that worked:
1. **Job-first, not feature-first.** Find the user's REAL job before drawing. For
   Paul: he's not filing NEPOOL quarterly reports (the old tab's mental model) — he
   invoices 4 customers each billing period: GMP posts generation → apply each
   customer's fixed % split (e.g. Danville 95% customer / 5% landowner) → customer
   invoice PDF + GMP PDF → review/edit a drafted email → approve & SEND (never
   auto-send) → every run saved as an auditable, durable record. Source his real
   asks: `docs/plans/2026-06-17-paul-bozuwa-followups.md` captures the demo call.
2. **Build 2–3 GENUINELY different stances** (use the `sketch` skill), not pixel
   variants. Here: period-centric "Billing Run", customer-centric "Customer Ledger",
   action-first "Command Bar". Different organizing units, not different accent colors.
3. **VERIFY EACH WITH YOUR OWN EYES.** Subagents that "verified via DOM assertions"
   are NOT enough — screenshot each variant (headless Playwright) and load the PNG
   with vision_analyze. Real layout bugs (washout, collapsed flex, contradictory
   states) only show visually. Ford does his own visual QA and expects you to too.
4. **Opinionated head-to-head**, tied to the user's job — recommend, don't just list.
   Paul's job is periodic → variant 1 spine; he also manages splits → fold in
   variant 2's inline add/edit. Picked = hybrid (period batch + "Manage customers"
   mode on one surface).
5. **HAND OFF AS A SPEC, do NOT fork the live component.** ReportsTab.tsx is shared
   across work streams; a parallel rebuild collides at merge. Wrote
   `sketches/reports-redesign/HANDOFF.md` mapping each UI element to the REAL backend
   (most endpoints already existed: subscriptions CRUD + manual-create + the
   draft→gmp-invoice→approve→dismiss inbox; helpers listBillingSubscriptions /
   createManualSubscription / listAllArrays). The redesign is mostly a FRONTEND
   RE-LAYOUT replacing NEPOOL-quarter scaffolding (QuarterCard / "ship status") with
   the billing-run layout. Per the sketch skill's mockup-to-production-handoff ref.
6. **Deliverables to BOTH desktops** for this user: `/mnt/c/Users/fordg/Desktop/` and
   `/mnt/c/Users/fordg/OneDrive/Desktop/` (+ keep working copies in `sketches/`).

Hard requirements that recur for Paul's billing UI: never auto-send (every path ends
at a human "Approve & send"); show the math inline always (gen × % = kWh × rate = $)
— auditability IS the product; remainder of an array's % = landowner; durability
(every sent run is a saved record, history reads from it, never recomputes); keep the
xlsx-upload path working alongside the manual add path.

Mockups live at `/root/solar-operator/sketches/reports-redesign/` (001-billing-run,
002-customer-ledger, 003-command-bar, 004-hybrid=picked) + HANDOFF.md.
