"""Seed the ANNA-SCALE demo tenant: 800 offtakers (Ford, 2026-07-02: "Anna has
eight hundred offtakers... create an 800-offtaker account demo for me to explore
so that I can control it and also test it").

This is the at-scale stress tenant for the Offtaker Invoice Generator — 8× the
largest fleet the system had ever held (96 subs on ten_demo_realistic). Shape
mirrors Anna's real world (GMP group net metering):

  • 26 community arrays, each with a HOST GMP account whose bills carry the
    array's GROUP excess (kwh_sent_to_grid) for 15 months (2025-04 → 2026-06);
  • 800 offtakers, EVERY one bound to their OWN GMP account whose bills carry
    their allocated excess (share × group), so every one is SENDABLE under the
    settled-bill gate (delivery.py: unbound offtakers never email, BY DESIGN);
  • realistic allocation shares: an anchor business + varied tail per array,
    4-decimal shares summing to 94–99% of each array (never > 100%);
  • ~1 in 12 offtakers has a RIGGED GMP bill (credited ≠ share × group) so the
    bill-accuracy check has a known, countable set of genuine catches — the
    invoice itself stays CORRECT (real-math bills share × group, the rig only
    shows as the audit ⚑);
  • edge coverage inside the 800 (all still sendable): 20 quarterly cadence,
    8 budget billing, 8 net-rate overrides, 8 discount overrides, 4 legacy flat
    rates, 4 multi-array (array_allocations + own account), ~40 sequential
    invoice numbering, no-email/to_me and to_both send modes;
  • PLUS a 27th "Guard Rail Demos" array with 8 offtakers BEYOND the 800, each
    named "DEMO-HOLD — ..." and deliberately in a refusal state (unbound,
    missing bill, disabled, quarterly hold, no-recipient) to prove the system
    correctly REFUSES to send those. enabled+sendable subs == exactly 800.

EMAIL SAFETY (nothing here can reach a real inbox):
  • every offtaker client_email  = delivered+anna800-oNNNN@resend.dev (Resend's
    official sink domain — accepted, never delivered anywhere);
  • every sub's operator_email   = delivered+anna800-op@resend.dev, which
    absorbs the operator BCC on every send AND the per-draft "ready to review"
    notes, so Ford's real inbox sees nothing it didn't ask for;
  • the tenant's contact_email (login) is ford.genereaux+anna800@gmail.com —
    it only ever receives what Ford deliberately triggers (e.g. test sends).

STRIPE SAFETY: plan/subscription_status = "comped", stripe ids None — the usage
reporter targets active+stripe-linked tenants only, so nothing here can ever
touch a live meter. RATE-SCHEDULE SAFETY: every seeded bill has raw_json=None,
so derive_blended_rate_from_bills (which scans bills GLOBALLY) can never fold
fake bills into real tenants' auto rates.

IDEMPOTENT + deterministic (no RNG): re-running wipes and rebuilds this tenant
via api.seed_demo._wipe. Fingerprints: tenant ten_anna_800, accounts
80########/81########, document numbers ANNA800-*, InverterDaily.source
='anna800_seed'.

Run on Railway prod:
    railway ssh "cd /app && python scripts/seed_anna_800.py"
Wipe only:
    railway ssh "cd /app && python scripts/seed_anna_800.py --wipe-only"
"""
from __future__ import annotations

import os
import secrets
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.db import SessionLocal, init_db
from api.models import (
    Tenant, Client, Array, UtilityAccount, Bill,
    BillingReportSubscription, Inverter, InverterDaily, local_today,
)
from api.seed_demo import _wipe, _MINI_PDF

TENANT_ID = "ten_anna_800"
TENANT_NAME = "Anna Scale Demo — 800 Offtakers"
COMPANY_NAME = "Sunrise Commons Energy (Demo)"
CONTACT_EMAIL = "ford.genereaux+anna800@gmail.com"
DEMO_PASSWORD = "Anna800Scale!"          # printed for Ford; bcrypt-hashed below
SINK = "resend.dev"                       # Resend's official test sink domain
OPERATOR_SINK = f"delivered+anna800-op@{SINK}"

