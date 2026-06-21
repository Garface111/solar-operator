# Stripe billing & per-array pricing (NEPOOL + Array Operator)

How billing is structured in `/root/solar-operator`, and how to add a SECOND
product's pricing by reusing the existing graduated-tier engine. Proven Jun 2026
building AND taking LIVE Array Operator (owner) billing.

**STATUS: Array Operator billing is LIVE end-to-end on the per-kWh METERED price (re-verified
Jun 2026).** The CURRENT live price is `price_1TiJ4GC69Dj6DbzdJ6NqJ15u` (metered/graduated:
0.5¢ <20k kWh/mo, 0.45¢ to 200k, 0.4¢ above; product `prod_UhIeV7EGkFR4uP`, `livemode=true`).
Railway env is `STRIPE_AO_KWH_PRICE_ID=price_1TiJ4G…`; `STRIPE_AO_ARRAY_PRICE_ID` is EMPTY/None
(fine — code reads kWh first). The old Option B per-array price `price_1Thu2xC69Dj6DbzdllYcfKYc`
is SUPERSEDED by the kWh conversion — do NOT quote it as live. `tenants.product` migrated,
14-day no-card trial identical to NEPOOL. The owner site front door is live too.

### When Ford says "billing isn't working" — PROBE LIVE STATE before assuming a code bug (Jun 2026)
The notes can say "shipped" while a symptom persists; verify the actual prod state instead of
trusting the reference. Order of probes that pinned it as HEALTHY in one pass:
  1. **Confirm the live price + env match.** `railway ssh "cd /app && python -c \"import os,stripe;
     stripe.api_key=os.environ['STRIPE_SECRET_KEY'];
     print(os.getenv('STRIPE_AO_KWH_PRICE_ID'), os.getenv('STRIPE_AO_ARRAY_PRICE_ID'));
     p=stripe.Price.retrieve('<price_id>',expand=['tiers']);
     print(p.livemode,p.active,p.billing_scheme,p.recurring.usage_type,[(t.up_to,t.unit_amount_decimal) for t in p.tiers])\""`
     → expect livemode=True, active, tiered/graduated, usage_type=metered, the right tier table.
  2. **Confirm code routes to it:** grep `STRIPE_AO_KWH_PRICE_ID|array_price_id_for_product|
     is_array_operator|create_usage_record` — the router must read the kWh var, the metered line
     carries NO quantity + NO setup fee, billing-summary is product-aware.
  3. **Confirm the usage job is SCHEDULED** (not just that the file exists): grep
     `usage_report|report_usage_for_all_owners` in `api/scheduler.py` (job id `ao_usage_report`).
  4. **Run the billing test slice:** `.venv/bin/python -m pytest tests/test_pricing_array_operator.py
     tests/test_product_price_routing.py tests/test_public_preview.py tests/test_array_owners.py -q`.
  5. **The DEFINITIVE launch gate — drive the live signup→card path against prod** (the one step
     that actually needs the live Stripe key). See the smoke-test block below; a real `cs_live_`
     Checkout URL is proof the live key mints Checkout for an AO owner.
If all five are green, the backend is sound and "not working" is elsewhere — the static
arrayoperator.com onboarding JS, a specific toast, or a trial-end edge case. ASK for the exact
symptom rather than re-debugging the (healthy) backend.

### Prod smoke test — signup → add-card → live Checkout (verified Jun 2026, then DELETE the tenant)
Throwaway email on a domain you own. Hit `https://arrayoperator.com` (same-origin proxy):
  1. `POST /v1/onboarding/start` `{"email","full_name","company","array_count":1,"product":"array_operator"}`
     → `{onboarding_token, tenant_id}`.
  2. `POST /v1/onboarding/complete?token=…` → `{ok:true, session_token, magic_link_email_sent:true}`.
  3. `POST /v1/account/add-payment-method` with `Authorization: Bearer <session_token>` →
     **HTTP 200 + `{"checkout_url":"https://checkout.stripe.com/c/pay/cs_live_…"}`**. The `cs_live_`
     prefix is the launch gate — live key minted a real hosted Checkout.
  4. **Clean up** via `railway ssh` python: delete `login_tokens`, `clients`, then `tenants` for
     that `tenant_id` (FK order; per-tenant try/except + rollback) so it doesn't fire trial-reminder
     emails. (The pk_test/sk_live mismatch is VESTIGIAL — Checkout is server-side via the secret key,
     proven by the live `cs_live_` session — not a blocker.)

