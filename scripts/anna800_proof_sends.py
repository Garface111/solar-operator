"""Send Ford 3 visible proof emails from the Anna-800 demo through the REAL
pipeline: one monthly, one quarterly, one budget-billing offtaker, delivered as
test sends (operator-addressed, 'Test send' banner, real invoice PDF attached)
to ford.genereaux+anna800@gmail.com. Restores each sub's sink operator_email
afterwards — the demo stays inbox-safe.

Run: railway ssh "cd /app && python scripts/anna800_proof_sends.py"
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import Tenant, BillingReportSubscription

TENANT_ID = "ten_anna_800"
FORD = "ford.genereaux+anna800@gmail.com"


def pick(db):
    subs = list(db.execute(
        select(BillingReportSubscription)
        .where(BillingReportSubscription.tenant_id == TENANT_ID,
               BillingReportSubscription.enabled == True,  # noqa: E712
               ~BillingReportSubscription.customer_name.like("DEMO-HOLD%"))
        .order_by(BillingReportSubscription.id)).scalars())
    monthly = next(s for s in subs if s.cadence == "monthly"
                   and s.budget_amount_usd is None)
    quarterly = next(s for s in subs if s.cadence == "quarterly")
    budget = next(s for s in subs if s.budget_amount_usd is not None)
    return [("monthly", monthly), ("quarterly", quarterly), ("budget", budget)]


def main() -> int:
    init_db()
    from api.billing.delivery import deliver_subscription
    with SessionLocal() as db:
        tenant = db.get(Tenant, TENANT_ID)
        for label, sub in pick(db):
            keep = sub.operator_email
            try:
                sub.operator_email = FORD
                r = deliver_subscription(db, sub, tenant, is_test=True,
                                         triggered_by="anna800-ford-proof")
                print(f"{label:<9} {sub.customer_name!r} → ok={r.get('ok')} "
                      f"to={r.get('to')} err={r.get('error')}")
            finally:
                sub.operator_email = keep
                db.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
