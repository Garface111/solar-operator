# Final remediation verification evidence

Backend code: 38d4a43f. Frontend code: 5bdc7bbc. These are isolated audit branches; nothing in these checks deploys production.

- `norwich-final-verified-full.log`: complete default SQLite suite after all fixes, including the seven original launch gates and the formerly expected-failure imports.
- `norwich-final-verified-pg.log`: PostgreSQL 16 billing concurrency, immutable evidence, payment lifecycle, backlog, recovery and 300-invoice suite (74 passed).
- `norwich-final-migration1.log` / `norwich-final-migration2.log`: repeated additive upgrades on a populated disposable database. The final rate-gate table was also confirmed by PostgreSQL schema inspection.
- `norwich-pg-migration-verification.log`: populated upgrade and restoration from the pre-upgrade backup into a separate disposable database, preserving the original paid invoice and credit.
- `norwich-throttle-intake.log`: 63 dispatch/intake tests after the last intake and shared throttle changes.
- `frontend-smoke.cjs` / `.log` / `frontend-recovery.png`: isolated Chromium component smoke test against actual frontend functions. All network traffic is intercepted; this does not assert real provider delivery or render the entire production site. The script contains this workstation's runtime/worktree paths.

## Reproduction

Run the backend default suite with `python -m pytest -q`. The test fixture deletes real mail credentials, uses an isolated database and accelerates only the shared billing throttle. Never point the suite at production.

For PostgreSQL, provision the disposable local database `norwich_audit_20260919` and local role `root`, then set `NORWICH_TEST_PG_URL=postgresql+psycopg2://root@/norwich_audit_20260919`. The fixture accepts only this exact local URL and creates/drops test tables. Run these files:

```
tests/test_norwich_launch_gate.py
tests/test_norwich_300_dispatch.py
tests/test_norwich_dispatch_recovery.py
tests/test_norwich_frozen_evidence.py
tests/test_norwich_payment_lifecycle.py
tests/test_norwich_data_guards.py
tests/test_norwich_reporting_recovery.py
tests/test_norwich_backlog.py
tests/test_norwich_payment_policy_routes.py
```

Warnings in the logs are retained, including existing UTC datetime and library deprecations. Passing tests are evidence of the exercised behaviors, not certification of unavailable real customer inputs, live payment setup, bank settlement or production recovery targets.
