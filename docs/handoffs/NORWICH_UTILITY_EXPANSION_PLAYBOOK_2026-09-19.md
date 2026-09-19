# Norwich: preparing for utilities we have not connected yet

September 19, 2026. Norwich's utility/state list is entirely pending. This is an implementation and intake plan, not certification that all 300 offtakers can be invoiced today. This playbook accompanies the utility hardening release in [PR 105](https://github.com/Garface111/solar-operator/pull/105). Deployment verification is recorded in that release after rollout; this document does not certify live Norwich utility coverage.

## What determines the work

The number of distinct utility portals and billing arrangements determines integration work more than the number of offtakers. Several hundred customers may share a small number of portal families, generation meters or allocation statements. Conversely, one portal can require several different parsers or meter mappings.

For each group establish whether invoices depend on the array's generation, each offtaker's actual utility credits, or an allocation statement. Those are different source requirements. We should not request 300 separate portal logins if the contracts require only a few producing-meter accounts, nor assume producing-meter data replaces customer credit statements.

## What the current code actually supports

This is a code inventory, not a live-login qualification of Norwich's utilities.

| Family | Existing collection | Invoice preparation still required |
|---|---|---|
| GMP | Account, bill JSON/PDF and meter-generation paths | Confirm actual accounts, source periods, credit lines, source versus contract rate, mappings and complete capture. |
| NISC SmartHub | Shared account/bill-history/PDF/meter adapter; 611 cataloged hosts | Verify the exact utility host and actual meter channels. Qualify statement credit/excess mapping or complete measured generation. Current subscription flow supports monthly SmartHub billing; quarterly requires additional implementation. |
| Eversource | Account and meter-candidate collection | Current collector emits no Bill records. Bill-credit collection and field semantics need implementation and real statement evidence. |
| CMP | Account and meter-candidate collection | Current collector emits no Bill records. Obtain and qualify any applicable allocation statement; daily generation is not that ledger. |
| Other portals | Discovery and candidate-extractor tooling | Authorized portal inspection, login/session support, parser, reconciliation fixtures and canary qualification. |

The catalog contains 1,622 utility entries: 616 labeled live, 885 in progress and 121 manual. The live label describes wiring, not proof of correct invoices. None of those counts establishes Norwich coverage. Portal compatibility, account access, correct billing evidence and safe invoice output are separate checks.

## The packet to get from Norwich

Use the adjacent `norwich-utility-kit-2026-09-19/NORWICH_UTILITY_INTAKE.csv`. One row describes a disjoint utility/source/billing group. `group_id` is a stable non-secret identifier; `offtaker_count` is the group's count, not an estimated default. If one customer legitimately appears in several source groups, note that assignment counts are not unique-customer counts.

Required initial information:

1. Utility's full legal/display name, state and exact customer portal address, taken from its official site or bill. Similar names are not sufficient.
2. Number of affected offtakers, producing arrays/meters, billing cadence and invoice basis: utility dollar credit, production kWh or allocation statement. Identify which account's evidence supplies the invoice.
3. Two consecutive closed billing cycles of original utility statements/exports and one previously approved invoice or hand-checked calculation. Include a credit, zero-generation or adjustment example if available.
4. Stable producing-meter and receiving-account identifiers, allocation/share mapping, service start dates, contractual rate/discount, rate changes, and opening debt/credits. Keep identifiers intact in the private source; anonymize shared test fixtures consistently.
5. Authorized access method, delegated/service-account availability, MFA contact, and who can renew access if a session expires. Passwords, cookies and tokens belong in the existing vault, not this CSV, chat, a source-control commit or an email attachment.

The CSV intentionally contains no passwords, account secrets or authorization tokens. A companion human-readable request packet is ready to share; it has not been sent.

## Fast integration workflow

1. **Triage immediately:** run the readiness tool on the completed CSV. It groups work by actual family/source/cadence, highlights conflicting identities and prioritizes by supplied offtaker count. It never grants production approval.
2. **Reuse a verified family:** a compatible SmartHub host can use the existing adapter after host identity and credentials are confirmed. An Eversource/CMP login must not be counted as complete bill extraction. Prefer an authorized stable API or original structured export when available.
3. **Create a candidate pack:** the scaffold creates a qualification manifest, inert parser stub and evidence checklist. It neither changes the provider catalog nor starts capture.
4. **Implement against saved evidence:** independently transcribe expected account, period, units and amounts; keep the original source hash. Compare candidate output to those expectations with the fixture verifier. Do not derive both actual and expected amounts from the parser under test.
5. **Exercise failure paths:** wrong account; consumption/export mistaken for generation; partial/overlapping periods; missing accounts in a multi-account login; zero generation; duplicate capture; revised statements; expired session; timeout/429; changed markup; MFA/CAPTCHA; and interrupted persistence. Stop for required human authentication instead of bypassing it or repeatedly retrying a password.
6. **Qualify an actual account:** compare all expected accounts/cycles with what landed. Prove a second capture and fresh session renewal, not just one warm-browser success. A successful login or overall harvest flag is insufficient.
7. **Release in reviewed groups:** produce a draft, reconcile PDF/XLSX/invoice inputs and amount, then use an approved canary. Enable a qualified group independently while unsupported groups remain held. Reconcile expected, captured, drafted, issued and held counts after each expansion.

