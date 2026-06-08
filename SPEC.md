# feat/no-upfront-payment — SPEC

## Vector
Remove card-collection from signup entirely. Operator hits sign-up → instantly in a 14-day trial → adds card later from the Plan & billing card in the Accounts tab. Welcoming, less offensive, matches "kitchen-table" voice.

## Decisions locked
- **Q1 (trial expires, no card on file):** AUTO-PAUSE — keep dashboard read-only, stop sending client reports, banner "add a card to resume." Tenant stays alive. No deletion. Operator can add card any time and unpause.
- **Q2 ($250 setup fee timing):** Rolled into the first subscription invoice at trial-end (current behavior, just deferred until card lands + trial expires).
- **Q3 (trial length):** 14 days, unchanged.

## Frontend — web/onboarding/

### ClientSetup.tsx
- DELETE the createCheckout() redirect from handleContinue. Just `sessionStorage.setItem(SO_ARRAY_ESTIMATE_KEY, ...)`, call new `startOnboarding({full_name, email, company, password, array_count})` (replaces createCheckout), stash returned token, then `navigate('/extension')`.
- Copy: heading stays "About how many arrays do you manage?" Subhead → "A ballpark is fine — we use it to set up your subscription. No payment today; your 14-day free trial begins right now."
- Pricing breakdown block (lines 184+): keep the $250 setup + $15/array math BUT reframe — heading "After your trial" instead of "Charged today $0.00". Add a single line at the bottom: "You'll add your card later from the dashboard."
- Drop "Continue to checkout" CTA wording → "Start my free trial →"

### Welcome.tsx
- TOS bullet 1: replace "Free for 14 days — we collect your card to start the trial, but you aren't charged until the trial ends." → "Free for 14 days — no card required. Add a payment method from your dashboard before the trial ends to keep reports flowing."
- Hero callout box (the primary-50 box): replace subline copy → "Trial starts the moment you finish signup — no card needed today. Add your card from the Accounts tab whenever you're ready."

### GetStarted.tsx
- Any copy mentioning card / Stripe / payment at signup — strip. If there's a CTA "Start my free setup" keep it; if it mentions payment, drop the payment reference.

### Done.tsx
- No behavior change — still calls completeOnboarding → mints session → redirects to /accounts/?fresh=1.

### lib/onboarding.ts
- Rename / add `startOnboarding({full_name, email, company, password, array_count}) → {onboarding_token, tenant_id}`. Keep `createCheckout` as a thin wrapper that calls startOnboarding then returns a fake `checkout_url=null` for any stale callers (but ClientSetup is the only caller — delete it once everything works).

## Backend — api/

### onboarding.py
- Add new endpoint `POST /v1/onboarding/start` accepting `{full_name, email, company, password, array_count}`:
  - Create Tenant with `active=True`, `subscription_status='trialing'`, `trial_ends_at=now()+14 days`, `onboarding_stage='extension'`, `onboarding_token=secrets.token_urlsafe(24)`, `stripe_customer_id=None`, `stripe_payment_method_id=None`, `stripe_subscription_id=None`. Hash password into `password_hash` via existing `_hash_password`.
  - Return `{onboarding_token, tenant_id}`.
- KEEP the existing `POST /v1/onboarding/checkout` endpoint mounted but make it a thin shim: do the same tenant creation as /start, then return `{checkout_url: None, onboarding_token, tenant_id}` so any stale wizard bundle in a tab somewhere doesn't crash. Add a deprecation log line.
- `/v1/onboarding/complete` — unchanged semantically, but the tenant is already active when we get here. Don't double-set trial_ends_at; only set it if NULL (the legacy webhook path used to set it). Make sure the welcome email variant matches the new no-card reality.
- `/v1/onboarding/reconcile-checkout` — make it a no-op for tenants that already have `active=True` (just return current state). Old extension popups call this; don't 500 them.

### stripe_webhook.py
- `_process_onboarding_checkout_completed` — keep alive for in-flight legacy sessions (any operator who started checkout BEFORE this deploy and finished after). Add a comment that this is now the legacy path.
- Add `setup_intent.succeeded` handler — when the dashboard's add-card flow completes Stripe Checkout in setup mode, the SetupIntent fires with metadata `tenant_id`. Look up tenant, store `stripe_customer_id` and `stripe_payment_method_id`. Idempotent.

### scheduler.py — finalize_expired_trials()
- BEFORE creating the subscription, check `t.stripe_payment_method_id`. If NULL:
  - Set `t.subscription_status = 'paused_no_card'` (new value), `t.trial_ends_at = None`.
  - Set `t.active = False` (this is what gates report delivery — read existing `Tenant.active` filters in scheduler.py `enqueue_pull_for_all_tenants` and `_deliver_clients_with_frequency` and confirm they exclude `paused_no_card`).
  - Send new `send_trial_paused_no_card_email(to=..., name=...)` — copy: "Your trial ended. Add a payment method from your dashboard to resume reports. We've held all your data — nothing is deleted."
  - Send internal alert.
  - `continue` to next tenant.
- The existing zero-arrays branch still applies BEFORE the no-card check.

### notify.py
- Add `send_trial_paused_no_card_email(to, name)`.
- Update `send_trial_welcome_email` (or whichever fires on /complete) — copy variant for no-card-on-file path. "Your 14-day trial is live. Add your card whenever you're ready — we'll remind you a few days before the trial ends."
- Trial-end reminder emails (if any exist) — branch on `stripe_payment_method_id IS NULL` → "Add a card to keep reports flowing" CTA. If no such reminder exists, ADD one fired from the scheduler at trial_ends_at - 3 days for tenants with no PM on file.

