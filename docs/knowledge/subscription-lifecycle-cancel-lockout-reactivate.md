# Subscription lifecycle: cancel → lockout → reactivate (BOTH products)

Covers self-cancel, the cancelled-account lockout gate, and "start subscription
again (no trial)" reactivation. The class lesson: a tenant **status change must
be enforced in BOTH frontends independently**, and the reactivation path reuses
existing Stripe setup-checkout + webhook plumbing rather than inventing a new one.

## TWO frontends, enforce in BOTH (the #1 trap)
Solar Operator has two independent frontends on ONE shared FastAPI backend:
- **NEPOOL Operator** = React SPA at `solar-operator/web/app/` (served by Railway
  at `/app/`, proxied to `nepooloperator.com/accounts`). Account UI lives in
  `screens/DashboardLayout.tsx` (the authed shell) + `screens/AccountTab.tsx`.
- **Array Operator** = plain-JS Netlify app at `/root/array-operator/public/`
  (`index.html` + `app.js` global boot + `sandbox.js` owns the `#account` tab).
  `arrayoperator.com/accounts` DOES proxy to the React SPA, BUT AO owners actually
  live in the plain-JS app — so a React-only fix does NOT cover AO.

A backend change (e.g. flip tenant to `cancelled`) is invisible until EACH
frontend enforces it. When asked to "fix for both services, independently,"
build the gate/control in BOTH codebases — they share nothing on the client.

## Cancelled-account lockout gate (mirror the paused_no_card pattern)
The backend `cancel-trial` correctly sets `subscription_status='cancelled',
active=False` — but neither frontend locked a cancelled tenant out, so "cancel did
nothing" from the user's seat (login mints a token regardless of status; `/account`
deliberately lets cancelled tenants through to view/export).

Fix = full-page gate, modeled on the existing `paused_no_card` →
`TrialEndedGate` pattern:
- **NEPOOL (React):** add `components/CancelledGate.tsx`; in `DashboardLayout.tsx`
  compute `cancelled = account.active === false && status ∈ {cancelled, canceled}`,
  and gate the `<main>` Outlet (`cancelled ? <CancelledGate/> : pausedNoCard ?
  <TrialEndedGate/> : <Outlet/>`). Also suppress the trial banner + heartbeat
  banner when cancelled.
- **Array Operator (plain-JS):** add `aoIsCancelled(a)` + `aoShowCancelledGate()`
  to `app.js` (loads first → covers every tab). Render a fixed full-viewport
  overlay (`z-index:2147483647`, `body.style.overflow='hidden'`). Trigger it from
  BOTH authoritative `/v1/account` reads: the boot-time whoami fetch in `app.js`
  AND `loadAccount()` in `sandbox.js`. Expose `window.aoIsCancelled` /
  `window.aoShowCancelledGate` so sandbox.js reuses the same logic.
- Match BOTH spellings: `"cancelled"` (from `cancel-trial`) and `"canceled"`
  (from the Stripe `_process_subscription_deleted` webhook). Guard on
  `active === false` so a re-subscribed tenant is never gated.

## Self-cancel control (AO had none)
AO's plain-JS app had NO cancel button at all. Add a "Danger zone → Cancel my
account" row in the `#account` tab (`sandbox.js renderAccountList` → `cancelRow(a)`
+ `wireCancelRow()`), shown ONLY while `trialing` (mirrors NEPOOL's
`DangerZoneCard` gating — post-trial cancels go through the Stripe billing
portal). Inline confirm (Cancel → Keep / Yes), POST `/v1/onboarding/cancel-trial`,
then hand straight off to `window.aoShowCancelledGate()`.

## "Start subscription again — NO trial" reactivation
Requirement: a cancelled operator restarts a PAID subscription immediately, no new
trial (they already used it). Reuse existing plumbing — do NOT build a new
subscription creator:
- `create_subscription_for_tenant()` (stripe_helpers.py) ALREADY creates a
  no-trial paid sub for BOTH products (NEPOOL: setup fee + per-array; AO: per-kWh
  metered, no fee/quantity) and clears `trial_ends_at`. Never pass
  `trial_period_days`.
- Add `POST /v1/account/reactivate` (account.py) — gated to cancelled tenants
  only; returns a Stripe Checkout `mode='setup'` URL tagged
  `metadata.reactivate='1'`. Mirror `add_payment_method` (lazy-create Customer).
- Extend the `setup_intent.succeeded` webhook: previously it auto-subscribed ONLY
  when `was_paused` (paused_no_card). Broaden to also fire when `was_cancelled`
  (active=False + status ∈ {cancelled,canceled}) OR `reactivate='1'` metadata.
- Frontends: replace any "email us to reactivate" CTA with a "Start my
  subscription →" button. NEPOOL: `reactivateAccount()` in `lib/api.ts` →
  `CancelledGate.tsx`. AO: button in `aoShowCancelledGate()` that POSTs
  `/v1/account/reactivate` and redirects to `checkout_url`. Honest microcopy:
  "Billing starts today — your free trial has already been used."

### LANDMINE: the `already_active` short-circuit on webhook-cancelled tenants
`create_subscription_for_tenant` no-ops (returns `already_active`) if
`stripe_subscription_id` is still set. The Stripe-webhook cancel path
(`_process_subscription_deleted`) set `active=False` but LEFT the dead sub id on
the tenant → reactivation would silently no-op. FIX: clear
`t.stripe_subscription_id = None` on cancellation. (Trial-cancel is fine — a
trialing tenant has no sub id yet.)

## Verification (Ford clamped down on prod endpoint probing — Jun'26)
Do NOT interactively curl prod MUTATING/auth'd endpoints without per-command OK.
Acceptable proof:
- Route-exists: a no-auth POST returns **401, not 404** (route registered).
- Served-bundle: `railway ssh "grep -l '<marker>' /app/api/app_dist/assets/index-*.js"`
  (image has no git; grep the disk, not deploy-list SUCCESS).
- AO live: `curl arrayoperator.com/app.js | grep -c '<marker>'`.
- Logic: unit tests with Stripe mocked (see tests/test_reactivate.py — covers
  NEPOOL no-trial, AO metered no-trial, non-cancelled rejection, checkout-URL).
- State the honest gap: route+bundle+unit-tests is NOT a live human Stripe
  round-trip; say so and offer the walkthrough rather than claiming "verified".

## Dead GMP reauth link (notify.py)
`send_gmp_reauth_needed_email` pointed at `https://mypower.greenmountainpower.com/`
which does NOT resolve (curl → 000). Correct GMP login (used by the extension +
rest of codebase) = `https://greenmountainpower.com/account/login/` (200). When a
hardcoded URL "doesn't work," curl it first — don't assume.

## Per-product email FROM
`branding.from_address('nepool')` resolves to `hello@nepooloperator.com` (note:
auto-generated reauth emails Bruce received showed `admin@nepooloperator.com` —
cosmetic difference on the same verified domain; both deliver, Reply-To routes to
support).

## Deploy mechanics recap (both repos are independent)
- solar-operator (backend + NEPOOL SPA): `build_app.sh` builds `web/app` → copies
  `dist` to `api/app_dist`; `git push origin HEAD:main` auto-deploys Railway (~70s).
  After a column-adding change: push → WAIT deploy → migrate. (No new column here.)
- array-operator: deploy via `scripts/netlify_api_deploy.py` (Netlify CLI is
  unreliable here — cached expired session overrides the token). site_id
  966cb1f5-944e-41fd-855b-10053edc5d18.
- Stage only your own hunks on the shared solar-operator tree; never `git add -A`.
