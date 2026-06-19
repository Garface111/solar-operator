"""READ-ONLY diagnostic: per-active-NEPOOL-client report data coverage.

Reproduces build_workbook's reporting window (6 rolling complete quarters) and
reports, for each active client of each active NEPOOL tenant:
  - array count (non-excluded)
  - total Bill kWh in window
  - total DailyGeneration kWh in window
  - whether the rendered workbook would have ANY non-zero month

NO WRITES. Pure SELECTs. Safe to run against prod.
"""
from __future__ import annotations
from datetime import date, timedelta
from collections import defaultdict

from sqlalchemy import select
from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, Bill, DailyGeneration
from api.writers.gmcs_writer import _rolling_quarters
from api.bill_attribution import distribute_kwh_by_calendar_day


def window(ref: date, quarters: int = 6):
    qlist = _rolling_quarters(ref, count=quarters)
    sy, sq = qlist[0]
    start = date(sy, (sq - 1) * 3 + 1, 1)
    ey, eq = qlist[-1]
    em = eq * 3
    end = date(ey, 12, 31) if em == 12 else date(ey, em + 1, 1) - timedelta(days=1)
    return qlist, start, end


def main():
    ref = date.today()
    qlist, wstart, wend = window(ref)
    qmonths = set()
    for (qy, qq) in qlist:
        for m in range((qq - 1) * 3 + 1, (qq - 1) * 3 + 4):
            qmonths.add((qy, m))

    print(f"reporting window: {wstart} .. {wend}  (quarters={qlist})")
    print("=" * 100)

    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(
                Tenant.product == "nepool",
            )
        ).scalars().all()
        tenants = [t for t in tenants
                   if (t.active or t.subscription_status in ("comped", "trialing"))]

        grand = {"clients": 0, "empty": 0, "nonempty": 0, "noarrays": 0}
        for t in tenants:
            clients = db.execute(
                select(Client).where(
                    Client.tenant_id == t.id,
                    Client.deleted_at.is_(None),
                    Client.active.is_(True),
                )
            ).scalars().all()
            if not clients:
                continue
            print(f"\nTENANT {t.id}  {t.company_name or t.name!r}  "
                  f"email={t.contact_email!r}  freq={t.report_frequency!r}  "
                  f"active={t.active} status={t.subscription_status}")
            for c in clients:
                grand["clients"] += 1
                arrays = db.execute(
                    select(Array).where(
                        Array.client_id == c.id, Array.excluded.is_(False),
                        Array.deleted_at.is_(None),
                    )
                ).scalars().all()
                arr_ids = [a.id for a in arrays]
                if not arr_ids:
                    grand["noarrays"] += 1
                    print(f"   - client {c.id:<5} {c.name[:32]:<32} ARRAYS=0  "
                          f"contact={c.contact_email!r}  -> EMPTY (no arrays)")
                    grand["empty"] += 1
                    continue
                accts = db.execute(
                    select(UtilityAccount).where(UtilityAccount.array_id.in_(arr_ids))
                ).scalars().all()
                acct_ids = [a.id for a in accts]
                # Bill kWh in window
                bill_kwh = 0.0
                if acct_ids:
                    bills = db.execute(
                        select(Bill).where(Bill.account_id.in_(acct_ids))
                    ).scalars().all()
                    for b in bills:
                        for (yy, mm), kwh in distribute_kwh_by_calendar_day(b).items():
                            if (yy, mm) in qmonths:
                                bill_kwh += kwh
                # DailyGeneration kWh in window
                dg_kwh = 0.0
                dg_rows = db.execute(
                    select(DailyGeneration).where(
                        DailyGeneration.array_id.in_(arr_ids),
                        DailyGeneration.day >= wstart,
                        DailyGeneration.day <= wend,
                    )
                ).scalars().all()
                for r in dg_rows:
                    dg_kwh += (r.kwh or 0.0)
                rendered_kwh = max(bill_kwh, dg_kwh)  # daily takes precedence per-month, but for "is it empty" max is fine
                empty = rendered_kwh <= 0
                grand["empty" if empty else "nonempty"] += 1
                flag = "EMPTY" if empty else "ok"
                print(f"   - client {c.id:<5} {c.name[:32]:<32} arrays={len(arr_ids):<3} "
                      f"accts={len(acct_ids):<3} bill_kWh={bill_kwh:>12.1f} "
                      f"daily_kWh={dg_kwh:>12.1f}  -> {flag}  contact={c.contact_email!r}")

        print("\n" + "=" * 100)
        print(f"TOTAL active NEPOOL clients: {grand['clients']}")
        print(f"  would render NON-EMPTY: {grand['nonempty']}")
        print(f"  would render EMPTY/zero: {grand['empty']}  (of which no-arrays: {grand['noarrays']})")


if __name__ == "__main__":
    main()
