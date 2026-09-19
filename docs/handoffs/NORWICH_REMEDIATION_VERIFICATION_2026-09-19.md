# Norwich remediation and independent verification

2026-09-19. This supersedes the open software findings in the original audit. It does not certify the unavailable customer roster or a production deployment.

**Software fixes are on the audit branches; production remains unchanged.** The seven original launch failures now pass and run during normal test discovery. Fixes were delegated, then the coordinating agent reviewed and tested the combined implementation independently on SQLite and disposable PostgreSQL 16.

## Findings addressed

| Original finding | Implemented control |
|---|---|
| C1 duplicate/crash sends | Unique immutable invoice identity; frozen attachment bytes, hashes, recipients and email; permanent dispatch record; atomic claim and stable provider key. Unknown acceptance stays held. |
| C2 concurrent/uncertain Checkout | Exact request persisted before provider action; stable generation key, serialized creation/refresh, verified expiration and no replacement for processing or unknown payments. |
| C3 wrong Connect ownership | Matching tenant metadata and exclusive ownership required; shared email cannot select a payout account. |
| C4 collectible test invoices | Regular/true-up tests create no payment obligation and carry no Pay link. |
| H1 historical summaries | Accepted immutable invoice history plus expected, held and unissued accounts, respecting onboarding and cadence. |
| H2 phantom true-up credit | Actual issued invoices and credits replace assumed monthly budgets; incomplete evidence holds. Four quarterly $100 invoices versus $600 actual produces a $200 adjustment. Unpaid debt stays separate. |
| H3 partial VEC months | Closed months require every day of finite, nonnegative evidence; both readers support historical selection. |
| H4 partial roster commit | Whole-batch validation and one transaction, serialized with allocation writers; over-allocation and conflicts cannot leave a partial roster. |
| H5 partial refunds | Refund records, monotonic totals and verified application-fee refunds; gross/refunded/after-platform-fee totals separated. Actual bank net and Stripe processing fees remain explicitly unknown. |
| H6 missed periods | Persisted closed-period backlog, historical targeting and daily retries; proven-rejected emails retry every minute with bounded backoff. Missing evidence stays visible as a hold. |
| H7 superseded links | Unchanged obligations reuse their session; revisions require verified expiration and old-token revocation. Settled/uncertain revisions hold. Replays cannot consume more credit. |
| H8 webhook races | Atomic event claims, locked monetary transitions, exact session/amount/currency/account validation, durable receipt obligations and per-recipient dispatch keys. |
| H9 summary retries | Frozen payload/recipients, permanent dispatch identity, concurrent/crash deduplication and enumeration of older missing periods. |
| H10 no payment path | Explicit online-required default or audited offline policy; unavailable required links hold. Offline receipts record actor, date, method, reference and unique request key, reject overpayment and reconcile existing online collection first. |
| H11 payment evidence | Missing/wrong settlement evidence cannot mark paid; late paid/ACH events cannot reopen refunded debt. |
| H12 hidden import changes | Rates, budgets, arrays, cadence and delivery settings participate in conflict checks; explicit header-row mapping supported. |
| H13 wrong email receipt | Context-local receipt/outcome tracking survives concurrent and nested sends. |
| H14 false paid screen | Public return page cannot assert settlement from query parameters. |

Also fixed: undated invoices, repeated credit consumption, historical approvals, true-up amount/window drift and BCC parity, review-job pause/mode/history checks, invisible holds, legacy rate precedence, cash-rate sanity, and versioned downward utility corrections. Canonical coverage checks ignore empty placeholders and shared meter-read boundaries.

## Independent evidence

- All seven original launch gates pass without skips or xfail waivers. Concurrency starts before the send; crash injection occurs after transport acceptance, so pre-send database writes cannot trivialize the test.
- PostgreSQL integrated billing/recovery suite: **74 passed in 24.74 seconds**, including the full 300-dispatch scenario. This includes provider-evidence recovery and the shared cross-worker rate gate.
- 300-offtaker rehearsal: 100 arrays, **300 accepted immutable invoices, exactly 300 unique accepted emails**, 30 deliberate rejections safely retried concurrently, 330 total attempts, duplicate replays blocked, $206.98 each / **$62,094 total**. Explicit offline mode creates no Stripe rows/calls. Credits banked after preparation remain untouched.
- Separate messy-roster rehearsal verifies all 300 calculations, normalized emails, mapping, atomic import/replay, and representative real PDF/XLSX output.
- Populated PostgreSQL baseline upgraded repeatedly: tenant, paid $100 invoice and $12.34 credit preserved. A pre-upgrade backup restored into a second database with the original schema/data intact.
- Full suite: **2,699 passed in 405.56 seconds; zero failures, skips or expected failures**. Existing deprecation warnings are retained in the log.
- Frontend: syntax and isolated Chromium checks passed against the actual rendering/action functions. Malicious customer text stays escaped; provider-receipt recovery sends the expected request; a lost offline-payment response followed by a reload reuses the exact request body/key and records only one logical receipt. All browser network traffic was intercepted.

