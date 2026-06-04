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

from .notify import send_internal_alert

logger = logging.getLogger(__name__)

STRIPE_ARRAY_PRICE_ID = os.getenv("STRIPE_ARRAY_PRICE_ID", "")


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
