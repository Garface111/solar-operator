"""One-off: backfill last_sent_amount_usd for ten_anna_800's June sends.

The column is stamped on every send going forward; June's 800 sends predate
it. Sources, in order of fidelity: the sub's SENT draft amount (the exact
figure approved), else the independent recompute from bills (the same rules
the accuracy harness proved 800/800 against build_match).

Run: railway ssh "cd /app && python scripts/anna800_backfill_amounts.py"
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import BillingReportSubscription, ReportDraft
from anna800_verify import load_ground_truth, expected_invoice  # noqa: E402

TENANT_ID = "ten_anna_800"


def main() -> int:
    init_db()
    with SessionLocal() as db:
        gt = load_ground_truth(db)
        draft_amt = {}
        for sid, amt in db.execute(
                select(ReportDraft.subscription_id, ReportDraft.amount_usd)
                .where(ReportDraft.tenant_id == TENANT_ID,
                       ReportDraft.status == "sent",
                       ReportDraft.amount_usd.isnot(None))):
            draft_amt[sid] = float(amt)
        subs = db.execute(
            select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == TENANT_ID,
                   BillingReportSubscription.deleted_at.is_(None),
                   BillingReportSubscription.last_sent_at.isnot(None))
        ).scalars().all()
        from_draft = from_calc = skipped = 0
        for s in subs:
            if s.last_sent_amount_usd is not None:
                skipped += 1
                continue
            if s.id in draft_amt:
                s.last_sent_amount_usd = draft_amt[s.id]
                from_draft += 1
                continue
            exp = expected_invoice(s, gt)
            if exp is not None:
                s.last_sent_amount_usd = exp["amount"]
                from_calc += 1
        db.commit()
        total = sum(s.last_sent_amount_usd or 0 for s in subs)
    print(f"backfilled: {from_draft} from sent drafts, {from_calc} recomputed, "
          f"{skipped} already set; June total = ${total:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