FIRST_MONTH = (2025, 4)                   # 15 months of history → 2026-06
LAST_MONTH = (2026, 6)
TELEMETRY_DAYS = 21

# Per-array offtaker counts — MUST sum to exactly 800 (asserted).
COUNTS = [75, 68, 62, 58, 55, 50, 47, 42, 40, 36, 33, 30, 28, 26, 24,
          22, 20, 18, 16, 13, 11, 9, 8, 6, 2, 1]

ARRAY_NAMES = [
    "Kettle Brook Solar", "Sugarhouse Ridge", "Codding Hollow", "Foggy Meadow",
    "Beaver Pond Commons", "Cider Hill", "Quarry Road Solar", "Stonewall Orchard",
    "Birchwood Commons", "Tamarack Flats", "Gale Meadows", "Hollister Hill",
    "Peacham Corner", "Greensboro Bend", "Adamant Village", "Maidstone Lake",
    "Somerset Reservoir", "Bakersfield Ridge", "Chittenden Basin", "Halifax Gorge",
    "Jamaica Depot", "Ludlow Mills", "Norton Pond", "Orwell Landing",
    "Panton Shore", "Ripton Hollow",
]
GUARD_ARRAY_NAME = "Guard Rail Demos"

SEASONAL = [0.42, 0.55, 0.82, 1.06, 1.22, 1.31, 1.30, 1.18, 0.98, 0.74, 0.48, 0.38]

FIRSTS = [
    "Abigail", "Amos", "Bea", "Calvin", "Celia", "Dana", "Eli", "Esther",
    "Frank", "Greta", "Harlan", "Ida", "Jonas", "June", "Kip", "Lena",
    "Mabel", "Nate", "Opal", "Perry", "Quinn", "Rosa", "Silas", "Tess",
    "Uri", "Vera", "Wade", "Willa", "Amara", "Boone", "Clara", "Dexter",
    "Edith", "Ford", "Gwen", "Hollis", "Iris", "Jasper", "Kendra", "Lowell",
    "Marge", "Nora", "Orin", "Prue", "Reid", "Sadie", "Thea", "Vaughn",
]
LASTS = [
    "Aldrich", "Bemis", "Chittenden", "Dunbar", "Eastman", "Fassett", "Goss",
    "Hazen", "Isham", "Jewett", "Kingsbury", "Ladd", "Marsh", "Nutting",
    "Orcutt", "Peck", "Quimby", "Royce", "Stannard", "Thatcher", "Underwood",
    "Vail", "Wheelock", "Yandow", "Ainsworth", "Bugbee", "Cushman", "Delano",
    "Emerson", "Farnham", "Gleason", "Hubbard", "Ives", "Joslin", "Kelton",
    "Lamson", "Morrill", "Nichols", "Osgood", "Prentiss", "Ranney", "Safford",
    "Tupper", "Utley", "Varnum", "Wainwright", "Alden", "Bixby", "Colburn",
    "Dorr", "Eaton", "Fletcher", "Gove", "Hatch", "Ingalls", "Jacobs",
    "Keeler", "Loomis", "Mead", "Newell", "Otis", "Paige", "Rublee", "Sawyer",
]
BIZ = [
    "Feed & Grain", "Hardware", "Creamery Cooperative", "Cider Works",
    "Timber Frames", "Maple Sugarhouse", "General Store", "Machine Shop",
    "Growers Collective", "Wool Mill", "Cold Storage", "Farm Supply",
    "Book Barn", "Granite Works", "Brewing Co", "Orchard Partners",
]
BIZ_TOWNS = [
    "Hartland", "Corinth", "Tunbridge", "Craftsbury", "Marshfield", "Wolcott",
    "Sutton", "Topsham", "Vershire", "Strafford", "Roxbury", "Fayston",
    "Belvidere", "Granby", "Stannard", "Goshen",
]

VENDORS = ["solaredge", "fronius", "sma", "chint"]
MODELS = {
    "solaredge": ["SE10000H-US", "SE17.3K-US", "SE33.3K-US"],
    "fronius":   ["Symo 10.0-3", "Symo 15.0-3", "Symo 24.0-3"],
    "sma":       ["Sunny Tripower 12.0", "Sunny Tripower 25.0", "Core1 50.0"],
    "chint":     ["CPS SCA25KTL", "CPS SCA36KTL", "CPS SCA50KTL"],
}


