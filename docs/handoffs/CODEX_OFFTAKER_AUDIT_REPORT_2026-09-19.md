# Norwich offtaker invoicing: independent readiness audit

Date: 2026-09-19. Decision: **NO-GO for unattended invoicing of Norwich’s 300 offtakers.**

Ordinary billing calculations and a synthetic 300-row import work under the tested conditions. The system still has demonstrated duplicate-send, historical-record, refund, partial-data, and reconciliation failures. The fixes on the audit branches reduce risk but do not make the entire invoicing lifecycle safe.

Backend baseline: `6f2ee2ffe5c1ce68bcea60d6d846ba07818668b8`, also the observed production deployment. Frontend baseline: `019d31d4`. Review branches in both repositories: `codex/offtaker-audit-2026-09-19`.

No production deployments, tenant edits, migrations, live payments, customer emails, refunds, or money movement were performed. The handoff was treated as claims to check, not authority to operate production or contact others.

## Scope and evidence

Reviewed invoice build/render/email/payment paths, send guards, scheduler registration, draft approval, roster parsing/commit, Stripe settlement/refund handlers, monthly summaries, tenant ownership helpers, public payment routes, and deployed frontend assets. Used an isolated WSL worktree, throwaway SQLite database, and mocked Stripe/email for reproductions. Production database checks used explicitly read-only PostgreSQL transactions. Stripe endpoint listing was read-only.

This is a billing-readiness audit, not an exhaustive penetration test or certification of unrelated product modules. SQLite tests do not establish PostgreSQL locking behavior, production throughput, actual inbox delivery, or bank settlement. Norwich’s real roster and contracts are unavailable, so customer-specific shares, rates, dates, recipients, opening balances, and bank destination remain unverified.

Production observations:

- Norwich exists and is active, with **zero arrays, zero subscriptions, zero payment rows, and no connected Stripe account**. Charges are disabled.
- Norwich’s scheduled sending switch is **unpaused**. Nothing is currently queued for this empty tenant; importing the roster changes that situation.
- Stripe platform/Connect signing-secret variables and Resend credentials are present. Only presence booleans were exposed. Email dry-run and sink settings are absent.
- Required payment token/expiry/account columns, tenant charge-model column, and monthly-report table exist.
- Web, worker, capture, database, and document-renderer deployments report SUCCESS. Web/worker/capture run the audited baseline. Direct API `/health` returned 200.
- The billing proxy rejects unauthenticated pipeline access with 401. An invalid public pay token returns 404 with `Cache-Control: no-store`.
- The two relevant Stripe webhook URLs are enabled. The Connect endpoint lists Checkout completed/expired/async-success/async-failure, charge refunded, and account updated. Registration does not prove Norwich’s future account delivers events successfully.
- Deployed `reports.js` matches the inspected checkout and **already contains the monthly-report UI**. The deployed payment-return page also matched its source and incorrectly claimed payment was complete.
- An archive count by tenant could not be obtained: `email_archive` has no `tenant_id` column. That failed SELECT made no changes. The frontend `/v1/health` is not the direct API health route; the billing proxy was checked separately.

## Tests

- Original billing slice: **150 passed, 1376 warnings in 9.46s**.
- First independent regression run: **10 failed, 112 warnings in 2.95s**. All ten passed after fixes.
- Separately reproduced failures in concurrent mail-receipt tracking and a delayed ACH-failure event reopening a refunded invoice.
- Scale/isolation rehearsal: **2 passed, 1912 warnings in 9.69s**. Preview/commit/replay of 300 rows took 4.471s; adding checks of all 300 amounts, email figures, Stripe cents, and a representative PDF/XLSX took 5.947s in that local run. These are not production throughput promises.
- Final focused billing/security/receipt validation: **183 passed, 3443 warnings in 21.30s**.
- Clean-main full suite, independently repeated: **27 failed, 2568 passed, 3 xfailed, 23421 warnings in 464.25s**. The handoff’s 33-failure count was not reproduced.
- Audit-branch full-suite snapshot: **27 failed, 2583 passed, 3 xfailed, 26713 warnings in 423.13s**; the failing test names exactly match clean main. This snapshot preceded the final nested-alert receipt refinement; that refinement and its extra regression are covered by the final 183-test slice.
- Explicit launch gate, `tests/norwich_launch_gate.py`: **7 failed, 78 warnings in 3.54s**. These are unwaived acceptance requirements, without xfail/skip marks. This separate qualification file must be run explicitly and must pass before launch.