The coordinating review found additional integration defects and corrected them: payment identity must be linked before sending; reused-payment locks must be released before independent dispatch transactions; minted payments must not hide failed sends from the backlog; placeholders must not block valid utility cycles; retries must use original source artifacts; Resend rate limits require the SDK's actual error fields; paused queue entries must not starve other tenants.

Pre-existing suite failures were investigated rather than waived. Real utility capture races and invalid inverter-reading redistribution were fixed. Stale tests were reconciled with documented session revocation, unified actual-roster pricing, the existing email theme, voice default-off behavior and retired orphan cards. Leaking database/provider mocks were isolated. Full-month fixture corrections preserved independent monetary assertions. The three formerly expected import failures now pass normally: banner headers, merged XLSX headers, and oversized account IDs. Oversized identities are preserved for correction and rejected before commit; no test remains marked xfail for these cases.

Verified source versions before this report-only commit: backend `38d4a43f`, frontend `5bdc7bbc`. Reproducible tests live in the repositories; execution logs and browser evidence are in `norwich-remediation-evidence-2026-09-19/` beside this report.

## Recovery and limits

- Billing transport defaults to one email per second through a shared database gate across web/worker processes. The isolated tests accelerate that gate; their runtime is not production throughput. Other nonbilling traffic still shares the provider account quota, so provider rejections honor Retry-After and bounded backoff.
- A failed dispatch is a proven rejection and receives bounded retries. A sending/uncertain dispatch may already have escaped and is never blindly resent. The UI verifies a provider email ID against its unique dispatch tag, exact recipients and frozen content; only authenticated matching evidence clears the hold.
- Provider acceptance is not inbox delivery, payment or bank settlement. Monitor bounces and payment failures; do not delete rows or mint replacements to bypass an uncertain action.
- Attachments are frozen before payment-provider holds. The PDF directs the recipient to payment instructions in the invoice email; online email carries the durable Pay link.
- Refunded invoices do not automatically become collectible again. Corrections to accepted invoices need a reviewed adjustment process. True-up contract treatment of billed versus collected budgets still requires Norwich's confirmation.
- Utilities do not provide a universal revision sequence. Known old captures cannot replay over newer evidence, and changes are retained; previously unseen stale documents still need authoritative-source review.
- Existing legacy test-created payments or bad Connect links elsewhere need reconciliation. Norwich was empty at the read-only audit; no historical Norwich ledger was fabricated.
- Local verification used Python 3.12 and PostgreSQL 16; the deployed Python 3.11 environment still needs a staging smoke check with its exact installed dependencies. Existing datetime/library deprecation warnings remain recorded in the logs.
- Local mocked throughput is not a claim about provider capacity, actual inbox delivery or bank settlement.

## Production launch gates

1. Obtain and reconcile Norwich's real roster, contracts, rates, account identities, service dates, recipients and opening debt/credits. Pause scheduled sending before import; start in approval mode.
2. Confirm the correct collection policy and bank destination. For online collection, complete Norwich Connect onboarding and enable charges.
3. Enable `application_fee.refunded` alongside the observed Checkout/refund/account events; verify signed test events. Verify sender domain, bounce handling and capacity for 300 accounts.
4. Back up production, deploy additive migrations and matching backend web/worker versions while paused, and deploy the frontend separately. A git push alone does not publish Netlify.
5. Run an approved live canary through actual email delivery and payment/refund reconciliation, then expand in reviewed batches. Reconcile expected/issued/held counts and dollar totals after each batch before enabling unattended billing.

No production deployments, migrations, customer emails, live payments, refunds or money movement were performed.

Primary provider references: [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests), [Resend key retention](https://resend.com/changelog/idempotency-keys), [Resend receipt retrieval](https://resend.com/docs/api-reference/emails/retrieve-email). Permanent application records protect retries beyond provider retention windows.
