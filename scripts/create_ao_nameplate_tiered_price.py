"""One-shot: create the GRADUATED (volume-discounted) Array Operator per-kW
NAMEPLATE Stripe price, replacing the flat $0.15/kW price minted by
create_ao_nameplate_price.py.

Stripe prices are IMMUTABLE — the existing flat price (billing_scheme='per_unit')
can't gain tiers. This creates a NEW price (billing_scheme='tiered',
tiers_mode='graduated') on the SAME "Array Operator — Monitoring" product, whose
tiers come straight from api.pricing_ao_nameplate.stripe_tiers() (the single
source of truth for the rate curve). The subscription-item mechanism is
unchanged — quantity = the tenant's summed inverter nameplate (kW), kept current
by api/jobs/nameplate_sync.py; Stripe bins that quantity into the new bands.

After minting: point STRIPE_AO_NAMEPLATE_PRICE_ID at the new price id (Railway
env var) — NEW subscriptions pick it up immediately. Any EXISTING subscription
item still on the old flat price needs a separate item-level swap (see
scripts/migrate_ao_bulk_tiers_live.py) — creating a price never touches a live
subscription by itself.

SAFETY: refuses an sk_live_ key unless --confirm-live. --dry-run prints only.
Run (live): railway ssh --service web "cd /app && python -m scripts.create_ao_nameplate_tiered_price --confirm-live"
"""
import os
import sys

DRY_RUN = "--dry-run" in sys.argv
CONFIRM_LIVE = "--confirm-live" in sys.argv


def main() -> None:
    from api.pricing_ao_nameplate import stripe_tiers, TIERS

    print("Array Operator NAMEPLATE price (GRADUATED, licensed, tiered):")
    for up_to, unit in TIERS:
        band = f"up to {up_to:,} kW" if up_to is not None else "20,000+ kW"
        print(f"  {band:<20} {unit}¢/kW")

    if DRY_RUN:
        print("\n[--dry-run] Would create a licensed monthly TIERED/graduated price "
              "on product 'Array Operator — Monitoring' with the tiers above. "
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
    print(f"\nOperating in Stripe {mode} mode.\n")

    name = "Array Operator — Monitoring"
    existing = stripe.Product.search(query=f'name:"{name}" AND active:"true"').data
    if not existing:
        sys.exit(f"Product '{name}' not found — expected it to already exist "
                  "(created by create_ao_nameplate_price.py).")
    product = existing[0]
    print(f"  product: {product.id}")

    price = stripe.Price.create(
        product=product.id, currency="usd",
        billing_scheme="tiered",
        tiers_mode="graduated",
        tiers=stripe_tiers(),
        recurring={"interval": "month", "usage_type": "licensed"},
        nickname="AO nameplate — graduated volume discount",
        metadata={"basis": "inverter_nameplate_kw", "pricing_module": "pricing_ao_nameplate"},
    )
    print(f"  created tiered nameplate price: {price.id}")

    print("\n" + "=" * 60)
    print("Set this on Railway (Array Operator per-kW nameplate billing):")
    print(f"  STRIPE_AO_NAMEPLATE_PRICE_ID={price.id}")
    print("Then migrate any live subscription's nameplate item to it — see")
    print("scripts/migrate_ao_bulk_tiers_live.py.")
    print("=" * 60)


if __name__ == "__main__":
    main()
