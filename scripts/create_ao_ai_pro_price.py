"""One-shot: mint the Energy Agent Pro Stripe price ($50/mo licensed).

Env after mint: STRIPE_AO_AI_PRO_PRICE_ID=<price_id>

SAFETY: refuses sk_live_ without --confirm-live. Use --dry-run to print only.

  python -m scripts.create_ao_ai_pro_price --dry-run
  railway ssh "cd /app && python -m scripts.create_ao_ai_pro_price --confirm-live"
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.pricing_ao_unified import AI_PRO_MONTHLY_CENTS, AI_PRO_MONTHLY_USD  # noqa: E402

DRY_RUN = "--dry-run" in sys.argv
CONFIRM_LIVE = "--confirm-live" in sys.argv

PRODUCT_NAME = "Array Operator — Energy Agent Pro"
PRICE_NICKNAME = f"Energy Agent Pro — ${AI_PRO_MONTHLY_USD:.0f}/mo unlimited AI"


def main() -> None:
    print(f"Energy Agent Pro: ${AI_PRO_MONTHLY_USD:.0f}/mo flat (licensed)")
    print(f"  unit_amount={AI_PRO_MONTHLY_CENTS} cents")
    print()

    if DRY_RUN:
        print("[--dry-run] Would create:")
        print(f'  Product: "{PRODUCT_NAME}"')
        print(f'  Recurring monthly licensed price: "{PRICE_NICKNAME}"')
        print(f"  unit_amount={AI_PRO_MONTHLY_CENTS}")
        print("\nNo Stripe calls made.")
        return

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("STRIPE_SECRET_KEY not set. Use --dry-run, or export a test key.")
    if key.startswith("sk_live_") and not CONFIRM_LIVE:
        sys.exit(
            "REFUSING to create a LIVE price without --confirm-live.\n"
            "Re-run with --confirm-live only after Ford signs off the $50/mo point."
        )

    import stripe
    stripe.api_key = key
    mode = "LIVE" if key.startswith("sk_live_") else "TEST"
    print(f"Operating in Stripe {mode} mode.\n")

    existing = stripe.Product.search(query=f'name:"{PRODUCT_NAME}" AND active:"true"').data
    if existing:
        product = existing[0]
        print(f"  found existing product: {product.id}")
    else:
        product = stripe.Product.create(
            name=PRODUCT_NAME,
            description=(
                "Unlimited integrated Energy Agent (thinking + voice). "
                f"Free accounts keep a small weekly sample; Pro is ${AI_PRO_MONTHLY_USD:.0f}/mo."
            ),
            metadata={"product": "energy_agent_pro"},
        )
        print(f"  created product: {product.id}")

    price = stripe.Price.create(
        product=product.id,
        currency="usd",
        unit_amount=int(AI_PRO_MONTHLY_CENTS),
        recurring={"interval": "month", "usage_type": "licensed"},
        nickname=PRICE_NICKNAME,
        metadata={"product": "energy_agent_pro"},
    )
    print(f"  created price: {price.id}")
    print()
    print("Set on Railway (web / production):")
    print(f"  STRIPE_AO_AI_PRO_PRICE_ID={price.id}")
    print("Then: Upgrade on Account Billing → live Checkout.")


if __name__ == "__main__":
    main()
