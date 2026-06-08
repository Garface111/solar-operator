"""
Shared Stripe helpers.

Kept separate to avoid circular imports: both api/onboarding.py and
api/account.py need reconcile_subscription_quantity, but onboarding
already imports from account (for mint_session_for_tenant).
"""
from __future__ import annotations

import os
import logging

import stripe
from sqlalchemy import func, select

from .notify import send_internal_alert

logger = logging.getLogger(__name__)

STRIPE_ARRAY_PRICE_ID = os.getenv("STRIPE_ARRAY_PRICE_ID", "")


def create_subscription_for_tenant(tenant_id: str) -> dict:
    """Create the live Stripe subscription for a tenant that has a card on file.

    Builds the subscription items (one-time setup fee + per-array line at the
    current billable array count, minimum 1) on the tenant's stored payment
    method, then flips the tenant to active/'active' and clears the trial clock.

    Shared by:
      - account.resume-from-pause (operator-/webhook-driven resume after a
        'paused_no_card' tenant adds a card)
    Reads price IDs from the environment at call time (so tests can monkeypatch
    via env). Returns {"ok": bool, ...}. Never raises — surfaces failures via an
    internal alert and an {"ok": False, "error": ...} payload so callers (and the
    webhook) don't 500.
    """
    # Deferred imports avoid a circular dependency (db/models ← account ← helpers).
    from .db import SessionLocal
    from .models import Tenant, Array

    setup_price_id = os.getenv("STRIPE_SETUP_PRICE_ID", "")
    array_price_id = os.getenv("STRIPE_ARRAY_PRICE_ID", "")
    if not os.getenv("STRIPE_SECRET_KEY"):
        return {"ok": False, "error": "stripe-not-configured"}
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t:
            return {"ok": False, "error": "tenant-not-found"}
        if not t.stripe_payment_method_id or not t.stripe_customer_id:
            return {"ok": False, "error": "no-payment-method"}
        if t.stripe_subscription_id:
            # Already has a live subscription — nothing to create. Idempotent.
            return {"ok": True, "subscription_id": t.stripe_subscription_id,
                    "already_active": True}
        array_count = db.execute(
            select(func.count()).select_from(Array).where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False),
            )
        ).scalar() or 0
        customer_id = t.stripe_customer_id
        pm_id = t.stripe_payment_method_id
        email = t.contact_email

    quantity = max(int(array_count), 1)
    items: list[dict] = []
    if setup_price_id:
        items.append({"price": setup_price_id, "quantity": 1})
    if array_price_id:
        items.append({"price": array_price_id, "quantity": quantity})

    try:
        sub = stripe.Subscription.create(
            customer=customer_id,
            items=items if items else None,
            default_payment_method=pm_id,
        )
        sub_dict = sub.to_dict() if hasattr(sub, "to_dict") else sub
        sub_id = sub_dict["id"]
    except Exception as e:  # noqa: BLE001 — never 500 the caller / webhook
        logger.exception("create_subscription_for_tenant failed for %s", tenant_id)
        send_internal_alert(
            f"⚠️ Resume subscription FAILED: {tenant_id}",
            f"Tenant {tenant_id} ({email}) added a card but creating the "
            f"subscription failed: {e}\nArrays: {array_count}, pm: {pm_id}. "
            f"Fix manually in the Stripe dashboard."
        )
        return {"ok": False, "error": str(e)}

    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if t:
            t.stripe_subscription_id = sub_id
            t.subscription_status = "active"
            t.active = True
            t.trial_ends_at = None
            db.commit()

    send_internal_alert(
        f"✅ Subscription resumed: {tenant_id}",
        f"Tenant {tenant_id} ({email}) added a card and resumed. "
        f"Arrays: {array_count}, billed qty: {quantity}. Subscription: {sub_id}"
    )
    return {"ok": True, "subscription_id": sub_id, "array_count": int(array_count),
            "quantity": quantity}


def reconcile_subscription_quantity(
    subscription_id: str, array_count: int, tenant_id: str, email: str
) -> None:
    """Bring the recurring per-array Stripe line item to match array_count.

    Finds the subscription item matching STRIPE_ARRAY_PRICE_ID and sets its
    quantity, prorating the current billing period. Best-effort — never raises
    so callers are never blocked by a Stripe hiccup. Fires an internal alert on
    failure so Ford can fix the quantity manually.

    Also a no-op when array_count is 0 (comped/free accounts) or when
    subscription_id is blank (no Stripe subscription on record).
    """
    if not subscription_id:
        logger.error(
            "Cannot reconcile billing for tenant %s — no stripe_subscription_id "
            "on record. Array count = %d.", tenant_id, array_count)
        send_internal_alert(
            "⚠️ Billing not reconciled — missing subscription id",
            f"Tenant {tenant_id} ({email}) changed array count to {array_count} "
            f"but has no stripe_subscription_id. Fix the subscription quantity manually."
        )
        return

    if not STRIPE_ARRAY_PRICE_ID:
        logger.warning(
            "STRIPE_ARRAY_PRICE_ID not set — skipping quantity reconciliation "
            "for tenant %s (array_count=%d)", tenant_id, array_count)
        return

    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        recurring_item = None
        for item in sub["items"]["data"]:
            if item["price"]["id"] == STRIPE_ARRAY_PRICE_ID:
                recurring_item = item
                break
        if recurring_item is None:
            raise RuntimeError(
                f"no line item matching STRIPE_ARRAY_PRICE_ID="
                f"{STRIPE_ARRAY_PRICE_ID!r} on subscription {subscription_id}")

        target_qty = max(array_count, 1)  # Stripe requires quantity >= 1
        current_qty = recurring_item.get("quantity", 0)
        if current_qty == target_qty:
            logger.info(
                "reconcile: subscription %s for tenant %s already at quantity=%d — no-op",
                subscription_id, tenant_id, target_qty)
            return

        stripe.SubscriptionItem.modify(
            recurring_item["id"],
            quantity=target_qty,
            proration_behavior="create_prorations",
        )
        logger.info(
            "Reconciled subscription %s for tenant %s: quantity %d → %d",
            subscription_id, tenant_id, current_qty, target_qty)
    except Exception as e:  # noqa: BLE001 — must never block callers
        logger.exception(
            "Stripe billing reconciliation FAILED for tenant %s (sub %s, "
            "wanted quantity=%d): %s", tenant_id, subscription_id, array_count, e)
        send_internal_alert(
            "⚠️ Stripe billing reconciliation failed",
            f"Tenant {tenant_id} ({email}) changed array count to {array_count}, "
            f"but updating subscription {subscription_id} to "
            f"quantity={array_count} failed: {e}\n\n"
            f"Stripe is still billing for the old quantity. Fix it manually in "
            f"the Stripe dashboard (proration_behavior=create_prorations)."
        )
