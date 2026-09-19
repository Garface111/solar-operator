# Independent audit handoff — Array Operator off-taker invoicing (Norwich Technologies)

**Written:** 2026-09-19, by the Claude session that did the first audit pass on 2026-09-18.
**For:** an independent agent (Codex) with no prior context.
**Owner:** Ford Genereaux (ford.genereaux@gmail.com). Ford directs and tests; he does
not run commands. Report to him in plain language; never hand him CLI steps.

## 1. The mission

Array Operator (arrayoperator.com) is about to take over monthly off-taker
invoicing for Norwich Technologies: roughly 250 off-takers (towns, fire
districts, businesses) who buy shares of community-solar arrays in Vermont.
Each month the platform reads the utility bill for each array, computes each
off-taker's share and dollar amount, emails them an invoice on Norwich's
letterhead with a Pay button, collects payment through Stripe into Norwich's
own account, and sends Norwich a monthly Excel summary.

Your job: **independently verify that this pipeline produces correct, honest,
non-duplicated invoices and correct money flows before the first real run.**
Do not trust anything in this document or in commit messages — verify against
the code, the tests, and (read-only) production. A wrong or duplicate invoice
to a real off-taker under Norwich's name would end the deal.

What a good outcome looks like: a findings report ranked by severity with
file:line, a concrete failure scenario with numbers, and a fix; plus a branch
with fixes and regression tests for what you found.

## 2. Where things live

| Thing | Location |
|---|---|
| Backend (FastAPI, SQLAlchemy, Postgres) | github.com/Garface111/solar-operator — `api/` |
| Off-taker billing code | `api/billing/` (delivery, invoice, payments, routes, roster_detector, monthly_report, trueup, reconcile_bills, invoice_ledger, qb_export) |
| Scheduler jobs | `api/scheduler.py` (`deliver_billing_reports`, `send_due_offtaker_reports`) |
| Stripe webhooks | `api/stripe_webhook.py` |
| Models / migrations | `api/models.py`, `api/migrate.py` (idempotent ALTERs; `python -m api.migrate`) |
| Frontend SPA (Netlify) | github.com/Garface111/array-operator — `public/reports.js` (Statements tab: off-taker invoices, rail, archive), `public/paid.html` |
| Tests | `tests/` — pytest (config in `pytest.ini`; `tests/conftest.py` builds a throwaway sqlite DB and unsets live keys) |
| Design notes | `docs/knowledge/*offtaker*`, `*billing*`, `*rates*`, `docs/plans/2026-07-13-offtaker-pay-links-v2.md`, `SPEC.md`, `CLAUDE.md` |
| Production | Railway project "Solar-Operator"; API at `https://web-production-49c83.up.railway.app`, proxied same-origin at `https://arrayoperator.com/v1/*` |
| Fleet coordination stash | `C:\Users\fordg\.claude\projects\C--Users-fordg-CC\memory\SHARED-BACKLOG.md` (WSL: `/mnt/c/Users/fordg/.claude/projects/C--Users-fordg-CC/memory/SHARED-BACKLOG.md`) — read the 2026-09-18 entries first; append your own findings there too |

Working copies on this machine: **use** `/root/solar-operator` in WSL (Ubuntu;
has `venv/` and the `railway` CLI logged in) or make a fresh worktree from
`origin/main`. **Do not use** `C:\Users\fordg\solar-operator` — it is hundreds
of commits stale and stuck mid-merge. Other agents work in this repo
concurrently: `git add` specific files, never `git add -A`; rebase before
pushing. Pushing to `main` auto-deploys to production on Railway.

The Norwich tenant on production is `ten_dbf34fca0cbe` (name "Norwich
Technologies", `product=array_operator`, `plan=comped`, contact
`ford.genereaux+norwich@gmail.com`). It currently has no arrays, no off-takers
and no Stripe Connect account; the roster and utility access are still
pending from Norwich. Ford holds the login link.

## 3. What was already found and changed (re-verify, do not re-discover)

Commits on `main` from the 2026-09-18 audit, in order:

- `22045f41` billing fixes: exactly-once guard now compares the billing
  PERIOD (`_period_guard_label`) instead of raw cycle-end dates (a host-bill
  fallback used to bill the same June twice); true-up send crashed after the
  email went out on an unbound `bcc` and re-sent on retry; true-up no longer
  overwrites `last_sent_period_end`; approve refuses when the amount moved
  since review (`expected_amount_usd`); roster import: a `Rate ($/kWh)`
  column was scored as an 18.4% DISCOUNT or silently dropped, rate now flows
  to the subscription, emails normalized/flagged, in-batch duplicates
  skipped, total footer ignored. Tests: `tests/test_audit_offtaker_fixes.py`.
