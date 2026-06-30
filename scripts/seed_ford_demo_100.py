"""Seed a PRIVATE 100-array / 100-offtaker tenant for Ford to explore + audit
the at-scale UX (Ford, 2026-06-30: "what does the website look like with a
hundred arrays and a hundred offtakers... we need to look at it first").

This is DELIBERATELY a different tenant from the shared public
`ten_demo_readonly_v1` (scripts/seed_demo_tenant.py) — that one is read-only
(every write 403s via api.account.require_not_demo) and backs the public
homepage "Try it" CTA, so it can't be touched here. This tenant is a REAL,
mutable (is_demo=False) account so Ford can actually click around, add an
offtaker, use the bulk-import feature, etc. — comped (plan="comped",
subscription_status="comped"), never billed, never touches Stripe.

IDEMPOTENT: re-running wipes and rebuilds this tenant's data from the
deterministic config below (same pattern as seed_demo_tenant.py's
_wipe_demo_data), so re-seeding is always safe.

This script seeds the STRUCTURE only (tenant, clients, arrays, utility
accounts + service_address + 24mo of bills) — it does NOT create the 100
offtaker subscriptions. Those are created via a REAL call to the
POST /subscriptions/bulk-import endpoint this session just shipped (the
natural way an operator at this scale would actually do it), driven by a
companion script/CSV generated from this script's printed account numbers.

Run on Railway prod:
    railway ssh "cd /app && python scripts/seed_ford_demo_100.py"
"""
from __future__ import annotations

import os
import secrets
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, delete

from api.db import SessionLocal, init_db
from api.models import (
    Tenant, Client, Array, UtilityAccount, Bill, DailyGeneration,
    CaptureEvent, DeleteHistory, ClientMergeDismissal, ArrayMergeDismissal,
    UtilitySession, LoginToken, BillingReportSubscription,
)

TENANT_ID = "ten_ford_demo_100"
TENANT_NAME = "Ford Demo — 100 Arrays"
CONTACT_EMAIL = "ford.genereaux@gmail.com"
DEMO_PASSWORD = "Ford100Demo!"   # plain text printed for Ford; hashed below before storing
DELIVERED_AT = datetime(2026, 4, 15, 14, 30, 0)
FIRST_CONNECT = datetime(2022, 6, 1)
MONTHS_OF_HISTORY = 24
TARGET_ARRAYS = 100

SEASONAL = [0.42, 0.55, 0.82, 1.06, 1.22, 1.31, 1.30, 1.18, 0.98, 0.74, 0.48, 0.38]

# Fictional VT-style community names — distinct from BOTH real customers and
# the public demo's client list (seed_demo_tenant.py) so the two demos never
# read as the same fleet if compared side by side.
COMMUNITIES = [
    "Otter Creek Solar Collective", "Winooski Valley Cooperative", "Black River Energy Trust",
    "Mettawee Hill Solar Group", "Lemon Fair Community Power", "Ottauquechee Cohousing",
    "Battenkill Grange Solar", "White River Watershed Co-op", "Williams River Energy Collective",
    "Saxtons River Mutual", "Mill Brook Solar Partners", "Dog River Community Solar",
    "Wells River Energy Trust", "Passumpsic Valley Co-op", "Moose River Solar Group",
    "Clyde River Energy Collective", "Missisquoi Valley Solar", "Lamoille River Cooperative",
    "Huntington Gorge Solar", "Mad Brook Energy Trust", "Joes Brook Community Power",
    "Sleepers River Solar Co-op", "Wait River Energy Collective", "Roaring Brook Solar Group",
    "Cold Hollow Cooperative", "Worcester Range Solar Trust", "Hazens Notch Energy Co-op",
    "Granville Gulf Solar", "Lincoln Gap Community Power", "Brandon Gap Solar Collective",
    "Middlebury Gap Cooperative", "Appalachian Gap Energy Trust",
]

PROVIDERS = ["gmp"] * 5 + ["vec"] * 3 + ["wec"] * 2   # ~50/30/20 mix, matches real distribution
REGIONS = ["north", "central", "south"]
ARRAY_SITE_WORDS = [
    "Solar Field", "Community Array", "Carport", "Roof Array", "Town Garage Array",
    "School Roof", "Meadow Array", "Hillside Array", "Industrial Park Array", "Library Roof",
]

