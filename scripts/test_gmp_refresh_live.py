"""Stress-test the GMP refresh worker against ALL live sessions."""
from api.db import SessionLocal
from api.scheduler import refresh_expiring_gmp_tokens
from api.models import UtilitySession, Tenant
from sqlalchemy import select
from datetime import datetime, timedelta


def show(label):
    print(f"\n=== {label} ===")
    with SessionLocal() as db:
        rows = db.execute(
            select(UtilitySession, Tenant)
            .join(Tenant, UtilitySession.tenant_id == Tenant.id)
            .where(UtilitySession.provider == "gmp")
            .order_by(UtilitySession.captured_at.desc())
        ).all()
        for sess, t in rows:
            days = (sess.expires_at - datetime.utcnow()).days if sess.expires_at else None
            has_refresh = sess.refresh_token is not None
            lr = sess.last_refresh_at.isoformat() if sess.last_refresh_at else "—"
            print(
                f"  id={sess.id:<4} tenant={(t.contact_email or '?')[:28]:28} "
                f"expires_in={days}d  has_refresh={has_refresh}  "
                f"last_refresh={lr}  failures={sess.refresh_failures}"
            )


show("BEFORE: all GMP sessions on prod")

print("\n=== Forcing ALL refreshable sessions into the 7-day window ===")
with SessionLocal() as db:
    rows = (
        db.execute(
            select(UtilitySession)
            .where(UtilitySession.provider == "gmp")
            .where(UtilitySession.refresh_token.is_not(None))
        )
        .scalars()
        .all()
    )
    for sess in rows:
        sess.expires_at = datetime.utcnow() + timedelta(days=2)
    db.commit()
    print(f"  reset {len(rows)} session(s) to expire in 2 days")

print("\n=== Running refresh_expiring_gmp_tokens() ===")
result = refresh_expiring_gmp_tokens()
print(f"  REFRESHED: {result.get('refreshed')}")
print(f"  FAILED:    {result.get('failed')}")
print(f"  SKIPPED:   {result.get('skipped')}")

show("AFTER: all GMP sessions on prod")