## The billing model (don't re-derive)

- **No upfront card.** Signup creates a `trialing` tenant with no payment method.
  Adding a card is `POST /v1/account/add-payment-method` → Stripe Checkout
  `mode="setup"` (a SetupIntent carrying `metadata.tenant_id`). The
  `setup_intent.succeeded` webhook stores the pm; the real subscription is
  created at trial end (or on resume from `paused_no_card`).
- **Subscription creation:** `api/stripe_helpers.create_subscription_for_tenant`
  builds items = [setup-fee (qty 1, one-time), per-array line (qty = billable
  array count, min 1)] on the stored pm. Never raises — alerts + returns
  `{ok:False}` so the webhook never 500s.
- **Array-count changes** reconcile via
  `stripe_helpers.reconcile_subscription_quantity` — finds the per-array line by
  price id and `SubscriptionItem.modify(quantity=, proration_behavior="create_prorations")`.
- **Graduated/tiered pricing is the engine.** `api/pricing.py` holds a `TIERS`
  table = `[(up_to, unit_cents), …, (None, floor)]`, `compute_monthly_cents()`
  mirrors Stripe `tiers_mode="graduated"` (each band's unit applies only to
  arrays within it → no revenue cliff), and `stripe_tiers()` emits the Stripe
  payload. `scripts/create_stripe_prices.py` reads `TIERS` and mints the live
  Stripe graduated price. **TIERS is the single source of truth — edit there
  only, then re-run the price script.** The dashboard "next charge" preview
  calls the same `compute_monthly_cents()` so it matches the invoice to the penny.

## NEPOOL Operator pricing (the verifier side)
$250 one-time setup + $15/array/mo graduated: 1-50 @ $15, 51-100 @ $13.50,
101-150 @ $12, 151+ @ $10.50 (30% floor). Env: `STRIPE_SETUP_PRICE_ID`,
`STRIPE_ARRAY_PRICE_ID`. Tests pin every boundary in `tests/test_pricing.py`.

## Adding a SECOND product's billing (the Array Operator pattern)

A different buyer needs a different price, but reuse ALL the machinery. The clean
shape (built + taken live Jun 2026):

1. **Own pricing module** `api/pricing_array_operator.py` — a parallel `TIERS`
   table + identical graduated math. Owner default ("Option B"): 1st array FREE
   (modeled as a `(1, 0)` first tier — Stripe handles $0 tiers natively under
   graduated), then $9 (2-10) / $8 (11-50) / $6.50 (51+), **no setup fee**.
2. **Own price-mint script** `scripts/create_array_operator_prices.py` — mirrors
   the NEPOOL one but creates a separate Stripe product. SAFETY: make it
   **refuse to run against an `sk_live_` key without `--confirm-live`**, and give
   it `--dry-run` (prints bands + tiers, no Stripe calls). This is the gate that
   lets you build the whole thing without risking a live price.
3. **`Tenant.product` column** (`"nepool"` default | `"array_operator"`,
   `server_default="nepool"`, NOT NULL) + an idempotent `api/migrate.py` ALTER.
   Every existing tenant stays `nepool` untouched.
4. **Product-aware price routing** `stripe_helpers.array_price_id_for_product(product)`
   → returns `STRIPE_AO_ARRAY_PRICE_ID` for array_operator else
   `STRIPE_ARRAY_PRICE_ID`. If the AO price id is unset, FALL BACK to the NEPOOL
   price AND fire an internal alert (never create a $0/broken subscription).
   `create_subscription_for_tenant` reads `tenant.product`, uses this router, and
   **skips the setup-fee item for array_operator tenants**.
