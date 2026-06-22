"""
Shared Stripe helpers.

Kept separate to avoid circular imports: both api/onboarding.py and
api/account.py need reconcile_subscription_quantity, but onboarding
already imports from account (for mint_session_for_tenant).

Two billing meters live here:
  - NEPOOL Operator (product != "array_operator"): per-ARRAY licensed price.
    Stripe quantity = array count; reconcile_subscription_quantity keeps it in
    sync; create_subscription_for_tenant adds a $250 one-time setup item.
  - Array Operator (product == "array_operator"): per-kWh METERED price. The
    subscription item carries NO quantity — a usage-reporting job
    (api/jobs/usage_report.py) reports each tenant's monthly kWh to Stripe and
    Stripe applies the graduated tiers. reconcile is a no-op for these (there is
    no array quantity to reconcile), and there is NO setup fee.
"""
from __future__ import annotations

import os
import logging

import stripe
from sqlalchemy import func, select

from .notify import send_internal_alert

logger = logging.getLogger(__name__)

STRIPE_ARRAY_PRICE_ID = os.getenv("STRIPE_ARRAY_PRICE_ID", "")
# Array Operator (owner-side) per-kWh METERED price. Separate Stripe price from
# the NEPOOL per-array price above; set once
# scripts/create_array_operator_prices.py has minted it. When blank, AO tenants
# fall back to the NEPOOL price so billing never silently bills $0 — but the
# fallback fires an alert.
#
# NOTE: renamed from STRIPE_AO_ARRAY_PRICE_ID → STRIPE_AO_KWH_PRICE_ID when owner
# billing moved from per-array to per-kWh (Jun 2026). The old env var is still
# read as a fallback so a half-migrated environment never bills $0.
STRIPE_AO_KWH_PRICE_ID = (
    os.getenv("STRIPE_AO_KWH_PRICE_ID", "")
    or os.getenv("STRIPE_AO_ARRAY_PRICE_ID", "")
)


def _ao_kwh_price_id() -> str:
    """Resolve the AO per-kWh metered price id from env at call time."""
    return (
        os.getenv("STRIPE_AO_KWH_PRICE_ID", "")
        or os.getenv("STRIPE_AO_ARRAY_PRICE_ID", "")
    )


def is_array_operator(product: str | None) -> bool:
    """True when a tenant bills on the Array Operator per-kWh meter."""
    return (product or "nepool") == "array_operator"


def billable_array_count(db, tenant_id: str) -> int:
    """The canonical "how many arrays does this tenant pay for" count.

    Billable = NOT soft-deleted AND NOT excluded (see Array.excluded:
    "excluded from reports AND billing"). This is the single source of truth so
    the Stripe quantity, the dashboard "next charge" estimate
    (_billing_summary_arrays), and the original subscription
    (create_subscription_for_tenant) can never disagree. EVERY
    reconcile_subscription_quantity callsite must feed its count through here —
    callers that counted only `deleted_at IS NULL` (or nothing) were billing
    excluded/soft-deleted arrays, overcharging the customer (fixed June 2026).
    """
    from .models import Array
    return int(db.execute(
        select(func.count()).select_from(Array).where(
            Array.tenant_id == tenant_id,
            Array.deleted_at.is_(None),
            Array.excluded.is_(False),
        )
    ).scalar() or 0)


def array_price_id_for_product(product: str | None) -> str:
    """Return the recurring Stripe price id for a tenant's product.

    "array_operator" → the per-kWh METERED price (STRIPE_AO_KWH_PRICE_ID).
    anything else ("nepool"/None/legacy) → the per-array price (STRIPE_ARRAY_PRICE_ID).

    Reads env at call time so tests can monkeypatch. If an Array Operator tenant
    is hit before the AO price exists, we fall back to the NEPOOL price AND
    alert, rather than create a broken/empty subscription.
    """
    if is_array_operator(product):
        ao = _ao_kwh_price_id()
        if ao:
            return ao
        send_internal_alert(
            "⚠️ Array Operator per-kWh price id missing",
            "An array_operator tenant needs billing but STRIPE_AO_KWH_PRICE_ID "
            "is not set. Falling back to the NEPOOL price. Run "
            "scripts/create_array_operator_prices.py and set the env var.",
        )
    return os.getenv("STRIPE_ARRAY_PRICE_ID", "")


