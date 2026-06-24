"""One-shot: create the ARRAY OPERATOR *invoicing* Stripe prices.

The invoicing plan (api/pricing_ao_invoicing.py) is a per-OFFTAKER LICENSED price —
for operators who use Array Operator only to auto-generate + send offtaker invoices
(value scales with billing relationships, not generation). Distinct from the AO
per-kWh monitoring meter and the NEPOOL per-array price. It creates:

  - Product "Array Operator — Invoicing"
  - A graduated, LICENSED, recurring monthly price from
    api.pricing_ao_invoicing.stripe_tiers()  → $100 base (incl 4 offtakers) + $25
    per offtaker beyond. Subscription quantity = the OFFTAKER count, kept in sync by
    stripe_helpers.reconcile.  → STRIPE_AO_INVOICING_PRICE_ID
  - A ONE-TIME $250 setup price (waivable per-customer). → STRIPE_AO_INVOICING_SETUP_PRICE_ID

SAFETY: runs against whatever STRIPE_SECRET_KEY is in the environment. It REFUSES to
run against an sk_live_ key unless you pass --confirm-live, so you can't accidentally
mint a LIVE price. For a dry run that prints the payload without any Stripe call, use
--dry-run.

Run (dry):   python -m scripts.create_ao_invoicing_price --dry-run
Run (test):  STRIPE_SECRET_KEY=sk_test_... python -m scripts.create_ao_invoicing_price
Run (live):  railway ssh "cd /app && python -m scripts.create_ao_invoicing_price --confirm-live"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.pricing_ao_invoicing import (  # noqa: E402
    stripe_tiers, BASE_CENTS, BASE_INCLUDES_OFFTAKERS, PER_OFFTAKER_CENTS, SETUP_CENTS,
)

DRY_RUN = "--dry-run" in sys.argv
CONFIRM_LIVE = "--confirm-live" in sys.argv

PRODUCT_NAME = "Array Operator — Invoicing"
SETUP_NICKNAME = "AO Invoicing — one-time setup"
PRICE_NICKNAME = "AO Invoicing — per offtaker (graduated)"


def _summary() -> str:
    return (f"${BASE_CENTS/100:.0f}/mo base (includes {BASE_INCLUDES_OFFTAKERS} offtakers) "
            f"+ ${PER_OFFTAKER_CENTS/100:.0f}/offtaker beyond  ·  ${SETUP_CENTS/100:.0f} one-time setup")


def main() -> None:
    print("Array Operator INVOICING plan: " + _summary())
    print()

    if DRY_RUN:
        print("[--dry-run] Would create:")
        print(f'  Product: "{PRODUCT_NAME}"')
        print("  Graduated LICENSED monthly price (usage_type=licensed) with tiers:")
        for t in stripe_tiers():
            flat = t.get("flat_amount")
            print(f"    up_to={t['up_to']!r:>5}  flat_amount={flat!r}  unit_amount={t.get('unit_amount')!r} (cents)")
        print(f"  One-time setup price: unit_amount={SETUP_CENTS} (cents) = ${SETUP_CENTS/100:.2f}")
        print("\nNo Stripe calls made. Re-run without --dry-run against a real key.")
        return

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("STRIPE_SECRET_KEY not set. Use --dry-run, or export a test key.")
    if key.startswith("sk_live_") and not CONFIRM_LIVE:
        sys.exit(
            "REFUSING to create a LIVE price without --confirm-live.\n"
            "This is a real customer-facing price. Re-run with --confirm-live\n"
            "only after the price point is signed off."
        )

    import stripe
    stripe.api_key = key
    mode = "LIVE" if key.startswith("sk_live_") else "TEST"
    print(f"Operating in Stripe {mode} mode.\n")

    # Product — reuse by name if present.
    existing = stripe.Product.search(query=f'name:"{PRODUCT_NAME}" AND active:"true"').data
    if existing:
        product = existing[0]
        print(f"  found existing product: {product.id}")
    else:
        product = stripe.Product.create(
            name=PRODUCT_NAME,
            description="Automatic offtaker invoicing for solar operators "
                        "(EnergyAgent — Array Operator). Billed per offtaker.",
        )
        print(f"  created product: {product.id}")

    # Recurring LICENSED graduated price — reuse if an identical one exists.
    def norm_tiers(ts):
        return [((  "inf" if t.get("up_to") in (None, "inf") else int(t["up_to"])),
                 int(t.get("flat_amount") or 0), int(t.get("unit_amount") or 0)) for t in ts]
    want = norm_tiers(stripe_tiers())
    price = None
    for p in stripe.Price.list(product=product.id, active=True, limit=100, expand=["data.tiers"]).data:
        if getattr(p, "billing_scheme", None) != "tiered":
            continue
        if p.recurring is None or p.recurring.interval != "month":
            continue
        if getattr(p.recurring, "usage_type", None) != "licensed":
            continue
        have = norm_tiers([
            {"up_to": t.up_to, "flat_amount": t.flat_amount, "unit_amount": t.unit_amount}
            for t in (p.tiers or [])
        ])
        if have == want:
            price = p
            print(f"  found existing licensed tiered price: {price.id}")
            break
    if price is None:
        price = stripe.Price.create(
            product=product.id, currency="usd", nickname=PRICE_NICKNAME,
            billing_scheme="tiered", tiers_mode="graduated",
            tiers=stripe_tiers(),
            recurring={"interval": "month", "usage_type": "licensed"},
            expand=["tiers"],
        )
        print(f"  created licensed tiered price: {price.id}")

    # One-time $250 setup price — reuse if an identical one exists.
    setup = None
    for p in stripe.Price.list(product=product.id, active=True, limit=100, type="one_time").data:
        if p.unit_amount == SETUP_CENTS and p.currency == "usd":
            setup = p
            print(f"  found existing setup price: {setup.id}")
            break
    if setup is None:
        setup = stripe.Price.create(
            product=product.id, currency="usd", nickname=SETUP_NICKNAME,
            unit_amount=SETUP_CENTS,
        )
        print(f"  created one-time setup price: {setup.id}")

    print()
    print("=" * 64)
    print("Set these on Railway (Array Operator invoicing billing):")
    print(f"  STRIPE_AO_INVOICING_PRICE_ID={price.id}")
    print(f"  STRIPE_AO_INVOICING_SETUP_PRICE_ID={setup.id}")
    print("=" * 64)


if __name__ == "__main__":
    main()
