"""Ops tool: seed a Cloud Capture credential into the DB, or resolve a tenant.

Run IN the prod container (railway ssh --service web) — it needs DB + SO_CONFIG_KEY.
The password is read from env CC_SEED_PASS so it is never a CLI argument.

  Resolve which tenant owns an array (read-only):
    python scripts/seed_cloud_capture_cred.py --find-array "Londonderry"

  Seed + enable a login for a tenant:
    CC_SEED_PASS=... python scripts/seed_cloud_capture_cred.py \
      --tenant ten_xxx --provider chint --username user@example.com [--host <coop-host>]
"""
import argparse
import os
import sys

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, Tenant
from api.harvester import credentials as cc


def find_array(term: str):
    with SessionLocal() as db:
        rows = db.execute(
            select(Array.tenant_id, Array.name).where(Array.name.ilike(f"%{term}%"))
        ).all()
        by_tenant = {}
        for tid, name in rows:
            by_tenant.setdefault(tid, []).append(name)
        for tid, names in by_tenant.items():
            t = db.get(Tenant, tid)
            print(f"tenant {tid}  ({t.name if t else '?'} · {t.contact_email if t else '?'})")
            for n in names[:6]:
                print(f"    array: {n}")


def seed(tenant: str, provider: str, username: str, host: str | None):
    pw = os.environ.get("CC_SEED_PASS")
    if not pw:
        print("CC_SEED_PASS env not set — refusing to seed without a password.")
        sys.exit(1)
    if not cc.crypto_ready():
        print("SO_CONFIG_KEY not set — cannot encrypt. Aborting.")
        sys.exit(1)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant)
        if not t:
            print(f"no such tenant {tenant}")
            sys.exit(1)
        cc.upsert_credential(db, tenant, provider, username, pw,
                             login_host=host, enable=True)
        db.commit()
    print(f"seeded + enabled Cloud Capture: tenant={tenant} provider={provider} "
          f"username={username} (password stored encrypted)")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--find-array")
    ap.add_argument("--tenant")
    ap.add_argument("--provider")
    ap.add_argument("--username")
    ap.add_argument("--host")
    args = ap.parse_args(argv)
    if args.find_array:
        find_array(args.find_array)
        return
    if args.tenant and args.provider and args.username:
        seed(args.tenant, args.provider, args.username, args.host)
        return
    ap.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
