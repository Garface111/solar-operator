"""Per-client raw GMP generation export (Ford 2026-07-16).

GET /v1/account/clients/{id}/gmp-generation.xlsx?quarter= returns a workbook with
a Monthly Summary (projects x the quarter's months) built from GMP data — the
interval meter where present, the GMP bill otherwise. GMP only.
"""
from __future__ import annotations
import io
import secrets
from datetime import datetime

from openpyxl import load_workbook

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, Bill


def _seed_gmp_client(kwh=30000):
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Export Co", contact_email=f"{tid}@t.test",
                      tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True))
        c = Client(tenant_id=tid, name="Londonderry Client", active=True)
        db.add(c); db.flush()
        arr = Array(tenant_id=tid, client_id=c.id, name="Londonderry", nepool_gis_id="GIS42")
        db.add(arr); db.flush()
        ua = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                            account_number="ACC" + secrets.token_hex(3))
        db.add(ua); db.flush()
        # A VEC account on the SAME client must NOT leak into the GMP export.
        arr2 = Array(tenant_id=tid, client_id=c.id, name="Glover VEC")
        db.add(arr2); db.flush()
        ua2 = UtilityAccount(tenant_id=tid, array_id=arr2.id, provider="vec",
                             account_number="VEC" + secrets.token_hex(3))
        db.add(ua2); db.flush()
        for (y, m) in [(2026, 1), (2026, 2), (2026, 3)]:
            db.add(Bill(tenant_id=tid, account_id=ua.id, bill_date=datetime(y, m, 15),
                        period_start=datetime(y, m, 1), kwh_generated=kwh,
                        document_number=f"g-{tid}-{y}-{m}", parse_status="parsed"))
            db.add(Bill(tenant_id=tid, account_id=ua2.id, bill_date=datetime(y, m, 15),
                        period_start=datetime(y, m, 1), kwh_generated=999,
                        document_number=f"v-{tid}-{y}-{m}", parse_status="parsed"))
        db.commit()
        return tid, c.id


def test_gmp_generation_export_downloads_and_summarizes(client):
    tid, cid = _seed_gmp_client(kwh=30000)
    auth = {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}
    r = client.get(f"/v1/account/clients/{cid}/gmp-generation.xlsx?quarter=Q1-2026", headers=auth)
    assert r.status_code == 200, r.text
    assert "spreadsheetml" in r.headers["content-type"]

    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    assert "Monthly Summary" in wb.sheetnames
    sh = wb["Monthly Summary"]
    text = "\n".join(
        " ".join(str(sh.cell(row, col).value) for col in range(1, 7))
        for row in range(1, 15)
    )
    assert "Londonderry" in text          # GMP project present
    assert "Glover VEC" not in text       # VEC project excluded from GMP export
    assert "30000" in text or "90000" in text  # monthly (30k) and/or quarter total (90k)
    wb.close()


def test_gmp_generation_export_wrong_tenant_404(client):
    tid, cid = _seed_gmp_client()
    other = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=other, name="Other", contact_email=f"{other}@t.test",
                      tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True))
        db.commit()
    auth = {"Authorization": f"Bearer {mint_session_for_tenant(other)}"}
    r = client.get(f"/v1/account/clients/{cid}/gmp-generation.xlsx?quarter=Q1-2026", headers=auth)
    assert r.status_code == 404
