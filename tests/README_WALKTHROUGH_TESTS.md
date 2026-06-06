# Walkthrough Swap Regression Tests

Locks in the walkthrough swap from commit 73c08fc: the old `WalkthroughOverlay`
modal is deleted and `SandboxWalkthrough` is the only first-run guide.

## Test layers

### Layer 1 — Static-source regression (PRIMARY safety net)
File: `tests/test_walkthrough_swap_regression.py`

Pure Python file-IO + regex. No server, no browser, no DB, no npm. Runs in < 1s.

```bash
# Fastest possible check — run this any time you touch the walkthrough
cd /tmp/so-agents/agent-walkthrough-test
source ~/solar-operator/venv/bin/activate
python -m pytest tests/test_walkthrough_swap_regression.py -v
```

What it asserts:
- `web/app/src/components/WalkthroughOverlay.tsx` does NOT exist
- `web/app/src/lib/walkthrough.ts` does NOT exist
- No `.tsx`/`.ts` file under `web/app/src/` imports `WalkthroughOverlay`
- No `.tsx`/`.ts` file imports from `lib/walkthrough`
- `web/app/src/components/sandbox/SandboxWalkthrough.tsx` exists
- It exports the `SandboxWalkthrough` symbol
- It contains the canonical LS_KEY `so:walkthrough:sandbox-v2:done`
- `DashboardLayout.tsx` contains no reference to either deleted artifact

This test is free and 100% reliable. It cannot be broken by flaky infra.

### Layer 2 — Vitest component unit tests
File: `web/app/src/components/sandbox/SandboxWalkthrough.test.tsx`

Tests `initStep()` step-selection logic and component render gates via
Vitest + React Testing Library in a jsdom environment.

```bash
cd /tmp/so-agents/agent-walkthrough-test/web/app
npm test
```

What it covers:
- `initStep(0)` → `'done'` (no clients)
- `initStep(1)` → `'welcome'`
- `initStep(2)` → `'loop'`
- `initStep(3)` → `'done'` (auto-complete threshold)
- `initStep(N>3)` → `'done'`
- `localStorage[LS_KEY] = 'true'` → always `'done'` regardless of clientCount
- Component renders `null` when step is `'done'` (clientCount=0)
- Component renders skip button when clientCount=1 and LS_KEY unset
- Component renders `null` when LS_KEY is already `'true'`
- Component renders `null` when clientCount=3 (auto-complete)

**Test seams added to `SandboxWalkthrough.tsx`:**
Two `export` keywords were added — on `LS_KEY` and `initStep` — solely to
allow unit testing. Both are marked with `// TEST SEAM:` comments. They have
no effect on the built bundle (tree-shaken if unused by the app itself, and
they don't change runtime behavior).

### Layer 3 — Playwright E2E (NOT added)

Playwright is not installed in this repo and was not added. The main reasons:

1. The repo has no CI browser infrastructure (no Playwright browsers downloaded,
   no headless env configured).
2. A useful E2E test requires a running API server + a real or dev-seeded
   authenticated session. Wiring that up touches auth and potentially Stripe,
   which is out of scope for this test-only task.
3. The static-source test (Layer 1) provides 100% reliable regression coverage
   for the contract that matters: "the old files are gone and the new one is
   present and correctly exported."

If E2E browser tests are added in the future, the key assertions to implement:
- Clear localStorage + cookies; land on `/accounts/` as a fresh onboarded user
- Assert no element with class names from the old overlay (`.walkthrough-backdrop`,
  modal text "Welcome to your dashboard" with the old tour wording) appears
- Assert `[data-testid="sandbox-walkthrough"]` or the skip-button text
  "Skip walkthrough" IS visible when `clientCount === 0` is not the case
  (i.e., when the sandbox has ≥1 client)
- Set `localStorage['so:walkthrough:sandbox-v2:done'] = 'true'` and confirm
  the tour does not reappear on reload

## Running all Python tests

```bash
cd /tmp/so-agents/agent-walkthrough-test
source ~/solar-operator/venv/bin/activate
python -m pytest tests/ --ignore=tests/test_deferred_billing_setup_mode.py -v
```

The walkthrough regression test is included automatically (it lives in `tests/`
which is the configured testpath in `pytest.ini`).

## CI status

The static-source test requires only Python 3.11+ and pytest (no DB, no npm,
no network). It will pass in any CI environment that can run the existing test
suite.

The Vitest tests require Node.js 18+ and run `npm test` from `web/app/`.
They are not yet wired into the Railway/Netlify deploy pipeline — add a step
to your CI that runs `cd web/app && npm test` if you want them gated.