def _months() -> list[tuple[int, int]]:
    out = []
    y, m = FIRST_MONTH
    while (y, m) <= LAST_MONTH:
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _last_day(yy: int, mm: int) -> int:
    return 31 if mm == 12 else (date(yy, mm + 1, 1) - timedelta(days=1)).day


def _rate(ai: int, yy: int, mm: int, vintage: int) -> float:
    """Deterministic GMP-ish net-metering credit rate: vintage base + a small
    monthly wiggle + a Jan-2026 step, ~0.148-0.19 $/kWh."""
    base = 0.148 + (vintage - 2013) * 0.003
    wiggle = ((mm * 13 + ai * 7) % 5 - 2) * 0.0004
    step = 0.004 if yy >= 2026 else 0.0
    return round(base + wiggle + step, 5)


def _jit(ai: int, yy: int, mm: int) -> float:
    """±6% deterministic monthly jitter."""
    return (((ai * 31 + mm * 17 + yy * 7) % 13) - 6) / 100.0


def _group_excess(ai: int, n_members: int, yy: int, mm: int) -> float:
    per_member_mid = 700 + (ai * 37) % 220          # 700-920 kWh/member mid-season
    group_mid = n_members * per_member_mid
    return round(group_mid * SEASONAL[mm - 1] * (1.0 + _jit(ai, yy, mm)), 1)


def _shares(ai: int, n: int) -> list[float]:
    """Realistic 4-decimal allocation shares summing to the array's fill target
    (94-99%): an anchor business slot, a large second, then a varied tail."""
    fill = round(0.94 + (ai % 6) * 0.01, 2)
    weights = []
    for i in range(n):
        if i == 0 and n >= 20:
            w = 6.0
        elif i == 1 and n >= 40:
            w = 3.5
        else:
            w = 1.0 + ((i * 7 + ai * 3) % 12) / 10.0
        weights.append(w)
    tot = sum(weights)
    shares = [round(w / tot * fill, 4) for w in weights]
    # Land the sum EXACTLY on fill by absorbing rounding residue in the anchor.
    shares[0] = round(shares[0] + (fill - sum(shares)), 4)
    assert abs(sum(shares) - fill) < 1e-9 and all(s > 0 for s in shares)
    return shares


def _offtaker_name(g: int, ai: int, oi: int, seen: set[str]) -> str:
    """Deterministic realistic VT names; anchors are businesses."""
    if oi == 0 and COUNTS[ai] >= 20:
        nm = f"{BIZ_TOWNS[(ai * 5 + g) % len(BIZ_TOWNS)]} {BIZ[(g * 3) % len(BIZ)]}"
    elif g % 9 == 4:
        nm = f"{BIZ_TOWNS[(g * 7) % len(BIZ_TOWNS)]} {BIZ[(g * 11) % len(BIZ)]} LLC"
    elif g % 7 == 2:
        nm = f"{LASTS[(g * 13) % len(LASTS)]} Family Trust"
    else:
        nm = f"{FIRSTS[(g * 17) % len(FIRSTS)]} {LASTS[(g * 5) % len(LASTS)]}"
    while nm in seen:
        nm = nm + " II"
    seen.add(nm)
    return nm


