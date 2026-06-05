# Deferred Billing Design (Backlog #2)

## Goal

At onboarding, collect the operator's payment method WITHOUT charging.
Give them 4 days to finalize real clients + arrays via portal captures.
Then charge based on actual array count.

## Current state (immediate billing)

```
Sign up
  → /v1/onboarding/checkout creates Stripe Checkout Session
  → Stripe Checkout collects card + charges immediately
    (setup fee one-time + per-array recurring × estimated_quantity)
  → checkout.session.completed webhook → tenant.active = True
  → onboarding wizard advances to extension step
```

Problem: operator must lock in array count at the payment step.
Either they over-estimate and overpay, or under-estimate and we
under-bill. Neither matches "the system figures it out for you" vector.

## Proposed state (deferred billing)

```
Sign up
  → /v1/onboarding/checkout creates Stripe Checkout in SETUP mode
    (mode='setup' instead of 'subscription')
  → Stripe collects card details, $0 authorization
  → checkout.session.completed webhook fires with mode=setup
  → backend creates Stripe Customer + attaches PaymentMethod
  → tenant.active = True
  → tenant.trial_ends_at = now() + 4 days
  → tenant.stripe_payment_method_id = pm_xxx (stored for later)
  → onboarding wizard advances normally

For 4 days:
  → operator captures portal logins, multi-login autopop creates Clients
  → dashboard shows banner: "X days left to finalize — we'll charge
    based on your final array count"

At trial_ends_at:
  → backend cron job (existing scheduler or new one) walks all tenants
    where trial_ends_at <= now AND no subscription yet
  → for each: count current arrays, create Stripe Subscription
    (customer=stored cus_xxx, items=[setup, per-array × real_count],
     default_payment_method=stored pm_xxx)
  → Stripe charges the saved card immediately on subscription creation
  → tenant.stripe_subscription_id stored
  → tenant.trial_ends_at = None (sentinel: no longer in trial)
  → email operator: "Charged $X today for Y arrays. First invoice attached."

Edge cases:
  → Trial expires with zero arrays → still charge minimum (1 array)
    with a "first month at minimum" explainer email; encourage them to
    finalize so future invoices reflect real count.
  → Operator adds arrays after trial ends → existing
    reconcile_subscription_quantity runs at next array change (already
    wired in api/account.py and api/onboarding.py); next invoice
    reflects new count.
  → Operator deletes everything before trial ends → trial expires,
    minimum bill applies. (Manual cancellation handled separately.)
  → Card fails at trial end → Stripe `invoice.payment_failed` webhook
    fires; we email operator + flag tenant.payment_status='past_due'.
    Existing webhook is in api/stripe_webhook.py.
```

## Data model changes

`tenants` table:
- `trial_ends_at` (timestamp, nullable) — when 0 the operator is post-trial
- `stripe_payment_method_id` (text, nullable) — stored pm_xxx from setup
  intent so we can attach it as the default on subscription creation
- `subscription_status` (already exists) — extends with 'trialing' value

Migrations needed:
- `ALTER TABLE tenants ADD COLUMN trial_ends_at TIMESTAMP NULL`
- `ALTER TABLE tenants ADD COLUMN stripe_payment_method_id TEXT NULL`

## API changes

- `/v1/onboarding/checkout` → switches Stripe Checkout from
  `mode='subscription'` to `mode='setup'`. No line_items at this stage.
  `setup_intent_data.metadata` carries the onboarding_token.

- `api/stripe_webhook.py` checkout.session.completed handler:
  - if mode='setup': extract setup_intent, attach pm to customer,
    activate tenant with trial_ends_at = now + 4d, store pm_id
  - if mode='subscription' (legacy): existing flow (kept for backwards
    compat with any in-flight checkouts)

- New cron: `api/trial_processor.py` or extend `api/scheduler.py`:
  - runs hourly: SELECT tenants WHERE trial_ends_at <= NOW()
    AND stripe_subscription_id IS NULL AND active = TRUE
  - for each, count arrays, create Subscription, email confirmation

## SPA changes

- Onboarding wizard /plan screen copy: "You won't be charged today.
  We'll give you 4 days to finalize your clients, then bill you based
  on what's there."
- Dashboard banner during trial: "X days left in your trial — your first
  bill on {date} will be ${total} based on current count ({n} arrays).
  Add more clients to lock in the right amount."
- /done screen: emphasize the 4-day window.

## Test strategy

- Unit: mock stripe.checkout.Session.create with mode='setup',
  assert tenant.trial_ends_at is set
- Integration: simulate 4-day-later cron tick, assert Subscription
  created with correct quantity from real array count
- Edge: zero arrays at trial end, payment method declined, etc.

## Open product questions for Ford

1. Trial length: 4 days is what you said. Reasonable? Want a knob
   per-tenant in case you want to extend for whales?
2. Trial countdown surfacing: top banner on dashboard? Email at
   day 1/2/3?
3. Zero-array trial expiry: minimum bill (1 array) or pause and
   keep trial going? (I'd default to minimum bill so we don't have
   free freeloaders.)
4. Cancellation during trial: should an operator be able to bail
   without being charged? Probably yes — Stripe supports
   cus.delete or detach pm.
5. Existing tenants: this is greenfield (no production paying users
   yet). Skip the migration of existing tenants?
