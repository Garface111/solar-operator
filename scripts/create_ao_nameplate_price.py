"""One-shot: create the Array Operator per-kW NAMEPLATE (licensed) Stripe price.

Owner monitoring is billed on REGISTERED INVERTER NAMEPLATE (kW), not metered
kWh — deterministic and immune to capture gaps (Fronius/SMA have no backend API,
so daily-kWh capture is partial). This creates a LICENSED, recurring, per-unit
price ($0.30 / kW / month) on the existing "Array Operator — Monitoring" product.
The subscription-item quantity = the tenant's summed inverter nameplate (kW),
kept current by api/jobs/nameplate_sync.py.

SAFETY: refuses an sk_live_ key unless --confirm-live. --dry-run prints only.
Run (live): railway ssh --service web "cd /app && python -m scripts.create_ao_nameplate_price --confirm-live"
"""
import os
import sys

RATE_CENTS_PER_KW = 30   # $0.30 / kW / month
DRY_RUN = "--dry-run" in sys.argv
CONFIRM_LIVE = "--confirm-live" in sys.argv


def main() -> None:
    print(f"Array Operator NAMEPLATE price: {RATE_CENTS_PER_KW}¢ / kW / month (licensed, per_unit).")
    if DRY_RUN:
        print("[--dry-run] Would reuse-or-create a licensed monthly per-unit price "
              f"at unit_amount={RATE_CENTS_PER_KW} on product 'Array Operator — Monitoring'. "
              "No Stripe calls made.")
        return
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("STRIPE_SECRET_KEY not set. Use --dry-run, or export a test key.")
    if key.startswith("sk_live_") and not CONFIRM_LIVE:
        sys.exit("REFUSING to create a LIVE price without --confirm-live.")

    import stripe
    stripe.api_key = key
    mode = "LIVE" if key.startswith("sk_live_") else "TEST"
    print(f"Operating in Stripe {mode} mode.\n")

    name = "Array Operator — Monitoring"
    existing = stripe.Product.search(query=f'name:"{name}" AND active:"true"').data
    if existing:
        product = existing[0]
        print(f"  found existing product: {product.id}")
    else:
        product = stripe.Product.create(
            name=name,
            description="Always-on, dollar-first monitoring for solar array owners "
                        "(EnergyAgent — Array Operator). Billed per kW of registered nameplate.",
        )
        print(f"  created product: {product.id}")

    found = None
    for p in stripe.Price.list(product=product.id, active=True, limit=100).data:
        rec = getattr(p, "recurring", None)
        if (rec and rec.interval == "month" and getattr(rec, "usage_type", None) == "licensed"
                and getattr(p, "billing_scheme", None) == "per_unit"
                and p.unit_amount == RATE_CENTS_PER_KW and p.currency == "usd"):
            found = p
            break
    if found:
        price = found
        print(f"  found existing nameplate price: {price.id}")
    else:
        price = stripe.Price.create(
            product=product.id, currency="usd",
            unit_amount=RATE_CENTS_PER_KW,
            recurring={"interval": "month", "usage_type": "licensed"},
            nickname="AO nameplate $0.30/kW-mo",
            metadata={"basis": "inverter_nameplate_kw"},
        )
        print(f"  created nameplate price: {price.id}")

    print("\n" + "=" * 60)
    print("Set this on Railway (Array Operator per-kW nameplate billing):")
    print(f"  STRIPE_AO_NAMEPLATE_PRICE_ID={price.id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
