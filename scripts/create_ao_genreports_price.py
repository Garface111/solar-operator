"""One-shot: create the ARRAY OPERATOR *generation-reports* Stripe price.

The generation-reports plan (api/pricing_ao_genreports.py) bills $15.00 per client
per calendar QUARTER, charged on the FIRST real OUTPUT (a report SEND or a DOWNLOAD
of the deliverable) for that (client, quarter), then unlimited (THE FOLD — Invoices
-> "Generation reports"). Building + previewing + auto-propagating the fleet is FREE.
So the Stripe price is METERED (usage-based), NOT a licensed per-client quantity:

  - Product "Array Operator — Generation reports"
  - A METERED, recurring monthly price: unit_amount = $15.00, usage_type='metered',
    aggregate_usage='sum'. Each FIRST output for a (client, quarter) records one $15
    ledger row (api/delivery.py's GenReportCharge) and api/jobs/genreports_usage.py
    pushes one usage unit per row to Stripe. Idempotent per (tenant, client, quarter)
    so repeat outputs of the same quarter never double-charge.
    -> STRIPE_AO_GENREPORTS_PRICE_ID

There is NO setup fee and NO per-client subscription quantity for this plan.

SAFETY — this script does NOTHING to Stripe unless you pass --confirm-live:
  * With NO flag (the default): it PRINTS exactly what it WOULD create and exits.
    No Stripe SDK is even imported. Use this to review the payload.
  * With --confirm-live: it talks to Stripe using STRIPE_SECRET_KEY (test OR live —
    the flag is the single deliberate signal that authorizes a real mint).

  Review (no Stripe call):  python -m scripts.create_ao_genreports_price
  Mint (test key):          STRIPE_SECRET_KEY=sk_test_... python -m scripts.create_ao_genreports_price --confirm-live
  Mint (LIVE — Ford only):  railway ssh --service web "cd /app && python -m scripts.create_ao_genreports_price --confirm-live"

DO NOT run the --confirm-live form as part of building this feature. The live mint
+ STRIPE_AO_GENREPORTS_PRICE_ID env set is Ford's confirm-gated activation step.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.pricing_ao_genreports import PRICE_CENTS  # noqa: E402

CONFIRM_LIVE = "--confirm-live" in sys.argv

PRODUCT_NAME = "Array Operator — Generation reports"
PRICE_NICKNAME = "AO Generation reports — per client per quarter (metered)"


def _summary() -> str:
    return (f"${PRICE_CENTS/100:.2f} per client per quarter (first output), metered "
            f"(usage_type=metered, aggregate_usage=sum; 1 usage unit per client-quarter)")


def main() -> None:
    print("Array Operator GENERATION-REPORTS plan: " + _summary())
    print()

    if not CONFIRM_LIVE:
        # Default: describe the payload and exit WITHOUT importing/calling Stripe.
        print("[no --confirm-live] Would create:")
        print(f'  Product: "{PRODUCT_NAME}"')
        print("  METERED monthly price:")
        print("    billing_scheme=per_unit  usage_type=metered  aggregate_usage=sum  interval=month")
        print(f"    unit_amount={PRICE_CENTS} (cents) = ${PRICE_CENTS/100:.2f} per usage unit (per client-quarter)")
        print("  (No setup fee; no per-client subscription quantity — usage-based.)")
        print("\nNo Stripe calls made. Re-run with --confirm-live against a real key to mint.")
        return

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("STRIPE_SECRET_KEY not set. Re-run without --confirm-live to preview, "
                 "or export a Stripe key.")
    # --confirm-live is required for ANY Stripe call; a live key is minted only via
    # this same deliberate flag. (There is intentionally no way to mint silently.)

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
            description="Automatic NEPOOL/REC generation reports for solar operators "
                        "(EnergyAgent — Array Operator). Billed $15 per client per "
                        "report sent (metered).",
        )
        print(f"  created product: {product.id}")

    # Recurring METERED price — reuse if an identical one already exists.
    price = None
    for p in stripe.Price.list(product=product.id, active=True, limit=100).data:
        if getattr(p, "billing_scheme", None) != "per_unit":
            continue
        if p.recurring is None or p.recurring.interval != "month":
            continue
        if getattr(p.recurring, "usage_type", None) != "metered":
            continue
        if p.unit_amount == PRICE_CENTS and p.currency == "usd":
            price = p
            print(f"  found existing metered price: {price.id}")
            break
    if price is None:
        price = stripe.Price.create(
            product=product.id, currency="usd", nickname=PRICE_NICKNAME,
            unit_amount=PRICE_CENTS,
            recurring={"interval": "month", "usage_type": "metered",
                       "aggregate_usage": "sum"},
        )
        print(f"  created metered price: {price.id}")

    print()
    print("=" * 64)
    print("Set this on Railway (Array Operator generation-reports billing):")
    print(f"  STRIPE_AO_GENREPORTS_PRICE_ID={price.id}")
    print("=" * 64)


if __name__ == "__main__":
    main()