5. **Reconcile matches EITHER price id** — `reconcile_subscription_quantity`
   builds `known_price_ids = {STRIPE_ARRAY_PRICE_ID, STRIPE_AO_ARRAY_PRICE_ID,
   os.getenv(...) both}` and matches the line against the set. **Include BOTH the
   module-global AND `os.getenv` values** — the onboarding tests monkeypatch the
   module global `STRIPE_ARRAY_PRICE_ID` (e.g. `test_clients_reconciles_stripe_quantity`),
   so a pure-env refactor breaks them. Reading both keeps prod (env) and tests
   (monkeypatched global) green.
6. **Tests:** pin the new TIER boundaries (`tests/test_pricing_array_operator.py`)
   and the price router (`tests/test_product_price_routing.py`,
   monkeypatch-driven). Run the FULL suite — a model/helper change can break an
   unrelated billing test elsewhere.
7. **Tag the tenant's product at signup.** Thread `product` through the request
   model + `_create_trial_tenant()` + the `Tenant(...)` construction in
   `api/onboarding.py`. `StartRequest.product` is a constrained field
   (`Field("nepool", pattern="^(nepool|array_operator)$")`) so a bad value 422s.
   The OWNER SITE posts `product:"array_operator"` to `/v1/onboarding/start`.
   PITFALL: when patching the request model, don't let the patch swallow the next
   `class StartResponse:` header or the `-> tuple[str,str]` return annotation —
   re-read after each patch (the find/replace can eat an adjacent line).
8. **The 14-day trial is PRODUCT-AGNOSTIC** — it's created at
   `/v1/onboarding/start` (`trial_ends_at = now()+timedelta(days=14)`,
   `subscription_status="trialing"`, no card) regardless of product. So "give the
   new product an identical trial" needs ZERO trial code — just tag `product` and
   the same trial mechanics apply. Only the per-array PRICE differs (via the
   router) when they later add a card.

## Converting a product from per-ARRAY to per-kWh billing (licensed → metered)

**This is an ARCHITECTURE change, not a number swap — budget for it.** Per-array
billing uses a Stripe `licensed` price whose QUANTITY = array count. Per-kWh
billing uses a `metered` price that bills on reported USAGE. The two have
different Stripe mechanics, so a "bill by kWh instead" request touches the whole
stack. The data is already there: `DailyGeneration` (one row per array per day,
from the inverter pulls) is the authoritative meter. Pattern that worked
(Jun 2026, Array Operator owners; NEPOOL verifier intentionally LEFT per-array
because RECs are minted per-array):

1. **Scope it to the ONE product.** Per-array stays correct for the verifier
   side. Gate every change on `Tenant.product` / `stripe_helpers.is_array_operator`.
2. **Pricing module in DECIMAL cents.** A useful per-kWh rate is sub-cent
   (0.5¢/kWh). Stripe's integer `unit_amount` (whole cents) CANNOT represent it
   — emit `unit_amount_decimal` (a STRING of cents, e.g. `"0.5"`) from
   `stripe_tiers()` instead. `compute_monthly_cents()` returns a FLOAT (may be
   fractional). Keep the graduated-tier shape (bands of kWh/mo) — same engine as
   per-array, just kWh boundaries + decimal units.
3. **Metered price** (`scripts/create_array_operator_prices.py`):
   `billing_scheme="tiered"`, `tiers_mode="graduated"`,
   `recurring={"interval":"month","usage_type":"metered","aggregate_usage":"last_during_period"}`.
   `last_during_period` is the key choice — you report cumulative month-to-date
   each day and Stripe bills the LAST value, so reporting is idempotent.
4. **Subscription item carries NO quantity.** Stripe REJECTS `quantity=` on a
   metered line. In `create_subscription_for_tenant` (and the scheduler's
   `finalize_expired_trials` charge path) build the AO item as `{"price": id}`
   with no qty, and no setup fee.
