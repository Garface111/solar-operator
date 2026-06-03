"""One-shot: seed a smoke tenant, build the GMCS-format workbook with values
that match the source GMCS.xlsx exactly, email it to Ford for review, then
clean up the smoke tenant.

Run on Railway via:
  railway ssh "cd /app && python -m scripts.email_gmcs_sample"
"""
from datetime import datetime, date
from pathlib import Path
import secrets, tempfile, sys, os

from api.db import SessionLocal, init_db
from api.models import Tenant, Array, UtilityAccount, Bill
from api.writers.gmcs_writer import build_workbook
from api.notify import send_workbook_email

TID = "ten_gmcs_review_sample"
TARGET = os.getenv("REVIEW_TO", "ford.genereaux@dysonswarmtechnologies.com")

# Exact MWh values from /mnt/c/Users/fordg/Desktop/Solar Operator/GMCS.xlsx
GMCS = {
    ("Chester", "53984"): [28.468,24.852,25.720,24.825,17.275,12.673,15.672,16.116,22.690,24.632,20.973,26.745,29.210,29.730,25.723,22.675,12.528,11.620],
    ("Johnson - JSIS 150 Kw Farm", "JSIS"): [27.960,23.092,20.194,14.079,7.265,2.124,1.665,1.511,13.350,15.164,15.729,21.515,23.264,22.989,18.148,12.859,3.831,1.368],
    ("Londonderry", "98179"): [61.816,51.185,51.905,49.025,31.373,19.844,18.567,22.258,45.218,48.886,38.960,21.749,55.733,58.225,54.743,49.024,22.617,15.436],
    ("Tannery Brook", "46425"): [21.951,16.956,17.732,15.331,7.841,1.935,3.854,1.297,14.779,17.552,15.831,19.067,17.981,17.307,17.952,13.648,6.109,0.894],
    ("Timberworks", "61959"): [28.350,22.932,22.059,23.387,11.526,4.298,6.985,4.134,20.656,21.958,20.488,25.805,27.499,26.947,27.564,18.919,8.820,2.454],
    ("Waterford", "78671"): [28.752,23.255,25.599,20.856,9.955,4.027,11.349,9.894,20.811,21.855,20.862,25.699,27.663,28.218,26.351,18.167,7.721,3.370],
}

# 18 months in chronological order matching the GMCS source: Jul 2024 → Dec 2025
MONTHS = [
    (2024,7),(2024,8),(2024,9),(2024,10),(2024,11),(2024,12),
    (2025,1),(2025,2),(2025,3),(2025,4),(2025,5),(2025,6),
    (2025,7),(2025,8),(2025,9),(2025,10),(2025,11),(2025,12),
]

def main():
    init_db()
    with SessionLocal() as db:
        # idempotent cleanup of any previous run
        for t in db.query(Tenant).filter_by(id=TID).all():
            db.delete(t)
        db.commit()
        t = Tenant(id=TID, name="GMCS Reproduction Sample",
                   contact_email=TARGET,
                   tenant_key="sol_test_"+secrets.token_hex(8),
                   plan="comped", active=True,
                   subscription_status="comped",
                   report_frequency="quarterly")
        db.add(t); db.flush()
        for (aname, nid), vals in GMCS.items():
            arr = Array(tenant_id=TID, name=aname, nepool_gis_id=nid,
                        bill_offset_months=0)
            db.add(arr); db.flush()
            acct = UtilityAccount(tenant_id=TID, array_id=arr.id,
                                  provider="gmp", account_number=f"acct-{nid}",
                                  nickname=aname)
            db.add(acct); db.flush()
            for (y, m), mwh in zip(MONTHS, vals):
                kwh = int(round(mwh * 1000))
                db.add(Bill(tenant_id=TID, account_id=acct.id,
                            bill_date=datetime(y, m, 15),
                            period_start=datetime(y, m, 1),
                            period_end=datetime(y, m, 28),
                            billing_days=28, kwh_generated=kwh,
                            document_number=f"{TID}-{arr.id}-{y}-{m:02d}",
                            parse_status="parsed"))
        db.commit()

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "GMCS-reproduction-sample.xlsx"
        path = build_workbook(TID, reference_date=date(2026, 2, 1), out_path=out)
        print(f"Built: {path} ({path.stat().st_size} bytes)")
        sent = send_workbook_email(
            to=TARGET,
            subject="GMCS-format reproduction — review sample",
            html=(
                "<p>Hi Ford,</p>"
                "<p>Attached is the new GMCS-format workbook produced by the "
                "Solar Operator backend, populated with the exact values from "
                "your dad's master GMCS.xlsx so you can verify the layout matches "
                "cell-for-cell.</p>"
                "<p>Six sheets: Chester, Johnson, Londonderry, Tannery Brook, "
                "Timberworks, Waterford. Rolling 6 complete quarters "
                "(Q3 2024 — Q4 2025). MWh to 3 decimals; RECs = floor(MWh).</p>"
                "<p>Once you confirm it looks right, this is the format every "
                "Solar Operator customer will receive by default.</p>"
                "<p>— Solar Operator</p>"
            ),
            text=("Attached: GMCS-format reproduction sample, six sheets, "
                  "rolling 6 quarters. Populated with the exact values from "
                  "Bruce's master GMCS.xlsx for visual cell-by-cell verification."),
            workbook_path=str(path),
            filename="GMCS-reproduction-sample.xlsx",
        )
        print(f"Email sent: {sent}")

    # Cleanup smoke tenant
    with SessionLocal() as db:
        for t in db.query(Tenant).filter_by(id=TID).all():
            db.delete(t)
        db.commit()
    print("Cleanup done.")

    sys.exit(0 if sent else 1)

if __name__ == "__main__":
    main()
