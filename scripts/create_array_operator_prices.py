"""One-shot: create ARRAY OPERATOR (owner-side) Stripe price.

This is the EnergyAgent owner product — separate from the NEPOOL Operator
prices created by scripts/create_stripe_prices.py. It creates:
  - Product "Array Operator — Monitoring"
  - A graduated tiered recurring price from api/pricing_array_operator.TIERS
    (first array free, then $9 / $8 / $6.50 bands)

There is NO setup-fee product on the owner side.

SAFETY: this runs against whatever STRIPE_SECRET_KEY is in the environment. To
avoid accidentally minting a LIVE price, the script REFUSES to run against an
sk_live_ key unless you pass --confirm-live. For a dry run against test mode,
set STRIPE_SECRET_KEY to your sk_test_ key (or run with --dry-run to just print
what it would create without calling Stripe).

Run (test):  STRIPE_SECRET_KEY=sk_test_... python -m scripts.create_array_operator_prices
Run (live):  railway ssh "cd /app && python -m scripts.create_array_operator_prices --confirm-live"
Dry run:     python -m scripts.create_array_operator_prices --dry-run
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.pricing_array_operator import stripe_tiers, TIERS  # noqa: E402

DRY_RUN = "--dry-run" in sys.argv
CONFIRM_LIVE = "--confirm-live" in sys.argv


def _band_labels() -> str:
    parts = []
    for i, (up_to, unit) in enumerate(TIERS):
        if i == 0:
            lo, hi = 1, up_to
        else:
            prev = TIERS[i - 1][0]
            lo = (prev + 1) if prev is not None else 1
            hi = up_to
        rng = f"{lo}+" if hi is None else (f"{lo}" if lo == hi else f"{lo}-{hi}")
        price = "FREE" if unit == 0 else f"${unit/100:.2f}"
        parts.append(f"{rng} @ {price}")
    return ", ".join(parts)


def main() -> None:
    print("Array Operator pricing bands: " + _band_labels())
    print()

    if DRY_RUN:
        print("[--dry-run] Would create:")
        print('  Product: "Array Operator — Monitoring"')
        print("  Graduated tiered monthly price with tiers:")
        for t in stripe_tiers():
            print(f"    up_to={t['up_to']!r:>5}  unit_amount={t['unit_amount']}")
        print("\nNo Stripe calls made. Re-run without --dry-run against a real key.")
        return

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("STRIPE_SECRET_KEY not set. Use --dry-run, or export a test key.")
    if key.startswith("sk_live_") and not CONFIRM_LIVE:
        sys.exit(
            "REFUSING to create a LIVE price without --confirm-live.\n"
            "This is the owner-facing PUBLIC price. Re-run with --confirm-live\n"
            "only after the price point is signed off."
        )

    import stripe
    stripe.api_key = key
    mode = "LIVE" if key.startswith("sk_live_") else "TEST"
    print(f"Operating in Stripe {mode} mode.\n")

    # Idempotent-ish: reuse a product with the same name if present.
    name = "Array Operator — Monitoring"
    existing = stripe.Product.search(query=f'name:"{name}" AND active:"true"').data
    if existing:
        product = existing[0]
        print(f"  found existing product: {product.id}")
    else:
        product = stripe.Product.create(
            name=name,
            description="Always-on, dollar-first monitoring for solar array owners "
                        "(EnergyAgent — Array Operator). First array free.",
        )
        print(f"  created product: {product.id}")

    # Reuse a graduated price whose tiers match, else create one.
    def norm(ts):
        return [(("inf" if t.get("up_to") in (None, "inf") else int(t["up_to"])),
                 int(t["unit_amount"])) for t in ts]
    want = norm(stripe_tiers())
    found = None
    for p in stripe.Price.list(product=product.id, active=True, limit=100, expand=["data.tiers"]).data:
        if getattr(p, "billing_scheme", None) != "tiered":
            continue
        if p.recurring is None or p.recurring.interval != "month":
            continue
        have = norm([{"up_to": t.up_to, "unit_amount": t.unit_amount} for t in (p.tiers or [])])
        if have == want:
            found = p
            break
    if found:
        price = found
        print(f"  found existing tiered price: {price.id}")
    else:
        price = stripe.Price.create(
            product=product.id, currency="usd",
            billing_scheme="tiered", tiers_mode="graduated",
            tiers=stripe_tiers(),
            recurring={"interval": "month", "usage_type": "licensed"},
            expand=["tiers"],
        )
        print(f"  created tiered price: {price.id}")

    print()
    print("=" * 60)
    print("Set this on Railway (Array Operator owner billing):")
    print(f"  STRIPE_AO_ARRAY_PRICE_ID={price.id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
