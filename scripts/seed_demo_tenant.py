"""Seed (or re-seed) the shared read-only demo tenant.

ONE shared demo tenant backs the homepage "Try it" path: every visitor who
clicks the magic link (/demo-account → GET /v1/demo/enter) signs in as this
tenant and can browse freely but cannot mutate anything (every write endpoint
refuses via api.account.require_not_demo).

This script is IDEMPOTENT: if `ten_demo_readonly_v1` already exists it deletes
all of its data and rebuilds from the deterministic config below, so the demo
state never drifts. Run it again any time the demo looks stale.

Run locally:
    python scripts/seed_demo_tenant.py
Run on Railway prod (after merge, before the homepage CTA goes live):
    railway ssh "cd /app && python scripts/seed_demo_tenant.py"

──────────────────────────────────────────────────────────────────────────
Fictional names actually used (verified non-colliding at seed time — see
_resolve_client_name; the demo refuses to reuse a real customer's name):
    Green Mountain Community Solar, Catamount Community Power,
    Northeast Kingdom Cooperative, Green Hollow Methodist Church,
    Riverbend Cohousing, Putney Library, Maple Ridge Cooperative,
    Mad River School District, Lamoille Valley Dairy,
    Central Vermont Medical, Hill Farmstead Brewery, Worcester Town Office,
    Smugglers' Notch Lodge, Calais Congregational, Sharon Academy,
    Marlboro Homestead
Spare fallback names, used only if one of the above collides with a real
non-demo Client already in the database:
    Cabot Public School · Stowe Mountain Co-op · Worcester Grange ·
    Bristol Falls Collective · Underhill Maple Co-op · Brookfield Town Hall ·
    Marshfield Energy Trust · Plainfield Mutual
NEPOOL-GIS IDs are all 5-digit and start with "99"; GMP account numbers are
all 10-digit and start with "99" — neither can collide with a real grid asset
or utility account.
──────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

# Allow `python scripts/seed_demo_tenant.py` (the documented prod invocation)
# by putting the repo root on sys.path so `import api.*` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, delete

from api.db import SessionLocal, init_db
from api.models import (
    Tenant, Client, Array, UtilityAccount, Bill, DailyGeneration,
    CaptureEvent, DeleteHistory, ClientMergeDismissal, ArrayMergeDismissal,
    UtilitySession, LoginToken,
)

# ── identity ─────────────────────────────────────────────────────────────
DEMO_TENANT_ID = "ten_demo_readonly_v1"
DEMO_TENANT_NAME = "Northeast Community Solar"   # fictional umbrella operator
DEMO_EMAIL = "demo@solaroperator.org"
DEMO_TENANT_KEY = "demo-public-readonly"

# ── deterministic seed config ────────────────────────────────────────────
# Static so two consecutive runs produce byte-identical generation data.
# (year, quarter) "sent" status in the dashboard is derived from bills +
# Client.last_delivery_at, so we stamp a Q2-2026 delivery date below to make
# every complete quarter through Q1 2026 read as "sent".
DELIVERED_AT = datetime(2026, 4, 15, 14, 30, 0)
FIRST_CONNECT = datetime(2023, 9, 1)
MONTHS_OF_HISTORY = 24

# Vermont community-solar seasonal shape (Jan..Dec): low winter, peak summer.
SEASONAL = [0.42, 0.55, 0.82, 1.06, 1.22, 1.31, 1.30, 1.18, 0.98, 0.74, 0.48, 0.38]

# Preferred client name → spare fallback if it collides with a real customer.
SPARE_NAMES = [
    "Cabot Public School", "Stowe Mountain Co-op",
    "Worcester Grange", "Bristol Falls Collective",
    "Underhill Maple Co-op", "Brookfield Town Hall",
    "Marshfield Energy Trust", "Plainfield Mutual",
]

# client name → list of arrays; each array tuple:
#   (name, nepool_gis_id, base_mwh, region, n_accounts, provider)
# base_mwh is the array's TOTAL typical mid-season month; accounts split it
# evenly (believable sub-meters). provider is one of "gmp", "vec", "wec" —
# mixing utilities so the canvas shows the multi-provider chip variety.
#
# Sized to show off the operator's range: ~15 clients, ~28 arrays, ~60
# accounts. Mix of single-array residences/churches and large multi-array
# cooperatives. Sub-metered arrays sprinkled throughout so the new "N meters"
# dedupe chip is visible without hunting.
DEMO_CLIENTS: list[dict] = [
    # ── Showpiece: GMCS — the operator's own community-solar book, 7 arrays,
    #    one sub-metered showpiece (Starlake = 3 meters into one array). Mirrors
    #    Bruce Genereaux's real portfolio shape so the demo reads as "this is
    #    what your book will look like." Fictional NEPOOL/account IDs.
    {
        "name": "Green Mountain Community Solar",
        "email": "gmcs@demo.example",
        "arrays": [
            ("Starlake",          "99100", 21.0, "central", 3, "gmp"),  # sub-metered
            ("Chester",           "99133", 12.4, "south",   1, "gmp"),
            ("JSIS",              "99134",  8.8, "central", 1, "gmp"),
            ("Londonderry",       "99135",  9.6, "south",   1, "gmp"),
            ("Tannery Brook",     "99136",  7.2, "central", 1, "gmp"),
            ("Timberworks",       "99137", 10.4, "north",   1, "gmp"),
            ("Waterford",         "99138",  6.8, "north",   1, "gmp"),
        ],
    },
    # ── Anchor customer: 5 arrays, mixed providers, two sub-metered ─────────
    {
        "name": "Catamount Community Power",
        "email": "catamount@demo.example",
        "arrays": [
            ("Catamount Ridge Solar",   "99101", 18.0, "central", 3, "gmp"),
            ("Hardwick Field Array",    "99102", 11.5, "north",   2, "gmp"),
            ("Greensboro Carport",      "99103",  7.8, "north",   1, "gmp"),
            ("Walden Town Garage",      "99104",  9.2, "north",   1, "vec"),
            ("Craftsbury Common Roof",  "99105",  6.4, "north",   1, "vec"),
        ],
    },
    # ── Big multi-utility co-op: 4 arrays across all three providers ───────
    {
        "name": "Northeast Kingdom Cooperative",
        "email": "nekcoop@demo.example",
        "arrays": [
            ("Lyndon Industrial Park",  "99106", 22.0, "north",   3, "gmp"),
            ("St Johnsbury Mill Roof",  "99107", 16.4, "north",   2, "wec"),
            ("Burke Mountain Lot",      "99108", 14.1, "north",   1, "vec"),
            ("Newport Lakefront Array", "99109", 19.6, "north",   2, "vec"),
        ],
    },
    # ── Mid-size church + carport (the original Green Hollow, expanded) ────
    {
        "name": "Green Hollow Methodist Church",
        "email": "greenhollow@demo.example",
        "arrays": [
            ("Fellowship Hall Roof",    "99110",  6.2, "central", 2, "gmp"),
            ("Parish Carport",          "99111",  4.1, "central", 1, "gmp"),
        ],
    },
    # ── Cohousing community: 3 arrays, one sub-metered ─────────────────────
    {
        "name": "Riverbend Cohousing",
        "email": "riverbend@demo.example",
        "arrays": [
            ("Riverbend Commons",       "99112",  9.4, "south",   2, "gmp"),
            ("Riverbend Carport",       "99113",  4.8, "south",   2, "gmp"),
            ("Riverbend Workshop",      "99114",  3.6, "south",   1, "gmp"),
        ],
    },
    # ── Small single-array library (kept tiny to show the floor case) ──────
    {
        "name": "Putney Library",
        "email": "putney@demo.example",
        "arrays": [
            ("Putney Library Roof",     "99115",  7.1, "south",   2, "gmp"),
        ],
    },
    # ── Multi-array farm co-op ─────────────────────────────────────────────
    {
        "name": "Maple Ridge Cooperative",
        "email": "mapleridge@demo.example",
        "arrays": [
            ("Maple Ridge North",       "99116", 15.3, "north",   3, "gmp"),
            ("Maple Ridge South",       "99117", 13.6, "north",   2, "gmp"),
            ("Maple Ridge Sugarhouse",  "99118",  4.2, "north",   1, "wec"),
        ],
    },
    # ── Municipal customer: school district, three buildings ───────────────
    {
        "name": "Mad River School District",
        "email": "madriver@demo.example",
        "arrays": [
            ("Waitsfield Elementary",   "99119",  8.7, "central", 1, "gmp"),
            ("Harwood Union High",      "99120", 17.2, "central", 2, "gmp"),
            ("Crossett Brook Middle",   "99121", 10.4, "central", 1, "gmp"),
        ],
    },
    # ── Single-array residential farm ──────────────────────────────────────
    {
        "name": "Lamoille Valley Dairy",
        "email": "lamoille@demo.example",
        "arrays": [
            ("Dairy Barn Roof",         "99122",  5.6, "north",   1, "gmp"),
        ],
    },
    # ── Hospital — one big array fed by FOUR meters (showpiece) ────────────
    {
        "name": "Central Vermont Medical",
        "email": "cvmed@demo.example",
        "arrays": [
            ("CVMC Main Campus Solar",  "99123", 28.4, "central", 4, "gmp"),
            ("CVMC Parking Deck",       "99124", 11.0, "central", 1, "gmp"),
        ],
    },
    # ── Brewery: single big array ──────────────────────────────────────────
    {
        "name": "Hill Farmstead Brewery",
        "email": "hillfarmstead@demo.example",
        "arrays": [
            ("Brewery Roof Array",      "99125",  9.8, "north",   2, "vec"),
        ],
    },
    # ── Town: small municipal building ─────────────────────────────────────
    {
        "name": "Worcester Town Office",
        "email": "worcester@demo.example",
        "arrays": [
            ("Town Hall Roof",          "99126",  3.2, "central", 1, "wec"),
        ],
    },
    # ── Resort: multi-building ─────────────────────────────────────────────
    {
        "name": "Smugglers' Notch Lodge",
        "email": "smuggs@demo.example",
        "arrays": [
            ("Main Lodge Roof",         "99127", 18.6, "north",   2, "vec"),
            ("Base Lodge Carport",      "99128", 12.4, "north",   1, "vec"),
        ],
    },
    # ── Single-array church (small) ────────────────────────────────────────
    {
        "name": "Calais Congregational",
        "email": "calais@demo.example",
        "arrays": [
            ("Sanctuary Roof",          "99129",  3.8, "central", 1, "gmp"),
        ],
    },
    # ── Mid-size school ────────────────────────────────────────────────────
    {
        "name": "Sharon Academy",
        "email": "sharon@demo.example",
        "arrays": [
            ("Academy Building Roof",   "99130", 11.4, "south",   2, "gmp"),
            ("Athletic Field Array",    "99131",  7.9, "south",   1, "gmp"),
        ],
    },
    # ── Floor case: single small array (homestead) ─────────────────────────
    {
        "name": "Marlboro Homestead",
        "email": "marlboro@demo.example",
        "arrays": [
            ("Homestead Roof",          "99132",  2.4, "south",   1, "gmp"),
        ],
    },
]


def _history_months(today: date) -> list[tuple[int, int]]:
    """The last MONTHS_OF_HISTORY complete (year, month), oldest first.

    Never includes the current (incomplete) month, so there are no future or
    partial bills."""
    y, m = today.year, today.month
    months: list[tuple[int, int]] = []
    # Walk back from last complete month.
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
    """Deterministic seasonal monthly kWh for one account.

    Pure function of its inputs (no RNG / clock) so re-seeding is stable.
    `seed` (a per-account integer) adds a small ±5% reproducible variation so
    arrays and accounts don't read as flat clones of each other; a tiny
    year-over-year drift makes the two years differ believably."""
    factor = SEASONAL[month - 1]
    jitter = (((seed * 31 + month * 17 + year * 7) % 11) - 5) / 100.0  # ±0.05
    yoy = 1.0 + 0.025 * (year - 2024)  # gentle upward drift
    mwh = base_mwh * factor * (1.0 + jitter) * yoy
    return max(0, int(round(mwh * 1000)))  # kWh, integer


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _wipe_demo_data(db) -> None:
    """Delete every row belonging to the demo tenant, children first.

    Done with explicit per-table deletes (not ORM cascade) so the order is
    obvious and it works the same on SQLite and Postgres."""
    tid = DEMO_TENANT_ID
    # Bills reference utility_accounts; delete by tenant_id directly.
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


def _resolve_client_name(db, preferred: str, spares: list[str]) -> str:
    """Return `preferred` unless a real (non-demo) tenant already has a Client
    with that exact name; then fall back to the first free spare. Never reuses
    a real customer's name in the public demo."""
    def taken(name: str) -> bool:
        row = db.execute(
            select(Client.id)
            .join(Tenant, Client.tenant_id == Tenant.id)
            .where(Client.name == name, Tenant.is_demo.is_(False))
        ).first()
        return row is not None

    if not taken(preferred):
        return preferred
    for spare in spares:
        if not taken(spare):
            print(f"  ! name '{preferred}' collides with a real customer — "
                  f"using fallback '{spare}'")
            return spare
    raise SystemExit(
        f"Name '{preferred}' collides and all spares are taken — "
        "add more SPARE_NAMES."
    )


