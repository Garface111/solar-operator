"""One-shot: migrate active Array Operator subscription ITEMS from the flat
per-kW nameplate / per-offtaker invoicing prices onto the new GRADUATED
(volume-discounted) prices minted by create_ao_nameplate_tiered_price.py and
create_ao_invoicing_price.py.

For each active AO sub, any item whose price id matches an OLD price (passed
explicitly, never guessed) is swapped via SubscriptionItem.modify to the
corresponding NEW price (env STRIPE_AO_NAMEPLATE_PRICE_ID /
STRIPE_AO_INVOICING_PRICE_ID), with the SAME quantity. An item already on the
new price, or matching neither old price, is left untouched.

This is a LICENSED -> LICENSED price swap on the SAME line — no clear_usage
needed (that's only required when REMOVING a metered item). quantity is
preserved exactly, so a fleet/portfolio still inside the first (full-price)
tier is charged IDENTICALLY after the swap; only fleets/portfolios that cross a
breakpoint see a lower bill. proration_behavior="none" — no surprise mid-cycle
charge/credit; the new price takes effect from the next invoice.

Old price ids are passed explicitly (never inferred from a price's shape) so
this can never misfire against an unrelated Stripe price:
  --old-nameplate <price_id>   the flat nameplate price being retired
  --old-invoicing <price_id>   the flat (single-tier) invoicing price being retired
Either may be omitted to skip that meter.

SAFETY: refuses an sk_live_ key unless --confirm-live. Default is a DRY RUN
that prints exactly what it would do, mutating nothing.
Run (live): railway ssh --service web "cd /app && python -m scripts.migrate_ao_bulk_tiers_live --old-nameplate price_XXX --old-invoicing price_YYY --confirm-live"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIRM_LIVE = "--confirm-live" in sys.argv


def _arg(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main() -> None:
    from sqlalchemy import select
    from api.db import SessionLocal
    from api.models import Tenant
    from api.stripe_helpers import _ao_nameplate_price_id, _ao_invoicing_price_id

    old_np = _arg("--old-nameplate")
    old_inv = _arg("--old-invoicing")
    new_np = _ao_nameplate_price_id()
    new_inv = _ao_invoicing_price_id()
    if not old_np and not old_inv:
        sys.exit("Pass --old-nameplate and/or --old-invoicing (the price id(s) "
                  "being retired) — nothing to migrate without at least one.")
    if old_np and not new_np:
        sys.exit("--old-nameplate given but STRIPE_AO_NAMEPLATE_PRICE_ID (new) is unset.")
    if old_inv and not new_inv:
        sys.exit("--old-invoicing given but STRIPE_AO_INVOICING_PRICE_ID (new) is unset.")

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("STRIPE_SECRET_KEY not set.")
    live = key.startswith("sk_live_")
    do_it = CONFIRM_LIVE
    import stripe
    stripe.api_key = key
    print(f"Stripe {'LIVE' if live else 'TEST'} mode.")
    if old_np:
        print(f"  nameplate: {old_np} -> {new_np}")
    if old_inv:
        print(f"  invoicing: {old_inv} -> {new_inv}")
    print(f"  {'EXECUTING' if do_it else 'DRY RUN (no changes)'}.\n")

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
    swap_map = {}
    if old_np:
        swap_map[old_np] = ("nameplate", new_np)
    if old_inv:
        swap_map[old_inv] = ("invoicing", new_inv)

    for tenant_id, email, sub_id in targets:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            items = sub["items"]["data"]
            matched_any = False
            for it in items:
                pid, qty = it["price"]["id"], it.get("quantity")
                if pid not in swap_map:
                    continue
                matched_any = True
                kind, new_price = swap_map[pid]
                print(f"  MIGRATE {tenant_id} ({email}) sub={sub_id} item={it['id']}: "
                      f"{kind} {pid} (qty {qty}) -> {new_price} (qty {qty}, unchanged)")
                if do_it:
                    stripe.SubscriptionItem.modify(
                        it["id"], price=new_price, quantity=qty,
                        proration_behavior="none")
                    print("    done.")
            if not matched_any:
                print(f"  SKIP  {tenant_id} ({email}) — no item on an old price "
                      f"(items: {[i['price']['id'] for i in items]}).")
        except Exception as e:  # noqa: BLE001 — one tenant's Stripe hiccup must not abort the batch
            print(f"  ERROR {tenant_id} ({email}) sub={sub_id}: {e!r}")

    if not do_it:
        print("\n(dry run — re-run with --confirm-live to apply)")


if __name__ == "__main__":
    main()