def create_subscription_for_tenant(tenant_id: str) -> dict:
    """Create the live Stripe subscription for a tenant that has a card on file.

    NEPOOL: items = [one-time setup fee, per-array line at current billable
    array count (min 1)].
    Array Operator: items = [per-kWh METERED line with NO quantity] — usage is
    reported separately by the usage-report job; no setup fee.

    Then flips the tenant to active/'active' and clears the trial clock.

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
        product = getattr(t, "product", "nepool")

    array_price_id = array_price_id_for_product(product)
    ao = is_array_operator(product)

    quantity = max(int(array_count), 1)
    items: list[dict] = []
    add_invoice_items: list[dict] = []
    if ao:
        # Per-kWh metered line — Stripe REJECTS `quantity` on a metered price.
        # No setup fee on the owner side. Usage is reported by the usage-report job.
        if array_price_id:
            items.append({"price": array_price_id})
    else:
        if array_price_id:
            items.append({"price": array_price_id, "quantity": quantity})
        # The $250 setup is a ONE-TIME price. Stripe REJECTS a one_time price in
        # subscription `items` (those must be recurring) — so attach it to the
        # FIRST invoice via add_invoice_items instead. (Putting it in `items`
        # made every NEPOOL subscription-create fail with InvalidRequestError.)
        if setup_price_id:
            add_invoice_items.append({"price": setup_price_id, "quantity": 1})

    try:
        sub = stripe.Subscription.create(
            customer=customer_id,
            items=items if items else None,
            default_payment_method=pm_id,
            add_invoice_items=add_invoice_items or None,
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

    meter = "per-kWh (metered)" if ao else f"per-array qty {quantity}"
    send_internal_alert(
        f"✅ Subscription resumed: {tenant_id}",
        f"Tenant {tenant_id} ({email}) added a card and resumed. "
        f"Arrays: {array_count}, billed: {meter}. Subscription: {sub_id}"
    )
    return {"ok": True, "subscription_id": sub_id, "array_count": int(array_count),
            "quantity": (None if ao else quantity), "metered": ao}


def reconcile_subscription_quantity(
    subscription_id: str, array_count: int, tenant_id: str, email: str
) -> None:
    """Bring the recurring per-array Stripe line item to match array_count.

    Per-ARRAY (NEPOOL) only: finds the subscription item matching the per-array
    price and sets its quantity, prorating the current period. Best-effort —
    never raises so callers are never blocked by a Stripe hiccup. Fires an
    internal alert on failure.

    For Array Operator (per-kWh METERED) tenants this is a NO-OP: a metered line
    has no quantity (Stripe rejects SubscriptionItem.modify(quantity=...) on it),
    and billing volume is driven by the usage-report job, not the array count.
    We detect a metered line on the subscription and skip silently.

    Also a no-op when array_count is 0 (comped/free accounts) or when
    subscription_id is blank (no Stripe subscription on record).
    """
    if not subscription_id:
        # A comped / canceled / demo tenant legitimately has no Stripe
        # subscription — there is nothing to reconcile and it is NOT a billing
        # fault, so don't log an error (-> Sentry) or fire a manual-fix alert.
        # Only a tenant that should be billing but is missing its sub id is real.
        from .db import SessionLocal
        from .models import Tenant
        _plan = _status = None
        try:
            with SessionLocal() as _db:
                _t = _db.get(Tenant, tenant_id)
                if _t is not None:
                    _plan = (_t.plan or "").lower()
                    _status = (_t.subscription_status or "").lower()
        except Exception:  # pragma: no cover — fall through to the alert path
            pass
        if _plan in ("comped", "demo") or _status in ("comped", "canceled"):
            logger.info(
                "reconcile: tenant %s has no stripe_subscription_id and is "
                "%s/%s (no active billing) — nothing to reconcile, skipping.",
                tenant_id, _plan or "?", _status or "?")
            return
        logger.error(
            "Cannot reconcile billing for tenant %s — no stripe_subscription_id "
            "on record. Array count = %d.", tenant_id, array_count)
        send_internal_alert(
            "⚠️ Billing not reconciled — missing subscription id",
            f"Tenant {tenant_id} ({email}) changed array count to {array_count} "
            f"but has no stripe_subscription_id. Fix the subscription quantity manually."
        )
        return

    # Match against the per-array price (NEPOOL). The AO per-kWh price is metered
    # and intentionally NOT in this set — an AO subscription has no licensed
    # per-array line to reconcile.
    known_price_ids = {
        pid for pid in (
            STRIPE_ARRAY_PRICE_ID,
            os.getenv("STRIPE_ARRAY_PRICE_ID", ""),
        ) if pid
    }

    try:
        sub = stripe.Subscription.retrieve(subscription_id)

        # If ANY line on this subscription is metered, this tenant is billed by
        # usage (Array Operator) — array-count reconciliation does not apply.
        for item in sub["items"]["data"]:
            recurring = item["price"].get("recurring") or {}
            if recurring.get("usage_type") == "metered":
                logger.info(
                    "reconcile: subscription %s for tenant %s is metered (per-kWh) "
                    "— skipping array-quantity reconciliation", subscription_id, tenant_id)
                return

        if not known_price_ids:
            logger.warning(
                "No per-array Stripe price ids set — skipping quantity reconciliation "
                "for tenant %s (array_count=%d)", tenant_id, array_count)
            return

        recurring_item = None
        for item in sub["items"]["data"]:
            if item["price"]["id"] in known_price_ids:
                recurring_item = item
                break
        if recurring_item is None:
            raise RuntimeError(
                f"no line item matching a known per-array price "
                f"{known_price_ids!r} on subscription {subscription_id}")

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
