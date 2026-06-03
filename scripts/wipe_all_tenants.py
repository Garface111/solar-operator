"""DESTRUCTIVE: wipe every tenant and all dependent rows. Run only when you really mean it.
Invoke: railway ssh "cd /app && python scripts/wipe_all_tenants.py --yes-i-mean-it"
"""
import sys
from api.db import SessionLocal
from api.models import (
    Tenant, Client, Array, UtilityAccount, UtilitySession,
    Bill, StripeEvent,
)

if "--yes-i-mean-it" not in sys.argv:
    print("Refusing to wipe without --yes-i-mean-it flag.")
    sys.exit(1)

with SessionLocal() as db:
    # Order matters: leaves first to satisfy FKs.
    counts = {}
    for model in (Bill, UtilityAccount, UtilitySession, Array, Client, Tenant, StripeEvent):
        n = db.query(model).count()
        counts[model.__tablename__] = n
        db.query(model).delete(synchronize_session=False)
    db.commit()

print("Wiped (rows deleted):")
for name, n in counts.items():
    print(f"  {name:24} {n}")
print("Done. DB is empty.")