- `6ca1dc72` durable pay links: invoices carry
  `/v1/array-operator/billing/pay/{token}`; the click mints/refreshes the
  Stripe Checkout Session (Stripe caps a Session at 24h while the invoice says
  "due within 28 days"). Webhooks added: `checkout.session.expired`,
  `checkout.session.async_payment_succeeded/failed`, `charge.refunded`.
  Tests: `tests/test_offtaker_pay_link_durable.py`.
- `638dd811` direct charges by default: the Session is created on the
  operator's connected account (`stripe_account=`), so the OPERATOR pays
  Stripe's processing fee and the platform receives only
  `application_fee_amount` (0.5%, `AO_OFFTAKER_FEE_BPS`). Per-tenant override
  `Tenant.offtaker_charge_model`; env `AO_OFFTAKER_CHARGE_MODEL`.
  `OfftakerPayment.stripe_account_id` records where each Session lives.
- `8422b3a2`, `ca9e93ef` payment-method pin (`AO_OFFTAKER_PAYMENT_METHODS=
  us_bank_account,card` on Railway) with fallback to automatic methods.
- `5ad53c8e` scheduler rolls back after a failed subscription; four stale
  tests repaired; conftest stubs the outbound-email DNS preflight.
- From another session the same day: `a6d71455`, `76fb317e`, `d020f2e0` —
  the monthly off-taker summary (`api/billing/monthly_report.py`, routes
  `/monthly-report/*`, scheduler job `send_due_offtaker_reports`).

Production state as of 2026-09-18 evening: migrations run (verified columns
`offtaker_payments.pay_token/checkout_expires_at/stripe_account_id`,
`tenants.offtaker_charge_model`, table `offtaker_monthly_reports`); Stripe
account webhook endpoint updated to 12 events; a Connect webhook endpoint
created; `STRIPE_CONNECT_WEBHOOK_SECRET` set on Railway. **Still open:** ACH
for connected accounts must be switched on by Ford in the Stripe Dashboard
(the API reports the setting is "not overridable"); until then direct-charge
checkouts fall back to card only.

## 4. Known open findings (unfixed) — confirm, rank, fix what you can

Invoice math (`api/billing/delivery.py`, `invoice.py`, `trueup.py`, `rate_schedule*.py`):

1. Annual true-up (`trueup.py` `_period_figures`) assumes `budget × months
   with a bill` was paid instead of what was actually sent; a quarterly-cadence
   budget offtaker gets a phantom credit (`pending_credit_usd`).
2. VEC/SmartHub off-takers (`_array_period_kwh`, `_build_smarthub_offtaker_match`)
   bill the latest month that has ANY daily rows — a partial month — and
   re-bill when the rest lands.
3. A draft for a non-newest period can never be approved: `deliver_subscription`
   always rebuilds from the newest bill → `period_changed` 422.
4. A forced re-send re-applies remaining `pending_credit_usd`.
5. Legacy `Tenant.default_billing_rate_per_kwh` is ignored by bill-bound
   pricing but still writable from the UI/agent.
6. `bill_cash` credit rate has no sanity band; upserts are climb-only.

Send pipeline (`delivery.py`, `api/scheduler.py`, `api/jobs/new_bill_review.py`, `routes.py`):

7. A failed or missed monthly run is never retried (cron on the 1st, 1h
   misfire grace); the next month's build returns the newer bill, so the
   missed period is silently lost for auto-mode off-takers.
8. Scheduler auto-send vs. manual approve/send-now has no row lock → two emails.
9. `new_bill_review` ignores `sending_paused`, `delivery_mode` and the
   already-sent state.
10. Scheduler holds (rate not operator-entered; GMP mismatch) are invisible
    in `/send-pipeline`.
11. `notify.last_resend_id()` is a process global → the wrong Resend id can be
    stamped on a subscription under concurrent sends.
12. Workbook subscriptions with no period dates bypass the exactly-once guard.

Payments (`api/billing/payments.py`, `api/stripe_webhook.py`, `invoice_ledger.py`):

13. A test send mints a real pay link; paying it marks the period paid and the
    real send then embeds the consumed session.
14. When the amount changes, the superseded open Session is expired only on
    click, not at re-send (`create_offtaker_payment` with `force=True`).