def seed(wipe_only: bool = False) -> dict:
    init_db()
    from api.account import _hash_password

    assert sum(COUNTS) == 800, f"COUNTS sums to {sum(COUNTS)}, expected 800"
    assert len(COUNTS) == len(ARRAY_NAMES)
    months = _months()
    now_ts = datetime.utcnow()
    today = local_today()

    with SessionLocal() as db:
        _wipe(db, TENANT_ID)
        if wipe_only:
            print(f"=== {TENANT_ID} wiped ===")
            return {"ok": True, "wiped": True}

        t = Tenant(
            id=TENANT_ID, tenant_key="sol_live_" + secrets.token_urlsafe(24),
            name=TENANT_NAME, company_name=COMPANY_NAME,
            operator_name="Anna (Demo)",
            contact_email=CONTACT_EMAIL, active=True,
            product="array_operator", billing_plan="both",
            plan="comped", subscription_status="comped",
            is_demo=False,                      # REAL mutable tenant for Ford
        )
        t.password_hash = _hash_password(DEMO_PASSWORD)
        t.onboarding_stage = "done"
        t.onboarding_token = None
        t.stripe_customer_id = None
        t.stripe_subscription_id = None
        t.trial_ends_at = None
        db.add(t)
        db.flush()

        counts = {"arrays": 0, "accounts": 0, "bills": 0, "subs": 0,
                  "rigged": 0, "quarterly": 0, "budget": 0, "net_override": 0,
                  "discount_override": 0, "legacy_flat": 0, "multi_array": 0,
                  "sequential_invoice": 0, "inverters": 0, "guard_demos": 0}
        seen_names: set[str] = set()
        g = 0                       # global offtaker index 0..799
        array_ids: list[int] = []

        for ai, (aname, n_members) in enumerate(zip(ARRAY_NAMES, COUNTS)):
            vintage = 2013 + (ai % 12)
            arr = Array(tenant_id=TENANT_ID, name=aname, region="VT",
                        first_connect_date=datetime(vintage, 5, 1),
                        nepool_gis_id=f"88{ai:03d}")
            db.add(arr)
            db.flush()
            array_ids.append(arr.id)
            counts["arrays"] += 1

            # Host GMP account: the array's group-excess bill, one per month.
            host = UtilityAccount(
                tenant_id=TENANT_ID, provider="gmp", array_id=arr.id,
                account_number=f"81{ai:08d}", customer_number=f"81{ai:08d}",
                nickname=f"{aname} (host)", enabled=True, is_residential=False,
                service_address={"line1": f"{100 + ai * 7} Solar Farm Rd, "
                                          f"{BIZ_TOWNS[ai % len(BIZ_TOWNS)]}, VT"},
            )
            db.add(host)
            db.flush()
            counts["accounts"] += 1
            excess_by_month: dict[tuple[int, int], float] = {}
            rate_by_month: dict[tuple[int, int], float] = {}
            for (yy, mm) in months:
                ex = _group_excess(ai, n_members, yy, mm)
                rt = _rate(ai, yy, mm, vintage)
                excess_by_month[(yy, mm)] = ex
                rate_by_month[(yy, mm)] = rt
                last = _last_day(yy, mm)
                db.add(Bill(
                    tenant_id=TENANT_ID, account_id=host.id,
                    bill_date=datetime(yy, mm, last),
                    period_start=datetime(yy, mm, 1),
                    period_end=datetime(yy, mm, last), billing_days=last,
                    kwh_generated=int(round(ex * 1.06)),
                    kwh_consumed=int(round(ex * 0.06)),
                    kwh_sent_to_grid=ex,
                    solar_credit_usd=round(ex * rt, 2),
                    avg_rate_cents_kwh=round(rt * 100, 3),
                    is_net_metered=True, parse_status="parsed",
                    document_number=f"ANNA800-H{ai:03d}-{yy}{mm:02d}",
                    pdf_bytes=_MINI_PDF, pdf_content_type="application/pdf",
                ))
                counts["bills"] += 1

            shares = _shares(ai, n_members)
            for oi in range(n_members):
                share = shares[oi]
                oname = _offtaker_name(g, ai, oi, seen_names)
                rigged = (g % 12 == 5)
                quarterly = (g % 40 == 7)
                budget = (g % 100 == 11)
                net_override = (g % 100 == 23)
                discount_override = (g % 100 == 47)
                legacy_flat = (g % 200 == 63)
                multi = (g % 200 == 87)          # 4 multi-array (own acct too)
                sequential = (ai in (2, 6))      # two cohorts number sequentially
                no_email = (g % 197 == 97)       # 4-ish: operator-only sends
                mode = ("to_me" if no_email or g % 10 == 8
                        else ("to_both" if g % 20 == 13 else "to_client"))

                acct = UtilityAccount(
                    tenant_id=TENANT_ID, provider="gmp",
                    account_number=f"80{g:08d}", customer_number=f"80{g:08d}",
                    nickname=oname, enabled=True, is_residential=(g % 9 != 4),
                    service_address={"line1": f"{10 + (g * 13) % 980} "
                                              f"{LASTS[(g * 5) % len(LASTS)]} Rd, "
                                              f"{BIZ_TOWNS[g % len(BIZ_TOWNS)]}, VT"},
                )
                db.add(acct)
                db.flush()
                counts["accounts"] += 1

                for (yy, mm) in months:
                    ex_m = excess_by_month[(yy, mm)]
                    rt_m = rate_by_month[(yy, mm)]
                    their = round(share * ex_m, 1)
                    credited = (round(their + 6.0 + their * 0.004, 1)
                                if rigged else their)
                    last = _last_day(yy, mm)
                    db.add(Bill(
                        tenant_id=TENANT_ID, account_id=acct.id,
                        bill_date=datetime(yy, mm, last),
                        period_start=datetime(yy, mm, 1),
                        period_end=datetime(yy, mm, last), billing_days=last,
                        kwh_generated=int(round(credited)),
                        kwh_consumed=int(round(credited * 0.12)),
                        kwh_sent_to_grid=credited,
                        solar_credit_usd=round(credited * rt_m, 2),
                        avg_rate_cents_kwh=round(rt_m * 100, 3),
                        is_net_metered=True, parse_status="parsed",
                        document_number=f"ANNA800-{g:04d}-{yy}{mm:02d}",
                        pdf_bytes=_MINI_PDF, pdf_content_type="application/pdf",
                    ))
                    counts["bills"] += 1

                c = Client(tenant_id=TENANT_ID, name=oname, active=True,
                           contact_email=f"delivered+anna800-o{g:04d}@{SINK}")
                db.add(c)
                db.flush()

                sub = BillingReportSubscription(
                    tenant_id=TENANT_ID, client_id=c.id, customer_name=oname,
                    array_id=arr.id, allocation_pct=1.0, array_share_pct=share,
                    utility_account_id=acct.id,
                    billing_model="percent_of_array",
                    cadence="quarterly" if quarterly else "monthly",
                    delivery_mode="approval" if g % 2 == 0 else "auto",
                    send_mode=mode,
                    client_email=(None if no_email
                                  else f"delivered+anna800-o{g:04d}@{SINK}"),
                    operator_email=OPERATOR_SINK,
                    formats=["pdf"], include_summary=False,
                    auto_attach_gmp=True, enabled=True,
                )
                if budget:
                    sub.budget_amount_usd = float(40 + (g % 7) * 12)
                    counts["budget"] += 1
                if net_override:
                    sub.net_rate_per_kwh = round(0.16 + (g % 4) * 0.005, 4)
                    sub.discount_pct = round(0.05 + (g % 3) * 0.05, 2)
                    counts["net_override"] += 1
                elif discount_override:
                    sub.discount_pct = 0.15
                    counts["discount_override"] += 1
                elif legacy_flat:
                    sub.rate_per_kwh = 0.145
                    counts["legacy_flat"] += 1
                if multi and ai + 1 < len(ARRAY_NAMES):
                    # Own GMP account still drives the INVOICE (bill-bound path
                    # wins); the allocations document the cross-array holding.
                    sub.array_allocations = [
                        {"array_id": arr.id, "allocation_pct": round(share * 0.7, 4)},
                        {"array_id": arr.id + 1, "allocation_pct": round(share * 0.3, 4)},
                    ]
                    counts["multi_array"] += 1
                if sequential:
                    sub.invoice_number_start = 1000 + g * 10
                    sub.invoice_number_next = 1000 + g * 10
                    counts["sequential_invoice"] += 1
                if rigged:
                    counts["rigged"] += 1
                if quarterly:
                    counts["quarterly"] += 1
                db.add(sub)
                counts["subs"] += 1
                g += 1

            # Synthetic vendor inverters so Fleet Health / Inverter Dashboard /
            # forecasting see this fleet (utility-meter-only arrays are invisible
            # there by design — same lesson as seed_ford_demo_100).
            per_member_mid = 700 + (ai * 37) % 220
            array_kw = round(n_members * per_member_mid / 30.0 / 3.5, 1)
            n_inv = 3 + (ai % 5)
            per_inv_kw = round(array_kw / n_inv, 2)
            vendor = VENDORS[ai % len(VENDORS)]
            model = MODELS[vendor][ai % 3]
            invs = []
            for ii in range(n_inv):
                inv = Inverter(
                    tenant_id=TENANT_ID, array_id=arr.id, position=ii,
                    vendor=vendor, serial=f"ANNA800-{ai:03d}-{ii + 1}",
                    source_site_id=f"81{ai:08d}", source_array_id=arr.id,
                    name=f"{aname} ({ii + 1})", model=model,
                    nameplate_kw=per_inv_kw, last_seen_at=now_ts,
                    last_power_w=round(per_inv_kw * 1000
                                       * (0.55 + ((ai + ii) % 30) / 100.0)),
                    last_power_at=now_ts, source_last_data_at=now_ts,
                    created_at=datetime(vintage, 5, 1),
                )
                db.add(inv)
                db.flush()
                invs.append(inv)
            daily_base = n_members * per_member_mid / 30.0
            for d in range(TELEMETRY_DAYS):
                day = today - timedelta(days=d)
                factor = SEASONAL[day.month - 1]
                for ii, inv in enumerate(invs):
                    jj = (((ai * 13 + ii * 7 + d * 5) % 21) - 10) / 100.0
                    kwh = max(0.0, round((daily_base / n_inv) * factor * (1 + jj), 2))
                    db.add(InverterDaily(tenant_id=TENANT_ID, inverter_id=inv.id,
                                         day=day, kwh=kwh, source="anna800_seed"))
            counts["inverters"] += n_inv
            db.flush()

        assert g == 800, f"seeded {g} offtakers, expected 800"

        # ── Guard Rail Demos: 8 labeled refusal-state offtakers BEYOND the 800 ──
        pen = Array(tenant_id=TENANT_ID, name=GUARD_ARRAY_NAME, region="VT",
                    first_connect_date=datetime(2020, 5, 1))
        db.add(pen)
        db.flush()
        pen_host = UtilityAccount(tenant_id=TENANT_ID, provider="gmp",
                                  array_id=pen.id, account_number="8299999999",
                                  nickname=f"{GUARD_ARRAY_NAME} (host)",
                                  enabled=True, is_residential=False)
        db.add(pen_host)
        db.flush()
        for (yy, mm) in months:
            ex = _group_excess(26, 10, yy, mm)
            rt = _rate(26, yy, mm, 2020)
            last = _last_day(yy, mm)
            db.add(Bill(tenant_id=TENANT_ID, account_id=pen_host.id,
                        bill_date=datetime(yy, mm, last),
                        period_start=datetime(yy, mm, 1),
                        period_end=datetime(yy, mm, last), billing_days=last,
                        kwh_generated=int(round(ex * 1.06)), kwh_sent_to_grid=ex,
                        solar_credit_usd=round(ex * rt, 2), is_net_metered=True,
                        parse_status="parsed",
                        document_number=f"ANNA800-PEN-{yy}{mm:02d}",
                        pdf_bytes=_MINI_PDF, pdf_content_type="application/pdf"))
            counts["bills"] += 1

        def _pen_sub(name: str, **kw) -> BillingReportSubscription:
            s = BillingReportSubscription(
                tenant_id=TENANT_ID, customer_name=f"DEMO-HOLD — {name}",
                billing_model="percent_of_array", cadence="monthly",
                delivery_mode="approval", send_mode="to_client",
                client_email=f"delivered+anna800-pen@{SINK}",
                operator_email=OPERATOR_SINK, formats=["pdf"],
                include_summary=False, auto_attach_gmp=True, enabled=True,
            )
            for k, v in kw.items():
                setattr(s, k, v)
            db.add(s)
            counts["guard_demos"] += 1
            counts["subs"] += 1
            return s

        # 1-2: unbound (array-linked only) → gate refuses: never sendable.
        _pen_sub("Unbound offtaker (no GMP account)", array_id=pen.id,
                 allocation_pct=0.05)
        _pen_sub("Unbound offtaker 2 (no GMP account)", array_id=pen.id,
                 allocation_pct=0.03)
        # 3: multi-array WITHOUT an own account → telemetry mix → gate refuses.
        _pen_sub("Multi-array, no own GMP account",
                 array_allocations=[
                     {"array_id": pen.id, "allocation_pct": 0.02},
                     {"array_id": array_ids[0], "allocation_pct": 0.01}])
        # 4: bound to a GMP account with NO bill landed at all → waiting. (NOTE:
        # an account whose bills merely STOP at April is SENDABLE by design —
        # the pipeline invoices the latest settled bill; verified at seed time.)
        stale = UtilityAccount(tenant_id=TENANT_ID, provider="gmp",
                               account_number="8290000001",
                               nickname="DEMO-HOLD no-bill account",
                               enabled=True, is_residential=True)
        db.add(stale)
        db.flush()
        counts["accounts"] += 1
        _pen_sub("Bound, no bill landed yet", array_id=pen.id,
                 allocation_pct=1.0, array_share_pct=0.04,
                 utility_account_id=stale.id)
        # 5: quarterly with a MID-quarter gap (April + June, May missing) → held.
        # (May+June-only would read as a mid-quarter service START and send, by
        # design — the April bill makes the May hole a genuine gap.)
        qacct = UtilityAccount(tenant_id=TENANT_ID, provider="gmp",
                               account_number="8290000002",
                               nickname="DEMO-HOLD quarterly-gap account",
                               enabled=True, is_residential=True)
        db.add(qacct)
        db.flush()
        counts["accounts"] += 1
        for (yy, mm) in [(2026, 4), (2026, 6)]:
            rt = _rate(26, yy, mm, 2020)
            kw = round(0.03 * _group_excess(26, 10, yy, mm), 1)
            last = _last_day(yy, mm)
            db.add(Bill(tenant_id=TENANT_ID, account_id=qacct.id,
                        bill_date=datetime(yy, mm, last),
                        period_start=datetime(yy, mm, 1),
                        period_end=datetime(yy, mm, last),
                        kwh_generated=int(round(kw)), kwh_sent_to_grid=kw,
                        solar_credit_usd=round(kw * rt, 2), is_net_metered=True,
                        parse_status="parsed",
                        document_number=f"ANNA800-QGAP-{yy}{mm:02d}",
                        pdf_bytes=_MINI_PDF, pdf_content_type="application/pdf"))
            counts["bills"] += 1
        _pen_sub("Quarterly held (April bill missing)", array_id=pen.id,
                 allocation_pct=1.0, array_share_pct=0.03,
                 utility_account_id=qacct.id, cadence="quarterly")
        # 6-7: disabled subs (scheduler never touches them).
        _pen_sub("Disabled offtaker", array_id=pen.id, allocation_pct=0.02,
                 enabled=False)
        _pen_sub("Disabled offtaker 2", array_id=pen.id, allocation_pct=0.02,
                 enabled=False)
        # 8: to_client with NO email → resolve_recipients problem surfaces.
        _pen_sub("No recipient (to_client, no email)", array_id=pen.id,
                 allocation_pct=0.02, client_email=None)

        db.commit()

    # Safety net: no seeded recipient outside the sink domain.
    with SessionLocal() as db:
        from sqlalchemy import select as _sel
        bad = [
            (s.id, s.client_email, s.operator_email)
            for s in db.execute(_sel(BillingReportSubscription).where(
                BillingReportSubscription.tenant_id == TENANT_ID)).scalars()
            if (s.client_email and not s.client_email.endswith("@" + SINK))
            or (s.operator_email and not s.operator_email.endswith("@" + SINK))
        ]
        assert not bad, f"non-sink recipients seeded: {bad[:5]}"

    print("=== Anna Scale Demo (800 offtakers) seeded ===")
    print(f"  tenant        : {TENANT_ID}")
    print(f"  login email   : {CONTACT_EMAIL}")
    print(f"  login password: {DEMO_PASSWORD}")
    for k, v in counts.items():
        print(f"  {k:<18}: {v}")
    print(f"  months        : {len(months)}  ({FIRST_MONTH} → {LAST_MONTH})")
    print(f"  sendable subs : {counts['subs'] - counts['guard_demos']} (must be 800)")
    return counts


if __name__ == "__main__":
    seed(wipe_only="--wipe-only" in sys.argv)
    sys.exit(0)
