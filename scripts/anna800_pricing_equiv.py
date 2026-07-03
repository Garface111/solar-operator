"""Batched pricing ctx equivalence + timing: ctx path must be byte-identical to
the per-row path for every sub, and dramatically faster. (One-off check for the
list-endpoint N+1 fix; kept for re-runs after pricing changes.)"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import Tenant, BillingReportSubscription
from api.billing.delivery import resolve_discount_pricing, build_pricing_ctx

TENANT_ID = "ten_anna_800"

init_db()
with SessionLocal() as db:
    subs = list(db.execute(
        select(BillingReportSubscription)
        .where(BillingReportSubscription.tenant_id == TENANT_ID,
               BillingReportSubscription.deleted_at.is_(None))
    ).scalars())
    t0 = time.time()
    ctx = build_pricing_ctx(db, db.get(Tenant, TENANT_ID))
    with_ctx = [resolve_discount_pricing(s, ctx=ctx) for s in subs]
    t_ctx = time.time() - t0

t0 = time.time()
without = [resolve_discount_pricing(s) for s in subs]
t_solo = time.time() - t0

diff = [(s.id, a, b) for s, a, b in zip(subs, with_ctx, without) if a != b]
print(f"subs={len(subs)}  ctx={t_ctx:.2f}s  per-row={t_solo:.2f}s  "
      f"speedup={t_solo / max(t_ctx, 1e-9):.0f}x  mismatches={len(diff)}")
for d in diff[:5]:
    print("MISMATCH", d)
sys.exit(0 if not diff else 1)
