"""READ-ONLY: deep-dive Bruce Genereaux's accounts, focused on VEC/SmartHub and
recent-month data coverage (April/May/June 2026).

For every tenant whose contact_email or any client contact is a bruce.genereaux
address, list clients -> arrays -> utility accounts (provider) -> latest Bill
rows and DailyGeneration date ranges. Highlights VEC/smarthub providers and
whether 2026-04/05/06 data exists.

NO WRITES.
"""
from __future__ import annotations
from datetime import date
from collections import defaultdict
from sqlalchemy import select, func, or_
from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, Bill, DailyGeneration

TARGET = "bruce.genereaux"
RECENT = [(2026, 4), (2026, 5), (2026, 6)]


def main():
    with SessionLocal() as db:
        # Tenants whose contact OR any client contact matches Bruce
        tenants = db.execute(select(Tenant)).scalars().all()
        hit_tenants = []
        for t in tenants:
            if TARGET in (t.contact_email or "").lower():
                hit_tenants.append(t); continue
            cs = db.execute(select(Client).where(Client.tenant_id == t.id)).scalars().all()
            if any(TARGET in (c.contact_email or "").lower() for c in cs):
                hit_tenants.append(t)

        for t in hit_tenants:
            clients = db.execute(select(Client).where(
                Client.tenant_id == t.id, Client.deleted_at.is_(None))).scalars().all()
            print(f"\n=== TENANT {t.id}  {t.company_name or t.name!r}  "
                  f"contact={t.contact_email!r}  active={t.active} status={t.subscription_status}")
            for c in clients:
                arrs = db.execute(select(Array).where(
                    Array.client_id == c.id, Array.deleted_at.is_(None))).scalars().all()
                arr_ids = [a.id for a in arrs]
                # VEC autopop fields
                vec_fields = (f"vec_email={getattr(c,'vec_email',None)!r} "
                              f"vec_autopop={getattr(c,'vec_autopopulate',None)} "
                              f"vec_last_sync={getattr(c,'vec_last_sync_at',None)}")
                print(f"  CLIENT {c.id}  {c.name[:34]!r}  active={c.active}  arrays={len(arr_ids)}  "
                      f"contact={c.contact_email!r}")
                print(f"        {vec_fields}")
                if not arr_ids:
                    continue
                accts = db.execute(select(UtilityAccount).where(
                    UtilityAccount.array_id.in_(arr_ids))).scalars().all()
                prov_counts = defaultdict(int)
                for a in accts:
                    prov_counts[a.provider] += 1
                print(f"        accounts={len(accts)}  providers={dict(prov_counts)}")
                acct_ids = [a.id for a in accts]
                # Bill coverage
                if acct_ids:
                    bills = db.execute(select(Bill).where(Bill.account_id.in_(acct_ids))).scalars().all()
                    bmonths = defaultdict(float)
                    bdates = []
                    for b in bills:
                        src = b.period_start or b.bill_date
                        if src:
                            bdates.append(src.date() if hasattr(src,'date') else src)
                            if b.kwh_generated:
                                bmonths[(src.year, src.month)] += b.kwh_generated
                    if bdates:
                        print(f"        BILLS: {len(bills)} rows, dates {min(bdates)}..{max(bdates)}")
                    else:
                        print(f"        BILLS: {len(bills)} rows, NO usable dates")
                    for ym in RECENT:
                        print(f"          {ym[0]}-{ym[1]:02d} bill kWh: {bmonths.get(ym,0.0):.1f}")
                # DailyGeneration coverage
                dg = db.execute(select(
                    func.min(DailyGeneration.day), func.max(DailyGeneration.day),
                    func.count()).where(DailyGeneration.array_id.in_(arr_ids))).first()
                print(f"        DAILY_GEN: count={dg[2]} range={dg[0]}..{dg[1]}")
                for ym in RECENT:
                    cnt = db.execute(select(func.count(), func.coalesce(func.sum(DailyGeneration.kwh),0)).where(
                        DailyGeneration.array_id.in_(arr_ids),
                        func.extract('year', DailyGeneration.day) == ym[0],
                        func.extract('month', DailyGeneration.day) == ym[1])).first()
                    print(f"          {ym[0]}-{ym[1]:02d} daily rows={cnt[0]} sum_kWh={cnt[1]:.1f}")


if __name__ == "__main__":
    main()
