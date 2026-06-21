# Two-products parity, cancel→reactivate, and sandbox source-routing

Session-specific detail (Jun 2026). Class lessons for: shipping a feature across
BOTH NEPOOL + Array Operator, the cancel/reactivate billing flow, and the
sandbox's data-source behavior. Load this alongside the umbrella when a request
says "do this for both services" or touches cancel/reactivate or the AO sandbox.

## 0. The two-products / two-surfaces map (memorize this first)

A request rarely means "one place." Before coding, decide WHICH product(s) and
WHICH surface:

- **NEPOOL Operator** = the React SPA at `solar-operator/web/app/`.
  - Served by the Railway backend from `api/app_dist/` (NOT a separate host).
  - Deploy = build SPA → `rm -rf api/app_dist && cp -r web/app/dist api/app_dist`
    → `git push origin HEAD:main` (Railway auto-deploys ~70s). `build_app.sh` does
    the build+copy.
  - Bruce Genereaux logs in here. This is what he screenshots.
- **Array Operator** = a SEPARATE plain-JS Netlify app at `/root/array-operator/public/`
  (`index.html` + `app.js` + `sandbox.js` + `fleet-store.js` + `reports.js`).
  - Deploy = `python3 scripts/netlify_api_deploy.py` (Netlify CLI is unreliable
    here; site_id array-operator-ea = 966cb1f5-944e-41fd-855b-10053edc5d18). Then
    commit+push the separate `Garface111/array-operator` repo.
