"""One-shot: migrate active Array Operator subscriptions from per-kWh METERED
monitoring to per-kW NAMEPLATE monitoring.

For each active AO sub that has a monitoring line:
  * if it already has the nameplate line  -> skip (idempotent)
  * else if it has a metered (per-kWh) line -> REPLACE it with the nameplate line
    (quantity = tenant_nameplate_kw); any other line (per-offtaker invoicing) is
    left untouched.

proration_behavior="none": the metered line settles its accrued usage on removal;
the nameplate line takes full effect from the next invoice (no surprise proration).

SAFETY: refuses an sk_live_ key unless --confirm-live. Default is a DRY RUN that
prints exactly what it would do. Requires STRIPE_AO_NAMEPLATE_PRICE_ID to be set
(run create_ao_nameplate_price.py first + set the env var).
Run (live): railway ssh --service web "cd /app && python -m scripts.migrate_ao_to_nameplate --confirm-live"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIRM_LIVE = "--confirm-live" in sys.argv


def main() -> None:
    from sqlalchemy import select
    from api.db import SessionLocal
    from api.models import Tenant
    from api.stripe_helpers import _ao_nameplate_price_id, tenant_nameplate_kw

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    np_price = _ao_nameplate_price_id()
    if not np_price:
        sys.exit("STRIPE_AO_NAMEPLATE_PRICE_ID is not set — create the price first.")
    if not key:
        sys.exit("STRIPE_SECRET_KEY not set.")
    live = key.startswith("sk_live_")
    # Default run is a DRY RUN (safe in live mode too — it only prints). Mutations
    # happen only with --confirm-live.
    do_it = CONFIRM_LIVE
    import stripe
    stripe.api_key = key
    print(f"Stripe {'LIVE' if live else 'TEST'} mode. nameplate price={np_price}. "
          f"{'EXECUTING' if do_it else 'DRY RUN (no changes)'}.\n")

    with SessionLocal() as db:
        owners = db.execute(
            select(Tenant).where(
                Tenant.product == "array_operator",
                Tenant.subscription_status == "active",
                Tenant.stripe_subscription_id.isnot(None),
            )
        ).scalars().all()
        targets = [(t.id, t.contact_email, t.stripe_subscription_id) for t in owners]

    print(f"{len(targets)} active AO subscription(s).\n")
    for tenant_id, email, sub_id in targets:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            items = sub["items"]["data"]
            has_nameplate = any(it["price"]["id"] == np_price for it in items)
            metered = [it for it in items
                       if (it["price"].get("recurring") or {}).get("usage_type") == "metered"]
            with SessionLocal() as db:
                kw = max(tenant_nameplate_kw(db, tenant_id), 1)
            if has_nameplate:
                print(f"  SKIP  {tenant_id} ({email}) — already on nameplate.")
                continue
            if not metered:
                print(f"  SKIP  {tenant_id} ({email}) — no metered monitoring line "
                      f"(items: {[it['price']['id'] for it in items]}).")
                continue
            other = [it['price']['id'] for it in items if it not in metered]
            print(f"  MIGRATE {tenant_id} ({email}) sub={sub_id}: "
                  f"drop metered {[it['price']['id'] for it in metered]} -> "
                  f"nameplate {np_price} qty {kw} kW (${kw*0.50:.2f}/mo); keep {other}")
            if do_it:
                # clear_usage=True is REQUIRED by Stripe to remove a metered item.
                new_items = [{"id": it["id"], "deleted": True, "clear_usage": True}
                             for it in metered]
                new_items.append({"price": np_price, "quantity": kw})
                stripe.Subscription.modify(
                    sub_id, items=new_items, proration_behavior="none",
                )
                print(f"        ✓ migrated.")
        except Exception as exc:
            print(f"  ERROR {tenant_id} ({email}): {exc}")

    if not do_it:
        print("\nDry run only. Re-run with --confirm-live to apply.")


if __name__ == "__main__":
    main()