5. **`reconcile_subscription_quantity` becomes a NO-OP for metered.** Detect it:
   loop the sub's items, and if ANY line's `price.recurring.usage_type ==
   "metered"`, return early (there's no array quantity to reconcile; volume is
   driven by usage). The NEPOOL per-array path is unchanged — its fake test
   sub-items have no `recurring` key so the metered check falls through cleanly.
6. **Usage-reporting job** (`api/jobs/usage_report.py`, scheduled daily AFTER the
   inverter pull): for each active `product=array_operator` tenant with a live
   sub, find the metered item, sum `DailyGeneration.kwh` for billable arrays
   (exclude soft-deleted + `excluded`) since the sub's `current_period_start`
   (NOT the calendar 1st — Stripe bills on the sub's own anchor), and
   `SubscriptionItem.create_usage_record(item, quantity=round(kwh), action="set")`.
   Usage records need INTEGER quantity. Never crash the scheduler — per-tenant
   try/except + internal alert.
7. **`billing-summary` becomes product-aware** — return `billing_basis:"kwh"`
   with month-to-date kWh + graduated estimate (decimal cents) for owners, keep
   the per-array shape for NEPOOL. `next-invoice` is unchanged (it reads the
   upcoming invoice from Stripe, which computes the metered amount itself).
8. **Frontend** (`array-operator/public`): rewrite the pricing section + the
   Master Account billing cards (`sandbox.js renderBilling` — it reads
   `billing_basis`; amounts are CENTS so divide by 100, the old code passed raw
   fields through a dollars formatter — fix that) + onboarding copy. Sweep
   "first array free" / "$N/array" language for the new "billed by the kWh"
   story.
9. **Env var rename:** `STRIPE_AO_ARRAY_PRICE_ID` → `STRIPE_AO_KWH_PRICE_ID`.
   Read the OLD var as a fallback so a half-migrated env never bills $0.
10. **Tests:** pin kWh tier boundaries with `math.isclose` (floats!), the
    metered-skip in reconcile, and the pure-DB `tenant_period_kwh` summation
    (billable-only, since-date, tenant isolation). Full suite must stay green.

BONUS BUG this surfaced: the scheduler's `finalize_expired_trials` charged EVERY
trial-end tenant on NEPOOL's price+setup regardless of product — an AO owner
hitting trial-end was billed NEPOOL pricing. Make that charge path product-aware
in the same pass.

MIGRATION GAP to flag: an EXISTING AO tenant that already has a per-array
(licensed) subscription needs a one-time Stripe-side swap to the metered price —
the code change alone doesn't move a live sub. Script it before flipping prod.

The per-kWh price POINT is a money escalation (Ford picked 0.5¢/kWh graduated:
0.5¢ <20k kWh/mo, 0.45¢ to 200k, 0.40¢ above — Resi ~$4.50/mo, 99kW ~$50/mo).
Flag the redistribution tradeoff LOUDLY: per-kWh shifts revenue OFF the
residential wedge ONTO big producers (a 99kW array pays ~5× a rooftop) — that's
the "tied to how much they get paid" intent, but confirm Ford wants that shape.

## Owner-vs-verifier pricing logic (why not just copy $15)
The NEPOOL $15/array replaces a ~$540/array/yr human consultant — overwhelming
value. For an array OWNER, a single residential rooftop leaks maybe $100-300/yr,
so a $15/mo ($180/yr) fee can rival the loss it catches → prices out the widest
(residential) funnel. Owner pricing must stay BELOW the loss it catches and drop
the setup fee; "1st array free" makes the residential top-of-funnel frictionless
and turns the dashboard into the sales pitch. Multi-array (Bruce's 7 = $54/mo)
still scales. State this tradeoff to Ford; the price POINT itself is a
money/branding escalation — build with a default, get sign-off before minting live.
(Jun 2026: Ford approved Option B — 1st free / $9 / $8 / $6.50 — and it is now live.)

## GOTCHAS (cost real debugging)
- **Live/test key mismatch in prod (found Jun 2026):** `STRIPE_SECRET_KEY` is
  `sk_live_…` but `STRIPE_PUBLISHABLE_KEY` is `pk_test_…`. Checkout misbehaves on
  a mismatch — check `railway variables | grep -i stripe` and fix before any new
  checkout ships. (Prod also: `STRIPE_WEBHOOK_SECRET`, `STRIPE_ARRAY_PRICE_ID`,
  `STRIPE_SETUP_PRICE_ID` all set.)
- **`railway variables` prints secrets** including the full `sk_live_` key — flag
  it, redact in any echo, never save to memory. (See the SKILL's secret-handling
  section for the redactor/quoting workaround.)
- Minting a price runs against whatever `STRIPE_SECRET_KEY` is in the env — on
  prod that's LIVE. Always `--dry-run` first; the `--confirm-live` gate exists so
  you can't fat-finger a public price.
- The price script's Stripe attribute access (`product.id`, `price.id`, dynamic
  `tiers`) trips Pyright `reportAttributeAccessIssue` — these are false positives
  (Stripe returns dynamic objects), same as the existing `create_stripe_prices.py`.
- Adding billing to a product is BACKEND only — the owner site still needs a
  pricing surface + checkout-launch wiring (reuse the `mode="setup"` add-card
  flow). Don't claim billing is "done" when only the backend half exists.

## Owner site front-door wiring (static site → backend signup, PROVEN Jun 13 2026)

The owner site (`array-operator-ea`, `/root/array-operator`, publish dir `public/`) is a
STATIC site that signs people up against the SHARED backend cross-origin. The flow that works:

1. **Pricing section** in `public/index.html` (black/green sun-mirror skin — see SKILL's
   Array Operator visual-language pitfall). State the live tiers verbatim (1st free / $9 /
   $8 / $6.50), "14-day free trial · no card to start", "no setup fee". CTA links to
   `onboarding.html`. Add a "Get started" nav link; if you REMOVE the old `#addArray` nav
   link, make `app.js`'s `document.getElementById("addArray").onclick` NULL-SAFE or the
   marketing page throws on load.
2. **Signup happens at the END of `onboarding.html`** (`finish()`), not at a separate form.
   Collect name + email earlier in the Connect step. `finish()` does, in order:
   `POST {API_BASE}/v1/onboarding/start` with `{email, full_name, company, array_count,
   product:"array_operator"}` → on 200, take `onboarding_token` →
   `POST /v1/onboarding/complete?token=…` (marks done, EMAILS a magic-link sign-in, and
   returns a `session_token`) → stash the session → offer an inline "Add a card" button that
   calls `POST /v1/account/add-payment-method` with `Authorization: Bearer <session_token>`
   and `window.location.href = checkout_url` (Stripe Checkout setup mode). Handle 409
   (email exists) as "go sign in", and wrap every fetch so CORS/offline fails GRACEFULLY
   (show an "Almost there / Try again" done-screen, store nothing). CORS already allows
   `array-operator-ea.netlify.app` (it's in `CORS_ALLOWED_ORIGINS`).
3. **The "dashboard" the magic link opens is the SHARED SPA** at `solaroperator.org/accounts`
   — owners and NEPOOL verifiers currently land in the SAME dashboard UI. Fine for
   billing/trials; a dedicated owner dashboard is a separate future effort.

### PITFALL — `/v1/array-owners/solaredge/discover` is AUTH-GATED, returns 401 pre-signup.
That discover endpoint requires a bearer token (built for signed-in dashboard users). During
pre-signup onboarding the owner has NO account, so it returns **401 "Missing bearer token"**.
Don't treat that as a bad key — it dead-ends the flow. In the onboarding `discover()`, branch:
a 401 whose message matches `/bearer|token|tenant|unauthor|sign|session/` is an AUTH GATE →
fall through to the demo cascade so the trial still gets created; a genuine 400/403 (or a
non-auth 401) is a real key rejection → show the error. CONSEQUENCE to state to Ford + in the
UI: the owner's real SolarEdge key is NOT verified or attached during onboarding (it's demo
sites until they sign in and connect from the dashboard). TRUE pre-signup discovery would need
a new UNAUTHENTICATED, rate-limited "validate key + list sites" endpoint, OR collecting the
key at onboarding and attaching it server-side right after tenant create.
**UPDATE Jun 13 2026 — this WAS built (the "sublime pre-signup" pass). See the next section.**

## Sublime pre-signup REAL preview (unauthenticated discovery + value reveal, BUILT+LIVE Jun 13 2026)

Ford asked to make new signups "as sublime as possible" — owners should see their REAL
arrays and dollar value BEFORE creating an account, not demo data. The reusable pattern:

1. **New UNAUTHENTICATED, rate-limited endpoint** `POST /v1/array-owners/public/preview`
   (`api/array_owners.py`). Takes `{api_key}`, calls the SAME `inverters.solaredge.discover_sites()`,
   returns real sites + a per-site AND total annual `$` estimate. **Saves nothing.** This is
   how you give a pre-signup user a real result without the auth gate.
   - **Rate-limit an open endpoint or it's a free key-oracle / scraping proxy.** Crude
     in-memory per-IP sliding window is enough for one Railway web replica:
     `_PREVIEW_HITS: dict[str,list[float]]`, `time.monotonic()`, prune-then-check
     (8 hits / 5 min here) → `HTTPException(429, …)`. Client IP behind Railway's proxy =
     first hop of `request.headers["x-forwarded-for"]` (fall back to `request.client.host`).
     Needs `from fastapi import …, Request` and `request: Request` in the signature.
   - **Friendly bodies, not 4xx, for user-fixable cases.** Bad key / site-level key / empty
     account each return `{"ok": false, "message": "<plain English>"}` (site-level adds
     `"scope":"site"`) so the static site shows an inline hint without special-casing HTTP
     codes. RESERVE real status codes for 429 (rate limit) and 502 (SolarEdge 5xx).
   - **Value estimate** (pre-signup teaser): `annual_kwh = peak_kw × 8760 × 0.14` (a
     conservative blended capacity factor), `$ = annual_kwh × get_energy_rate(None) +
     floor(MWh) × REC_PRICE_USD_PER_MWH`. Deliberately ROUGH — copy MUST say "estimated · we
     pin the exact figure once you're live" so it never overpromises. Tighter pre-signup
     numbers would need a real production-history call per site (slow, burns SolarEdge quota).
   - Tests: `tests/test_public_preview.py` — no-auth, real sites+value, bad-key friendly,
     site-level hint, and the rate-limit 429 (clear `ao._PREVIEW_HITS` in an autouse fixture).

2. **Frontend reveal** (`onboarding.html`): step 2 `discover()` now calls `/public/preview`
   (not the auth-gated `/discover`); step 3 renders the owner's REAL array names + per-array
   `~$N/yr`, plus a **value hero that counts up** from $0 to the total
   (`requestAnimationFrame` ease-out cubic). Track `state.realSites` so copy differs for
   real vs demo-fallback ("There they are — your arrays" + "nothing's been saved yet, just a
   preview" vs "Here's what we'll watch"). The count-up + green glow is what earned "sublime".

3. **Auto-attach the real key AFTER signup** so the dashboard is populated the instant they
   sign in (no re-pasting). In `finish()`, AFTER `/complete` mints the `session_token`, attach
   the credential with `Authorization: Bearer <session_token>`. **Best-effort — a bad key 400s
   but must NEVER block the trial** (wrap in try/catch; reflect success in the done-screen copy
   "connected and being watched right now" vs "queued"). This is now VENDOR-AWARE (see the
   "One-click post-signup auto-attach for ALL vendors" subsection below) — originally SolarEdge
   only via `connect-account`, now Fronius/SMA/Locus-single attach via `connect-single`.

E2E verified on the deployed origin (preview 200 → start 200 → complete 200 →
connect-account 200/real-or-400/fake → trial live), tenant confirmed + deleted.

### Generalized to ALL vendors, not just SolarEdge (Jun 13 2026)
Ford then asked to make the preview \"all sublime\" — so `/public/preview` now works for
EVERY available inverter brand, keeping `{api_key}` → SolarEdge back-compat:
- **Body is `{vendor, config}`** (config = the per-vendor credential field dict the connect
  form collects). A bare `{api_key}` is still accepted and means SolarEdge. Unknown vendor or
  `AVAILABLE=False` (Chint) → friendly `ok:false` (NEVER a 5xx — a defensive check before the
  vendor dispatch). Missing required fields → friendly `ok:false` (\"Fill in your credentials\").
- **Discovery vs single-system routing** (`_preview_sites_for_vendor`): SolarEdge AND
  Locus-with-`partner_id` enumerate the whole account/partner via `discover_sites`; Locus
  without a partner_id, Fronius, and SMA have NO discovery API, so preview the ONE named
  system via the vendor module's `validate()` and return it as a single site. (Capability
  matrix: only solaredge + locus have `discover_sites`; fronius/sma/chint don't.)
- **Normalize each vendor's shape** (`_normalize_preview_site`): coerce to
  `{site_id, name, peak_power_kw, status}` + value estimate. KEY UNIT GOTCHA: Fronius
  `validate()` returns `peak_power` in **watts-peak** → divide by 1000 for kW. SolarEdge/Locus
  discovery already give `peak_power_kw`. SMA returns NO peak → the array still previews but
  `annual_value_usd` is `null`, and `totals.annual_value_usd` is `null` when nothing could be
  estimated (the UI then HIDES the dollar hero rather than showing a fake/zero number).
- **Per-vendor friendly copy** dicts for the two recoverable failures (`_PREVIEW_AUTH_MSG`,
  `_PREVIEW_SCOPE_MSG`) keyed by vendor, so a Fronius bad key says \"check the Access Key
  ID/Value\" not \"account-level API key\".
- **Frontend**: a single `connectVendor()` builds `config` from each vendor's `fields`
  (map the SolarEdge `apiKey` field name → `api_key`) and POSTs `{vendor, config}` for ALL
  brands — the old fake `connectSingle()` path is gone. The reveal lede uses the REAL vendor
  label (was hardcoded \"your SolarEdge account\" — a bug; sweep hardcoded vendor names).
- Tests: `tests/test_public_preview.py` covers each vendor path (locus partner discovery,
  fronius Wp→kW single-system, sma no-peak/no-value, unavailable-vendor friendly, missing
  fields). Verified live per-vendor via curl (friendly messages, no 500s) + playwright reveal.
### One-click post-signup auto-attach for ALL vendors (Jun 13 2026)
Closing the last gap: every vendor now auto-attaches its real credential after signup so the
dashboard is populated the instant the owner signs in (no re-pasting), not just SolarEdge.
- **Capability split drives which endpoint to call:** vendors WITH account-level enumeration
  (SolarEdge always; Locus only when a `partner_id` is supplied) use their `connect-account`
  endpoint (attach ALL discovered sites). Vendors WITHOUT discovery (Fronius, SMA, Locus
  without a partner_id) use the NEW `POST /v1/array-owners/connect-single`.
- **`connect-single`** (`api/array_owners.py`): `{vendor, config, name?}` → `validate()` the
  one system FIRST (so bad creds 400 and persist NOTHING), then attach to an array matched by
  EXACT case-insensitive name, else CREATE one (solar, no client). Idempotent by name. Reuses
  `_connect_inverter()` for the upsert so the write path is identical to the per-array connect
  endpoint. Rejects `AVAILABLE=False` vendors (Chint) with a 400.
- **Frontend `finish()`** routes per vendor using the freshly-minted session: SolarEdge →
  `solaredge/connect-account`; Locus+partner_id → `locus/connect-account`; everything else →
  `connect-single` (builds `config` from the collected fields, `apiKey`→`api_key`). Sets
  `state.arraysConnected` for the done-screen copy. Best-effort — a failure never blocks the
  trial. Added an OPTIONAL Locus \"Partner ID\" field to the onboarding vendor catalog so Locus
  can do the account-wide attach.
- Tests: `tests/test_array_owners.py` — connect-single create-new / match-existing-by-name /
  bad-creds-400-no-write / unavailable-vendor-400. Live E2E: a Fronius signup fired
  start→complete→connect-single in order; bogus creds correctly 400'd from the real Solar.web
  API WITHOUT blocking the trial or orphaning rows.
- Honest test limit: you can't feed REAL Fronius/SMA creds to prod, so the live E2E necessarily
  used fake creds (which 400). The attach-on-SUCCESS path is proven by the unit tests
  (mock the vendor module's `validate`), not live — note this when reporting.

### E2E verification of the front door (do this, file:// is NOT enough)
`file://` exercises only the graceful-offline path (server unreachable). To prove the real
signup works you MUST drive the DEPLOYED origin so CORS is genuinely exercised: playwright →
`https://array-operator-ea.netlify.app/onboarding.html`, fill the form, click through, and
assert the network calls (`/v1/onboarding/start` 200, `/complete` 200) via a `page.on
("response")` listener — then query prod to confirm the tenant's `product=array_operator` +
`trialing`, and DELETE the test tenant (and its placeholder Client + login_tokens row) after.
Deleting the tenant may hit FK constraints — clear child rows first (`clients`, `login_tokens`;
note `inverter_connections` has NO `tenant_id` column, it's array-scoped) then the tenant.

## Go-live runbook for a new product's billing (PROVEN Jun 13 2026)
Full runbook in `docs/plans/ARRAY_OPERATOR_BILLING.md`. The ORDER matters:

1. Get Ford's sign-off on the price POINT (money escalation).
2. **Deploy the CODE first** (merge branch -> main -> push -> wait for Railway SUCCESS).
   The price-mint script imports `api.pricing_array_operator`, which doesn't
   exist on prod until you deploy — so minting the price BEFORE deploy fails with
   ModuleNotFound. Code-first, then mint via `railway ssh`.
3. `railway ssh "cd /app && python -m api.migrate"` — adds `tenants.product`.
   Then VERIFY the column actually landed: `migrate.py` prints "All tenant columns
   already present" even when create_all didn't ALTER, so confirm independently
   with an `inspect(engine).get_columns('tenants')` one-liner over `railway ssh`.
4. `railway ssh "cd /app && python -m scripts.create_array_operator_prices --confirm-live"`
   -> copy the printed `price_…` id.
5. `railway variables --set "STRIPE_AO_ARRAY_PRICE_ID=price_…"` (this itself triggers a redeploy).
6. **Verify the live price via Stripe** before trusting it: retrieve it with
   `expand=['tiers']` over `railway ssh` and assert `livemode=True`, `billing_scheme=tiered`,
   `tiers_mode=graduated`, and the exact `(up_to, unit_amount)` table.
7. **Smoke-test end-to-end, then DELETE the test tenant.** POST a real signup to
   prod (`curl … /v1/onboarding/start` with a `product:"array_operator"` body on a
   throwaway email/domain you own), query the tenant in prod to confirm
   `product`/`trialing`/`trial_ends_at`/no-card, then DELETE it (and its placeholder
   Client) via `railway ssh` python so it doesn't pollute real data or fire
   trial-reminder emails.

Note the pk/sk mismatch (gotcha above) is VESTIGIAL — the publishable key is not
referenced anywhere in the backend (`grep -r STRIPE_PUBLISHABLE_KEY` -> no hits;
Checkout is server-side via the secret key), so it does NOT block owner checkout.
Clean it up opportunistically, not as a go-live blocker.
