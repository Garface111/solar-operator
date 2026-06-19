"""READ-ONLY: dump VEC (smarthub) Bill rows for West Glover clients (267/270),
plus the latest CaptureEvent payload_excerpts for those tenants, to see exactly
what landed for April/May/June 2026 and whether kWh is present.
NO WRITES.
"""
from __future__ import annotations
from sqlalchemy import select, desc
from api.db import SessionLocal
from api.models import (Tenant, Client, Array, UtilityAccount, Bill,
                        CaptureEvent)


def main():
    with SessionLocal() as db:
        for cid in (267, 270):
            c = db.get(Client, cid)
            if not c:
                print(f"client {cid}: NOT FOUND"); continue
            arrs = db.execute(select(Array).where(Array.client_id == cid)).scalars().all()
            arr_ids = [a.id for a in arrs]
            accts = db.execute(select(UtilityAccount).where(
                UtilityAccount.array_id.in_(arr_ids))).scalars().all() if arr_ids else []
            print(f"\n===== CLIENT {cid} {c.name!r}  tenant={c.tenant_id} =====")
            for a in accts:
                print(f"  ACCT id={a.id} number={a.account_number!r} provider={a.provider!r} "
                      f"nickname={a.nickname!r}")
            acct_ids = [a.id for a in accts]
            if acct_ids:
                bills = db.execute(select(Bill).where(Bill.account_id.in_(acct_ids))
                                   .order_by(desc(Bill.bill_date)).limit(12)).scalars().all()
                print(f"  --- latest {len(bills)} bills ---")
                for b in bills:
                    print(f"    bill_date={b.bill_date.date() if b.bill_date else None} "
                          f"period={b.period_start.date() if b.period_start else None}.."
                          f"{b.period_end.date() if b.period_end else None} "
                          f"kwh_gen={b.kwh_generated} status={b.parse_status} "
                          f"doc={b.document_number!r}")

        # Latest capture events for the West Glover tenants
        tids = set()
        for cid in (267, 270):
            c = db.get(Client, cid)
            if c:
                tids.add(c.tenant_id)
        for tid in tids:
            print(f"\n===== latest CaptureEvents tenant={tid} =====")
            evs = db.execute(select(CaptureEvent).where(CaptureEvent.tenant_id == tid)
                             .order_by(desc(CaptureEvent.id)).limit(25)).scalars().all()
            for e in evs:
                excerpt = e.payload_excerpt
                exc_str = str(excerpt)[:300] if excerpt else ""
                print(f"  [{getattr(e,'created_at',None)}] stage={getattr(e,'stage',None)!r} "
                      f"decision={getattr(e,'decision',None)!r} {exc_str}")


if __name__ == "__main__":
    main()
