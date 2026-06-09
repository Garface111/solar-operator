# Solar Operator — Go-Live Payment Runbook

Date prepared: 2026-06-09. **Status: NOTHING FLIPPED.** Prod is still Stripe TEST mode.
This is the checklist to take payments live. Do it when you (Ford) are at the keyboard —
every step except #0 touches real money or prod.

Current prod state (verified 2026-06-09):
  - Railway prod `web` service: STRIPE_SECRET_KEY = sk_test_…, STRIPE_PUBLISHABLE_KEY = pk_test_…
  - STRIPE_SETUP_PRICE_ID / STRIPE_ARRAY_PRICE_ID = test-mode price IDs
  - => Onboarding "works" today but captures cards in TEST mode and never really charges.

────────────────────────────────────────────────────────
STEP 0 (do first, no money) — clear the 2 launch-blockers from PAYMENT_AUDIT
────────────────────────────────────────────────────────
[ ] AI-#1: Trial-end charge failure must email the operator (today it's silent).
        File: api/scheduler.py ~line 204-210 (except block). Add send_trial_charge_failed_email.
[ ] AI-#2: Fix broken trial-length test so the 14-day contract is covered.
        File: tests/test_deferred_billing_setup_mode.py:127  (4 days → 14 days). Un-skip it.
[ ] Run: cd ~/solar-operator && source venv/bin/activate && pytest -q   (all green)
[ ] Commit + push (Railway auto-deploys). Confirm deploy healthy.

NOTE: working tree is mid frontend-rebuild (new app_dist assets + 12-line account.py edit).
Resolve/commit THAT intentionally first — don't let the go-live commit smuggle it in.

────────────────────────────────────────────────────────
STEP 1 — Live API keys
────────────────────────────────────────────────────────
[ ] Stripe Dashboard → toggle to LIVE mode (top-right).
[ ] Developers → API keys → copy:  Publishable pk_live_… and Secret sk_live_…
    (Reveal the secret key once; store it in a password manager.)

────────────────────────────────────────────────────────
STEP 2 — Live products/prices  (GRADUATED — must mint the tiered array price)
────────────────────────────────────────────────────────
The array price is a GRADUATED tiered price (1–50 $15 / 51–100 $13.50 / 101–150 $12 /
151+ $10.50), sourced from api/pricing.py. Use the repo script — it builds the tiered
price from that table so app + Stripe never drift. Do NOT use a flat $15 price.

    cd ~/solar-operator && source venv/bin/activate
    # Point the script at the LIVE key for this one invocation:
    STRIPE_SECRET_KEY=sk_live_xxx python -m scripts.create_stripe_prices
    → prints STRIPE_SETUP_PRICE_ID (live, $250 one-time) and
      STRIPE_ARRAY_PRICE_ID (live, graduated). Copy both.

[ ] Confirm output shows "created tiered price" + the 4 bands.
[ ] Have both live price_… IDs in hand.

(Already proven in TEST mode 2026-06-09: minted graduated price
 price_1TgWrtFsNVe0j9z83WMi4Fkd, Stripe reports billing_scheme=tiered/graduated,
 app compute_monthly_cents(120)=$1665 matches Stripe. The flat test price has been
 replaced by the graduated one in test env. scripts/create_live_prices.py is RETIRED
 — it only made a flat price; use create_stripe_prices.py for both test and live.)

────────────────────────────────────────────────────────
STEP 3 — Live webhook endpoint
────────────────────────────────────────────────────────
[ ] Stripe (LIVE) → Developers → Webhooks → Add endpoint
        URL:  https://solaroperator.org/v1/stripe/webhook   (confirm exact prod path)
        Events: checkout.session.completed, customer.subscription.updated,
                customer.subscription.deleted, invoice.payment_failed
                (+ invoice.payment_succeeded for audit; payment_method.detached if AI-#3 added)
[ ] Copy the new LIVE signing secret  whsec_…

────────────────────────────────────────────────────────
STEP 4 — Swap into Railway prod + deploy
────────────────────────────────────────────────────────
    railway variables --set STRIPE_SECRET_KEY=sk_live_xxx \
                       --set STRIPE_PUBLISHABLE_KEY=pk_live_xxx \
                       --set STRIPE_WEBHOOK_SECRET=whsec_live_xxx \
                       --set STRIPE_SETUP_PRICE_ID=price_live_setup \
                       --set STRIPE_ARRAY_PRICE_ID=price_live_array
[ ] Redeploy (railway up / git push) so the process restarts and re-reads keys.
[ ] railway variables | grep STRIPE   → confirm all show live_ prefixes.

────────────────────────────────────────────────────────
STEP 5 — Live smoke test (REAL card — yours)
────────────────────────────────────────────────────────
[ ] Run a fresh onboarding with a throwaway/real email + your real card.
[ ] Confirm: Stripe LIVE → Customers shows the new customer + SetupIntent (card captured, $0).
[ ] Confirm tenant in DB: subscription_status=trialing, trial_ends_at = +14d, pm stored.
[ ] (Optional, proves the charge path) Temporarily set that tenant's trial_ends_at to the past,
    wait for the hourly finalize_expired_trials, confirm a real invoice (setup+array) is created
    and paid — then REFUND it in Stripe and reset the tenant. This is the only way to prove the
    full charge path before a prospect hits it.
[ ] Delete/clean up the test tenant.

────────────────────────────────────────────────────────
STEP 6 — Only now: send the cold emails
────────────────────────────────────────────────────────
[ ] Use a SEPARATE warmed sending domain, NOT admin@solaroperator.org (deliverability note in
    docs/cold-email-verifiers.md). Bruce already approved the design.
[ ] Small batches, personalized line 1, CAN-SPAM footer.

Done = payments live, charge path proven with a real refunded transaction, THEN outreach.
