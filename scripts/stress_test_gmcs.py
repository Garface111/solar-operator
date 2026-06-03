"""Stress test: create a synthetic tenant with many arrays + many accounts
(some 1:1, some multi-meter), populate 18 months of realistic generation
data, build the GMCS workbook, and email it to Ford for visual review.

Run on Railway:
  railway ssh "cd /app && python -m scripts.stress_test_gmcs"
"""
from __future__ import annotations
import os, sys, secrets, tempfile, math, random
from pathlib import Path
from datetime import datetime, date

from api.db import SessionLocal, init_db
from api.models import Tenant, Array, UtilityAccount, Bill
from api.writers.gmcs_writer import build_workbook
from api.notify import send_workbook_email

TID = "ten_stress_gmcs_demo"
TARGET = os.getenv("REVIEW_TO", "ford.genereaux@gmail.com")

# 15 arrays, mixed sizes & meter configurations
# (array_name, nepool_id, kw_capacity, num_sub_meters, town)
ARRAYS = [
    ("Bennington Town Farm",     "B-10044", 500, 2, "Bennington"),
    ("Burlington South Solar",   "BS-22871", 250, 1, "Burlington"),
    ("Charlotte Meadow Array",   "CM-31298", 150, 1, "Charlotte"),
    ("Colchester Reservoir",     "CR-44190", 400, 3, "Colchester"),
    ("Dorset Hollow Solar",      "DH-50037", 100, 1, "Dorset"),
    ("Essex Junction Community", "EJ-61283", 300, 2, "Essex Junction"),
    ("Ferrisburgh Array A",      "FR-72441", 500, 1, "Ferrisburgh"),
    ("Hardwick Cooperative",     "HW-83012", 200, 1, "Hardwick"),
    ("Middlebury College Solar", "MC-91174", 750, 4, "Middlebury"),
    ("Montpelier Capitol Array", "MP-10299", 350, 2, "Montpelier"),
    ("Norwich Riverside",        "NR-11833", 175, 1, "Norwich"),
    ("Putney Hilltop",           "PH-12557", 125, 1, "Putney"),
    ("Rutland West Solar",       "RW-13701", 600, 3, "Rutland"),
    ("Shelburne Bay Array",      "SB-14209", 225, 1, "Shelburne"),
    ("Williston Industrial",     "WI-15838", 450, 2, "Williston"),
]

# 18 months in chronological order — last 6 complete quarters before 2026-Q2 ref date
MONTHS = [
    (2024,7),(2024,8),(2024,9),(2024,10),(2024,11),(2024,12),
    (2025,1),(2025,2),(2025,3),(2025,4),(2025,5),(2025,6),
    (2025,7),(2025,8),(2025,9),(2025,10),(2025,11),(2025,12),
]

# VT solar PV capacity factor — typical monthly shape (% of nameplate
# realized over the month). Peaks in Jul, troughs in Dec.
MONTHLY_CF_PCT = {
    1: 7.5, 2: 9.0, 3: 13.0, 4: 15.5, 5: 17.0, 6: 17.5,
    7: 18.0, 8: 17.0, 9: 14.0, 10: 10.5, 11: 6.5, 12: 5.5,
}

def kwh_for_month(kw_capacity: int, year: int, month: int, rng: random.Random) -> int:
    """Realistic synthetic kWh: capacity_kw * (cf% * hours_in_month) with
    ±8% jitter so different arrays don't all read identically."""
    days = 31 if month in (1,3,5,7,8,10,12) else 30 if month != 2 else 28
    hours = days * 24
    cf = MONTHLY_CF_PCT[month] / 100.0
    jitter = 1 + rng.uniform(-0.08, 0.08)
    kwh = kw_capacity * cf * hours * jitter
    return int(round(kwh))


