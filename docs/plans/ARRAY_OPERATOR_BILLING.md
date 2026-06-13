# Array Operator — Billing (handoff / go-live runbook)

Status: **LIVE (Jun 13 2026).** Option B is the live owner price; signups via
`/v1/onboarding/start` with `product:"array_operator"` get the identical 14-day
no-card trial and bill on the owner price when they add a card.

- Live Stripe price: `price_1Thu2xC69Dj6DbzdllYcfKYc` (product `prod_UhIeV7EGkFR4uP`),
  livemode=True, graduated monthly: 1 free / $9 (2–10) / $8 (11–50) / $6.50 (51+).
- Railway env: `STRIPE_AO_ARRAY_PRICE_ID=price_1Thu2xC69Dj6DbzdllYcfKYc` set.
- `tenants.product` column present in prod; migration ran.
- Smoke-tested end-to-end (AO trial signup → product/trial verified → tenant deleted).

## Owner front door (LIVE Jun 13 2026)
The array-operator-ea site now has the signup surface:
- `index.html`: black/green Pricing section (first array free, $9/$8/$6.50
  graduated, 14-day trial, no setup fee) + "Get started" nav CTA → onboarding.html.
- `onboarding.html`: final step calls `/v1/onboarding/start` with
  `product:"array_operator"` → `/v1/onboarding/complete` (mints session +
  magic-link email) → inline "Add a card" via `/v1/account/add-payment-method`
  (Stripe Checkout setup mode). Verified E2E against prod (start 200, complete
  200, tenant created with product=array_operator/trialing, then deleted).
- KNOWN LIMITATION: the SolarEdge `/discover` endpoint is auth-gated (needs a
  bearer token / signed-in tenant), so during pre-signup onboarding it returns
  401 and we fall through to the DEMO cascade. The owner's real key is NOT
  verified or attached during onboarding — it must be connected after sign-in
  from the dashboard. TODO if we want true pre-signup discovery: add an
  unauthenticated "validate key + list sites" endpoint (rate-limited) OR collect
  the key during onboarding and attach it server-side right after tenant create.

## Pre-signup REAL preview (LIVE Jun 13 2026 — resolves the limitation above)
The owner now sees their ACTUAL arrays + value before signing up:
- Backend: `POST /v1/array-owners/public/preview` — UNAUTHENTICATED, rate-limited
  (8 / 5 min per IP), lists real SolarEdge sites + per-site & total annual $
  estimate (peak_kw × 0.14 CF × rate + REC). Saves nothing. Bad/site-level/empty
  key → friendly ok:false; 429 on abuse; 502 on SolarEdge 5xx. CORS already
  allows array-operator-ea. Tests: tests/test_public_preview.py (5).
- Frontend onboarding step 2 → calls /preview, step 3 shows real array names +
  per-array $/yr + a value hero that counts up to the total. On finish, the real
  key is attached via /v1/array-owners/solaredge/connect-account using the
  freshly-minted session (best-effort; a bad key 400s but never blocks the trial).
  Falls back to demo cascade only when the backend is unreachable.
- Verified E2E on prod: preview 200 → start 200 → complete 200 → connect-account
  (200 real key / 400 fake) → trial live; tenant product=array_operator/trialing.
- Value estimate is intentionally rough (0.14 capacity factor); the dashboard
  pins the exact measured figure once live. Copy says "estimated / we pin it once
  you're live" so it never overpromises.

## All-vendor sublime preview (LIVE Jun 13 2026)
The pre-signup real-arrays reveal now works for EVERY available vendor, not just
SolarEdge:
- `POST /v1/array-owners/public/preview` takes `{vendor, config}` (keeps
  `{api_key}` → SolarEdge back-compat). SolarEdge + Locus(+partner_id) enumerate
  the whole account/partner; Locus(no partner)/Fronius/SMA preview the one named
  system via validate(). Per-vendor friendly auth/scope copy; missing fields and
  AVAILABLE=False vendors (Chint) → friendly ok:false (never 5xx). Value estimate
  where peak_kw is known (Fronius peakPower Wp→kW); SMA has no peak → array still
  shows, value null, totals.annual_value_usd null (UI hides the $ hero).
- Frontend: connectVendor() builds the per-vendor config from the collected
  fields and routes ALL brands through /preview; the reveal lede uses the real
  vendor label. SolarEdge still auto-attaches the key after signup via
  connect-account; Locus/Fronius/SMA connect from the dashboard post-signup
  (no account-level multi-attach endpoint for them yet).
- Verified live: SolarEdge + Locus reveals (correct vendor lede, no JS errors),
  Fronius single-system reveal (Wp→kW value), and friendly errors for bad creds /
  missing fields / Chint. Tests: tests/test_public_preview.py (10).

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
