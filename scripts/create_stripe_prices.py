"""One-shot: create Solar Operator Stripe prices in test mode and print env vars to set.
Run via: railway ssh "cd /app && python -m scripts.create_stripe_prices"
"""
import os, stripe
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

# Idempotency-ish: look up by product name first
def get_or_create_product(name, description):
    existing = stripe.Product.search(query=f'name:"{name}" AND active:"true"').data
    if existing:
        print(f"  found existing product: {existing[0].id}")
        return existing[0]
    p = stripe.Product.create(name=name, description=description)
    print(f"  created product: {p.id}")
    return p

def get_or_create_price(product_id, **kwargs):
    # Search prices on this product matching the recurring/amount
    prices = stripe.Price.list(product=product_id, active=True, limit=100).data
    for p in prices:
        if (p.unit_amount == kwargs.get("unit_amount") and
            ((p.recurring is None) == (kwargs.get("recurring") is None)) and
            (p.recurring is None or p.recurring.interval == kwargs["recurring"]["interval"])):
            print(f"  found existing price: {p.id}")
            return p
    p = stripe.Price.create(product=product_id, currency="usd", **kwargs)
    print(f"  created price: {p.id}")
    return p

print("Setup product ($250 one-time):")
setup_prod = get_or_create_product("Solar Operator — Setup", "One-time onboarding setup fee")
setup_price = get_or_create_price(setup_prod.id, unit_amount=25000)

print("Array subscription (graduated volume pricing — $15 down to $10.50):")
array_prod = get_or_create_product("Solar Operator — Array Reporting", "Monthly per-array net-metering reporting")

# Graduated tiered price: Stripe applies each band's unit price to the arrays
# within that band automatically given the subscription quantity. Source of
# truth for the bands is api/pricing.py — import so the two never drift.
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.pricing import stripe_tiers, TIERS


def get_or_create_tiered_price(product_id, tiers, recurring):
    """Find an existing graduated price whose tiers match, else create one."""
    def norm(ts):
        return [(("inf" if t.get("up_to") in (None, "inf") else int(t["up_to"])),
                 int(t["unit_amount"])) for t in ts]
    want = norm(tiers)
    for p in stripe.Price.list(product=product_id, active=True, limit=100).data:
        if getattr(p, "billing_scheme", None) != "tiered":
            continue
        if p.recurring is None or p.recurring.interval != recurring["interval"]:
            continue
        have = norm([{"up_to": t.up_to, "unit_amount": t.unit_amount}
                     for t in (p.tiers or [])])
        if have == want:
            print(f"  found existing tiered price: {p.id}")
            return p
    p = stripe.Price.create(
        product=product_id, currency="usd",
        billing_scheme="tiered", tiers_mode="graduated",
        tiers=tiers, recurring=recurring,
        expand=["tiers"],
    )
    print(f"  created tiered price: {p.id}")
    return p


array_price = get_or_create_tiered_price(
    array_prod.id,
    tiers=stripe_tiers(),
    recurring={"interval": "month", "usage_type": "licensed"},
)
print("  bands: " + ", ".join(
    f"{'151+' if up_to is None else ('1–%d' % up_to if i == 0 else '%d–%d' % (TIERS[i-1][0] + 1, up_to))}"
    f" @ ${unit/100:.2f}"
    for i, (up_to, unit) in enumerate(TIERS)))

print()
print("=" * 60)
print("Set these on Railway:")
print(f"  STRIPE_SETUP_PRICE_ID={setup_price.id}")
print(f"  STRIPE_ARRAY_PRICE_ID={array_price.id}")
print("=" * 60)
