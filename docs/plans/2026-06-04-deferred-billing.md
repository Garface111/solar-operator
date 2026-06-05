# Deferred Billing — Implementation

## Goal
Ship the deferred-billing flow described in
`docs/DEFERRED_BILLING_DESIGN.md`. Operator pays nothing at signup;
card is collected via Stripe `mode='setup'`; after a 4-day trial we
charge based on the real array count.

## Defaults (orchestrator-set, can be flagged in summary if you disagree)
1. **Trial length:** 4 days (calendar days, not business days).
2. **Trial countdown UI:** subtle dashboard banner only. No emails
   during the trial — that breaks the "kitchen-table" trust window.
3. **Zero arrays at trial end:** extend trial by 3 more days with a
   "Add your first array" CTA email. After the extension, charge
   minimum (1 array). This handles the legitimate "I haven't logged
   into my portal yet" case without dumping someone into past-due.
4. **Cancel-during-trial:** free, one-click, no questions.
5. **Existing tenants migration:** none — greenfield. Skip migration
   pathways. Any existing tenant.active=True without subscription is
   treated as comped (don't auto-charge them).

## Tasks

### Task 1 — Schema
- Add to `api/models.py`:
  - `Tenant.trial_ends_at: Optional[datetime]` (nullable)
  - `Tenant.stripe_payment_method_id: Optional[str]` (nullable, indexed)
  - `Tenant.trial_extended: bool = False` (default false, tracks
    whether we've already done the 3-day extension)
- Add columns to `api/migrate.py`:
  - `ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP NULL`
  - `ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_payment_method_id TEXT NULL`
  - `ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_extended BOOLEAN NOT NULL DEFAULT FALSE`

### Task 2 — Switch checkout to setup mode
In whichever file currently creates the Stripe Checkout Session for
onboarding (look for `mode='subscription'`):
- Change `mode='subscription'` → `mode='setup'`
- Remove `line_items` (setup mode doesn't take them)
- Add `payment_method_types=['card']` and a `setup_intent_data` if
  needed for SCA / off-session future charges (`usage='off_session'`).
- Keep `success_url` and `cancel_url` as they are.

### Task 3 — Webhook handler for setup mode
In the Stripe webhook handler, add a branch for
`checkout.session.completed` where `mode == 'setup'`:
- Pull `customer` and `setup_intent` IDs from the event.
- Retrieve the SetupIntent, get its `payment_method`.
- Store on tenant: `stripe_customer_id`, `stripe_payment_method_id`.
- Set `tenant.trial_ends_at = utcnow() + 4 days`.
- Set `tenant.active = True`.
- Set `tenant.subscription_status = 'trialing'`.

Leave the existing `mode='subscription'` branch intact for safety.

### Task 4 — Trial-end cron / scheduler
- Add a function `finalize_expired_trials()` in `api/scheduler.py`
  (or wherever scheduled jobs live):
  - Find tenants where `trial_ends_at <= now()` AND
    `subscription_status == 'trialing'`.
  - For each: count their arrays (sum across all clients).
  - If count == 0 AND not trial_extended:
    - Set `trial_ends_at += 3 days`, `trial_extended = True`
    - Send the "add your first array" email
    - Continue (no charge yet)
  - Else (count >= 1, OR already extended):
    - Create Stripe Subscription with:
      `customer=stripe_customer_id`,
      `items=[{price: SETUP_PRICE_ID, quantity: 1},
              {price: PER_ARRAY_PRICE_ID, quantity: max(count, 1)}]`,
      `default_payment_method=stripe_payment_method_id`
    - Store `tenant.stripe_subscription_id`
    - Set `subscription_status = 'active'`, `trial_ends_at = None`
    - Send "charged $X" email
- Wire this into the existing scheduler tick. Hourly is fine.

### Task 5 — Dashboard trial banner
In `web/app/src/`:
- Add a `TrialBanner` component that shows when
  `tenant.trial_ends_at` is set and in the future.
- Copy: "{N} days left in your trial — we'll charge based on your
  final array count." (warm tone, not anxious)
- Use the same Card/Button language as onboarding for consistency.
- DO NOT block any UI; informational only.

### Task 6 — Cancel-during-trial endpoint
- Add `POST /v1/onboarding/cancel-trial` that:
  - Verifies caller is tenant owner
  - Detaches PaymentMethod from Stripe Customer
  - Deletes tenant (or marks `cancelled_at=now()` if soft-delete is
    convention — check models.py)
- Add a "Cancel trial" link in dashboard Settings tab — confirm
  modal, no friction.

### Task 7 — Tests
Add to `tests/`:
- `test_deferred_billing_setup_mode.py` — checkout creates SetupIntent
- `test_trial_finalization.py` — cron creates subscription for arrays > 0
- `test_trial_zero_arrays.py` — first run extends; second run min-bills
- `test_trial_cancel.py` — cancel detaches PM and tombstones tenant
Run `pytest tests/test_deferred_billing*.py tests/test_trial*.py` —
must be green.

### Task 8 — Verify
- All tests green
- `python -m api.migrate` runs clean locally
- `npm --prefix web/app run build` clean
- DO NOT commit yet — leave dirty for orchestrator review.

## Constraints
- TypeScript+React+FastAPI+SQLAlchemy as in CLAUDE.md.
- NO new dependencies.
- Stripe price IDs come from env vars: `STRIPE_PRICE_SETUP_FEE`,
  `STRIPE_PRICE_PER_ARRAY`. Check `.stripe-keys-test.env` /
  `.stripe-keys-live.env` for the existing names — match them, don't
  invent new ones.
- DO NOT commit, DO NOT push.
- Emit a 5-line summary.
