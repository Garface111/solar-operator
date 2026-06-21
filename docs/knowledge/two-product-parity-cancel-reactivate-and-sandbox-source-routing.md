# Two-product parity: cancel / reactivate / lockout + sandbox source routing

Load when working on subscription lifecycle (cancel, reactivate, lockout gates)
or the Array Operator sandbox/fleet canvas. Covers the dual-frontend reality and
several real bugs fixed in this class of work.

## THE DUAL-FRONTEND RULE (the thing that bites every time)
Every owner-facing feature exists in TWO codebases that do NOT share UI code:
- **NEPOOL Operator** = React SPA at `solar-operator/web/app/` (TSX). Served by
  the Railway API from `api/app_dist/`. The `/accounts` route of BOTH products
  proxies to this SPA, and it is brand-aware via `brandFor(account.product)` /
  `account.product === "array_operator"`.
- **Array Operator** = plain-JS Netlify app at `/root/array-operator/public/`
  (`index.html` + `app.js` + `sandbox.js`). This is where AO owners actually
  live (arrayoperator.com), NOT the React `/accounts` proxy.

So a fix in the React SPA does NOT carry to AO and vice-versa. When the user says
"do this for both services, independently," build the React version in
`web/app/` AND a hand-built plain-JS equivalent in `array-operator/public/`.
Confirm which surface the user actually sees before assuming a fix lands.

Backend (`solar-operator/api/`) IS shared — one set of endpoints serves both,
keyed on `Tenant.product` ("nepool" | "array_operator").

### Deploy paths differ per frontend
- React SPA + backend: `cd web/app && npm run build` → `rm -rf api/app_dist &&
  cp -r web/app/dist api/app_dist` → `git push origin HEAD:main` (Railway
  auto-deploys ~70s). Verify served bundle via `railway ssh "grep -l '<marker>'
  /app/api/app_dist/assets/index-*.js"`.
- AO plain-JS: `node --check app.js sandbox.js` then deploy via
  `python3 scripts/netlify_api_deploy.py` (Netlify CLI is unreliable here — see
  ao-deploy ref). Verify with `curl -s https://arrayoperator.com/<file>.js | grep -c '<marker>'`.

## SUBSCRIPTION LIFECYCLE (cancel → lockout → reactivate, no second trial)

### Cancel
- Endpoint: `POST /v1/onboarding/cancel-trial` — only acts when
  `subscription_status == "trialing"` (else 400). Detaches the Stripe PM, sets
  `active=False, subscription_status="cancelled", trial_ends_at=None,
  stripe_payment_method_id=None`.