15. Connect not ready → scheduled invoices go out with no Pay button and no alert.
16. "Outstanding" is not derivable: no invoice record exists when no pay link
    was minted; no way to record an offline (check) payment.
17. Webhook idempotency is a status check, not a lock; a Stripe retry during a
    slow first run can double-process.
18. Auto-link of a Connect account by contact email can attach another
    tenant's account.

Roster import (`routes.py` bulk-import/bulk-commit, `roster_detector.py`):

19. `column_map` override always assumes header row 0.
20. Per-array allocation sums over 100% are not shown in the preview; commit
    fails at row N after N−1 rows are already created (no batch rollback).
21. Abbreviated array names ("Pomfret CS") do not match.
22. An off-taker account-number column demotes almost every row to review.

Monthly summary (`api/billing/monthly_report.py` — reviewed 2026-09-18, partly addressed since; re-check):

23. Off-takers drop off a period's report once their NEXT invoice goes out
    (report reconstructed from the subscription's single `last_sent_*`
    snapshot; a per-send record was recommended).
24. A failed send is terminal: the unique (tenant, period) row is committed
    before mailing, so `due_period` returns None forever and send-now says
    "already sent" for a report nobody received.
25. Unsent/held/pending off-takers are omitted rather than listed with a reason.
26. No frontend panel exists yet below "Utility bill archive" in
    `array-operator/public/reports.js` (~line 3384, `rb2RailBillArch`).

## 5. What to audit, area by area

For each area: read the code, read the tests, write down the invariant, then
try to break it with a concrete scenario. Prefer runnable repros (a pytest
that fails on `main`) over prose.

A. **One invoice, correct amount.** Trace `build_manual_match` →
   `compute_invoice` → `invoice_for_period` → PDF/XLSX/email/pay link. Confirm
   all four surfaces show the same `amount_owed` for: utility-bill-bound
   off-taker, multi-array allocations, tariff + adder with expiry, budget
   override, pending credit, quarterly cadence, group-host share
   (`array_share_pct`, `bill_anatomy`). Check rate precedence: per-offtaker →
   tenant master → bill-derived (must warn and hold autopilot).
B. **Exactly once.** For each send path (scheduler auto, scheduler draft →
   approve, send-now, bulk-draft, true-up) prove the same period cannot be
   billed twice, including after a bill re-capture, a cadence change, a
   late-landing own bill, and a crash after Resend accepted the mail.
C. **Recipient safety.** `send_mode` to_me / to_client / to_both,
   `sending_paused`, `delivery_mode`, test sends, BCC to operator, white-label
   from-name and reply-to. Nothing may reach a real off-taker before the
   operator deliberately chose to_client.
D. **Money.** Direct charge shape (`stripe_account`, `application_fee_amount`,
   no `transfer_data`), fee math in cents, durable link lifecycle (fresh /
   stale / expired / paid / refunded / Connect not ready / bad token), webhook
   signature verification with two secrets, idempotency, refund and ACH
   async handling, ledger and monthly summary agreeing on paid/collected.
E. **Roster import at scale.** Build a messy 250-row `.xlsx` (title rows,
   odd headers, `25` / `0.25` / `25%` shares, `$0.18398` rates, blank and
   malformed emails, exact duplicates, one array summing to 105%, a Total
   footer, near-miss array names) and run `bulk-import` then `bulk-commit`
   through the FastAPI `TestClient` with `ANTHROPIC_API_KEY` unset. Every row
   must be either created with correct values or blocked with a reason; nothing
   silently wrong.
F. **Data pipeline.** Utility bill capture → `Bill` rows → off-taker binding
   (`utility_account_id`) → stale-bill warning → partial-bill dedup. Confirm an
   off-taker with no bill for the period is SKIPPED, never billed from
   telemetry.
G. **Monthly summary.** Items 23–26 above; the 15-days-after-last-send
   trigger; idempotency across scheduler ticks and redeploys; recipient is the
   operator only.
H. **Frontend.** Statements tab: draft inbox shows the amount that will send;
   pay links render as arrayoperator.com URLs; archive and bill archive load;
   the monthly summary needs a panel.
I. **Security.** `/pay/{token}` is public by design (32-char random token,
   `Cache-Control: no-store`); check enumeration resistance, tenant scoping on
   every `/v1/array-operator/billing/*` route, and that no route leaks another
   tenant's rows.

## 6. How to run things

