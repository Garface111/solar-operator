"""Restore ten_anna_800's delivery-mode mix after a QA 'Draft all' / 'Auto-send
all' bulk flip — reapplies the seeder's rule (approval on even creation index,
auto on odd) to the 800 core offtakers, leaving the guard-rail demos on
approval. Cheap + non-destructive: touches only delivery_mode, so the delivered
band ($ + counts from last_sent_*) and everything else stay intact.

Run: railway ssh "cd /app && python scripts/anna800_restore_modes.py"
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import BillingReportSubscription

TENANT_ID = "ten_anna_800"


def main() -> int:
    init_db()
    with SessionLocal() as db:
        subs = db.execute(
            select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == TENANT_ID,
                   BillingReportSubscription.deleted_at.is_(None))
            .order_by(BillingReportSubscription.id)
        ).scalars().all()
        auto = approval = guard = 0
        idx = 0
        for s in subs:
            if s.customer_name.startswith("DEMO-HOLD"):
                s.delivery_mode = "approval"
                guard += 1
                continue
            s.delivery_mode = "approval" if idx % 2 == 0 else "auto"
            if s.delivery_mode == "auto":
                auto += 1
            else:
                approval += 1
            idx += 1
        db.commit()
    print(f"restored delivery modes: {auto} auto, {approval} approval, "
          f"{guard} guard(approval)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