- NEPOOL spelling is `"cancelled"` (two l's); the Stripe webhook
  (`_process_subscription_deleted`) writes `"canceled"` (one l). ALWAYS match
  BOTH spellings in any cancelled check.
- BUG class fixed: cancel flips the DB but the SPA still loaded the full working
  dashboard → user reads it as "cancel did nothing." A cancelled account MUST be
  locked out. Mirror the existing `paused_no_card` → `TrialEndedGate` pattern.
  - NEPOOL: `CancelledGate.tsx` rendered from `DashboardLayout.tsx` when
    `account.active === false && status ∈ {cancelled, canceled}`. Suppress trial
    banner + heartbeat banner in that state.
  - AO: full-viewport overlay built in `app.js` (`aoShowCancelledGate`,
    z-index 2147483647, `document.body.style.overflow="hidden"`), triggered from
    BOTH authoritative `/v1/account` reads (the whoami fetch in app.js AND
    `loadAccount()` in sandbox.js) for defense in depth. Exposed on `window` so
    sandbox.js can call it.
- A cancelled user can still LOG IN (token mints regardless of status) — they
  just hit the gate. Login-level refusal is a separate, optional hardening.

### Reactivate (start subscription again, NO second free trial)
The user's rule: a cancelled account reactivates into a PAID subscription with
no trial — they already used their trial.
- `POST /v1/account/reactivate` (added this class of work): gated to cancelled
  tenants only (400 otherwise); lazy-creates the Stripe Customer; returns a
  Stripe Checkout Session `mode="setup"` tagged `metadata.reactivate="1"` +
  `setup_intent_data.metadata.reactivate="1"`; success/cancel URLs
  `?reactivated=1` / `?reactivate_cancelled=1`.
- The `setup_intent.succeeded` webhook stores the card and, when the tenant is
  cancelled (both spellings) OR `reactivate=1` flag is set, calls
  `create_subscription_for_tenant(tid)` — which already creates a PAID, NO-TRIAL
  subscription (it clears `trial_ends_at`) and works for BOTH products:
  - NEPOOL: setup fee (`STRIPE_SETUP_PRICE_ID`, qty 1) + per-array
    (`STRIPE_ARRAY_PRICE_ID`, qty = billable array count).
  - Array Operator: single per-kWh METERED line (`STRIPE_AO_KWH_PRICE_ID`, NO
    quantity — Stripe rejects quantity on a metered price), no setup fee.
- **CRITICAL BUG fixed**: `create_subscription_for_tenant` short-circuits with
  `already_active` if `stripe_subscription_id` is still set. The webhook cancel
  path left the dead sub id populated → reactivation silently no-op'd. FIX:
  clear `t.stripe_subscription_id = None` in `_process_subscription_deleted`
  (the trial-cancel path is fine — a trialing tenant has no sub id yet).
- Frontend CTA on the gate becomes "Start my subscription →" (NOT "email us"):
  NEPOOL calls `reactivateAccount()` in `lib/api.ts`; AO POSTs
  `/v1/account/reactivate` and redirects to `d.checkout_url`. Microcopy:
  "Billing starts today — your free trial has already been used."
- Tests: `tests/test_reactivate.py` — cover NEPOOL no-trial, AO metered no-trial
  (assert `trial_period_days` NOT in the Subscription.create call, metered line
  has no quantity), non-cancelled rejection, checkout-url path. Run with
  `DATABASE_URL=sqlite:///./test.db python -m pytest`.

## SANDBOX DATA-SOURCE ROUTING + DEBUG TAG (Array Operator)
The fleet-tree (`GET /v1/array-owners/fleet-tree`, built by
`api/inverter_fleet.py build_fleet_tree`) returns per-array provenance fields you
can use entirely client-side (no backend change) for debugging + routing:
- `vendor` (single) / `vendors` (array) — inverter telemetry source(s).
- `daily_split.has_vendor` / `has_utility` — which DailyGeneration STREAMS carry
  data. Stream classification lives in `_daily_stream()` /`_VENDOR_SOURCES` vs
  `_UTILITY_SOURCES` (vendor = solaredge/fronius/sma/chint/extension_pull/csv/
  manual; utility = gmp_api/gmp_portal_scrape/utility_meter/smarthub/bill_prorate).
- `inverter_source` ("live"), `source_status.state` (ok|stale|dark|none),
  `inverter_count`.
- Per-array debug tag: `sourceDebugTag(col)` in sandbox.js renders a compact
  `src: <vendor> · V✓/✗ U✓/✗ · live · <state> · N inv` chip under the array
  name; red when no stream at all, amber when stale/dark.
- Source ROUTING: the existing Vendor/Utility stream toggle (`getStream()` /
  `STREAM_KEY`, default "vendor") now FILTERS which arrays show, not just the
  graph: `filterColsByStream(cols)` keeps vendor-fed arrays in vendor view,
  utility-fed in utility view; an array with both feeds shows in either. Applied
  in BOTH render paths (`render()` canvas + `renderGrid()`). EMPTY-SAFE: if a
  stream classifies to zero arrays, fall back to showing ALL (never blank the
  canvas by filter). Re-render is driven by `renderFromStore()` on toggle.

## Misc fixes from this class of work
- Dead email link: `send_gmp_reauth_needed_email` pointed at
  `mypower.greenmountainpower.com` which does NOT resolve (curl → 000). Correct
  GMP login URL the extension watches = `https://greenmountainpower.com/account/login/`.
- Energy-history ("data sponge", `SpongeProgressCard`) is an Array-Operator-only
  feature — gate it `account.product === "array_operator"` so NEPOOL operators
  don't see it on their master account.
- Removing a UI element from one product: e.g. the NEPOOL reports "Add customer"
  card (`AddCustomerCard`) and the "WEC support coming soon" note — these live in
  specific TSX (NepoolReportsTab.tsx, UtilityConnectionsCard.tsx); after removing
  the render + import, the leftover component file is harmless dead code.
