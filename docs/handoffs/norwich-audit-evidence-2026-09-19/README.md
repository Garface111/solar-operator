# Audit evidence, 2026-09-19

The `.log` files contain the exact pytest outputs. External Stripe and email sends were mocked; the database was the existing test fixture's temporary SQLite database.

Backend base commit: 6f2ee2ffe5c1ce68bcea60d6d846ba07818668b8.

Full baseline and branch snapshot: `python -m pytest tests -q -p no:cacheprovider --tb=short`.

The original billing slice is the twelve billing files listed in the handoff. Final focused slice adds `test_norwich_independent_audit.py`, `test_norwich_scale_and_isolation.py`, `test_offtaker_delivery_truth.py`, and `test_stripe_webhook_sig.py`. It runs `test_billing_delivery.py` first because two existing tests assert global database emptiness rather than tenant-local state.

Explicit launch gate: `python -m pytest tests/norwich_launch_gate.py -q --tb=short`. All seven tests fail and must be treated as blocking; they are not xfails. This separate acceptance file is outside default `test_*` discovery and must be included explicitly in any launch qualification.

The first independent reproduction log records ten failures before fixes. Separate receipt and delayed-refund logs prove two additional failures. The final focused slice includes fourteen independent regressions and two scale/isolation tests. The full-suite snapshot predates only the final nested-alert receipt refinement and its additional regression, subsequently verified in the final focused slice.

Scale timings are local synthetic results, not production load or delivery measurements. Representative PDF/XLSX content was parsed programmatically; there was no exhaustive visual audit of all operator templates.
