"""Realistic demo-data seed (Ford's ask, 2026-07-01): a large Array Operator
tenant with REAL-SHAPED GMP bills so the whole offtaker pipeline is testable end
to end — invoices with real amounts, the bill-accuracy allocation check (with
deliberately rigged errors to catch), the invoice archive, and the QB/Xero
export. The demo account's data was "fabricated" (arrays but no bills), so
nothing downstream lit up; this seeds proper bills so it all does.

Model (matches the real system + the array_share_pct fix):
  • each ARRAY has a host GMP account whose bill carries the GROUP excess;
  • each OFFTAKER is bound to their OWN GMP account (bill = their allocated
    excess), allocation_pct=1.0 (billed on their full allocation),
    array_share_pct = their GMP share of the array (the audit input);
  • ~1 in 6 offtakers gets a RIGGED allocation (GMP-credited != share × group)
    so the $25 accuracy check has genuine catches to surface.

Idempotent: re-seeding WIPES the fixed demo tenant and rebuilds it. Deterministic
(index-driven variation, no RNG) so the same call yields the same account.
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from .db import SessionLocal
from .models import (Tenant, Array, UtilityAccount, Bill, Client,
                     BillingReportSubscription, ReportDelivery, ReportDraft,
                     DailyGeneration, now)

DEMO_TENANT_ID = "ten_demo_realistic"
DEMO_EMAIL = "demo-realistic@energyagent-demo.com"
DEMO_PASSWORD = "SolarDemo2026!"
DEMO_COMPANY = "Green Valley Community Solar (Demo)"

# A minimal but openable one-page PDF, reused as every bill's stored bytes so the
# invoice archive has real files to list + zip (placeholder content, clearly a demo).
_MINI_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 120]/Resources"
    b"<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 58>>stream\n"
    b"BT /F1 12 Tf 20 60 Td (Demo GMP bill - placeholder) Tj ET\n"
    b"endstream endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

_ARRAY_NAMES = [
    "Timberworks", "Fair Haven", "Stratton Ridge", "Maple Grove", "Otter Creek",
    "Mad River", "Green Mountain", "Waterbury Flats", "Danville Ridge",
    "Londonderry", "Tannery Brook", "Waterford", "Chester Commons", "Bristol Cliffs",
    "Middlebury Gap", "Norwich Meadows", "Randolph Center", "Barre Quarry",
    "Shelburne Bay", "Montpelier Hill",
]
_OFFTAKER_STEMS = [
    "St. Johnsbury Municipal", "Fair Haven School District", "Green Mtn Coffee",
    "Vermont Creamery", "Cabot Cooperative", "Rutland Regional Medical",
    "Middlebury College", "Ben & Jerry's Waterbury", "Killington Resort",
    "Northfield Savings", "Vermont Country Store", "Gardener's Supply",
    "Seventh Generation", "King Arthur Baking", "Burton Corp", "Darn Tough",
    "Vermont Teddy Bear", "Simon Pearce", "Cabot Annex", "Shelburne Farms",
]

_PERIOD_START = datetime(2026, 6, 1)
_PERIOD_END = datetime(2026, 6, 30)


def _wipe(db, tid: str) -> None:
    """FK-safe bulk delete of every tenant-scoped row for a re-seed."""
    for model in (Bill, BillingReportSubscription, ReportDelivery, ReportDraft,
                  DailyGeneration, UtilityAccount, Array, Client):
        db.query(model).filter(model.tenant_id == tid).delete(synchronize_session=False)
    t = db.get(Tenant, tid)
    if t is not None:
        db.delete(t)
    db.commit()


def seed_realistic_demo(arrays: int = 12, offtakers_per_array: int = 5) -> dict:
    """(Re)build the realistic demo tenant. Returns a summary + login creds."""
    from .account import _hash_password
    arrays = max(1, min(int(arrays), len(_ARRAY_NAMES)))
    offtakers_per_array = max(1, min(int(offtakers_per_array), 8))

    with SessionLocal() as db:
        _wipe(db, DEMO_TENANT_ID)

        t = Tenant(id=DEMO_TENANT_ID, tenant_key="demo_realistic_key",
                   name=DEMO_COMPANY, company_name=DEMO_COMPANY,
                   contact_email=DEMO_EMAIL, active=True, product="array_operator",
                   subscription_status="trialing")
        t.password_hash = _hash_password(DEMO_PASSWORD)
        db.add(t)
        db.flush()

        n_offtakers = 0
        n_rigged = 0
        overall = 0
        for ai in range(arrays):
            name = _ARRAY_NAMES[ai]
            # Vary vintage 2013..2024 so age→rate exercises both Rate#1 and Blended.
            commission_year = 2013 + (ai % 12)
            arr = Array(tenant_id=DEMO_TENANT_ID, name=name, region="VT",
                        first_connect_date=datetime(commission_year, 5, 1))
            db.add(arr)
            db.flush()
            # Host account + bill carrying the array's GROUP excess (kwh_sent_to_grid).
            group_excess = float(18000 + (ai * 1873) % 22000)   # 18k..40k, deterministic
            rate = round(0.140 + (ai % 5) * 0.009, 5)           # 0.140..0.176
            host = UtilityAccount(tenant_id=DEMO_TENANT_ID, provider="gmp",
                                  account_number=f"HOST-{ai:03d}", array_id=arr.id,
                                  nickname=f"{name} (host)")
            db.add(host)
            db.flush()
            db.add(Bill(tenant_id=DEMO_TENANT_ID, account_id=host.id,
                        period_start=_PERIOD_START, period_end=_PERIOD_END,
                        kwh_generated=int(group_excess * 1.02),
                        kwh_sent_to_grid=group_excess,
                        solar_credit_usd=round(group_excess * rate, 2),
                        is_net_metered=True, pdf_bytes=_MINI_PDF,
                        pdf_content_type="application/pdf"))

            # Offtakers: shares summing to ~0.95 of the array.
            share_pool = 0.95
            base_share = round(share_pool / offtakers_per_array, 4)
            for oi in range(offtakers_per_array):
                stem = _OFFTAKER_STEMS[(ai * 3 + oi) % len(_OFFTAKER_STEMS)]
                oname = f"{stem} #{ai+1}.{oi+1}"
                share = base_share
                clean_credit = round(group_excess * share, 1)
                # Rig ~1 in 6 with an allocation GMP got "wrong" (a base on neither
                # bill) so the accuracy check has a real catch.
                rigged = ((ai * offtakers_per_array + oi) % 6 == 0)
                credited = clean_credit + (round(clean_credit * 0.004, 1) + 6.0) if rigged else clean_credit
                if rigged:
                    n_rigged += 1

                acct = UtilityAccount(tenant_id=DEMO_TENANT_ID, provider="gmp",
                                      account_number=f"OFF-{ai:03d}-{oi:02d}",
                                      nickname=oname)
                db.add(acct)
                db.flush()
                db.add(Bill(tenant_id=DEMO_TENANT_ID, account_id=acct.id,
                            period_start=_PERIOD_START, period_end=_PERIOD_END,
                            # kwh_generated makes the bill count as a SETTLED bill so
                            # it shows in the accounts list (has_bill) AND the review
                            # sweep (_utility_bill_period_kwh) sees it. The offtaker's
                            # allocated generation ~= what they were credited.
                            kwh_generated=int(round(credited)),
                            kwh_consumed=int(round(credited * 0.15)),
                            kwh_sent_to_grid=credited,
                            solar_credit_usd=round(credited * rate, 2),
                            is_net_metered=True, pdf_bytes=_MINI_PDF,
                            pdf_content_type="application/pdf"))
                c = Client(tenant_id=DEMO_TENANT_ID, name=oname, active=True)
                db.add(c)
                db.flush()
                db.add(BillingReportSubscription(
                    tenant_id=DEMO_TENANT_ID, client_id=c.id, customer_name=oname,
                    array_id=arr.id, allocation_pct=1.0, array_share_pct=share,
                    utility_account_id=acct.id, billing_model="percent_of_array",
                    cadence="monthly", enabled=True))
                n_offtakers += 1
        db.commit()

    return {
        "ok": True,
        "tenant_id": DEMO_TENANT_ID,
        "login_email": DEMO_EMAIL,
        "login_password": DEMO_PASSWORD,
        "arrays": arrays,
        "offtakers": n_offtakers,
        "rigged_allocation_errors": n_rigged,
        "note": "Log in at arrayoperator.com with the email+password above. The "
                "Offtaker Invoice Generator's Bill accuracy check should flag the "
                f"{n_rigged} rigged allocation(s); the archive + QB/Xero export are "
                "populated for June 2026.",
    }
