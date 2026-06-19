"""READ-ONLY: reproduce onboarding-status for ford.genereaux@gmail.com's AO
tenant(s) and show WHY complete is / isn't true. NO WRITES."""
from __future__ import annotations
from sqlalchemy import select, func
from api.db import SessionLocal
from api.models import Tenant, UtilityAccount, Array, UtilitySession

TARGET = "ford.genereaux@gmail.com"


def main():
    with SessionLocal() as db:
        tenants = db.execute(select(Tenant)).scalars().all()
        hits = [t for t in tenants if TARGET in (t.contact_email or "").lower()]
        if not hits:
            print("NO tenant with contact_email", TARGET)
            # show near-matches
            for t in tenants:
                if "ford.genereaux@gmail.com" in (t.contact_email or "").lower().replace(" ", ""):
                    hits.append(t)
        for t in hits:
            product = getattr(t, "product", None)
            gmp_sessions = db.execute(select(func.count(UtilitySession.id)).where(
                UtilitySession.tenant_id == t.id,
                UtilitySession.provider == "gmp")).scalar() or 0
            gmp_accts = db.execute(select(UtilityAccount).where(
                UtilityAccount.tenant_id == t.id,
                UtilityAccount.provider == "gmp",
                UtilityAccount.deleted_at.is_(None))).scalars().all()
            linked = sum(1 for a in gmp_accts if a.array_id is not None)
            unlinked = sum(1 for a in gmp_accts if a.array_id is None)
            arrays_total = db.execute(select(func.count(Array.id)).where(
                Array.tenant_id == t.id, Array.deleted_at.is_(None))).scalar() or 0
            # all providers present
            allprov = db.execute(select(UtilityAccount.provider, func.count()).where(
                UtilityAccount.tenant_id == t.id,
                UtilityAccount.deleted_at.is_(None)).group_by(UtilityAccount.provider)).all()
            gmp_connected = gmp_sessions > 0 or len(gmp_accts) > 0
            complete = gmp_connected
            print(f"\n=== TENANT {t.id}  product={product!r}  active={t.active} "
                  f"status={t.subscription_status}")
            print(f"    contact={t.contact_email!r}")
            print(f"    gmp_sessions={gmp_sessions}  gmp_accounts={len(gmp_accts)} "
                  f"(linked={linked} unlinked={unlinked})  arrays_total={arrays_total}")
            print(f"    ALL providers: {dict(allprov)}")
            print(f"    => gmp_connected={gmp_connected}  complete={complete}")


if __name__ == "__main__":
    main()
