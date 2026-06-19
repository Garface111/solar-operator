"""READ-ONLY: what is ten_a554c8e7a08f8cfa (Ford's AO tenant) actually connected
via? Inverter connections, utility accounts, daily-gen sources. NO WRITES."""
from __future__ import annotations
from collections import Counter
from sqlalchemy import select, func
from api.db import SessionLocal
from api.models import Array, UtilityAccount, InverterConnection, DailyGeneration

TID = "ten_a554c8e7a08f8cfa"


def main():
    with SessionLocal() as db:
        arrs = db.execute(select(Array).where(
            Array.tenant_id == TID, Array.deleted_at.is_(None))).scalars().all()
        print(f"arrays_total={len(arrs)}")
        # Inverter connections
        try:
            invs = db.execute(select(InverterConnection).where(
                InverterConnection.tenant_id == TID)).scalars().all()
            vendors = Counter(getattr(i, "vendor", None) or getattr(i, "provider", None) for i in invs)
            print(f"InverterConnections={len(invs)} vendors={dict(vendors)}")
        except Exception as e:
            print("inverter query err", e)
        # Utility accounts (any provider)
        uas = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == TID,
            UtilityAccount.deleted_at.is_(None))).scalars().all()
        print(f"UtilityAccounts={len(uas)} providers={dict(Counter(u.provider for u in uas))}")
        # Per-array: how is each connected?
        inv_provider_field = None
        src = db.execute(select(DailyGeneration.source, func.count()).where(
            DailyGeneration.tenant_id == TID).group_by(DailyGeneration.source)).all()
        print(f"DailyGeneration sources: {dict(src)}")
        # sample a few arrays
        for a in arrs[:8]:
            print(f"  array {a.id} {a.name[:30]!r} solaredge_site={getattr(a,'solaredge_site_id',None)} "
                  f"fuel={getattr(a,'fuel_type',None)}")


if __name__ == "__main__":
    main()
