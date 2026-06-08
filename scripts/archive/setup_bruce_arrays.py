"""One-shot: create Array rows for Bruce's Green Mountain Community Solar
tenant and link each GMP account to its array. Idempotent — safe to re-run.

Linking map (1:1 unless noted):
  Chester        → Chester (NEPOOL 53984)
  Londonderry    → Londonderry (NEPOOL 98179)
  Tannery Brook  → Tannery Brook (NEPOOL 46425)
  Timberworks    → Timberworks (NEPOOL 61959)
  Waterford      → Waterford (NEPOOL 78671)
  Pittsfield     → Pittsfield (NEPOOL unknown — backfill via /account later)
  Starlake N/S/C → Starlake (3 accounts summed; NEPOOL unknown)

Then build the GMCS workbook for Bruce and email it to him AND Ford for review.

Run on Railway:
  railway ssh "cd /app && python -m scripts.setup_bruce_arrays"
"""
from __future__ import annotations
import os, tempfile, sys
from pathlib import Path
from datetime import date, datetime

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Array, UtilityAccount
from api.writers.gmcs_writer import build_workbook
from api.notify import send_workbook_email

BRUCE_TID = "ten_14b76982523a3b47"

# (array_name, nepool_gis_id_or_None, list-of-account-nicknames-to-link)
ARRAYS = [
    ("Chester",       "53984",  ["Chester"]),
    ("Londonderry",   "98179",  ["Londonderry"]),
    ("Tannery Brook", "46425",  ["Tannery Brook"]),
    ("Timberworks",   "61959",  ["Timberworks"]),
    ("Waterford",     "78671",  ["Waterford"]),
    ("Pittsfield",    None,     ["Pittsfield"]),
    ("Starlake",      None,     ["Starlake Center", "Starlake North", "Starlake South"]),
]

# Starlake uses Bruce's same-month rule per memory; others use the default
# (prior month). bill_offset_months is documented in the Array model.
SAME_MONTH_ARRAYS = {"Starlake"}

def ensure_arrays_for_bruce():
    with SessionLocal() as db:
        tenant = db.get(Tenant, BRUCE_TID)
        if not tenant:
            raise SystemExit(f"Tenant {BRUCE_TID} not found on this database.")
        print(f"Tenant: {tenant.name} ({tenant.contact_email})")
        accounts = db.execute(
            select(UtilityAccount).where(UtilityAccount.tenant_id == BRUCE_TID)
        ).scalars().all()
        by_nickname = {a.nickname: a for a in accounts}
        print(f"  Found {len(accounts)} GMP accounts")

        for (aname, nid, nicks) in ARRAYS:
            # Reuse existing Array row by (tenant_id, name) if present
            arr = db.execute(
                select(Array).where(
                    Array.tenant_id == BRUCE_TID, Array.name == aname
                )
            ).scalar_one_or_none()
            if arr is None:
                arr = Array(
                    tenant_id=BRUCE_TID,
                    name=aname,
                    nepool_gis_id=nid,
                    bill_offset_months=0 if aname in SAME_MONTH_ARRAYS else 1,
                )
                db.add(arr); db.flush()
                action = "created"
            else:
                # Always keep NEPOOL ID + offset in sync
                if nid and arr.nepool_gis_id != nid:
                    arr.nepool_gis_id = nid
                arr.bill_offset_months = 0 if aname in SAME_MONTH_ARRAYS else 1
                action = "updated"
            linked = []
            missing = []
            for nick in nicks:
                acc = by_nickname.get(nick)
                if not acc:
                    missing.append(nick)
                    continue
                if acc.array_id != arr.id:
                    acc.array_id = arr.id
                    linked.append(nick)
                else:
                    linked.append(f"{nick}(already)")
            tag_nid = f" NEPOOL {nid}" if nid else " (NEPOOL pending)"
            print(f"  [{action}] {aname}{tag_nid}  →  {linked}"
                  + (f"  MISSING: {missing}" if missing else ""))
        db.commit()


def deliver_and_email():
    out_dir = Path(tempfile.mkdtemp(prefix="bruce-gmcs-"))
    out_path = out_dir / "Bruce-GMCS-reproduction.xlsx"
    # reference_date defaults to today; produces last 6 complete quarters
    path = build_workbook(BRUCE_TID, out_path=out_path)
    print(f"Built workbook: {path} ({path.stat().st_size} bytes)")

    # Send to Ford for review (not to Bruce yet — visual QA first)
    target = os.getenv("REVIEW_TO", "ford.genereaux@gmail.com")
    sent = send_workbook_email(
        to=target,
        subject="Bruce's first GMCS-format report — pre-Bruce review",
        html=(
            "<p>Hi Ford,</p>"
            "<p>This is the first GMCS-format workbook generated for Bruce's "
            "tenant on production using the real bills already in the database. "
            "Arrays now created and accounts linked:</p>"
            "<ul>"
            "<li>Chester (NEPOOL 53984)</li>"
            "<li>Londonderry (NEPOOL 98179)</li>"
            "<li>Tannery Brook (NEPOOL 46425)</li>"
            "<li>Timberworks (NEPOOL 61959)</li>"
            "<li>Waterford (NEPOOL 78671)</li>"
            "<li>Pittsfield (NEPOOL pending — Bruce to provide)</li>"
            "<li>Starlake (Center+North+South summed; NEPOOL pending)</li>"
            "</ul>"
            "<p>Open it, sanity-check the numbers against Bruce's master "
            "GMCS.xlsx, then green-light sending the real one to him.</p>"
            "<p>— Solar Operator</p>"
        ),
        text="GMCS-format workbook from Bruce's actual bill data on production.",
        workbook_path=str(path),
        filename="Bruce-GMCS-reproduction.xlsx",
    )
    print(f"Email sent to {target}: {sent}")


if __name__ == "__main__":
    ensure_arrays_for_bruce()
    deliver_and_email()
    sys.exit(0)