# Synthetic-but-real-shaped VT street addresses (for the geocoding feature this
# session is also building — gives it real addresses to chew on, not blanks).
STREETS = [
    "Main St", "Depot St", "School St", "Church St", "River Rd", "Mountain Rd",
    "Ridge Rd", "Pond Rd", "Mill St", "Maple St", "County Rd", "Town Farm Rd",
]
TOWNS = [
    ("Rutland", "VT", "05701"), ("Middlebury", "VT", "05753"), ("Montpelier", "VT", "05602"),
    ("Barre", "VT", "05641"), ("St Johnsbury", "VT", "05819"), ("Bennington", "VT", "05201"),
    ("Brattleboro", "VT", "05301"), ("Morrisville", "VT", "05661"), ("Newport", "VT", "05855"),
    ("Randolph", "VT", "05060"), ("Waterbury", "VT", "05676"), ("Bradford", "VT", "05033"),
]


def _history_months(today: date) -> list[tuple[int, int]]:
    y, m = today.year, today.month
    months: list[tuple[int, int]] = []
    for i in range(1, MONTHS_OF_HISTORY + 1):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append((yy, mm))
    months.reverse()
    return months


def _month_kwh(base_mwh: float, year: int, month: int, seed: int) -> int:
    factor = SEASONAL[month - 1]
    jitter = (((seed * 31 + month * 17 + year * 7) % 11) - 5) / 100.0
    yoy = 1.0 + 0.025 * (year - 2024)
    mwh = base_mwh * factor * (1.0 + jitter) * yoy
    return max(0, int(round(mwh * 1000)))


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _wipe(db) -> None:
    tid = TENANT_ID
    db.execute(delete(BillingReportSubscription).where(BillingReportSubscription.tenant_id == tid))
    db.execute(delete(Bill).where(Bill.tenant_id == tid))
    db.execute(delete(DailyGeneration).where(DailyGeneration.tenant_id == tid))
    db.execute(delete(UtilityAccount).where(UtilityAccount.tenant_id == tid))
    db.execute(delete(Array).where(Array.tenant_id == tid))
    db.execute(delete(ClientMergeDismissal).where(ClientMergeDismissal.tenant_id == tid))
    db.execute(delete(ArrayMergeDismissal).where(ArrayMergeDismissal.tenant_id == tid))
    db.execute(delete(Client).where(Client.tenant_id == tid))
    db.execute(delete(CaptureEvent).where(CaptureEvent.tenant_id == tid))
    db.execute(delete(DeleteHistory).where(DeleteHistory.tenant_id == tid))
    db.execute(delete(UtilitySession).where(UtilitySession.tenant_id == tid))
    db.execute(delete(LoginToken).where(LoginToken.tenant_id == tid))
    db.flush()


def _gen_array_plan() -> list[dict]:
    """Distribute exactly TARGET_ARRAYS arrays across the communities, 2-5 each,
    deterministically (no RNG — same plan every run)."""
    plan = []
    remaining = TARGET_ARRAYS
    ci = 0
    while remaining > 0:
        community = COMMUNITIES[ci % len(COMMUNITIES)]
        n = min(4 - (ci % 3), remaining)   # cycle 4,3,2,4,3,2... for variety
        n = max(1, n)
        for k in range(n):
            idx = TARGET_ARRAYS - remaining
            seed = idx * 7 + 3
            kw = round(2.0 + (seed % 280) / 10.0, 1)   # 2.0 - 30.0 kW spread
            provider = PROVIDERS[seed % len(PROVIDERS)]
            region = REGIONS[seed % len(REGIONS)]
            site_word = ARRAY_SITE_WORDS[seed % len(ARRAY_SITE_WORDS)]
            town, st, zipc = TOWNS[seed % len(TOWNS)]
            street_n = 100 + (seed * 13) % 900
            street = STREETS[seed % len(STREETS)]
            plan.append({
                "community": community,
                "array_name": f"{community.split()[0]} {site_word} {k + 1}" if n > 1 else f"{community.split()[0]} {site_word}",
                "kw": kw,
                "provider": provider,
                "region": region,
                "address": f"{street_n} {street}, {town}, {st} {zipc}",
                "nepool": f"98{idx:03d}",
            })
            remaining -= 1
            if remaining == 0:
                break
        ci += 1
    return plan