### account.py
- Add endpoint `POST /v1/account/add-payment-method` (authed) — returns Stripe Checkout Session URL in `mode='setup'`, customer=lazy-create-or-retrieve tenant's stripe_customer_id, success_url=`{APP_URL}/accounts/?card_added=1`, cancel_url=`{APP_URL}/accounts/?card_cancelled=1`, metadata `tenant_id` so the webhook can attribute. If `t.stripe_customer_id IS NULL`, create the Customer first with email/name.
- Endpoint `POST /v1/account/resume-from-pause` — invoked after card-added webhook fires while subscription_status='paused_no_card'. Creates subscription (setup fee + per-array × current count), sets `active=True`, `subscription_status='active'`. Can be triggered by the webhook directly after setup_intent.succeeded so the operator doesn't have to click anything to resume.
- Extend `/v1/account/billing-summary` payload to include `has_payment_method: bool` (derived from `stripe_payment_method_id IS NOT NULL`).

### migrate.py
- Add new subscription_status values: `'paused_no_card'` is just a string — no enum migration needed if it's a text column. Verify in models.py. If it's an enum, add migration.
- No new columns needed.

## Frontend dashboard — web/app/

### components/settings/PlanBillingCard.tsx
- When `billing.has_payment_method === false`: render an "Add payment method" CTA at the top of the card (above the manage-billing button). Button calls `addPaymentMethod()` → redirects to Stripe Checkout setup-mode URL.
- When `subscription_status === 'paused_no_card'`: top banner inside the card — amber/red, "Trial ended. Add a card to resume reports." with primary CTA "Add card →".
- Standard manage-billing button (Stripe billing portal) only shows when `has_payment_method === true`.

### components/TrialBanner.tsx
- Add prop `hasPaymentMethod: boolean`.
- If `!hasPaymentMethod`: copy becomes "X days left in your trial — add a card before {endDate} to keep reports flowing →" linking to /accounts/?tab=plan-billing (or wherever the PlanBillingCard lives).
- If `hasPaymentMethod`: keep existing "we'll charge based on your final array count" copy.

### lib/api.ts
- Add `addPaymentMethod()` → POST /v1/account/add-payment-method, returns `{checkout_url}`, then `window.location.href = checkout_url`.
- Extend `BillingSummary` type with `has_payment_method: boolean`.

### Pause UI gating
- DashboardLayout / wherever active=False gates the app: if `subscription_status === 'paused_no_card'`, render the dashboard READ-ONLY with a top-of-page banner — no scrape, no manual report send, no client edit. Just "Add card to resume." Operator can still see all their data.
- Read-only means: disable all mutation buttons in the existing components (look for the same pattern used elsewhere for inactive tenants — there should be one).

## Tests
- `tests/test_onboarding.py` — add test for `POST /v1/onboarding/start` → tenant created in trialing state with trial_ends_at set, no Stripe calls made.
- `tests/test_trial_finalization.py` — add test: tenant with `stripe_payment_method_id=None` at trial expiry → `subscription_status='paused_no_card'`, `active=False`, email sent, no Stripe.Subscription.create call.
- `tests/test_trial_zero_arrays.py` — verify zero-array grace still works for no-card path (zero arrays + no card → still extend 3 days; no card check comes AFTER the zero-arrays check).
- `tests/test_deferred_billing_setup_mode.py` — the existing setup-mode flow tests should be updated to the new no-card-at-signup model OR marked as legacy-flow tests.
- New test: `tests/test_add_payment_method.py` — POST /v1/account/add-payment-method returns Stripe Checkout setup URL, sets metadata tenant_id, lazy-creates Customer.
- New test: `tests/test_resume_from_pause.py` — webhook setup_intent.succeeded on a paused_no_card tenant resumes them: subscription created, status=active.

## Things that stop making sense (probe results)

1. `onboarding_stage='pending_payment'` — DELETE all references. New default stage is 'extension'.
2. `Welcome.tsx` TOS bullets need a copy pass — see above.
3. `Done.tsx` welcome email — already deferred, copy variant needed.
4. `reconcile-checkout` endpoint — becomes no-op for new flow but kept for legacy callers (stale extension popups).
5. `_legacy_signup.py` — already unmounted, no changes needed.
6. Stripe webhook stays mounted; setup-mode checkout completion is now legacy-only (in-flight sessions). New flow uses setup_intent.succeeded fired by the dashboard add-card flow.
7. Bruce (comped) — no changes; subscription_status='comped' bypasses all trial logic.
8. The `trial_extended` flag — still works; only used in the zero-arrays branch.

## Voice constraints (from skill solar-operator-saas)
- Kitchen-table, not enterprise.
- No "subscribe", "purchase", "billing cycle" — just "trial", "card", "charge", "your dashboard".
- Loud uncertainty only on bugs, not on UI copy. Be confident in copy.

## Out of scope
- Don't touch GMCS writer.
- Don't touch extension code unless it has copy mentioning payment at signup.
- Don't change pricing math anywhere.
- Don't deploy. Parent will merge + Bruce won't see the change today.

## Deliverable
- Commit on branch `feat/no-upfront-payment` in this workspace.
- Run `pytest tests/` and report any failures (don't try to mass-fix unrelated).
- Build frontend bundles (`bash build_onboarding.sh` and equivalent for web/app/ — check the package.json scripts) and commit the dist/ outputs so parent doesn't have to.
- Write a short PR-style summary at the end: what files changed, what tests pass, anything that needed a judgment call.
