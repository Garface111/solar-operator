"""Report generation-report OUTPUT charges to Stripe (metered billing — THE FOLD).

Generation reports bill $15 per client per calendar quarter, charged on the FIRST
real output (send or download) for that (client, quarter) — recorded as a
GenReportCharge ledger row in api/delivery.py (idempotent per client+quarter). This
job sums each tenant's UN-PUSHED ledger rows and reports them to Stripe as metered
usage against the tenant's METERED generation-reports subscription item (unit_amount
= $15, aggregate_usage='sum'), then stamps pushed_at so each $15 is billed exactly
once.

Mirrors api/jobs/usage_report.py (the per-kWh monitoring meter). Difference: the kWh
meter reports a cumulative "set" each day; this reports per-batch increments (one
unit per un-pushed charge) because each row is a discrete $15 output event.

INERT until activated: does NOTHING unless STRIPE_SECRET_KEY is set AND
STRIPE_AO_GENREPORTS_PRICE_ID (the metered price) is configured. Until Ford mints the
price + sets the env var, ledger rows just accumulate un-pushed and no one is billed.
Idempotent: only pushed_at IS NULL rows are summed, and they are stamped only after
the Stripe call succeeds — a re-run (or a crash before the stamp) re-pushes the same
rows, and the metered "sum" over the period stays correct because unstamped rows were
never counted as billed.

Per-tenant errors are logged + alerted but never crash the run.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import stripe
from sqlalchemy import select

from ..db import SessionLocal
from ..models import GenReportCharge, Tenant
from ..notify import send_internal_alert
from ..stripe_helpers import ao_genreports_price_id

log = logging.getLogger(__name__)


def _genreports_item_id(subscription_id: str, price_id: str) -> str | None:
    """Return the subscription item id of the metered generation-reports line
    (matched by price id, so it is never confused with the monitoring meter), or
    None if the subscription carries no such line."""
    sub = stripe.Subscription.retrieve(subscription_id)
    for item in sub["items"]["data"]:
        if item["price"]["id"] == price_id:
            return item["id"]
    return None


def report_genreport_charges_to_stripe() -> dict:
    """Push every un-pushed GenReportCharge to Stripe as metered usage, summed per
    tenant. Returns a summary dict. INERT (no Stripe calls, returns inert=True) until
    both STRIPE_SECRET_KEY and STRIPE_AO_GENREPORTS_PRICE_ID are configured."""
    price_id = ao_genreports_price_id()
    if not os.getenv("STRIPE_SECRET_KEY") or not price_id:
        # Not activated yet — ledger rows just wait. This is the guardrail that keeps
        # the whole feature from billing anyone until Ford mints + sets the price.
        return {"reported": 0, "tenants": 0, "skipped": 0, "errors": [], "inert": True}
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    # Snapshot the un-pushed ledger grouped by tenant (ids only; the session closes
    # before any Stripe I/O — never hold a DB session across an external HTTP call).
    tenants: dict[str, list[int]] = {}
    with SessionLocal() as db:
        rows = db.execute(
            select(GenReportCharge.id, GenReportCharge.tenant_id)
            .where(GenReportCharge.pushed_at.is_(None))
            .order_by(GenReportCharge.tenant_id, GenReportCharge.id)
        ).all()
    for charge_id, tenant_id in rows:
        tenants.setdefault(tenant_id, []).append(charge_id)

    if not tenants:
        return {"reported": 0, "tenants": 0, "skipped": 0, "errors": [], "inert": False}

    reported = 0
    pushed_tenants = 0
    skipped = 0
    errors: list[str] = []

    for tenant_id, charge_ids in tenants.items():
        email = None
        try:
            with SessionLocal() as db:
                t = db.get(Tenant, tenant_id)
                sub_id = getattr(t, "stripe_subscription_id", None) if t else None
                email = getattr(t, "contact_email", None) if t else None
                is_demo = getattr(t, "is_demo", False) if t else True
            # A demo tenant (belt-and-suspenders) or one with no live subscription
            # (comped/trialing) has nothing to meter against — leave the rows un-pushed
            # so they are picked up if the tenant later gets a paid metered sub.
            if is_demo or not sub_id:
                skipped += 1
                continue
            item_id = _genreports_item_id(sub_id, price_id)
            if not item_id:
                skipped += 1
                continue

            quantity = len(charge_ids)   # one $15 metered unit per un-pushed output
            stripe.SubscriptionItem.create_usage_record(
                item_id,
                quantity=quantity,
                timestamp=int(datetime.now(tz=timezone.utc).timestamp()),
                action="increment",
            )
            # Stamp only AFTER Stripe accepts the usage, so a failure leaves the rows
            # un-pushed for a safe retry (they were never counted as billed).
            with SessionLocal() as db:
                now = datetime.now(tz=timezone.utc)
                for cid in charge_ids:
                    row = db.get(GenReportCharge, cid)
                    if row is not None and row.pushed_at is None:
                        row.pushed_at = now
                db.commit()
            reported += quantity
            pushed_tenants += 1
            log.info("genreports_usage: tenant %s reported %d output(s) (=$%.2f)",
                     tenant_id, quantity, quantity * 15.0)
        except Exception as e:  # noqa: BLE001 — never crash the scheduler
            errors.append(tenant_id)
            log.exception("genreports_usage: failed for tenant %s", tenant_id)
            send_internal_alert(
                f"⚠️ Generation-reports usage report failed: {tenant_id}",
                f"Tenant {tenant_id} ({email}) had {len(charge_ids)} un-pushed $15 "
                f"generation-report charge(s) that could not be reported to Stripe: "
                f"{e}\nThey stay un-pushed and will retry. Investigate if it persists."
            )

    log.info("report_genreport_charges_to_stripe: reported=%d tenants=%d skipped=%d errors=%d",
             reported, pushed_tenants, skipped, len(errors))
    return {"reported": reported, "tenants": pushed_tenants, "skipped": skipped,
            "errors": errors, "inert": False}
