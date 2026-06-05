# Test Coverage Expansion

## Goal
Recent shipping pace has outrun the test suite. Backfill coverage on
the highest-risk recently-shipped surfaces.

## Scope (own ONLY)
- NEW or EDIT files under `tests/` ONLY
- READ everything else to understand what to test

## DO NOT TOUCH
- Anything outside `tests/`.
- DO NOT modify production code even if you find a bug — log it as a
  finding in the 5-line summary instead.

## Surfaces to cover
1. **Multi-login auto-create autopop** (api/app.py recent changes):
   - Naming priority: customer_name → email → username
   - Existing client adoption vs new-client creation
   - Placeholder client adoption + cleanup
   Add to `tests/test_multi_login_autocreate.py` if more cases exist,
   or new file `tests/test_autocreate_naming.py`.
2. **CaptureCeremony / Welcome reveal data path** — backend side only:
   - GET /v1/clients returns correct shape for the cascade
   - Empty-state behavior
3. **VEC adapter** — happy-path parse:
   - Sample bill PDF parse from a fixture
   - Sample usage scrape parse
   - bills_raw + usage_raw round-trip through /v1/sync
   Use a fixtures dir under `tests/fixtures/vec/`. If you don't have
   real samples, generate minimal synthetic ones that match the shape.
4. **GMCS writer regressions** (api/writers/gmcs_writer.py — sacred):
   - Pixel-format invariants: A1:C1 merge, row-5 header, RECs=int(MWh)
   - Footnote text byte-for-byte verbatim
   - Excluded arrays (Array.excluded=True) skipped
5. **Stripe webhook signature verification**:
   - Mock construct_event, verify 200 on good signature
   - Verify 400 on bad signature (current bug — replicate the failure mode)

## Tasks
1. Run existing `pytest tests/ -x` first to know your baseline.
2. Write each test file. Use existing test fixtures + factories.
3. Run `pytest tests/<your-new-files>` — all must be green.
4. Run full `pytest tests/` to ensure you didn't break anything.
5. Commit per surface ('tests: <surface>'). Do NOT push.
6. 5-line summary: which surfaces covered, total test count delta,
   any production bugs found (described, NOT fixed), confidence.

## Constraints
- pytest, no new test frameworks.
- Fixtures live in `tests/fixtures/`.
- Avoid Bruce-only data — use synthetic operators where possible.
- If you find a real prod bug, FLAG IT in the summary, don't fix.
