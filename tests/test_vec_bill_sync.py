"""
AUTOMATIC VEC bill-PDF pull → /v1/sync → settled net-meter Bill (Jun 2026).

The extension (smarthub_content.js v1.9.84) downloads each VEC bill's PDF from
SmartHub's billPdfService and attaches it base64 as `pdf_b64` on the bill record
it POSTs to /v1/sync. The backend (api/app.py SmartHub bill-persistence loop)
parses that PDF with adapters.vec_bill.parse_vec_bill_pdf and, when it reads a
net-meter bill, writes the AUTHORITATIVE generation + the bill's own credit into
the Bill (kwh_sent_to_grid + solar_credit_usd + is_net_metered). From there the
existing GMP offtaker-credit path auto-prices the invoice — no operator-entered
rate, no manual PDF upload.

This test exercises the REAL /v1/sync path end-to-end:
  1. seed a tenant + a VEC UtilityAccount for account 6578300 (so the bill loop's
     acct_map resolves it — the loop skips bills whose account isn't linkable),
  2. build a tiny VEC-bill-shaped PDF in-test with reportlab,
  3. POST it as `pdf_b64` on a bill row in the /v1/sync `bills` array,
  4. assert a settled net-meter Bill landed with the parsed figures.
"""
from __future__ import annotations

import base64
import io
import secrets

import pytest
from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, Bill, Tenant, UtilityAccount


# ── The real VEC bill text (PAGE 1 + PAGE 2 meter-period line), verbatim ───────
# Mirrors tests/test_vec_bill_parse.py's proven fixture: the
# "NM Credit -10,200 kWh @ 0.181160 $1,847.83" line carries generation + the
# bill's own credit rate + the $ credit; the date pair is the meter period; the
# "Billing Date" + "Account #" lines round out the parse.
VEC_BILL_TEXT = """\
Net Meter Statement   Monthly Energy Totals
Total Net Meter Credit   Amount by Month
-10,200
Prior Credit $243.71   06/26 $776.84
TOTAL
Expired Credit $0.00   kWh ENERGY GENERATED
Available Credit $776.84
Current Month Credits
NM Credit
-10,200 kWh @ 0.181160 $1,847.83
REC Credit -10,200 kWh @ 0.00 $0.00
Siting Credit -10,200 kWh @ 0.00 $0.00
...
Billing Date: 06/24/2026
Account #: 6578300
... 05/21/2026 06/21/2026 97730 97645 -85 120 -10,200
"""


def _build_vec_pdf_b64() -> str | None:
    """Build a tiny VEC-bill-shaped PDF in-test and return it base64-encoded.
    Returns None if reportlab is unavailable (then the test skips)."""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
    except Exception:
        return None
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 720
    for line in VEC_BILL_TEXT.splitlines():
        c.drawString(40, y, line)
        y -= 14
    c.showPage()
    c.save()
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _seed_tenant_with_vec_account() -> tuple[str, str, int]:
    """A tenant + array + a VEC UtilityAccount for account 6578300 — the state the
    Link-VEC capture leaves (the account must exist for the bill loop to bind a
    Bill to it). Returns (tenant_id, tenant_key, utility_account_id)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="VEC Sync Test", contact_email=f"op_{tid}@test.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.flush()
        arr = Array(tenant_id=tid, name="52 County RD, Glover, VT, 05839",
                    fuel_type="solar")
        db.add(arr)
        db.flush()
        ua = UtilityAccount(
            tenant_id=tid, array_id=arr.id, provider="vec",
            account_number="6578300", nickname="West Glover VEC",
        )
        db.add(ua)
        db.commit()
        return tid, key, ua.id


def test_vec_sync_pdf_lands_settled_net_meter_bill(client):
    """POST a VEC /v1/sync with a bill carrying `pdf_b64` → the backend parses the
    PDF and writes a settled net-meter Bill (kwh_sent_to_grid + solar_credit_usd +
    is_net_metered) for the bound VEC account."""
    pdf_b64 = _build_vec_pdf_b64()
    if pdf_b64 is None:
        pytest.skip("reportlab not available — VEC PDF sync test skipped")

    tid, key, ua_id = _seed_tenant_with_vec_account()

    payload = {
        "provider": "vec",
        "captureMethod": "api",
        "extensionVersion": "1.9.84",
        "user": {"hostname": "vermontelectric.smarthub.coop"},
        "accounts": [{
            "accountNumber": "6578300",
            "nickname": "West Glover VEC",
            "serviceAddress": {"line1": "52 County RD, Glover, VT"},
        }],
        "bills": [{
            "account_id": "6578300",
            "billing_date": "06/24/2026",
            "bill_uuid": "u1",
            "bill_timestamp": "1782294584000",
            "pdf_b64": pdf_b64,
        }],
    }

    r = client.post("/v1/sync", json=payload,
                    headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        bills = db.execute(
            select(Bill).where(Bill.account_id == ua_id)
        ).scalars().all()
        assert len(bills) == 1, f"expected one settled bill, got {len(bills)}"
        b = bills[0]
        # The parsed PDF is authoritative for the net-meter figures.
        assert b.kwh_sent_to_grid == 10200.0
        assert b.solar_credit_usd == 1847.83
        assert b.is_net_metered is True
        assert b.kwh_generated == 10200
        # The bill PDF's meter period is preferred.
        assert b.period_end is not None
        assert b.period_end.date().isoformat() == "2026-06-21"
        assert b.parse_status == "parsed"
        # The verbatim PDF bytes are persisted so auto-attach has something to send.
        import base64 as _b64
        assert b.pdf_bytes is not None and bytes(b.pdf_bytes) == _b64.b64decode(pdf_b64)
        assert b.pdf_content_type == "application/pdf"


def test_vec_sync_without_pdf_is_consumption_only(client):
    """REGRESSION guard: a VEC bill with NO pdf_b64 must NOT get net-meter fields —
    the non-PDF (consumption-only) behavior is unchanged. kwh_generated stays None,
    is_net_metered stays None."""
    tid, key, ua_id = _seed_tenant_with_vec_account()

    payload = {
        "provider": "vec",
        "captureMethod": "api",
        "user": {"hostname": "vermontelectric.smarthub.coop"},
        "accounts": [{
            "accountNumber": "6578300",
            "nickname": "West Glover VEC",
        }],
        "bills": [{
            "account_id": "6578300",
            "billing_date": "06/24/2026",
            "bill_uuid": "u2",
            "kwh": 0,
        }],
    }

    r = client.post("/v1/sync", json=payload,
                    headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        bills = db.execute(
            select(Bill).where(Bill.account_id == ua_id)
        ).scalars().all()
        assert len(bills) == 1
        b = bills[0]
        assert b.kwh_generated is None
        assert b.kwh_sent_to_grid is None
        assert b.solar_credit_usd is None
        assert b.is_net_metered is None
        # No PDF sent → no bytes persisted (nothing to auto-attach; honest).
        assert b.pdf_bytes is None