Exact logs accompany the repository report in `docs/handoffs/norwich-audit-evidence-2026-09-19/`. A passing ordinary billing slice does not supersede the failing launch gate.

Baseline failures include invoice-related period-selector and bill-rate tests, plus two suite-order billing-delivery failures. They are not dismissed simply because they predate this audit. The focused slice runs those two delivery tests first and passes them.

## Critical findings

**C1 — OPEN, reproduced: duplicate invoice sends and crash recovery.** `api/billing/delivery.py:2262`; `api/billing/routes.py:4342`. Two simultaneous callers both read an empty last-sent value and email the same invoice. A separate test simulates provider acceptance followed by database commit failure; retry sends it again. Draft locking does not protect scheduler versus manual sends. Two emails can also create two collectible payment rows. Required fix: immutable invoice identity, transactional dispatch/outbox records shared by every path, atomic claims, frozen payloads, provider idempotency, and reconciliation of uncertain sends. A row lock alone cannot close the external-email/database crash gap. Both acceptance tests remain red.

**C2 — FIXED ON BRANCH, wider races remain: pending/uncertain payments create another Checkout.** `api/billing/payments.py:735`. A $100 ACH payment can complete Checkout while still processing; another click previously minted a second $100 session. Retrieval/expiry failures and an expired local timestamp could also lead to another session despite payment completion. The branch reconciles existing open sessions regardless of local expiry and stops when status is unknown, expiry fails, or payment is processing. Concurrent refresh/mint and crashes between Stripe creation and persistence still need durable idempotency and database arbitration.

**C3 — FIXED ON BRANCH: Connect ownership inferred from shared email.** `api/billing/payments.py:151`. Two tenants sharing a contact address could attach the same connected account, directing Norwich’s money to another operator. Email matching is removed; matching tenant metadata is required, and accounts attached to another tenant are rejected. Existing mismatched links elsewhere are not automatically repaired; Norwich currently has none.

**C4 — FIXED ON BRANCH: test invoices create live payment obligations.** `api/billing/delivery.py:2262`, `:2747`, `:2038`. Testing a $100 invoice previously minted a real link; paying it marked the actual period paid before customer invoicing. Regular and true-up tests now skip payment creation; test email rendering strips supplied pay links and labels HTML/text as test copies. Any pre-existing test-created live payment rows require separate reconciliation.

## High findings

