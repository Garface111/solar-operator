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


# Array Operator NAMEPLATE billing (Jun 2026): owner monitoring is billed on
# REGISTERED INVERTER NAMEPLATE (kW), not metered kWh. Deterministic + immune to
# capture gaps — Fronius/SMA have no backend API, so daily-kWh capture is partial
# and we were under-billing. A LICENSED per-kW recurring price ($0.50/kW-month);
# the subscription-item quantity = the tenant's summed inverter nameplate (kW).
# When STRIPE_AO_NAMEPLATE_PRICE_ID is set it SUPERSEDES the per-kWh metered price
# for the monitoring line (the per-offtaker invoicing line is unaffected).
def _ao_nameplate_price_id() -> str:
    """Resolve the AO per-kW NAMEPLATE (licensed) price id from env at call time."""
    return os.getenv("STRIPE_AO_NAMEPLATE_PRICE_ID", "")


def tenant_nameplate_kw(db, tenant_id: str) -> int:
    """Total REGISTERED inverter nameplate (kW, rounded) across a tenant's billable
    arrays — the quantity AO monitoring bills on. Uses each inverter's stored
    nameplate, falling back to a model-code-derived rating (the SAME derivation the
    dashboard/fleet-tree uses), so the billed capacity matches what the owner sees.
    Excludes soft-deleted / excluded arrays. Cheap — no telemetry pull."""
    from .models import Inverter, Array
    from .inverter_fleet import _nameplate_from_model
    rows = db.execute(
        select(Inverter).join(Array, Array.id == Inverter.array_id).where(
            Inverter.tenant_id == tenant_id,
            Inverter.deleted_at.is_(None),
            Array.deleted_at.is_(None),
            Array.excluded.is_(False),
        )
    ).scalars().all()
    total = 0.0
    for iv in rows:
        np = getattr(iv, "nameplate_kw", None) or _nameplate_from_model(
            getattr(iv, "vendor", None), getattr(iv, "model", None))
        if np:
            total += float(np)
    return int(round(total))


def ao_nameplate_rate_cents() -> int | None:
    """The nameplate price's unit_amount (cents per kW-month) from Stripe, or None.
    Read from the LIVE price so the in-app bill display always matches what Stripe
    charges (no drift when the rate is changed — that drift was exactly the bug
    where the 'Your bill' panel still showed per-kWh after the switch)."""
    import os as _os
    pid = _ao_nameplate_price_id()
    if not pid or not _os.getenv("STRIPE_SECRET_KEY"):
        return None
    try:
        stripe.api_key = _os.getenv("STRIPE_SECRET_KEY", "")
        p = stripe.Price.retrieve(pid)
        amt = p.get("unit_amount") if isinstance(p, dict) else getattr(p, "unit_amount", None)
        return int(amt) if amt is not None else None
    except Exception:  # noqa: BLE001 — never fail a billing read on a Stripe hiccup
        return None


def ao_monitoring_item(db, tenant_id: str) -> dict | None:
    """The Stripe subscription line for AO MONITORING. Prefers the per-kW NAMEPLATE
    price (licensed; quantity = registered nameplate kW, min 1). Falls back to the
    legacy per-kWh metered price only if the nameplate price isn't configured yet.
    Returns None when no monitoring price is configured at all."""
    np_price = _ao_nameplate_price_id()
    if np_price:
        return {"price": np_price, "quantity": max(tenant_nameplate_kw(db, tenant_id), 1)}
    kwh_price = _ao_kwh_price_id()
    if kwh_price:
        return {"price": kwh_price}   # legacy metered line (Stripe rejects quantity)
    return None


def is_array_operator(product: str | None) -> bool:
    """True when a tenant is on the Array Operator product (owner app)."""
    return (product or "nepool") == "array_operator"


def _ao_invoicing_price_id() -> str:
    """Resolve the AO per-offtaker invoicing (licensed) price id from env."""
    return os.getenv("STRIPE_AO_INVOICING_PRICE_ID", "")


