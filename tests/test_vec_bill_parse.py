"""
VEC / SmartHub bill-PDF parser → settled net-meter Bill → bill-priced offtaker
invoice (Jun 2026).

A VEC bill PDF carries the generation + the bill's OWN net-meter credit rate + the
$ credit on a single GMP-shaped line:

    NM Credit  -10,200 kWh @ 0.181160  $1,847.83

Parsing it into a settled Bill (kwh_sent_to_grid + solar_credit_usd) lets the
EXISTING GMP offtaker-credit path price the invoice automatically — excess kWh ×
the bill's own rate — with no operator-entered rate. These tests cover:

  1. parse_vec_bill_text on the real PAGE-1+PAGE-2 fixture → exact fields.
  2. parse_vec_bill_text on junk / non-net-meter text → None.
  3. _upsert_vec_bill: creates a settled Bill; climb-only (a lower kWh never lowers
     it); a new period creates a SECOND Bill.
  4. ingest_vec_bill_pdf round-trip on a tiny reportlab-built VEC-shaped PDF.
  5. build_manual_match: a VEC offtaker whose account has a parsed Bill prices via
     the bill (excess × pct × the bill's rate × (1 − discount)), net_rate_source is
     the GMP-bill source — NOT 'needs_rate'/'customer'.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_vec_bill_test")

from datetime import date
import secrets as _secrets

import pytest

from api.adapters.vec_bill import (
    parse_vec_bill_text, _upsert_vec_bill, ingest_vec_bill_pdf,
)


# ── The real VEC bill text (PAGE 1 + PAGE 2 meter-period line), verbatim ───────
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


# ── 1. parse the real fixture ─────────────────────────────────────────────────
def test_parse_vec_bill_text_real_fixture():
    p = parse_vec_bill_text(VEC_BILL_TEXT)
    assert p is not None
    assert p["account_number"] == "6578300"
    assert p["kwh_generated"] == 10200
    assert p["kwh_sent_to_grid"] == 10200
    assert p["solar_credit_usd"] == 1847.83
    assert p["credit_rate"] == 0.18116
    assert p["period_start"] == date(2026, 5, 21)
    assert p["period_end"] == date(2026, 6, 21)
    assert p["bill_date"] == date(2026, 6, 24)
    assert p["is_net_metered"] is True


# ── 2. junk → None ────────────────────────────────────────────────────────────
def test_parse_vec_bill_text_junk_returns_none():
    assert parse_vec_bill_text("") is None
    assert parse_vec_bill_text("This is a water bill. Amount due $42.00.") is None
    # A bill that mentions kWh but has no NM-credit line is NOT parseable.
    assert parse_vec_bill_text(
        "Total Usage: 1200 kWh\nAmount Due: $180.00\nBilling Date: 06/24/2026"
    ) is None


# ── seed helper for upsert + routing tests ────────────────────────────────────
def _seed_account(provider="vec"):
    """One tenant + array + a VEC/SmartHub UtilityAccount (no bills yet)."""
    from api.db import SessionLocal
    from api.models import Tenant, Array, UtilityAccount

    tid = "ten_vbill_" + _secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="VEC Bill Test",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="West Glover Roaring Brook", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider=provider,
                              account_number="6578300", nickname="West Glover VEC")
        db.add(acct); db.commit()
        return tid, arr.id, acct.id


def _parsed(kwh=10200, credit=1847.83, pe=date(2026, 6, 21),
            ps=date(2026, 5, 21)):
    return {
        "account_number": "6578300",
        "kwh_generated": kwh, "kwh_sent_to_grid": kwh,
        "solar_credit_usd": credit, "credit_rate": round(credit / kwh, 6),
        "period_start": ps, "period_end": pe, "bill_date": pe,
        "is_net_metered": True,
    }


# ── 3. _upsert_vec_bill: create, climb-only, new-period ───────────────────────
def test_upsert_vec_bill_creates_climbs_and_adds_new_period():
    from api.db import SessionLocal
    from api.models import UtilityAccount, Bill
    from sqlalchemy import select

    tid, aid, acct_id = _seed_account()

    # (a) First upsert creates the settled Bill.
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, acct_id)
        bill = _upsert_vec_bill(db, ua, _parsed())
        db.commit()
        bid = bill.id
    with SessionLocal() as db:
        b = db.get(Bill, bid)
        assert b is not None
        assert b.kwh_sent_to_grid == 10200.0
        assert b.solar_credit_usd == 1847.83
        assert b.kwh_generated == 10200
        assert b.is_net_metered is True
        assert b.period_end.date() == date(2026, 6, 21)

    # (b) Re-running with a LOWER kwh/credit (same period) does NOT lower it.
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, acct_id)
        _upsert_vec_bill(db, ua, _parsed(kwh=5000, credit=900.00))
        db.commit()
    with SessionLocal() as db:
        rows = db.execute(select(Bill).where(Bill.account_id == acct_id)).scalars().all()
        assert len(rows) == 1, "same period must not spawn a 2nd Bill"
        b = rows[0]
        assert b.kwh_sent_to_grid == 10200.0   # climb-only: unchanged
        assert b.solar_credit_usd == 1847.83
        assert b.kwh_generated == 10200

    # (c) A NEW period creates a SECOND Bill.
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, acct_id)
        _upsert_vec_bill(db, ua, _parsed(kwh=8000, credit=1400.00,
                                         pe=date(2026, 7, 21),
                                         ps=date(2026, 6, 21)))
        db.commit()
    with SessionLocal() as db:
        rows = db.execute(
            select(Bill).where(Bill.account_id == acct_id)
            .order_by(Bill.period_end)
        ).scalars().all()
        assert len(rows) == 2
        assert {r.period_end.date() for r in rows} == {
            date(2026, 6, 21), date(2026, 7, 21)}
        new = next(r for r in rows if r.period_end.date() == date(2026, 7, 21))
        assert new.kwh_sent_to_grid == 8000.0
        assert new.solar_credit_usd == 1400.00


# ── 4. ingest_vec_bill_pdf round-trip on a reportlab-built PDF ────────────────
def _build_vec_pdf_bytes() -> bytes | None:
    """Build a tiny VEC-bill-shaped PDF in-test. Returns None if reportlab is
    unavailable (then the PDF test skips)."""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
    except Exception:
        return None
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 720
    for line in VEC_BILL_TEXT.splitlines():
        c.drawString(40, y, line)
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


def test_ingest_vec_bill_pdf_roundtrip():
    pdf = _build_vec_pdf_bytes()
    if pdf is None:
        pytest.skip("reportlab not available — PDF round-trip skipped")
    from api.db import SessionLocal
    from api.models import Bill

    tid, aid, acct_id = _seed_account()
    with SessionLocal() as db:
        res = ingest_vec_bill_pdf(db, tid, acct_id, pdf)
        db.commit()
    assert res["ok"] is True, res
    assert res["parsed"]["kwh_sent_to_grid"] == 10200
    assert res["parsed"]["solar_credit_usd"] == 1847.83
    assert res["parsed"]["account_number"] == "6578300"
    with SessionLocal() as db:
        b = db.get(Bill, res["bill_id"])
        assert b is not None
        assert b.kwh_sent_to_grid == 10200.0
        assert b.solar_credit_usd == 1847.83


def test_ingest_vec_bill_pdf_rejects_garbage_and_wrong_provider():
    from api.db import SessionLocal
    from api.models import UtilityAccount

    tid, aid, acct_id = _seed_account()
    # Garbage bytes → not a readable net-meter bill.
    with SessionLocal() as db:
        res = ingest_vec_bill_pdf(db, tid, acct_id, b"%PDF-1.4 not really a bill")
        assert res["ok"] is False
        assert "net-meter" in res["reason"].lower() or "could not" in res["reason"].lower()

    # A real-parsable PDF against a NON-SmartHub account → rejected.
    pdf = _build_vec_pdf_bytes()
    if pdf is not None:
        with SessionLocal() as db:
            ua = db.get(UtilityAccount, acct_id)
            ua.provider = "gmp"   # flip to a non-SmartHub provider
            db.commit()
        with SessionLocal() as db:
            res = ingest_vec_bill_pdf(db, tid, acct_id, pdf)
            assert res["ok"] is False
            assert "smarthub" in res["reason"].lower() or "vec" in res["reason"].lower()


# ── 5. routing: a parsed VEC bill auto-prices the offtaker via the bill ───────
def test_vec_offtaker_prices_from_parsed_bill():
    from api.db import SessionLocal
    from api.models import UtilityAccount, BillingReportSubscription
    from api.billing import delivery

    tid, aid, acct_id = _seed_account()
    # Parse a real bill into a settled Bill (kwh_sent_to_grid + solar_credit_usd).
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, acct_id)
        _upsert_vec_bill(db, ua, _parsed(kwh=10200, credit=1847.83))
        db.commit()

    # No per-offtaker rate set → it MUST price from the bill's own rate, not model A.
    pct, disc = 0.38, 0.10
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Paul Bozuwa",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=pct, discount_pct=disc,
        billing_model="percent_of_array",
    )
    m = delivery.build_manual_match(sub)
    assert m.matched
    ci = m.computed_invoice
    # Bill-priced — NOT the model-A 'needs_rate'/'customer' path.
    assert ci["net_rate_source"] == "gmp_bill_credit", ci["net_rate_source"]
    assert ci["kwh_source"] == "utility_bill"
    assert ci["has_utility_bill"] is True
    # The bill carries the EXCESS + the $ credit (so solar_credit_usd is populated).
    assert ci["solar_credit_usd"] is not None
    # excess = 10200; offtaker share = 38% = 3876 kWh; rate = 1847.83/10200.
    rate = round(1847.83 / 10200, 6)
    assert ci["net_rate_per_kwh"] == rate
    assert ci["array_kwh"] == 10200.0
    assert m.latest_period.customer_kwh == round(10200.0 * pct, 2)
    expected = round(round(10200.0 * pct, 2) * rate * (1.0 - disc), 2)
    assert ci["amount_owed"] == expected


# ── 6. PDF bytes are persisted for auto-attach ────────────────────────────────
def test_upsert_vec_bill_persists_pdf_bytes_and_keeps_latest():
    """The auto-attach crux: _upsert_vec_bill stores the verbatim PDF bytes on the
    Bill (create branch), and a later re-pull refreshes them (update branch). Without
    this there is nothing for delivery to attach."""
    from api.db import SessionLocal
    from api.models import UtilityAccount, Bill

    tid, aid, acct_id = _seed_account()

    # (a) Create with PDF bytes → persisted + content type stamped.
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, acct_id)
        bill = _upsert_vec_bill(db, ua, _parsed(), pdf_bytes=b"%PDF-1.4\nVEC v1\n")
        db.commit()
        bid = bill.id
    with SessionLocal() as db:
        b = db.get(Bill, bid)
        assert bytes(b.pdf_bytes) == b"%PDF-1.4\nVEC v1\n"
        assert b.pdf_content_type == "application/pdf"

    # (b) A re-pull of the SAME period refreshes the PDF (latest kept), even though
    #     kwh/credit are climb-only.
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, acct_id)
        _upsert_vec_bill(db, ua, _parsed(), pdf_bytes=b"%PDF-1.4\nVEC v2 fresh\n")
        db.commit()
    with SessionLocal() as db:
        b = db.get(Bill, bid)
        assert bytes(b.pdf_bytes) == b"%PDF-1.4\nVEC v2 fresh\n"

    # (c) A parse with NO bytes (rate-only path) must NOT wipe a stored PDF.
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, acct_id)
        _upsert_vec_bill(db, ua, _parsed(), pdf_bytes=None)
        db.commit()
    with SessionLocal() as db:
        b = db.get(Bill, bid)
        assert bytes(b.pdf_bytes) == b"%PDF-1.4\nVEC v2 fresh\n"


def test_ingest_vec_bill_pdf_persists_pdf_bytes():
    """ingest_vec_bill_pdf (the manual-upload path) stores the raw PDF bytes so the
    offtaker's invoice can auto-attach the VEC bill it was priced from."""
    pdf = _build_vec_pdf_bytes()
    if pdf is None:
        pytest.skip("reportlab not available — PDF round-trip skipped")
    from api.db import SessionLocal
    from api.models import Bill

    tid, aid, acct_id = _seed_account()
    with SessionLocal() as db:
        res = ingest_vec_bill_pdf(db, tid, acct_id, pdf)
        db.commit()
    assert res["ok"] is True, res
    with SessionLocal() as db:
        b = db.get(Bill, res["bill_id"])
        assert b.pdf_bytes is not None and bytes(b.pdf_bytes) == pdf
        assert b.pdf_content_type == "application/pdf"