Tests (from the repo root, inside the venv):

    python -m pytest tests -q -p no:cacheprovider

Full-suite baseline on `main` as of 2026-09-18: **33 failures that predate
this work** and are unrelated to off-taker billing — do not chase them:
`test_ao_plan_line_migration` (4), `test_array_soft_delete_visibility`,
`test_chint_unmerge`, `test_claude_cli` (2), `test_command_center_fleetstore_guard` (2),
`test_daily_upsert_race`, `test_db_pool_hardening`, `test_draft_period_selector`,
`test_email_skin` (2), `test_energy_agent_array_shadow`, `test_energy_agent_voice_weave`,
`test_gmp_hourly_parse_and_export`, `test_gmp_refresh`, `test_inverter_alert_sweep`,
`test_inverter_capture_nuclear` (2), `test_offtaker_utility_bill`, `test_password_auth` (4),
`test_peer_analysis`, `test_perf_verification_core`, `test_provider_registry`,
`test_solaredge_location_backfill`, `test_trends_view_liquid_aottrends_guard` (2).
Two tests in `test_billing_delivery.py` (`test_match_preview_saves_nothing`,
`test_create_subscription_links_client_and_defaults_to_me`) flake only when run
after other files (shared session DB); they pass alone.

Billing slices that must stay green:

    python -m pytest tests/test_billing_delivery.py tests/test_audit_offtaker_fixes.py \
      tests/test_offtaker_pay_link_durable.py tests/test_offtaker_payments.py \
      tests/test_invoice_ledger.py tests/test_offtaker_upload.py tests/test_annual_trueup.py \
      tests/test_quarterly_offtaker_billing.py tests/test_honest_rate_and_adder.py \
      tests/test_realmath_invoice.py tests/test_group_host_bill.py tests/test_reconcile_bills.py -q

Local shadow run: `tests/test_billing_delivery.py` shows the pattern
(`_make_tenant`, `_upload(client, auth, "norwich.xlsx")`, `/subscriptions/{id}/draft`,
`/preview`, `/drafts/{id}/approve`); `tests/test_offtaker_upload.py` shows how
to seed arrays with a settled GMP bill and drive bulk-import/commit;
`tests/test_offtaker_payments.py` shows how Stripe is mocked. Fixtures are in
`tests/fixtures/`. Emails never leave the test process (`RESEND_API_KEY` is
unset by conftest); `EMAIL_DRY_RUN=1` exists for any local server run.

Production, READ-ONLY, via the Railway CLI in WSL (`cd /root/solar-operator`):

    railway ssh "cd /app && python -c '<python that only SELECTs>'"
    railway logs
    railway variables --json   # print KEY NAMES only, never values

Useful read-only checks: the Norwich tenant row; count of
`billing_report_subscriptions` per tenant with `send_mode`, `delivery_mode`,
`enabled`; `offtaker_payments` by status; scheduler job list; recent
`email_archive` rows for the Norwich tenant.

## 7. Hard rules

- **Never send an email to a real off-taker, mint a live Stripe charge, refund,
  move money, change a fee, change the charge model, edit or delete tenant
  data, or run migrations against production.** Read-only on prod. If a check
  needs a write, describe it and stop.
- Never print secrets (Stripe keys, webhook secrets, Resend key, login tokens)
  into a transcript, a file, or a commit.
- Do not touch `C:\Users\fordg\solar-operator` (stale, broken merge) or the
  other agents' uncommitted work in `/root/solar-operator`; work on your own
  branch or worktree.
- Commit fixes with regression tests on a branch named `codex/offtaker-audit-<date>`
  and push the branch. Pushing to `main` deploys to production: only do so if
  the full billing slice above is green AND the change touches neither Stripe,
  fees, schema, nor anything that sends mail — otherwise leave it on the branch
  and say so in the report.
- Do not loosen or delete a failing test to make it pass; explain why it fails.
- Append a dated entry to the shared backlog stash (path in §2) with what you
  found and what you changed, so the other agents see it.

## 8. Deliverables

1. `docs/handoffs/CODEX_OFFTAKER_AUDIT_REPORT_<date>.md` in the repo: findings
   ranked critical → low, each with file:line, invariant violated, concrete
   scenario with numbers, and the fix (done or proposed); a "verified correct"
   list; a "could not verify" list with why.
2. The branch with fixes + tests, and the exact pytest output.
3. A one-paragraph plain-language summary for Ford: is the system ready to
   invoice Norwich's off-takers, and what must happen first.