def _ao_invoicing_setup_price_id() -> str:
    """Resolve the AO invoicing one-time $250 setup price id from env (optional —
    leave unset to launch without a setup fee / to grandfather early customers)."""
    return os.getenv("STRIPE_AO_INVOICING_SETUP_PRICE_ID", "")


def ao_genreports_price_id() -> str:
    """Resolve the AO per-client GENERATION-REPORTS (licensed, FLAT $15/client)
    price id from env at call time (so tests can monkeypatch).

    Blank until Ford mints the price + sets STRIPE_AO_GENREPORTS_PRICE_ID. While
    blank NO generation-reports line is ever added (see ao_genreports_item) and we
    NEVER fall back to another price — so the whole plan is inert and no one is
    billed for it. This is the fold's ($15/client) meter for the NEPOOL/REC
    generation-reports capability (api/pricing_ao_genreports.py)."""
    return os.getenv("STRIPE_AO_GENREPORTS_PRICE_ID", "")


# Legacy plan labels (monitoring / invoicing / both) — retired Jul 2026.
# Array Operator is ONE regular product: nameplate kW + offtaker count.
# AI Pro is the only paid add-on. billing_plan is ignored for AO billing.
_AO_PLANS = {"monitoring", "invoicing", "both"}


def is_ao_invoicing(product: str | None, billing_plan: str | None = None) -> bool:
    """True when Array Operator should bill the per-OFFTAKER line.

    Jul 2026: always on for every AO tenant (regular product = capacity + offtakers).
    ``billing_plan`` is accepted for call-site compatibility and ignored.
    """
    return is_array_operator(product)


def is_ao_monitoring(product: str | None, billing_plan: str | None = None) -> bool:
    """True when Array Operator should bill fleet monitoring (nameplate kW).

    Jul 2026: always on for every AO tenant. ``billing_plan`` ignored.
    """
    return is_array_operator(product)


def ao_plan_features(product: str | None, billing_plan: str | None = None) -> dict:
    """What a tenant can ACCESS. AO always has full product surface (no plan picker).

    Returns {plan, plan_chosen, vendor_data, invoicing}. plan is always "regular"
    for AO (legacy both-equivalent). NEPOOL gets everything True.
    """
    if not is_array_operator(product):
        return {"plan": None, "plan_chosen": True, "vendor_data": True, "invoicing": True}
    return {
        "plan": "regular",
        "plan_chosen": True,
        "vendor_data": True,
        "invoicing": True,
    }


def ao_gets_vendor_emails(product: str | None, billing_plan: str | None = None) -> bool:
    """Whether a tenant should receive VENDOR-DATA emails (fleet-health digests).

    Always on for AO (no invoicing-only plan anymore) and all NEPOOL tenants.
    """
    return True


def billable_offtaker_count(db, tenant_id: str) -> int:
    """Canonical "how many offtakers does this tenant pay for" on the invoicing plan
    = active, non-deleted BillingReportSubscription rows. Single source of truth so
    the Stripe quantity and any 'next charge' estimate can never disagree."""
    from .models import BillingReportSubscription
    return int(db.execute(
        select(func.count()).select_from(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None),
        )
    ).scalar() or 0)


