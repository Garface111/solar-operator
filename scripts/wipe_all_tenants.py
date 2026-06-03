"""DESTRUCTIVE: wipe every tenant and EVERYTHING that references it.
Uses TRUNCATE ... CASCADE on `tenants` to follow all FKs in one shot, then
clears StripeEvent (which has no FK to tenants) separately.

Invoke: railway ssh "cd /app && python -m scripts.wipe_all_tenants --yes-i-mean-it"
"""
import sys
from sqlalchemy import text
from api.db import SessionLocal
from api.models import StripeEvent

if "--yes-i-mean-it" not in sys.argv:
    print("Refusing to wipe without --yes-i-mean-it flag.")
    sys.exit(1)

with SessionLocal() as db:
    bind = db.get_bind()
    # Count first so we can report
    rows = db.execute(text("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
    """)).all()
    print("Tables before:")
    for (t,) in rows:
        try:
            n = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            print(f"  {t:30} {n}")
        except Exception as e:
            print(f"  {t:30} ?  ({e.__class__.__name__})")

    stripe_n = db.query(StripeEvent).count()
    db.query(StripeEvent).delete(synchronize_session=False)
    db.commit()
    print(f"\nCleared StripeEvent: {stripe_n}")

    # TRUNCATE CASCADE follows every FK automatically — no ordering needed.
    print("\nTRUNCATE tenants CASCADE...")
    with bind.begin() as conn:
        conn.execute(text("TRUNCATE TABLE tenants CASCADE"))
    print("Done.")

    print("\nTables after:")
    for (t,) in rows:
        try:
            n = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            print(f"  {t:30} {n}")
        except Exception:
            pass
