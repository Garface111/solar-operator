"""Array Prospectus builder — v0 (Array Secondary Market).

Verifies the honest core: captured vs owner-reported vs estimated production land
in the right tiers, coverage windows print, offtakers bind + redact, the utility
credit-rate history flags banked months, the SHA-256 is stable across rebuilds of
the same data, and both renderers (HTML + PDF) produce real output.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_prospectus_test")

import secrets
from datetime import datetime, timedelta

from api.db import SessionLocal
from api.models import (
    Array, BillingReportSubscription, Bill, DailyGeneration, Inverter,
    ReportDraft, Tenant, UtilityAccount, local_today,
)
from api.prospectus import (
    build_prospectus, content_sha256, redact_prospectus,
    render_prospectus_html, render_prospectus_pdf,
)

CASHED_RAW = {"billSegments": [{"segmentLineItems": [
    {"unitOfMeasure": "KWH", "unitCode": "EXCESS",
     "dollarAmount": -257.60, "unitCount": 1000.0}]}]}
BANKED_RAW = {"billSegments": [{"segmentLineItems": [
    {"unitOfMeasure": "KWH", "unitCode": "EXCESS",
     "dollarAmount": 0.0, "unitCount": 5000.0}]}]}


def _seed():
    """One tenant with a fully-populated array (arr1) and a utility-only array
    (arr2). Returns (tenant_id, arr1_id, arr2_id)."""
    tid = "ten_px_" + secrets.token_hex(3)
    today = local_today()
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Px Solar Co",
                      company_name="Px Solar Co", operator_name="Pat Px",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()

        arr1 = Array(tenant_id=tid, name="Chester Field", region="south",
                     nepool_gis_id="53984", fuel_type="solar",
                     first_connect_date=datetime(2019, 6, 1),
                     expected_kwh_per_kw_day=4.0)  # ratio-mode → offline expectation
        arr2 = Array(tenant_id=tid, name="Meter Only", region="north")
        db.add_all([arr1, arr2])
        db.flush()

        # arr1: two real inverters with nameplate.
        db.add_all([
            Inverter(tenant_id=tid, array_id=arr1.id, vendor="solaredge",
                     serial="SE-A", model="SE7600", nameplate_kw=5.0, position=0),
            Inverter(tenant_id=tid, array_id=arr1.id, vendor="solaredge",
                     serial="SE-B", model="SE7600", nameplate_kw=5.0, position=1),
        ])

        # Captured production: 20 consecutive days ending 3 days ago (source in
        # the captured tier). ~38 kWh/day against expected 40 (10kW × 4).
        for i in range(20):
            d = today - timedelta(days=22 - i)   # days -22 .. -3
            db.add(DailyGeneration(tenant_id=tid, array_id=arr1.id, day=d,
                                   kwh=38.0, source="solaredge"))
        # Owner-typed rows (must stay OUT of the captured tier).
        for d in (today - timedelta(days=30), today - timedelta(days=29)):
            db.add(DailyGeneration(tenant_id=tid, array_id=arr1.id, day=d,
                                   kwh=41.0, source="csv"))
        # Estimated (bill_prorate) rows.
        for d in (today - timedelta(days=40), today - timedelta(days=39)):
            db.add(DailyGeneration(tenant_id=tid, array_id=arr1.id, day=d,
                                   kwh=39.0, source="bill_prorate"))

        # Utility account + two bills: one cashed, one banked.
        acct = UtilityAccount(tenant_id=tid, array_id=arr1.id, provider="gmp",
                              account_number="A" + secrets.token_hex(3))
        db.add(acct); db.flush()
        db.add_all([
            Bill(tenant_id=tid, account_id=acct.id,
                 period_start=datetime(2026, 4, 1), period_end=datetime(2026, 4, 30),
                 document_number="D1", kwh_sent_to_grid=1000.0,
                 raw_json=CASHED_RAW, pdf_bytes=b"%PDF-1.4 fake"),
            Bill(tenant_id=tid, account_id=acct.id,
                 period_start=datetime(2026, 5, 1), period_end=datetime(2026, 5, 31),
                 document_number="D2", kwh_sent_to_grid=5000.0,
                 raw_json=BANKED_RAW),
        ])

        # Offtaker bound to arr1 (by array_id) with PII + a sent invoice.
        sub = BillingReportSubscription(
            tenant_id=tid, array_id=arr1.id, customer_name="Londonderry School",
            client_email="billing@londonderry.example", allocation_pct=0.25,
            discount_pct=0.10, cadence="monthly", delivery_mode="approval",
            last_sent_amount_usd=1410.00, last_sent_period_end="2026-05-31")
        db.add(sub); db.flush()
        db.add(ReportDraft(tenant_id=tid, subscription_id=sub.id,
                           customer_name="Londonderry School", status="sent",
                           period_label="2026-05", amount_usd=1410.00))
        db.commit()
        return tid, arr1.id, arr2.id


def test_sections_and_tiers():
    tid, a1, a2 = _seed()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        p = build_prospectus(db, t, a1, purpose="refinance")

    s = p["sections"]
    assert set(s) == {"asset", "production", "expectation", "health",
                      "revenue", "utility", "estimate"}
    assert p["purpose"] == "refinance"

    # Asset: nameplate summed, equipment listed.
    assert s["asset"]["nameplate_kw"] == 10.0
    assert s["asset"]["nameplate_available"] is True
    assert s["asset"]["inverter_count"] == 2
    assert s["asset"]["nepool_gis_id"] == "53984"

    # Production tiers strictly separated.
    prod = s["production"]
    cap = prod["row_counts"]["captured"]
    assert cap == 20
    assert prod["row_counts"]["owner_reported"] == 2
    assert prod["row_counts"]["estimated"] == 2
    assert prod["coverage"]["captured"]["day_count"] == 20
    # captured months carry captured kWh but not owner/estimate.
    total_owner = sum(m["owner_kwh"] for m in prod["monthly"])
    total_est = sum(m["estimate_kwh"] for m in prod["monthly"])
    assert total_owner > 0 and total_est > 0

    # Expectation (ratio-mode, offline): available with a real ratio.
    exp = s["expectation"]
    assert exp["available"] is True
    assert exp["ratio_pct"] is not None
    assert exp["inputs"]["measured_days"] >= 15

    # Health honesty.
    assert s["health"]["monitoring_since"] is not None
    assert "absence of observation" in s["health"]["honesty_note"]

    # Revenue: bound offtaker + trailing invoiced.
    rev = s["revenue"]
    assert rev["offtaker_count"] == 1
    o = rev["offtakers"][0]
    assert o["customer_name"] == "Londonderry School"
    assert any(t["period"] == "2026-05" for t in o["trailing_invoiced"])
    assert rev["pii_redacted"] is False

    # Utility: banked month flagged, cashed rate read from the bill.
    util = s["utility"]
    assert util["bill_count"] == 2
    assert util["banked_month_count"] == 1
    assert util["captured_pdf_count"] == 1
    rates = [r for r in util["credit_rate_history"] if not r["banked"]]
    assert rates and abs(rates[0]["credit_rate_per_kwh"] - 0.2576) < 1e-3

    # Estimate: reliability score computed with its inputs.
    est = s["estimate"]
    assert isinstance(est["reliability_score"], int)
    assert est["inputs"]["weights"] == {"performance": 0.7, "coverage": 0.3}


def test_utility_only_array_has_no_nameplate():
    tid, a1, a2 = _seed()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        p = build_prospectus(db, t, a2)
    assert p["sections"]["asset"]["nameplate_available"] is False
    assert p["sections"]["expectation"]["available"] is False
    assert p["sections"]["expectation"]["reason"] == "no_nameplate"


def test_hash_is_stable_across_rebuilds():
    tid, a1, a2 = _seed()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        p1 = build_prospectus(db, t, a1)
        p2 = build_prospectus(db, t, a1)
    # generated_at differs, but the content hash must be identical.
    assert p1["generated_at"] != p2["generated_at"] or p1["generated_at"]
    assert p1["content_sha256"] == p2["content_sha256"]
    # And re-deriving the hash from the payload matches the stamped value.
    assert content_sha256(p1) == p1["content_sha256"]


def test_redaction_strips_offtaker_pii():
    tid, a1, a2 = _seed()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        p = build_prospectus(db, t, a1)
    r = redact_prospectus(p)
    o = r["sections"]["revenue"]["offtakers"][0]
    assert o["customer_name"] == "Offtaker 1"
    assert o["client_email"] is None
    assert r["sections"]["revenue"]["pii_redacted"] is True
    # Terms survive redaction — the deal shape is the value.
    assert o["allocation_pct"] == 0.25
    # Original is untouched (deep copy).
    assert p["sections"]["revenue"]["offtakers"][0]["customer_name"] == "Londonderry School"


def test_renderers_produce_output_and_respect_redaction():
    tid, a1, a2 = _seed()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        p = build_prospectus(db, t, a1)

    pdf = render_prospectus_pdf(p)
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000

    full_html = render_prospectus_html(p)
    assert "Chester Field" in full_html
    # The rendered artifact never prints offtaker emails (PII stays out of the
    # visible doc); the owner sees the name.
    assert "Londonderry School" in full_html
    assert "billing@londonderry.example" not in full_html

    # The public/redacted view hides even the name — and the email is never in
    # the payload the public route rendered from (redact_prospectus stripped it).
    redacted_payload = redact_prospectus(p)
    assert "billing@londonderry.example" not in str(redacted_payload)
    redacted_html = render_prospectus_html(redacted_payload, public=True)
    assert "Londonderry School" not in redacted_html
    assert "Offtaker 1" in redacted_html
    # PDF re-renders from the redacted payload too.
    assert b"%PDF" == render_prospectus_pdf(redact_prospectus(p))[:4]


def test_endpoints_build_share_publish_revoke():
    """End-to-end through the real FastAPI app: build → mint (OFF) → 404 →
    publish → public HTML/PDF (redacted) → revoke → 404."""
    from fastapi.testclient import TestClient
    from api.app import app

    tid, a1, a2 = _seed()
    with SessionLocal() as db:
        key = db.get(Tenant, tid).tenant_key
    c = TestClient(app)
    H = {"Authorization": f"Bearer {key}"}

    # Build + persist.
    r = c.post(f"/v1/array-owners/arrays/{a1}/prospectus",
               json={"purpose": "refinance"}, headers=H)
    assert r.status_code == 200, r.text
    doc_id = r.json()["document_id"]
    sha = r.json()["content_sha256"]
    assert sha and len(sha) == 64

    # List shows it.
    lst = c.get("/v1/array-owners/prospectuses", headers=H).json()
    assert any(p["document_id"] == doc_id for p in lst["prospectuses"])

    # Mint a share — DEFAULTS to unpublished + redacted.
    sh = c.post(f"/v1/array-owners/prospectus/{doc_id}/share", json={}, headers=H).json()["share"]
    assert sh["published"] is False and sh["redact_offtaker_pii"] is True
    token = sh["token"]

    # Public route 404s while unpublished (the first external share is deliberate).
    assert c.get(f"/v1/prospectus/{token}").status_code == 404

    # Publish, then it renders — redacted by default (no offtaker name/email).
    up = c.patch(f"/v1/array-owners/prospectus/share/{sh['id']}",
                 json={"published": True}, headers=H)
    assert up.status_code == 200 and up.json()["share"]["published"] is True

    pub = c.get(f"/v1/prospectus/{token}")
    assert pub.status_code == 200
    assert "Chester Field" in pub.text
    assert "Londonderry School" not in pub.text
    assert "billing@londonderry.example" not in pub.text

    pdf = c.get(f"/v1/prospectus/{token}?format=pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:4] == b"%PDF"

    # View receipt incremented.
    lst2 = c.get("/v1/array-owners/prospectuses", headers=H).json()
    share2 = next(p for p in lst2["prospectuses"] if p["document_id"] == doc_id)["shares"][0]
    assert share2["view_count"] >= 2

    # Revoke → 404 again.
    c.patch(f"/v1/array-owners/prospectus/share/{sh['id']}",
            json={"revoked": True}, headers=H)
    assert c.get(f"/v1/prospectus/{token}").status_code == 404