def ao_genreports_metered_item(tenant) -> dict | None:
    """The Stripe subscription line for AO GENERATION REPORTS, or None.

    Generation reports bill METERED, per successful client SEND ($15/client/report —
    api/pricing_ao_genreports.py + the GenReportCharge ledger written in
    api/delivery.py), NOT as a licensed per-client quantity. So the subscription just
    carries a quantity-less METERED line; a separate job
    (api/jobs/genreports_usage.py) pushes one usage unit per recorded billable send.

    Returns {"price": <id>} ONLY when the tenant is in the reports world (and not a
    demo) AND STRIPE_AO_GENREPORTS_PRICE_ID is set. GUARD: when the price id is unset
    we return None and add NO line — we NEVER fall back to another price. So until
    Ford mints the metered price + sets the env var this is completely inert and no
    one is billed. (A metered line with zero reported usage bills $0 regardless.)
    """
    price_id = ao_genreports_price_id()
    if not price_id:
        return None
    from .report_eligibility import tenant_in_reports_world
    if getattr(tenant, "is_demo", False):
        return None
    if not tenant_in_reports_world(tenant):
        return None
    return {"price": price_id}   # metered — Stripe rejects a quantity here


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
        # Monitoring price: per-kW NAMEPLATE (licensed) preferred, legacy per-kWh
        # metered as fallback. Callers that build subscription items should use
        # ao_monitoring_item() (it carries the nameplate quantity); this returns
        # just the id for code that only needs the price.
        ao = _ao_nameplate_price_id() or _ao_kwh_price_id()
        if ao:
            return ao
        send_internal_alert(
            "⚠️ Array Operator monitoring price id missing",
            "An array_operator tenant needs billing but neither "
            "STRIPE_AO_NAMEPLATE_PRICE_ID nor STRIPE_AO_KWH_PRICE_ID is set. "
            "Falling back to the NEPOOL price. Run scripts/create_ao_nameplate_price.py "
            "and set the env var.",
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
        offtaker_count = billable_offtaker_count(db, t.id)
        customer_id = t.stripe_customer_id
        pm_id = t.stripe_payment_method_id
        email = t.contact_email
        product = getattr(t, "product", "nepool")
        billing_plan = getattr(t, "billing_plan", None)
        # Build the monitoring line (per-kW nameplate) while the session is open.
        monitoring_item = ao_monitoring_item(db, t.id)
        # Build the generation-reports METERED line ($15/client/report, billed per
        # successful send via the GenReportCharge ledger + jobs/genreports_usage.py).
        # None unless the tenant is in the reports world AND STRIPE_AO_GENREPORTS_PRICE_ID
        # is set — inert (no line) otherwise. A metered line at zero usage bills $0.
        genreports_item = ao_genreports_metered_item(t)

    ao = is_array_operator(product)
    # AO regular product: ALWAYS nameplate capacity + offtaker count (no plan split).
    has_invoicing = is_ao_invoicing(product, billing_plan)
    has_monitoring = is_ao_monitoring(product, billing_plan)

    quantity = max(int(array_count), 1)
    items: list[dict] = []
    add_invoice_items: list[dict] = []
    if ao:
        # Regular AO = fleet monitoring (nameplate kW) + offtaker invoices (count).
        # AI Pro is a separate add-on subscription (see account.ai_pro_checkout).
        if has_monitoring and monitoring_item:
            items.append(monitoring_item)
        if has_invoicing and int(offtaker_count or 0) > 0:
            inv_price = _ao_invoicing_price_id()
            if not inv_price:
                send_internal_alert(
                    "⚠️ AO invoicing price id missing",
                    f"Tenant {tenant_id} ({email}) has offtakers but "
                    "STRIPE_AO_INVOICING_PRICE_ID is not set. Run "
                    "scripts/create_ao_invoicing_price.py and set the env var.",
                )
                return {"ok": False, "error": "ao-invoicing-price-missing"}
            items.append({"price": inv_price, "quantity": int(offtaker_count)})
            inv_setup = _ao_invoicing_setup_price_id()
            if inv_setup:
                add_invoice_items.append({"price": inv_setup, "quantity": 1})
        # Generation-reports METERED line — the folded NEPOOL/REC reporting
        # capability, billed $15 per successful client send (jobs/genreports_usage.py
        # pushes usage). Independent of the monitoring/offtaker lines (keyed on the
        # generation_reports marker), so it rides on top: added whenever
        # ao_genreports_metered_item resolved a line above. Fully inert until
        # STRIPE_AO_GENREPORTS_PRICE_ID is set (item is None).
        if genreports_item:
            items.append(genreports_item)
        if not items and monitoring_item:
            # No offtakers yet and no nameplate — still need a line; monitoring covers it.
            items.append(monitoring_item)
    else:
        array_price_id = array_price_id_for_product(product)
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

    if ao:
        _parts = []
        if monitoring_item and "quantity" in monitoring_item:
            _parts.append(f"per-kW nameplate qty {monitoring_item['quantity']}")
        if int(offtaker_count or 0) > 0:
            _parts.append(f"per-offtaker qty {int(offtaker_count)}")
        if genreports_item:
            _parts.append("per-send genreports (metered)")
        meter = " + ".join(_parts) or "per-kW nameplate"
    else:
        meter = f"per-array qty {quantity}"
    send_internal_alert(
        f"✅ Subscription resumed: {tenant_id}",
        f"Tenant {tenant_id} ({email}) added a card and resumed. "
        f"Arrays: {array_count}, offtakers: {offtaker_count}, "
        f"plan: regular, billed: {meter}. Subscription: {sub_id}"
    )
    return {"ok": True, "subscription_id": sub_id, "array_count": int(array_count),
            "offtaker_count": int(offtaker_count), "metered": ao,
            "has_invoicing": has_invoicing, "has_monitoring": has_monitoring,
            "billing_plan": "regular"}


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

        # Array Operator is never reconciled by ARRAY count: a per-kWh line is
        # metered, and a per-kW NAMEPLATE line is synced by the nameplate-sync job
        # (quantity = nameplate kW, not array count). Skip if either is present.
        _np_price = _ao_nameplate_price_id()
        for item in sub["items"]["data"]:
            recurring = item["price"].get("recurring") or {}
            if recurring.get("usage_type") == "metered" or item["price"]["id"] == _np_price:
                logger.info(
                    "reconcile: subscription %s for tenant %s is Array Operator "
                    "(metered or per-kW nameplate) — skipping array-quantity reconciliation",
                    subscription_id, tenant_id)
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


def reconcile_offtaker_quantity(tenant_id: str) -> None:
    """Sync the AO offtaker Stripe line to the current offtaker count.

    - offtakers > 0: create or update the licensed invoicing line quantity
    - offtakers == 0: remove the invoicing line (bill $0 for offtakers)

    Best-effort — never raises. No-op for non-AO or no live subscription.
    """
    from .db import SessionLocal
    from .models import Tenant

    inv_price = _ao_invoicing_price_id()
    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if t is None or not is_ao_invoicing(getattr(t, "product", None),
                                            getattr(t, "billing_plan", None)):
            return
        subscription_id = t.stripe_subscription_id
        email = t.contact_email
        offtaker_count = billable_offtaker_count(db, t.id)
    if not subscription_id or not inv_price:
        return
    if not os.getenv("STRIPE_SECRET_KEY"):
        return
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    n = int(offtaker_count or 0)

    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        line = None
        for item in sub["items"]["data"]:
            if item["price"]["id"] == inv_price:
                line = item
                break
        if n <= 0:
            if line is not None:
                stripe.SubscriptionItem.delete(
                    line["id"], proration_behavior="create_prorations")
                logger.info(
                    "Removed AO invoicing line on %s for tenant %s (0 offtakers)",
                    subscription_id, tenant_id)
            return
        if line is None:
            stripe.SubscriptionItem.create(
                subscription=subscription_id, price=inv_price,
                quantity=n, proration_behavior="create_prorations")
            logger.info(
                "Added AO invoicing line on %s for tenant %s: offtakers=%d",
                subscription_id, tenant_id, n)
            return
        if line.get("quantity") == n:
            return
        stripe.SubscriptionItem.modify(
            line["id"], quantity=n,
            proration_behavior="create_prorations",
        )
        logger.info(
            "Reconciled AO invoicing subscription %s for tenant %s: offtakers → %d",
            subscription_id, tenant_id, n)
    except Exception as e:  # noqa: BLE001 — must never block callers
        logger.exception(
            "AO invoicing reconciliation FAILED for tenant %s (sub %s, "
            "offtakers=%d): %s", tenant_id, subscription_id, offtaker_count, e)
        send_internal_alert(
            "⚠️ AO invoicing reconciliation failed",
            f"Tenant {tenant_id} ({email}) changed offtaker count to "
            f"{offtaker_count}, but updating subscription {subscription_id} failed: "
            f"{e}\n\nStripe is still billing the old quantity — fix it manually "
            f"(proration_behavior=create_prorations)."
        )


def migrate_ao_subscription_lines(tenant_id: str) -> None:
    """Bring a LIVE Array Operator subscription's LINES in line with the tenant's
    currently-chosen plan — ADD or REMOVE the per-kWh monitoring meter and the
    per-offtaker invoicing line so a plan change (monitoring↔invoicing↔both) actually
    changes what they're billed, with proration.

    Call after Tenant.billing_plan changes. reconcile_offtaker_quantity owns the
    invoicing line's QUANTITY; this owns which lines EXIST. Best-effort — never
    raises. No-op for a non-AO tenant or one with no live subscription (a trialing
    tenant gets the right lines when they first add a card). Idempotent: re-selecting
    the same plan finds the lines already correct and touches nothing in Stripe.
    """
    from .db import SessionLocal
    from .models import Tenant

    if not os.getenv("STRIPE_SECRET_KEY"):
        return
    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if t is None or not is_array_operator(getattr(t, "product", None)):
            return
        subscription_id = getattr(t, "stripe_subscription_id", None)
        product = t.product
        billing_plan = getattr(t, "billing_plan", None)
        email = t.contact_email
        offtaker_count = billable_offtaker_count(db, t.id)
    if not subscription_id:
        return   # trialing — no live sub; lines are set when they first add a card

    # Regular AO product: always monitoring (nameplate) + offtakers when count > 0.
    want_invoicing = is_ao_invoicing(product, billing_plan) and int(offtaker_count or 0) > 0
    want_monitoring = is_ao_monitoring(product, billing_plan)
    inv_price = _ao_invoicing_price_id()
    np_price = _ao_nameplate_price_id()
    kwh_price = _ao_kwh_price_id()  # legacy metered fallback
    mon_price = np_price or kwh_price
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        items = sub["items"]["data"]
        inv_line = next((i for i in items if i["price"]["id"] == inv_price), None) if inv_price else None
        mon_line = None
        if mon_price:
            mon_line = next((i for i in items if i["price"]["id"] == mon_price), None)
            if mon_line is None and np_price and kwh_price:
                mon_line = next((i for i in items if i["price"]["id"] == kwh_price), None)

        # ADD required-but-missing lines FIRST.
        if want_invoicing and inv_line is None:
            if not inv_price:
                send_internal_alert(
                    "⚠️ AO invoicing price id missing (line sync)",
                    f"Tenant {tenant_id} ({email}) needs offtaker line but "
                    "STRIPE_AO_INVOICING_PRICE_ID is unset.")
            else:
                stripe.SubscriptionItem.create(
                    subscription=subscription_id, price=inv_price,
                    quantity=int(offtaker_count),
                    proration_behavior="create_prorations")
        if want_monitoring and mon_line is None and mon_price:
            if np_price:
                with SessionLocal() as db:
                    qty = max(tenant_nameplate_kw(db, tenant_id), 1)
                stripe.SubscriptionItem.create(
                    subscription=subscription_id, price=np_price,
                    quantity=qty, proration_behavior="create_prorations")
            elif kwh_price:
                stripe.SubscriptionItem.create(
                    subscription=subscription_id, price=kwh_price,
                    proration_behavior="create_prorations")

        # REMOVE offtaker line when roster is empty (not "plan change" anymore).
        if not want_invoicing and inv_line is not None:
            stripe.SubscriptionItem.delete(
                inv_line["id"], proration_behavior="create_prorations")

        logger.info(
            "Synced AO subscription %s lines for tenant %s "
            "(invoicing=%s offtakers=%s, monitoring=%s)",
            subscription_id, tenant_id, want_invoicing, offtaker_count, want_monitoring)
    except Exception as e:  # noqa: BLE001 — must never block callers
        logger.exception("AO subscription line migration FAILED for tenant %s", tenant_id)
        send_internal_alert(
            "⚠️ AO subscription line sync failed",
            f"Tenant {tenant_id} ({email}) line sync failed for "
            f"subscription {subscription_id}: {e}\n\nFix manually in Stripe.")