| Finding/status | Location | Concrete failure and required remedy |
|---|---|---|
| H1 OPEN, reproduced: historical summaries | `api/billing/monthly_report.py:110` | June’s $100 invoice disappears after July becomes the subscription’s last-sent period, even with a June payment row. Held/unsent offtakers are omitted, so 299 of 300 can look complete. Build reports from immutable invoice history and list every expected offtaker with exceptions. Stored emailed workbook bytes preserve one output, not completeness or reconstruction. |
| H2 OPEN, reproduced: phantom true-up credit | `api/billing/trueup.py:113`, `:162` | Four quarterly $100 invoices total $400; twelve $50 actual-value months total $600. Code assumes $1,200 budgeted and grants a $600 credit instead of a $200 adjustment. Reconcile actual issued invoices/credits and separately establish whether the contract settles billed or collected budgets. Failed monthly sends also invalidate the assumption. |
| H3 OPEN, reproduced: partial VEC month | `api/billing/delivery.py:395`, `:440`, `:504` | Ten May days at 100 kWh/day, 40% share, $0.25 rate and 10% discount yield a sendable $90 invoice; 31 days would yield $279. The month guard may then prevent the remainder from being billed. Require closed-period completeness and historical selection in both generation readers. |
| H4 OPEN, reproduced: partial roster commit | `api/billing/routes.py:2180` | Three 35% shares total 105%; the first two live subscriptions are committed before the third is rejected. Validate the whole batch and existing totals, then commit atomically under allocation locks. Concurrent imports also need arbitration. |
| H5 OPEN, reproduced: partial refund totals | `api/billing/payments.py:891`; `api/billing/invoice_ledger.py:86` | A $100 payment with a $40 refund still shows $99.50 collected after subtracting only the platform fee. Persist refund transactions and derive consistent gross/refunded/fee/net totals. “Collected” is also not verified bank-payout net. |
| H6 OPEN, code confirmed: missed periods lost | `api/scheduler.py:536`, `:1818` | A June run fails, then July’s bill arrives; the next build targets July with no durable June obligation. Monthly/quarterly jobs fire only on day 1 with a one-hour misfire grace. Queue every unbilled closed period and retry with bounded backoff/alerts. Increasing cron frequency alone amplifies duplicate risk. |
| H7 OPEN, code confirmed: superseded links remain collectible | `api/billing/payments.py:497`, `:735`; `api/models.py:1955` | Revising $100 to $120 leaves old sessions/tokens collectible, potentially collecting $220. There is no unique invoice/period constraint or stable session-creation idempotency key; the row is only flushed before Stripe creation. Version obligations, reconcile/expire old sessions, invalidate old tokens, and serialize mint/refresh. Same-amount forced-send reuse is fixed, but revisions are not. |
| H8 OPEN, code confirmed: webhook races | `api/stripe_webhook.py:688`; `api/billing/payments.py:635` | Two deliveries can both see a received event, run handlers, and emit two $100 receipts. No atomic handler claim or locked paid transition. Add durable event processing, monotonic transitions and a notification outbox; signature checks do not establish idempotency. |
| H9 OPEN, code confirmed: report retry race | `api/billing/monthly_report.py:503`, `:384` | A second caller may reuse a report row while its first email is in flight because `sent_at` is null. Post-acceptance crashes also become retryable. Older missing reports can be skipped once the newest is delivered. Use durable dispatch claims and enumerate missing periods. The handoff’s terminal-failure bug is partly fixed, but safe retry is not. |
| H10 OPEN, code confirmed: no payment path | `api/billing/delivery.py:2432` | Norwich has no Connect account; a scheduled $100 invoice can nevertheless email without a Pay button because payments are best effort. Define an explicit online-required versus offline policy; hold and alert when a required payment path is unavailable. Offline mode needs a settlement ledger. |
| H11 FIXED ON BRANCH: settlement evidence and late refund events | `api/billing/payments.py:635`, `:872` | Missing payment status previously marked a row paid; metadata could select a different session’s row. Late paid/ACH-failure events could overwrite refunded status. Require the tracked session and explicit successful status; preserve refunds. Amount/currency mismatch checks and full event ordering still need coverage. |
| H12 FIXED ON BRANCH: rate changes hidden as identical imports | `api/billing/routes.py:2180` | $0.20/kWh reimported as $0.25 was silently skipped—$50 difference at 1,000 kWh before discount. Compare rate, budget, array, cadence and delivery mode as well as prior fields; surface conflicts without overwriting live billing. |
| H13 FIXED ON BRANCH: wrong email receipt assignment | `api/notify.py:34`, `:176` | A deterministic two-thread test returned receipt B to caller A. Nested archive alerts can also replace an invoice’s receipt on the same call stack. Per-context receipt storage now preserves the correct receipt across both cases. Legacy global attributes are not authoritative. |
| H14 FIXED ON FRONTEND BRANCH: false paid confirmation | frontend `public/paid.html:92` | Public `?status=ok` claimed an invoice was paid even for pending ACH or fabricated URLs; cancel claimed no charge without checking. Replace both with neutral confirmation instructions. Production still serves the old page until deployment. |

## Other findings and limits