def seed(today: date | None = None) -> dict:
    """Create/replace the demo tenant and all its data. Returns a small summary
    dict (counts) handy for tests and ops logging."""
    today = today or datetime.utcnow().date()
    init_db()
    months = _history_months(today)

    with SessionLocal() as db:
        # Upsert the tenant row itself.
        t = db.get(Tenant, DEMO_TENANT_ID)
        if t is not None:
            _wipe_demo_data(db)
        else:
            t = Tenant(id=DEMO_TENANT_ID)
            db.add(t)
        t.name = DEMO_TENANT_NAME
        t.contact_email = DEMO_EMAIL
        t.tenant_key = DEMO_TENANT_KEY
        t.is_demo = True
        t.plan = "demo"
        t.subscription_status = "demo"
        t.active = True
        t.report_frequency = "quarterly"
        t.cc_on_reports = False
        t.onboarding_stage = "done"
        t.onboarding_token = None
        # Sentinel billing identity — never a real Stripe customer.
        t.stripe_customer_id = None
        t.stripe_subscription_id = None
        t.stripe_payment_method_id = None
        t.trial_ends_at = None
        t.last_delivery_at = DELIVERED_AT
        # Email templates: leave None → built-in defaults.
        t.send_from_email = None
        t.send_from_name = None
        t.email_subject_template = None
        t.email_body_template = None
        t.email_signoff = None
        db.flush()

        acct_seq = 0
        spares = list(SPARE_NAMES)
        counts = {"clients": 0, "arrays": 0, "accounts": 0, "bills": 0}

        for ci, cdef in enumerate(DEMO_CLIENTS):
            cname = _resolve_client_name(db, cdef["name"], spares)
            if cname in spares:
                spares.remove(cname)
            client = Client(
                tenant_id=DEMO_TENANT_ID,
                name=cname,
                contact_email=cdef["email"],
                report_frequency="quarterly",
                active=True,
                gmp_autopopulate=False,
                vec_autopopulate=False,
                last_delivery_at=DELIVERED_AT,
                created_at=FIRST_CONNECT,
            )
            db.add(client)
            db.flush()
            counts["clients"] += 1

            for ai, (aname, nepool, base_mwh, region, n_acc, provider) in enumerate(cdef["arrays"]):
                arr = Array(
                    tenant_id=DEMO_TENANT_ID,
                    client_id=client.id,
                    name=aname,
                    region=region,
                    nepool_gis_id=nepool,
                    bill_offset_months=1,
                    first_connect_date=FIRST_CONNECT,
                    created_at=FIRST_CONNECT,
                )
                db.add(arr)
                db.flush()
                counts["arrays"] += 1

                per_acct_base = base_mwh / n_acc
                for k in range(n_acc):
                    acct_seq += 1
                    acct = UtilityAccount(
                        tenant_id=DEMO_TENANT_ID,
                        array_id=arr.id,
                        provider=provider,
                        account_number=f"99{acct_seq:08d}",
                        customer_number=f"99{acct_seq:08d}",
                        nickname=f"{aname}" if n_acc == 1 else f"{aname} (meter {k + 1})",
                        enabled=True,
                        is_residential=False,
                    )
                    db.add(acct)
                    db.flush()
                    counts["accounts"] += 1

                    # Per-account deterministic seed (independent of DB ids).
                    seed_n = ci * 1000 + ai * 100 + k * 10 + 3
                    for (yy, mm) in months:
                        kwh = _month_kwh(per_acct_base, yy, mm, seed_n)
                        last = _last_day(yy, mm)
                        bill = Bill(
                            tenant_id=DEMO_TENANT_ID,
                            account_id=acct.id,
                            bill_date=datetime(yy, mm, last),
                            period_start=datetime(yy, mm, 1),
                            period_end=datetime(yy, mm, last),
                            billing_days=last,
                            kwh_generated=kwh,
                            kwh_consumed=0,
                            document_number=f"DEMO-{acct_seq:03d}-{yy}{mm:02d}",
                            parse_status="parsed",
                            pulled_at=DELIVERED_AT,
                        )
                        db.add(bill)
                        counts["bills"] += 1
                db.flush()

        db.commit()

    print("=== Demo tenant seeded ===")
    print(f"  tenant : {DEMO_TENANT_ID} ({DEMO_TENANT_NAME})")
    print(f"  clients: {counts['clients']}")
    print(f"  arrays : {counts['arrays']}")
    print(f"  accounts: {counts['accounts']}")
    print(f"  bills  : {counts['bills']}  ({MONTHS_OF_HISTORY} months each)")
    return counts


if __name__ == "__main__":
    seed()
    sys.exit(0)