def seed() -> dict:
    init_db()
    today = datetime.utcnow().date()
    months = _history_months(today)
    plan = _gen_array_plan()
    assert len(plan) == TARGET_ARRAYS, f"plan has {len(plan)} arrays, expected {TARGET_ARRAYS}"

    from passlib.context import CryptContext
    pw_hash = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12).hash(DEMO_PASSWORD)

    with SessionLocal() as db:
        t = db.get(Tenant, TENANT_ID)
        if t is not None:
            _wipe(db)
        else:
            t = Tenant(id=TENANT_ID)
            db.add(t)
        t.name = TENANT_NAME
        t.contact_email = CONTACT_EMAIL
        t.tenant_key = "sol_live_" + secrets.token_urlsafe(24)
        t.product = "array_operator"
        t.plan = "comped"
        t.subscription_status = "comped"
        t.is_demo = False              # REAL tenant — Ford can mutate it
        t.active = True
        t.password_hash = pw_hash
        t.onboarding_stage = "done"
        t.onboarding_token = None
        t.stripe_customer_id = None
        t.stripe_subscription_id = None
        t.trial_ends_at = None
        t.report_frequency = "quarterly"
        db.flush()

        by_community: dict[str, list[dict]] = {}
        for row in plan:
            by_community.setdefault(row["community"], []).append(row)

        counts = {"clients": 0, "arrays": 0, "accounts": 0, "bills": 0}
        account_rows = []   # for the companion offtaker-CSV generator

        for ci, (cname, arrays) in enumerate(by_community.items()):
            client = Client(
                tenant_id=TENANT_ID, name=cname,
                contact_email=cname.lower().replace(" ", ".").replace(",", "") + "@demo.example",
                report_frequency="quarterly", active=True,
                gmp_autopopulate=False, vec_autopopulate=False,
                created_at=FIRST_CONNECT,
            )
            db.add(client)
            db.flush()
            counts["clients"] += 1

            for ai, row in enumerate(arrays):
                arr = Array(
                    tenant_id=TENANT_ID, client_id=client.id, name=row["array_name"],
                    region=row["region"], nepool_gis_id=row["nepool"],
                    bill_offset_months=1, first_connect_date=FIRST_CONNECT,
                    created_at=FIRST_CONNECT,
                )
                db.add(arr)
                db.flush()
                counts["arrays"] += 1

                seed_n = ci * 1000 + ai * 10 + 3
                acct_num = f"98{counts['arrays']:08d}"
                acct = UtilityAccount(
                    tenant_id=TENANT_ID, array_id=arr.id, provider=row["provider"],
                    account_number=acct_num, customer_number=acct_num,
                    nickname=row["array_name"],
                    service_address={"line1": row["address"]},
                    enabled=True, is_residential=False,
                )
                db.add(acct)
                db.flush()
                counts["accounts"] += 1
                account_rows.append({
                    "array_name": row["array_name"], "account_number": acct_num,
                    "kw": row["kw"], "provider": row["provider"],
                })

                base_mwh = row["kw"] * 1.3   # rough VT capacity-factor-ish monthly mid-season MWh
                for (yy, mm) in months:
                    kwh = _month_kwh(base_mwh, yy, mm, seed_n)
                    last = _last_day(yy, mm)
                    db.add(Bill(
                        tenant_id=TENANT_ID, account_id=acct.id,
                        bill_date=datetime(yy, mm, last), period_start=datetime(yy, mm, 1),
                        period_end=datetime(yy, mm, last), billing_days=last,
                        kwh_generated=kwh, kwh_consumed=0,
                        document_number=f"FORD100-{counts['accounts']:03d}-{yy}{mm:02d}",
                        parse_status="parsed", pulled_at=DELIVERED_AT,
                    ))
                    counts["bills"] += 1
            db.flush()

        db.commit()

    # Write the account roster for the companion offtaker-import CSV.
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ford_demo_100_accounts.csv")
    with open(out_path, "w") as f:
        f.write("array_name,account_number,kw,provider\n")
        for r in account_rows:
            f.write(f"{r['array_name']},{r['account_number']},{r['kw']},{r['provider']}\n")

    print("=== Ford demo (100 arrays) seeded ===")
    print(f"  tenant       : {TENANT_ID}")
    print(f"  login email  : {CONTACT_EMAIL}")
    print(f"  login password: {DEMO_PASSWORD}")
    print(f"  clients      : {counts['clients']}")
    print(f"  arrays       : {counts['arrays']}")
    print(f"  accounts     : {counts['accounts']}")
    print(f"  bills        : {counts['bills']}  ({MONTHS_OF_HISTORY} months each)")
    print(f"  roster CSV   : {out_path}")
    return counts


if __name__ == "__main__":
    seed()
    sys.exit(0)