- **Historical draft approval:** delivery rebuilds the newest match and rejects older reviewed periods. Add period-pinned builds together with historical duplicate protection; changing only the selector could re-send an old billed month.
- **Repeated credit consumption:** `build_match:117` applies today’s pending balance. A $100 invoice with $250 banked credit leaves $150 after its first zero-dollar send; forcing a repeat can burn another $100. Re-send must reuse the original issued invoice and credit application.
- **Undated workbooks:** no period key means the duplicate guard has nothing to compare. Require explicit canonical periods before real sends.
- **Legacy rates:** bill-bound pricing does not honor every still-writable legacy flat-rate setting. Migrate/deprecate deliberately and reconcile against contracts.
- **Utility corrections:** `api/rate_schedule.py:760` accepts bill-cash rate division without the reference-rate sanity band. `api/adapters/vec_bill.py:153` and capture upserts retain larger values after downward corrections. A corrected $200-to-$150 bill can leave invoicing at $200. Version utility evidence and establish which revision supersedes prior data.
- **Pause/review semantics:** `api/jobs/new_bill_review.py:273` omits the scheduler’s pause, approval/auto-mode, and already-invoiced checks. Manual sends intentionally bypass scheduler pause. This is not a universal emergency stop.
- **Invisible holds:** `routes.py:5563` aggregates waiting/draft counts without itemizing rate/reconciliation holds. An exception list needs period, reason, amount, age, owner and retry state.
- **Column overrides:** bulk import initializes the header offset to zero; explicit column mapping does not carry the selected header row. The automatic title-row scale test passed, but explicit overrides need separate coverage.
- **Matching:** disconnected account numbers are deliberately demoted for review; that caution is appropriate. Real abbreviations cannot be certified without the roster. Never silently confirm a fuzzy match instead of utility-account identity.
- **Offline balances:** no complete issued-invoice ledger independent of Stripe and no audited check-payment workflow. Outstanding balance cannot reliably be inferred from payment links.
- **Email capacity:** local speed does not prove delivery of 300 emails. An invoice-specific durable throttle/retry queue is missing. Actual account quotas, sender readiness, bounces and inbox delivery remain unverified.
- **True-up parity:** normal-invoice approval-drift and BCC controls are not fully shared with the true-up path. Test amount/window drift and operator copies before enabling it.

## Verified under the stated scope

- The 300-row rehearsal preserves 25% shares, $0.18398 rates, normalized addresses and approval mode. A duplicate row and full replay create no extra subscriptions.
- Independent arithmetic—5,000 kWh × 25% × $0.18398 × 90%—produces **$206.98** for all 300. Render payloads, email amounts and Stripe cents agree; a representative PDF/XLSX contains that value.
- Existing focused tests cover rate precedence, adder expiry, credits, quarterly completeness, group-host math, settled-bill gates, durable-link expiry, fee cents and direct-charge request structure.
- Direct charges use the connected account and application fee without destination-transfer data. At default 50 basis points, $100 carries a $0.50 platform fee. This checks request construction, not bank settlement.
- Foreign-tenant preview, payment-list, send-now, patch, draft-approve and draft-test calls return 404. Structural inspection finds authentication calls on every billing route except the public blank template and token payment route; this is not proof of all authorization paths.
- Signature tests cover production failure without configured signing secrets and valid signatures. The production secrets are present. The supposedly missing monthly-summary frontend panel is already deployed.

## Required next work

Keep Norwich out of unattended sending until the launch gate passes, the open critical/high lifecycle issues are resolved, and the actual roster reconciles. These review branches are risk reductions, not a declaration of readiness. Do not merge solely because the focused slice passes.

The remaining core work changes persistent accounting and dispatch: immutable invoice history, a durable send queue, safe payment revisions, credits, historical reports, and offline settlement. Migrate deliberately in staging, test PostgreSQL contention and provider failure/recovery, and do not automatically rewrite live history or replay uncertain sends.

The paired [launch runbook](NORWICH_LAUNCH_RUNBOOK_2026-09-19.md) covers intake, rehearsal, staged release and incident handling. Real account onboarding and contract reconciliation remain prerequisites after code remediation.

Primary references: [Stripe webhooks](https://docs.stripe.com/webhooks), [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests), [Resend idempotency keys](https://resend.com/docs/dashboard/emails/idempotency-keys), and [Resend rate limits](https://resend.com/changelog/api-rate-limit). Resend documents a 24-hour idempotency window; provider deduplication alone cannot replace durable invoice history.