No credible completion-time promise is possible until we see access requirements and representative statements. Reusing an established portal family should reduce implementation work; MFA, access restrictions and missing billing fields can still require utility/customer assistance.

## Implemented preparation and safeguards

- Pure readiness evaluator and authenticated, read-only `POST /v1/bill-autopilot/readiness`. No portal connection, credential lookup, database write or sending is triggered by this assessment.
- CSV triage CLI, blank intake template, capability snapshot, synthetic 300-assignment demonstration and candidate integration-pack generator.
- Independent fixture comparison checks original-file hash, exact account/period/basis coverage, duplicate cycles, explicit source semantics, finite kWh and integer-cent money. It rejects estimated evidence and never marks a utility production-ready.
- Unknown utility codes no longer default to the SmartHub harvester. SmartHub checks the credential's exact provider/host mapping before navigation and scraping.
- Unsupported manual utility billing holds before entering GMP-specific calculations. Actual explicit workbook imports and already frozen invoice history remain supported.
- Even one estimated SmartHub daily reading prevents that month from qualifying as measured generation. Monthly evidence cannot be issued under a quarterly identity.
- Generic discovery retains a review candidate; structural synthesis can no longer auto-approve adapters, create parsed bills, invent month boundaries or equate generation with exported energy. Retained samples redact credential fields and remove token-bearing URL components.
- Autopilot capability descriptions now distinguish Eversource/CMP meter collectors from bill extraction.

## Tools for the implementation team

Ford does not need to run these commands; they make future onboarding reproducible for the agent doing it.

```
python -m scripts.norwich_utility_readiness --template intake.csv
python -m scripts.norwich_utility_readiness --roster intake.csv --output utility-review
python -m scripts.norwich_utility_readiness --scaffold utility_code --output utility-review
python -m scripts.verify_utility_fixture --expected expected.json --actual actual.json --source original-source.pdf
```

Fixture document contract:

- Top-level `source_sha256` and `records`.
- Each record: `provider_code`, anonymized but stable `account_reference`, canonical `period_start`/`period_end` (`YYYY-MM-DD`), `billing_basis`, `source_kind`, `currency: USD`, `estimated: false`, and `values`.
- `bill_credit`: `source_kind: utility_statement`; `values.net_meter_credit_cents`.
- `production_kwh`: `source_kind: utility_meter` or `utility_statement`; `values.generation_kwh`.
- `allocation_statement`: `source_kind: allocation_statement`; `values.allocated_kwh` and `values.credit_applied_cents`.
- Monetary fields are integer cents; kWh must be finite and nonnegative. Closed dates and exact independently reviewed record coverage are required. This verifier does not replace the runtime complete-daily-evidence check or a live qualification review.

## Operational fallback and ongoing reliability

If a portal is unavailable, obtain the original export or bill and use an existing, supported upload-and-review path. A new PDF layout still requires parser verification. Mark that group as manually supplied; do not present it as unattended scraping. Missing or ambiguous evidence holds the affected invoices without blocking already qualified groups.

For every connected group, record an owner and expected statement-arrival window. Review capture freshness and expected account/period coverage, session-expiry/MFA needs, missing or revised statements, visible invoice holds and retry exhaustion. A daily job that succeeds with zero useful bills is not success for invoicing. Regression fixtures must be rerun whenever a portal or parser changes.

Existing data from earlier generic discovery would need reconciliation before use. A read-only production query on this date found no UtilityAccount rows tagged `extra.source=bill_discovery` and hence no bills attached to such rows. That is a scoped check, not proof that every legacy capture has correct provenance.

## Verification

Targeted integrated checks: 727 passed; after merging the current main branch, 99 targeted checks passed. PostgreSQL checks: 73 passed initially and 75 passed on the final combined code. Full-suite result: 3415 passed, 33926 warnings in 348.56s (0:05:48). The synthetic 300-assignment rehearsal is not Norwich's actual portfolio. No live utility login, customer message or invoice was initiated by these checks. Norwich's sending remains paused pending real source qualification. See the adjacent kit's VERIFICATION.json for the backup and test evidence.