- **Shared React account screen**: `AccountTab` / `DashboardLayout` are brand-aware
  via `brandFor(account.product)` and serve BOTH products at `/accounts`
  (AO's `_redirects` proxies `/accounts` → Railway `/app/`). BUT AO owners
  primarily live in the plain-JS site, NOT this React screen.

CONSEQUENCE: a feature shipped only in the React SPA does NOT appear for AO owners
on their main site. "Do this for both services, independently" = TWO hand-built
implementations + TWO deploys. State plainly which product each piece of work hits.

## 1. Cancel → lockout → reactivate (both products, no new trial)

Backend is shared (`solar-operator` API). The frontends are separate.

- **cancel-trial** (`POST /v1/onboarding/cancel-trial`, `api/onboarding.py`): only
  fires while `subscription_status=='trialing'`; sets `active=False`,
  `subscription_status='cancelled'`, detaches the card. A trialing tenant has no
  Stripe subscription yet, so `stripe_subscription_id` is null there.
- **The lockout was the real gap**: cancelling flips the DB, but nothing locked a
  cancelled tenant OUT of the dashboard (login mints a token regardless of status;
  `/account` deliberately lets cancelled tenants through). So it "did nothing" from
  the user's seat. Fix = a full-page gate per product:
  - NEPOOL: React `CancelledGate` component, rendered in `DashboardLayout` when
    `account.active === false && status in ("cancelled","canceled")`. Mirror the
    existing `paused_no_card`→`TrialEndedGate` pattern. Suppress trial/heartbeat
    banners when cancelled.
  - AO: a vanilla full-viewport overlay in `app.js` (`aoShowCancelledGate`,
    `aoIsCancelled`), triggered from BOTH authoritative `/v1/account` reads
    (the boot-time whoami fetch in app.js AND `loadAccount()` in sandbox.js).
- **Reactivation = restart a PAID subscription with NO trial** (the user already
  used their trial). Reuse, don't reinvent:
  - `create_subscription_for_tenant()` in `api/stripe_helpers.py` ALREADY makes a
    no-trial paid subscription for BOTH products (NEPOOL: setup fee + per-array;
    AO: per-kWh METERED, no quantity, no setup fee). It clears `trial_ends_at`.
  - New `POST /v1/account/reactivate` (gated to cancelled tenants): returns a Stripe
    Checkout setup-mode URL tagged `reactivate=1`. The `setup_intent.succeeded`
    webhook stores the card and, seeing the tenant cancelled (or the flag), calls
    `create_subscription_for_tenant` → flips back to active.
  - **WEBHOOK BUG fixed**: `_process_subscription_deleted` (Stripe-side cancel →
    "canceled") left `stripe_subscription_id` populated. `create_subscription_for_tenant`
    short-circuits on `already_active` if that id is set → reactivation would
    silently no-op. FIX: clear `stripe_subscription_id = None` on cancel.
  - Gate cancel UI to `trialing` (NEPOOL: only render `DangerZoneCard` when trialing;
    AO: only show the cancel row when trialing). Post-trial cancellation goes
    through the Stripe billing portal, not this button.
- **AO gate CTA**: do NOT use "email us to reactivate" — the requirement was a
  real self-serve "Start my subscription →" that POSTs `/v1/account/reactivate`
  and redirects to checkout. Honest microcopy: "Billing starts today — your free
  trial has already been used."
- Tests live at `tests/test_reactivate.py` (NEPOOL no-trial, AO metered no-trial,
  non-cancelled rejection, checkout-URL path). Run with
  `DATABASE_URL=sqlite:///./test.db python -m pytest`. AO metered price env var is
  `STRIPE_AO_KWH_PRICE_ID` (NOT `STRIPE_ARRAY_OPERATOR_PRICE_ID`).

## 2. Sandbox data-source ROUTING (Array Operator, plain-JS)

Ford's rule, stated emphatically: **an array shows ONLY in the section matching
where its data comes from.** Vendor-sourced array → vendor view ONLY. Utility-
sourced array → utility view ONLY. Mutually exclusive — NO "appears in both," NO
"show everything" fallback. (My first two attempts were wrong: one let dual-feed
arrays show in both views; one had a `kept.length ? kept : cols` fallback that
leaked all arrays into the wrong section. Both were rejected.)

- The Vendor⇄Utility toggle already existed (it used to only swap each card's
  graph). The fix makes it FILTER which arrays render, in BOTH the canvas
  (`render`) and grid (`renderGrid`) paths.
- Strict classifier: `arrayStream(col)` returns exactly ONE of `"vendor"` /
  `"utility"`. Vendor = has inverters / a vendor / `daily_split.has_vendor`.
  Utility = `daily_split.has_utility` and not vendor. `filterColsByStream` =
  `cols.filter(c => arrayStream(c) === stream)` — no fallback.
- Empty-section honesty: when a section legitimately has 0 arrays but the other
  has some, show "No arrays get their data from a <x> source. N arrays are under
  <other> — switch above," keeping the toggle visible. Do NOT fall back to the
  "Nothing connected yet" empty state (misleading).
- Source fields come from `build_fleet_tree` (`api/inverter_fleet.py`): per-array
  `vendor`/`vendors`, `daily_split.has_vendor`/`has_utility`, `inverter_source`,
  `inverter_count`, `source_status`. Carried through `fleet-store.js` `adaptTree`
  + `toColumns`.

## 3. Sandbox "stale snapshot won't clear" — the source-offline banner

Symptom: the source-offline (`source_status.state==="stale"`) banner on an array
card never went away after the vendor feed came back.

- Backend was CORRECT: `_source_status` flips `"stale"`→`"ok"` once the freshest
  inverter `last_report` is within `_SOURCE_STALE_HOURS` (6h), and the frontend
  `sourceStatusHTML` already returns "" when not stale.
- ROOT CAUSE: the sandbox NEVER re-pulled `/v1/array-owners/fleet-tree` once open.
  `fleet-store.js` `load()` fetched once; `refetch()` only fired on user actions.
  So a recovered source kept showing the old snapshot until a manual reload.
- FIX: a guarded background auto-refresh in `fleet-store.js` (`startAutoRefresh`,
  started after first live ingest): refetch every 5 min (well under the 6h stale
  window) AND on tab refocus (`visibilitychange`). Guards so it never disrupts the
  user: skip when `!isLive()`, `document.hidden`, or `_userBusy()` (checks
  `.dragging-active`/`.inv-dragging-active`/`.sb-editing`/focused contenteditable).
  The store `notify()` → sandbox subscription → re-render clears the banner.
- GENERAL LESSON: any "stale UI that should self-clear" on a single-fetch SPA is
  usually a MISSING-REFRESH bug, not a render-condition bug. Check whether the
  view ever re-pulls before touching the display logic.

## 4. Small fixes from this session (class examples)

- **Dead transactional-email link**: the GMP reauth email
  (`send_gmp_reauth_needed_email`, `api/notify.py`) pointed at
  `mypower.greenmountainpower.com/` which doesn't resolve (curl → 000). The
  working login URL the extension/codebase uses is
  `https://greenmountainpower.com/account/login/` (200). LESSON: verify any
  hardcoded external URL in a transactional email actually resolves; cross-check
  against the URL the rest of the codebase uses.
- **Moving a card between products**: "energy history belongs in Array Operator,
  not NEPOOL" = gate the shared React component by product:
  `{account.product === "array_operator" && <SpongeProgressCard />}`. Same pattern
  for any product-specific surface on the shared screen.
- **Removing a "coming soon" note**: just delete the literal `<p>…coming soon</p>`.
- **Debug aids are temporary**: a per-array `src:` debug chip was added then
  removed when no longer needed — when adding a debug tag, expect to remove both
  the call site AND the helper function later; keep it self-contained.

## 5. Deploy + verify checklist (this product family)

- NEPOOL/React: `npx tsc --noEmit` → `npm run build` → sync `dist`→`api/app_dist`
  → commit `api/app_dist` too → `git push` → wait for Railway SUCCESS →
  verify the served bundle on the Railway image (grep the deployed
  `app_dist/assets/index-*.js` for your marker), NOT just deploy-list SUCCESS.
- AO/plain-JS: `node --check <file>.js` → `python3 scripts/netlify_api_deploy.py`
  → commit+push `array-operator` repo → `curl https://arrayoperator.com/<file>.js
  | grep -c <marker>` to confirm the live asset updated.
- Backend route live = `curl -X POST <railway>/v1/<route>` returns 401 (exists,
  needs auth) NOT 404. This is the standard "route deployed" check.
- HONESTY: Ford clamped down on interactive prod-endpoint probing and authenticated
  screenshots aren't possible for his private fleet/login. Verify via: passing
  unit tests (Stripe mocked), route-exists 401 checks, and grepping live served
  assets. SAY clearly when end-to-end (real card / real outage / logged-in UI)
  was NOT walked through — don't claim "verified" for what you couldn't exercise.
  Always tell Ford to HARD-REFRESH (Cmd/Ctrl+Shift+R) after an SPA change.
