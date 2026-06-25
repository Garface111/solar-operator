"""Daily AO nameplate-quantity sync (Array Operator monitoring billing).

AO monitoring is billed on REGISTERED INVERTER NAMEPLATE (kW) via a licensed
per-kW Stripe price (stripe_helpers._ao_nameplate_price_id). The subscription
item's quantity must track the tenant's current nameplate as inverters are
added/removed, so this job sets each active AO monitoring sub's nameplate-line
quantity = tenant_nameplate_kw, idempotently (only modifies on change).

It is also the SAFETY NET: if any subscription-create path ever set the wrong
quantity (or none), this corrects it on the next run — so a missed quantity bills
at most one cycle wrong, then self-heals.

proration_behavior="none": a capacity change applies at the next invoice (no
mid-cycle proration churn from a daily job). Read-only on our own data.
"""
from __future__ import annotations

import logging
import os

import stripe
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Tenant
from ..stripe_helpers import _ao_nameplate_price_id, tenant_nameplate_kw

log = logging.getLogger(__name__)


def sync_ao_nameplate_for_all_owners() -> dict:
    """Set every active AO monitoring sub's nameplate-line quantity = nameplate kW.
    Returns {'synced':[...], 'skipped':int, 'errors':[...]}."""
    np_price = _ao_nameplate_price_id()
    if not np_price or not os.getenv("STRIPE_SECRET_KEY"):
        return {"synced": [], "skipped": 0, "errors": []}
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    synced: list[dict] = []
    errors: list[str] = []
    skipped = 0

    with SessionLocal() as db:
        owners = db.execute(
            select(Tenant).where(
                Tenant.product == "array_operator",
                Tenant.subscription_status == "active",
                Tenant.stripe_subscription_id.isnot(None),
            )
        ).scalars().all()
        targets = [(t.id, t.stripe_subscription_id) for t in owners]

    for tenant_id, sub_id in targets:
        try:
            with SessionLocal() as db:
                want = max(tenant_nameplate_kw(db, tenant_id), 1)
            sub = stripe.Subscription.retrieve(sub_id)
            item = None
            for it in sub["items"]["data"]:
                if it["price"]["id"] == np_price:
                    item = it
                    break
            if item is None:
                # No nameplate line (legacy metered sub, or invoicing-only) — not ours.
                skipped += 1
                continue
            if int(item.get("quantity") or 0) == want:
                skipped += 1
                continue
            stripe.SubscriptionItem.modify(item["id"], quantity=want, proration_behavior="none")
            synced.append({"tenant_id": tenant_id, "kw": want})
            log.info("nameplate_sync: tenant %s set nameplate qty %d kW", tenant_id, want)
        except Exception as exc:  # one bad sub must not stall the rest
            errors.append(f"{tenant_id}: {exc}")
            log.warning("nameplate_sync: tenant %s failed: %s", tenant_id, exc)

    if errors:
        from ..notify import send_internal_alert
        send_internal_alert(
            f"AO nameplate sync: {len(errors)} tenant(s) failed",
            "Some AO nameplate quantities could not be synced:\n" + "\n".join(errors),
        )
    log.info("nameplate_sync: synced=%d skipped=%d errors=%d",
             len(synced), skipped, len(errors))
    return {"synced": synced, "skipped": skipped, "errors": errors}
