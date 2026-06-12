"""Provision CUSTOMER ZERO — our own agency, dogfooding Solar Operator.

Creates (idempotently):
  Tenant  ten_ocicbb_customer_zero   "OCICBB Solar Agency" (comped — dogfood,
                                      no Stripe objects are touched)
  Client  "OCICBB Solar Agency"      the self-client holding our arrays
  Array   "WEC – Evans (Maple Corner)"        + UtilityAccount provider=wec
  Array   "Lyndonville – O'Connor-Genereaux"  + UtilityAccount provider=lyndonville

Credentials come from ~/solar-operator-logins/ (registry.json + credentials/*.json),
staged by the solar-login-intake flow. xpansiv_ma is deliberately NOT provisioned:
ms.xpansiv.com is a REC/market-settlement registry, not a utility billing portal,
and `xpansiv_ma` is not a provider code — needs a bespoke adapter first.

Account numbers are not present in the staged credential files, so accounts are
created with PENDING-DISCOVERY placeholders. With --live, the script
authenticates against WEC's SmartHub deployment using the staged credentials,
discovers the real account_number + serviceLocationNumber via
fetch_account_list, upgrades the placeholder in place, and stores a
UtilitySession row so jobs/smarthub_pull can do real generation pulls.
Lyndonville has no adapter (InvoiceCloud portal) so it always stays placeholder.

Idempotent: every entity is looked up before insert; placeholders are upgraded,
never duplicated. Safe to re-run any number of times.

Run:
    python scripts/provision_customer_zero.py            # local DB, no network
    python scripts/provision_customer_zero.py --live     # + real WEC SmartHub auth
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from api.db import SessionLocal, init_db, DB_URL
from api.models import (
    Tenant, Client, Array, UtilityAccount, UtilitySession, now,
)
from api.providers import PROVIDER_CODES

LOGINS_DIR = pathlib.Path.home() / "solar-operator-logins"

TENANT_ID = "ten_ocicbb_customer_zero"
TENANT_KEY = "sol_dogfood_ocicbb_customer_zero"
TENANT_NAME = "OCICBB Solar Agency"
CONTACT_EMAIL = "admin@solaroperator.org"
CLIENT_NAME = "OCICBB Solar Agency"

# Placeholder account numbers (upgraded in place by --live discovery).
WEC_PLACEHOLDER = "WEC-PENDING-DISCOVERY"
LYNDONVILLE_PLACEHOLDER = "LYND-PENDING-DISCOVERY"

ARRAYS = [
    {
        "name": "WEC – Evans (Maple Corner)",
        "provider": "wec",
        "placeholder": WEC_PLACEHOLDER,
        "credentials_file": "wec.json",
        "notes": ("Customer-zero dogfood. Staged login from Richard Evans "
                  "(solar-login-intake msg 68678). SmartHub host "
                  "washingtonelectric.smarthub.coop — live pull possible."),
    },
    {
        "name": "Lyndonville – O'Connor-Genereaux",
        "provider": "lyndonville",
        "placeholder": LYNDONVILLE_PLACEHOLDER,
        "credentials_file": "lyndonville.json",
        "notes": ("Customer-zero dogfood. Staged login from Liam "
                  "O'Connor-Genereaux (msg 68648). InvoiceCloud portal — "
                  "NO adapter yet; manual upload until one exists."),
    },
]


def load_credentials(filename: str) -> dict:
    path = LOGINS_DIR / "credentials" / filename
    if not path.exists():
        raise SystemExit(f"missing credentials file: {path}")
    return json.loads(path.read_text())


def get_or_create_tenant(db) -> Tenant:
    t = db.get(Tenant, TENANT_ID)
    if t:
        print(f"  tenant exists: {t.id} ({t.name})")
        return t
    t = Tenant(
        id=TENANT_ID,
        name=TENANT_NAME,
        operator_name="OCICBB",
        company_name=TENANT_NAME,
        contact_email=CONTACT_EMAIL,
        tenant_key=TENANT_KEY,
        plan="comped",                 # dogfood — never billed, no Stripe objects
        subscription_status="comped",
        active=True,
        is_demo=False,
        report_frequency="quarterly",
        onboarding_stage="done",
    )
    db.add(t)
    db.flush()
    print(f"  tenant created: {t.id} ({t.name})")
    return t


def get_or_create_client(db, t: Tenant) -> Client:
    c = db.execute(
        select(Client).where(
            Client.tenant_id == t.id,
            Client.name == CLIENT_NAME,
            Client.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if c:
        print(f"  client exists: id={c.id} ({c.name})")
        return c
    c = Client(
        tenant_id=t.id,
        name=CLIENT_NAME,
        contact_email=CONTACT_EMAIL,
        report_frequency="quarterly",
        notes="CUSTOMER ZERO — our own agency dogfooding the product. "
              "Land-and-expand reference account.",
        active=True,
    )
    db.add(c)
    db.flush()
    print(f"  client created: id={c.id} ({c.name})")
    return c


def get_or_create_array(db, t: Tenant, c: Client, spec: dict) -> Array:
    a = db.execute(
        select(Array).where(
            Array.tenant_id == t.id,
            Array.name == spec["name"],
        )
    ).scalar_one_or_none()
    if a:
        if a.deleted_at is not None:
            a.deleted_at = None  # resurrect a soft-deleted dogfood array
            print(f"  array resurrected: id={a.id} ({a.name})")
        else:
            print(f"  array exists: id={a.id} ({a.name})")
        return a
    a = Array(
        tenant_id=t.id,
        client_id=c.id,
        name=spec["name"],
        bill_offset_months=1,
        notes=spec["notes"],
        fuel_type="solar",
    )
    db.add(a)
    db.flush()
    print(f"  array created: id={a.id} ({a.name})")
    return a


def get_or_create_account(db, t: Tenant, a: Array, spec: dict) -> UtilityAccount:
    provider = spec["provider"]
    assert provider in PROVIDER_CODES, f"{provider} not in PROVIDER_CODES"
    # Any account for this provider already on this array (placeholder or real)?
    acc = db.execute(
        select(UtilityAccount).where(
            UtilityAccount.tenant_id == t.id,
            UtilityAccount.array_id == a.id,
            UtilityAccount.provider == provider,
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if acc:
        print(f"    account exists: {provider} #{acc.account_number}")
        return acc
    acc = UtilityAccount(
        tenant_id=t.id,
        array_id=a.id,
        provider=provider,
        account_number=spec["placeholder"],
        nickname=a.name,
        extra={"customer_zero": True,
               "credentials_file": str(LOGINS_DIR / "credentials" / spec["credentials_file"])},
    )
    db.add(acc)
    db.flush()
    print(f"    account created: {provider} #{acc.account_number}")
    return acc


def live_wec_discovery(db, t: Tenant, a: Array, acc: UtilityAccount) -> dict:
    """Authenticate against WEC SmartHub with the staged creds, discover the
    real account_number + serviceLocationNumber, upgrade the placeholder, and
    store a UtilitySession so jobs/smarthub_pull can do real pulls."""
    from api.adapters.smarthub import authenticate, fetch_account_list

    creds = load_credentials("wec.json")
    host = creds["smarthub_host"]
    email = creds["username"]

    print(f"    [live] authenticating {email} @ {host} ...")
    session = authenticate(host, email, creds["password"])
    print(f"    [live] auth OK (primary_username={session['primary_username']!r})")

    locations = fetch_account_list(host, session)
    print(f"    [live] {len(locations)} service location(s) found")
    if not locations:
        return {"ok": False, "reason": "auth OK but no service locations returned"}

    loc = locations[0]
    real_acct = str(loc["account_number"])
    svc_loc = str(loc["service_location_number"])

    # Upgrade placeholder → real account number (don't dupe if a row with the
    # real number already exists from a prior --live run).
    existing_real = db.execute(
        select(UtilityAccount).where(
            UtilityAccount.tenant_id == t.id,
            UtilityAccount.provider == "wec",
            UtilityAccount.account_number == real_acct,
        )
    ).scalar_one_or_none()
    if existing_real and existing_real.id != acc.id:
        acc = existing_real
        print(f"    [live] real account row already exists: #{real_acct}")
    else:
        acc.account_number = real_acct
        print(f"    [live] account upgraded: #{real_acct}")
    acc.extra = {**(acc.extra or {}), "service_location_number": svc_loc}
    acc.last_seen = now()

    # Store/refresh the UtilitySession (latest row wins per smarthub_pull).
    db.add(UtilitySession(
        tenant_id=t.id,
        provider="wec",
        api_token=session["auth_token"],
        expires_at=datetime.utcnow() + timedelta(minutes=5),
        raw_payload={"user": {
            "email": email,
            "primary_username": session["primary_username"],
        }},
    ))
    print("    [live] UtilitySession stored (smarthub_pull can now run)")
    return {"ok": True, "account_number": real_acct,
            "service_location": svc_loc, "array_id": a.id}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="attempt real WEC SmartHub auth + account discovery")
    ap.add_argument("--pull", action="store_true",
                    help="after --live discovery, run a real generation pull")
    args = ap.parse_args()

    print(f"DB: {DB_URL}")
    init_db()

    live_result = None
    with SessionLocal() as db:
        t = get_or_create_tenant(db)
        c = get_or_create_client(db, t)
        accounts = {}
        for spec in ARRAYS:
            a = get_or_create_array(db, t, c, spec)
            accounts[spec["provider"]] = (a, get_or_create_account(db, t, a, spec))

        if args.live:
            a, acc = accounts["wec"]
            try:
                live_result = live_wec_discovery(db, t, a, acc)
            except Exception as exc:
                live_result = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
                print(f"    [live] FAILED: {live_result['reason']}")

        db.commit()

        # ── verify by querying back ──────────────────────────────────────
        print("\nVERIFY:")
        t2 = db.get(Tenant, TENANT_ID)
        clients = db.execute(select(Client).where(
            Client.tenant_id == TENANT_ID, Client.deleted_at.is_(None))
        ).scalars().all()
        arrays = db.execute(select(Array).where(
            Array.tenant_id == TENANT_ID, Array.deleted_at.is_(None))
        ).scalars().all()
        accts = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == TENANT_ID,
            UtilityAccount.deleted_at.is_(None))
        ).scalars().all()
        print(f"  tenant: {t2.id} plan={t2.plan} active={t2.active}")
        for cl in clients:
            print(f"  client: id={cl.id} name={cl.name!r}")
        for ar in arrays:
            print(f"  array:  id={ar.id} name={ar.name!r} client_id={ar.client_id}")
        for ac in accts:
            print(f"  acct:   id={ac.id} provider={ac.provider} "
                  f"#{ac.account_number} array_id={ac.array_id}")
        print(f"  totals: {len(clients)} client(s), {len(arrays)} array(s), "
              f"{len(accts)} account(s)")

    if args.pull and live_result and live_result.get("ok"):
        from api.jobs.smarthub_pull import pull_daily_generation_for_account
        print("\nPULL (real WEC generation data, 90 days):")
        res = pull_daily_generation_for_account(
            None, TENANT_ID, live_result["array_id"], days_back=90)
        print(f"  {res}")

    if live_result is not None:
        print(f"\nLIVE RESULT: {live_result}")


if __name__ == "__main__":
    main()
