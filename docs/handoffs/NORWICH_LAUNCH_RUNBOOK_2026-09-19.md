> Updated launch gates and recovery controls: [remediation verification](NORWICH_REMEDIATION_VERIFICATION_2026-09-19.md). The original seven software gates now pass; production qualification remains pending.

# Norwich launch and recovery runbook

**Current state: do not start unattended invoicing.** This is a plan, not a record that the steps have happened. Audit fixes are not deployed. Norwich has no imported offtakers or connected payout account.

## Data intake

Obtain the authoritative roster with stable utility-account IDs, producing-array/sub-meter relationships, service dates, shares, rates/discounts/adders and expiry dates, cadence, budget/true-up terms, authorized recipients, opening balances and banked credits. Preserve the source file and dated approval of the values. Missing email, unknown account or approximate array name means an unresolved row, not permission to guess.

Reconcile allocation totals per billing meter and group-host structure. A sub-meter billed at 100% of its own credit retains its separate group share; do not multiply its credit by that share again. Confirm zero generation, corrected bills, partial service and missed periods against the contract.

Record invoice numbering, sender/reply-to identity, approval operator, report recipients and payment methods. Establish accounting/tax requirements where applicable. Check-paying customers require an audited offline-payment workflow before balances can be called complete. Do not infer contract terms from software defaults.

Before a live import, establish a verified safe onboarding state. The patched scheduled delivery, draft and bill-review jobs honor the sending pause. Keep manual sends restricted to the approved canary; the pause is not a substitute for operator access control. Use a staging database, email recipient sinks and payment sandbox credentials for rehearsal. Preview must write nothing. The patched commit validates the entire batch and rolls back on failure; verify created, duplicate and rejected counts before retrying. Created, duplicate, rejected and unresolved counts must account for every source row.

## Engineering exit criteria

1. Every independent launch acceptance test passes without waiver, skip or xfail. The seven launch gates are now discovered by normal pytest runs in tests/test_norwich_launch_gate.py.
2. Immutable invoice identity binds tenant, offtaker, covered periods, approved amount, pricing/share inputs, invoice number, credit application, recipient snapshot and artifact hashes.
3. A durable queue coordinates scheduler, approval, manual send, retries and true-ups. Freeze payloads, atomically claim work, preserve provider receipts and reconcile uncertain outcomes. Re-send never creates another balance or spends credit again.
4. Due detection enumerates every unbilled closed period. Late bills and downtime create visible pending work, with bounded retries and alerts; they never silently jump to the latest month.
5. One obligation/revision has one collectible payment. Safe replacement retires the previous session/token. Pending ACH, unknown Stripe state, paid, refunded and superseded invoices cannot create another charge. Duplicate/reordered webhooks have atomic, monotonic effects.
6. Reports include all expected offtakers and exceptions. Historical months survive the next invoice. Gross collections, refunds, fees, offline payments, credits and outstanding totals reconcile to transactions.
7. Test PostgreSQL contention, double approval, concurrent pay clicks/imports, redeploy mid-batch, lost provider responses, post-acceptance database failure, provider 429/500, delayed bank failure, late webhooks, refunds and corrected utility bills.
8. Restore a staging backup and demonstrate recovery of invoice/payment history and pending/uncertain work. Establish production backup retention and recovery ownership. A populated disposable PostgreSQL backup/restore drill passed during remediation; production backup retention and restore access still require deployment qualification.

## Full rehearsal

Use a separate test tenant/database and Stripe sandbox; restrict all emails to approved test inboxes. Do not replay real payments or issue live refunds as tests.

Rehearse one expected result per offtaker. Independently reconcile PDF, XLSX, email, Checkout, ledger and monthly-report amounts. Cover 25/0.25/25% shares, dollar-formatted rates, title rows, explicit column overrides, blank/malformed emails, identical/conflicting duplicates, unknown accounts, near-match array names, 105% allocation, mixed cadence, zero generation, budget credit, missing bills, partial VEC coverage, expired adders and utility corrections. Every row must be correct or visibly blocked with a reason.

Verify the intended connected account, charge/payout readiness, card/ACH availability and webhook delivery. Verify sender-domain readiness, sending quota, rate limits, bounce processing and actual inbox delivery. The local 300-row test proves none of these external conditions. Norwich’s actual bank account was unavailable during the audit.

## Staged production release after qualification

Begin with five explicitly approved invoices. Reconcile customer, period, amount, provider receipt, delivered/bounced state, payment session and ledger before releasing another batch. Then release 25 and reconcile again. Release the remainder only when every earlier invoice has a known state and every exception has an owner. If ACH is enabled, choose an observation window that includes delayed settlement.

For every cycle, expected offtakers equal issued plus held/not-due accounts with reasons. Invoice totals equal immutable line items and adjustments. Collected, refunded, credited and outstanding amounts reconcile. The monthly report includes missing/held accounts. Provider acceptance must never be labeled inbox delivery.

## Incident response

- **Duplicate or uncertain send:** stop dispatch; preserve provider/database evidence; reconcile existing invoices and sessions before retry. Do not blindly re-run the month.
- **Pending payment or Stripe outage:** block a second checkout until status is established. Pending ACH is not failure. Do not suggest a second payment method while the first may settle.
- **Wrong rate, share or recipient:** hold affected invoices, preserve the original artifact and approved inputs, and use an audited revision/credit process. Do not overwrite history or silently send corrected duplicates.
- **Rejected roster batch:** the patched importer is atomic. Correct the rejected source and preview again. For imports made before deployment, reconcile any partially committed historical rows before replay.
- **Late/incomplete utility data:** hold with a visible reason. Never substitute telemetry into a GMP invoice or treat a partial VEC month as complete.
- **Provider throttling/outage:** billing dispatch uses a shared default one-email-per-second gate, provider Retry-After and bounded backoff. Eight rejected attempts hold for review. Check the visible failed/held queue and assign an owner; acceptance still does not prove delivery. Keep successful and uncertain sends out of blind retries.
- **Refund, dispute or check payment:** reconcile against the issued invoice before changing balances. A refund or bank failure is not permission to collect again.

For a deployment incident, pause sending and stop affected web/worker dispatch before replacing application versions. Preserve the additive schema, frozen invoice history and outbox rows. Do not roll back by restoring an old database over post-backup accepted sends or payments: reconcile external provider effects first, or the restored state can duplicate collection. The successful local restore drill proves backup readability, not production recovery-time or recovery-point targets.

The accountable operator needs a complete view of issued, delivered, paid, refunded, held, failed and uncertain invoices. Resume only after affected balances and sends are reconciled. This runbook alone authorizes no customer communications, refunds, bank changes or historical-data corrections.
