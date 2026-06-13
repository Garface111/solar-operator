# Array Operator — Billing (handoff / go-live runbook)

Status: **LIVE (Jun 13 2026).** Option B is the live owner price; signups via
`/v1/onboarding/start` with `product:"array_operator"` get the identical 14-day
no-card trial and bill on the owner price when they add a card.

- Live Stripe price: `price_1Thu2xC69Dj6DbzdllYcfKYc` (product `prod_UhIeV7EGkFR4uP`),
  livemode=True, graduated monthly: 1 free / $9 (2–10) / $8 (11–50) / $6.50 (51+).
- Railway env: `STRIPE_AO_ARRAY_PRICE_ID=price_1Thu2xC69Dj6DbzdllYcfKYc` set.
- `tenants.product` column present in prod; migration ran.
- Smoke-tested end-to-end (AO trial signup → product/trial verified → tenant deleted).

## The decision (pending Ford's final word)
Audited Array Operator as a customer (owner-facing app: dollar-first verdict,
done-for-you warranty claims, peer-index ground truth, one-credential discovery).

The owner is a DIFFERENT buyer from the NEPOOL verifier. Flat $15/array/mo (the
NEPOOL price) over-charges a single residential owner — the fee can rival the
loss it catches. So the default baked in here is **Option B**:

| arrays   | unit    | note                         |
|----------|---------|------------------------------|
| 1st      | FREE    | residential wedge            |
| 2–10     | $9.00   | full owner unit              |
| 11–50    | $8.00   | ~11% off (prosumer)          |
| 51+      | $6.50   | ~28% off (fleet host, Bruce) |

No setup fee (NEPOOL has $250; owners don't). Graduated tiers (no cliff).
Worked examples: 1 array = $0/mo, Bruce's 7 = $54/mo, 25 = $208/mo.

To switch to flat $15 (Option A) or $12 (Option C): edit `api/pricing_array_operator.py`
TIERS only, re-run the price script.

## What's built (all on the branch)
- `api/pricing_array_operator.py` — owner TIER table + graduated math (mirror of api/pricing.py).
- `scripts/create_array_operator_prices.py` — mints the Stripe product + graduated price.
  REFUSES to run against sk_live_ without `--confirm-live`. Has `--dry-run`.
- `api/stripe_helpers.py` — `array_price_id_for_product(product)` routes AO tenants
  to `STRIPE_AO_ARRAY_PRICE_ID`; `create_subscription_for_tenant` skips the setup
  fee for AO tenants; `reconcile_subscription_quantity` matches either price id.
- `api/models.py` — `Tenant.product` ("nepool" default | "array_operator").
- `api/migrate.py` — idempotent `ALTER TABLE tenants ADD COLUMN product`.
- Tests: `tests/test_pricing_array_operator.py`, `tests/test_product_price_routing.py`
  (full suite: 802 passed).

## ⚠️ Pre-go-live blockers
1. **Live/test key mismatch in prod RIGHT NOW**: `STRIPE_SECRET_KEY=sk_live_…`
   but `STRIPE_PUBLISHABLE_KEY=pk_test_…`. Fix before any owner checkout.
2. Creating the price runs against LIVE Stripe (sk_live). Needs explicit `--confirm-live`.
3. The Array Operator site has NO pricing surface and NO checkout wiring yet —
   this branch does the BACKEND billing only. The owner site (array-operator-ea)
   still needs a pricing section + a connect→signup→add-card flow pointed at the
   backend. (NEPOOL uses Checkout mode='setup' → add-card → subscription; reuse that.)

## Go-live sequence (when Ford says go)
1. Decide price (confirm Option B or switch TIERS).
2. Fix the pk_test/sk_live mismatch on Railway.
3. `railway ssh "cd /app && python -m scripts.create_array_operator_prices --confirm-live"`
4. `railway variables --set "STRIPE_AO_ARRAY_PRICE_ID=price_…"` (from step 3 output).
5. `railway ssh "cd /app && python -m api.migrate"` (adds tenants.product).
6. Merge branch → main → push (auto-deploys). Verify suite green first.
7. Mark owner signups with `product="array_operator"` at tenant creation.
8. Build the owner-site pricing + checkout surface (separate frontend task).
