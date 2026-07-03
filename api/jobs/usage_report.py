"""Report Array Operator per-kWh usage to Stripe (metered billing).

Array Operator owners are billed by kWh GENERATED, not by array count. Their
Stripe subscription carries a single METERED line (usage_type="metered",
aggregate_usage="last_during_period"). This job sums each owner-tenant's kWh
from DailyGeneration over the subscription's CURRENT billing period and reports
the running cumulative total to Stripe with action="set". Because the price uses
"last_during_period", the LAST value reported before the period closes is what
Stripe bills — so reporting month-to-date cumulative each day is correct and
idempotent (a re-run the same day just overwrites with the same number).

Why sum over the SUBSCRIPTION period, not the calendar month: Stripe bills on
the subscription's own monthly cycle (anchored to its creation date), which
rarely lines up with the 1st. Summing DailyGeneration from
current_period_start → today keeps the metered quantity aligned with the window
Stripe is actually invoicing.

Scheduled daily (api/scheduler.py). Per-tenant errors are logged + alerted but
never crash the run. Safe to run more often than daily — it's idempotent.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import stripe
from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Array, DailyGeneration, Tenant
from ..notify import send_internal_alert

log = logging.getLogger(__name__)


def _metered_item_id(subscription_id: str) -> str | None:
    """Return the subscription item id of the metered (per-kWh) line, or None."""
    sub = stripe.Subscription.retrieve(subscription_id)
    for item in sub["items"]["data"]:
        recurring = item["price"].get("recurring") or {}
        if recurring.get("usage_type") == "metered":
            return item["id"]
    return None


def _period_start_date(subscription_id: str):
    """The subscription's current_period_start as a date (UTC)."""
    sub = stripe.Subscription.retrieve(subscription_id)
    ts = sub.get("current_period_start")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()


def tenant_period_kwh(db, tenant_id: str, since_date) -> float:
    """Sum REAL metered DailyGeneration.kwh for all of a tenant's billable arrays
    since `since_date` (inclusive). Excludes soft-deleted/excluded arrays so the
    meter matches the dashboard.

    HONESTY (we bill what we actually metered, never an estimate): excludes
    source='bill_prorate' rows — a monthly utility bill smeared flat across its
    days (jobs/bill_to_daily.py). Those are an estimate of daily generation, so
    they must not raise the kWh quantity we report to Stripe. Every other source
    (extension_pull / utility_meter / gmp_api / smarthub / solaredge / csv) is a
    real reading and stays. NULL source (legacy rows) is kept via coalesce."""
    total = db.execute(
        select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
        .select_from(DailyGeneration)
        .join(Array, Array.id == DailyGeneration.array_id)
        .where(
            DailyGeneration.tenant_id == tenant_id,
            DailyGeneration.day >= since_date,
            Array.deleted_at.is_(None),
            Array.excluded.is_(False),
            func.coalesce(DailyGeneration.source, "") != "bill_prorate",
        )
    ).scalar() or 0.0
    return float(total)


def report_usage_for_all_owners() -> dict:
    """Report month-to-date kWh usage to Stripe for every active Array Operator
    tenant that has a live metered subscription. Returns a summary dict."""
    if not os.getenv("STRIPE_SECRET_KEY"):
        return {"reported": [], "skipped": 0, "errors": []}
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    reported: list[dict] = []
    errors: list[str] = []
    skipped = 0

    with SessionLocal() as db:
        owners = db.execute(
            select(Tenant).where(
                Tenant.product == "array_operator",
                Tenant.subscription_status == "active",
                Tenant.stripe_subscription_id.isnot(None),
                # Belt-and-suspenders: a demo tenant must NEVER reach a live
                # meter, even if someone hand-attaches a Stripe sub to one.
                Tenant.is_demo.is_(False),
            )
        ).scalars().all()
        targets = [(t.id, t.contact_email, t.stripe_subscription_id) for t in owners]

    for tenant_id, email, sub_id in targets:
        if not sub_id:
            continue
        try:
            item_id = _metered_item_id(sub_id)
            if not item_id:
                # Subscription has no metered line — likely a legacy per-array AO
                # tenant created before the kWh switch. Skip (don't guess).
                skipped += 1
                log.info("usage_report: tenant %s sub %s has no metered item — skipped",
                         tenant_id, sub_id)
                continue
            since = _period_start_date(sub_id)
            if since is None:
                skipped += 1
                continue
            with SessionLocal() as db:
                kwh = tenant_period_kwh(db, tenant_id, since)
            quantity = int(round(kwh))  # Stripe usage records require integer qty
            stripe.SubscriptionItem.create_usage_record(
                item_id,
                quantity=quantity,
                timestamp=int(datetime.now(tz=timezone.utc).timestamp()),
                action="set",
            )
            reported.append({"tenant_id": tenant_id, "kwh": quantity,
                             "since": since.isoformat()})
            log.info("usage_report: tenant %s reported %d kWh (since %s)",
                     tenant_id, quantity, since)
        except Exception as e:  # noqa: BLE001 — never crash the scheduler
            errors.append(tenant_id)
            log.exception("usage_report: failed for tenant %s", tenant_id)
            send_internal_alert(
                f"⚠️ Usage report failed: {tenant_id}",
                f"Tenant {tenant_id} ({email}) per-kWh usage could not be "
                f"reported to Stripe (sub {sub_id}): {e}\n"
                f"Stripe may bill stale usage this period. Investigate."
            )

    log.info("report_usage_for_all_owners: reported=%d skipped=%d errors=%d",
             len(reported), skipped, len(errors))
    return {"reported": reported, "skipped": skipped, "errors": errors}