def main():
    init_db()
    rng = random.Random(20260603)  # deterministic for repro
    with SessionLocal() as db:
        # idempotent: nuke any prior stress tenant
        for t in db.query(Tenant).filter_by(id=TID).all():
            db.delete(t)
        db.commit()

        t = Tenant(
            id=TID,
            name="Northeast Solar Holdings (stress test)",
            contact_email=TARGET,
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped",
            active=True,
            subscription_status="comped",
            report_frequency="quarterly",
        )
        db.add(t); db.flush()

        n_arrays = 0
        n_accounts = 0
        n_bills = 0
        for (name, nepool, kw, meters, town) in ARRAYS:
            arr = Array(
                tenant_id=TID, name=name, nepool_gis_id=nepool,
                region=town, bill_offset_months=1,
            )
            db.add(arr); db.flush()
            n_arrays += 1

            # Split capacity across the configured number of sub-meters.
            per_meter_kw = kw // meters
            remainder = kw - per_meter_kw * meters
            for m_idx in range(meters):
                meter_kw = per_meter_kw + (remainder if m_idx == 0 else 0)
                acct_num = f"{nepool.replace('-','')}{m_idx:02d}"
                nick = name if meters == 1 else f"{name} M{m_idx+1}"
                acc = UtilityAccount(
                    tenant_id=TID, array_id=arr.id, provider="gmp",
                    account_number=acct_num, nickname=nick,
                )
                db.add(acc); db.flush()
                n_accounts += 1

                for (y, mo) in MONTHS:
                    kwh = kwh_for_month(meter_kw, y, mo, rng)
                    if kwh <= 0:
                        continue
                    db.add(Bill(
                        tenant_id=TID, account_id=acc.id,
                        bill_date=datetime(y, mo, 15),
                        period_start=datetime(y, mo, 1),
                        period_end=datetime(y, mo, 28),
                        billing_days=28,
                        kwh_generated=kwh,
                        document_number=f"{TID}-{acc.id}-{y}-{mo:02d}",
                        parse_status="parsed",
                    ))
                    n_bills += 1
        db.commit()
        print(f"Seeded: {n_arrays} arrays, {n_accounts} GMP accounts, {n_bills} bills")

    out_dir = Path(tempfile.mkdtemp(prefix="stress-gmcs-"))
    out_path = out_dir / "Northeast-Solar-Holdings-GMCS-stress.xlsx"
    path = build_workbook(TID, reference_date=date(2026, 2, 1), out_path=out_path)
    print(f"Built: {path}  ({path.stat().st_size} bytes)")

    sent = send_workbook_email(
        to=TARGET,
        subject="GMCS stress test — 15-array synthetic tenant",
        html=(
            "<p>Hi Ford,</p>"
            "<p>This is a fully synthetic stress test of the GMCS-format "
            "writer on a tenant that's nothing like Bruce's — 15 arrays, "
            "26 GMP sub-meters, 18 months of realistic generation data "
            "(uses VT solar capacity-factor curves + ±8% jitter, "
            "deterministic so the numbers are reproducible).</p>"
            "<ul>"
            "<li>15 sheets, one per array, in alphabetical order</li>"
            "<li>Mix of single-meter and multi-meter arrays (one is 4 meters summed)</li>"
            "<li>Sizes 100kW → 750kW</li>"
            "<li>Each has a synthetic NEPOOL ID so the (XX-XXXXX) suffix on the title renders</li>"
            "</ul>"
            "<p>Sanity checks to do:</p>"
            "<ul>"
            "<li>Each sheet renders cleanly with the same layout as Bruce's</li>"
            "<li>Multi-meter arrays sum correctly (Middlebury College has 4 meters @ ~187kW each → ~750kW total)</li>"
            "<li>Sheet names with long titles aren't truncated badly</li>"
            "<li>Numbers look like real VT solar (winter dips, summer peaks)</li>"
            "</ul>"
            "<p>— Solar Operator</p>"
        ),
        text=("Synthetic 15-array tenant. 26 sub-meters total. 18 months "
              "of realistic capacity-factor-modeled generation. Each sheet "
              "should look pixel-identical to Bruce's GMCS format."),
        workbook_path=str(path),
        filename="Northeast-Solar-Holdings-GMCS-stress.xlsx",
    )
    print(f"Email sent to {TARGET}: {sent}")

    # cleanup so the stress tenant doesn't sit in prod DB
    with SessionLocal() as db:
        for tt in db.query(Tenant).filter_by(id=TID).all():
            db.delete(tt)
        db.commit()
    print("Cleanup done.")
    sys.exit(0 if sent else 1)


if __name__ == "__main__":
    main()
