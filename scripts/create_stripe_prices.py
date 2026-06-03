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

print("Array subscription ($45/array/month):")
array_prod = get_or_create_product("Solar Operator — Array Reporting", "Monthly per-array net-metering reporting")
array_price = get_or_create_price(
    array_prod.id, unit_amount=4500,
    recurring={"interval": "month", "usage_type": "licensed"},
)

print()
print("=" * 60)
print("Set these on Railway:")
print(f"  STRIPE_SETUP_PRICE_ID={setup_price.id}")
print(f"  STRIPE_ARRAY_PRICE_ID={array_price.id}")
print("=" * 60)
