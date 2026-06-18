"""Tests for the multi-year billing trends feature (CONTRACT 1 + 2).

Covers:
  * build_trends (pure): multi-year monthly_by_year + seasonal_yoy +
    latest_delta_pct; single-year (no YoY); empty (empty collections).
  * GET /subscriptions/{id}/trends: 200 shape; 404 for another tenant; thin
    data → empty 200 (never 500).
  * GMP invoice attach hook: null → attachment set unchanged; bytes present →
    the GMP pdf rides along in the returned attachment list.
"""
from __future__ import annotations

import pathlib
import secrets
import types
from datetime import date

from api.account import mint_session_for_tenant
from api.billing import summary as summ
from api.billing.delivery import build_match, generate_files
from api.billing.matcher import BillingMatch, Period
from api.db import SessionLocal
from api.models import Tenant

FIX = pathlib.Path(__file__).parent / "fixtures" / "billing"


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Trends Test Operator",
            contact_email=f"{tid}@operator.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _upload(client, auth, fixture="norwich.xlsx", **form):
    data = (FIX / fixture).read_bytes()
    files = {"file": (fixture, data,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return client.post("/v1/array-operator/billing/subscriptions",
                       files=files, data=form, headers={"Authorization": auth})


def _match(periods: list[Period], name: str = "Test Co",
           lifetime: float | None = None) -> BillingMatch:
    return BillingMatch(
        matched=True, confidence=1.0, source="schema",
        customer={"name": name}, periods=periods,
        latest_period=periods[-1] if periods else None,
        project_totals={"total_customer_kwh": lifetime} if lifetime is not None else {},
    )


# ─── build_trends (pure) ─────────────────────────────────────────────────────

def test_build_trends_multi_year_yoy():
    periods = [
        Period(end=date(2024, 1, 31), customer_kwh=1000.0, savings=100.0),
        Period(end=date(2024, 2, 28), customer_kwh=500.0, savings=50.0),
        Period(end=date(2025, 1, 31), customer_kwh=1100.0, savings=120.0),
    ]
    t = _match(periods, name="Acme", lifetime=2600.0)
    out = summ.build_trends(t)

    assert out["customer_name"] == "Acme"
    assert out["years"] == [2024, 2025]
    assert out["monthly_by_year"]["2024"] == [
        {"month": 1, "kwh": 1000.0, "savings": 100.0},
        {"month": 2, "kwh": 500.0, "savings": 50.0},
    ]
    assert out["monthly_by_year"]["2025"] == [
        {"month": 1, "kwh": 1100.0, "savings": 120.0},
    ]

    jan = next(s for s in out["seasonal_yoy"] if s["month"] == 1)
    assert jan["label"] == "Jan"
    assert jan["by_year"] == {"2024": 1000.0, "2025": 1100.0}
    assert jan["latest_delta_pct"] == 10.0  # 1000 → 1100

    feb = next(s for s in out["seasonal_yoy"] if s["month"] == 2)
    assert feb["by_year"] == {"2024": 500.0}
    assert feb["latest_delta_pct"] is None  # no prior year for February

    assert out["lifetime_kwh"] == 2600.0
    assert out["ttm_kwh"] == 2600.0  # all 3 periods inside trailing 12
    assert out["summary_note"]


def test_build_trends_gap_year_has_no_prior():
    # 2024 and 2026 present for January, 2025 absent → "immediately prior year"
    # (2025) has no value, so latest_delta_pct is null even though 2024 exists.
    periods = [
        Period(end=date(2024, 1, 31), customer_kwh=900.0),
        Period(end=date(2026, 1, 31), customer_kwh=1200.0),
    ]
    out = summ.build_trends(_match(periods))
    jan = next(s for s in out["seasonal_yoy"] if s["month"] == 1)
    assert jan["by_year"] == {"2024": 900.0, "2026": 1200.0}
    assert jan["latest_delta_pct"] is None


def test_build_trends_single_year_no_yoy():
    periods = [
        Period(end=date(2025, 6, 30), customer_kwh=800.0, savings=80.0),
        Period(end=date(2025, 7, 31), customer_kwh=850.0, savings=85.0),
    ]
    out = summ.build_trends(_match(periods))
    assert out["years"] == [2025]
    assert all(s["latest_delta_pct"] is None for s in out["seasonal_yoy"])
    assert out["summary_note"] == "1 year of billing history on record."


def test_build_trends_empty_is_empty_collections():
    out = summ.build_trends(_match([], name="Nobody"))
    assert out["customer_name"] == "Nobody"
    assert out["years"] == []
    assert out["monthly_by_year"] == {}
    assert out["seasonal_yoy"] == []
    assert out["ttm_kwh"] is None
    assert out["ttm_savings"] is None
    assert out["lifetime_kwh"] is None
    assert out["summary_note"] is None


# ─── trends endpoint ─────────────────────────────────────────────────────────

def test_trends_endpoint_returns_contract_shape(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sub_id}/trends",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customer_name"] == "Norwich Fire District"
    assert isinstance(body["years"], list) and len(body["years"]) > 1
    assert isinstance(body["monthly_by_year"], dict)
    for yr in body["years"]:
        rows = body["monthly_by_year"][str(yr)]
        assert all({"month", "kwh", "savings"} <= set(row) for row in rows)
    for s in body["seasonal_yoy"]:
        assert {"month", "label", "by_year", "latest_delta_pct"} <= set(s)
    assert body["ttm_kwh"] is not None
    assert body["lifetime_kwh"] is not None


def test_trends_endpoint_404_for_other_tenant(client):
    _, auth_a = _make_tenant()
    _, auth_b = _make_tenant()
    sub_id = _upload(client, auth_a, "norwich.xlsx").json()["subscription"]["id"]
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sub_id}/trends",
                   headers={"Authorization": auth_b})
    assert r.status_code == 404


def test_trends_endpoint_thin_data_empty_200(client, monkeypatch):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    # Simulate a workbook that no longer yields periods → empty 200, never 500.
    monkeypatch.setattr("api.billing.routes.build_match",
                        lambda sub: _match([], name=None))
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sub_id}/trends",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["years"] == []
    assert body["monthly_by_year"] == {}
    assert body["seasonal_yoy"] == []
    assert body["ttm_kwh"] is None
    # Falls back to the stored customer name when the match yields none.
    assert body["customer_name"] == "Norwich Fire District"


def test_trends_endpoint_resolves_by_client_id(client):
    """The reports UI is client-centric and links trends by CLIENT id, not
    subscription id. The endpoint must resolve a client id → that client's
    subscription and return its trends (the cross-agent integration seam)."""
    from api.models import BillingReportSubscription, Client
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    # Link the subscription to a client (reuse the auto-created one if the
    # upload made it, else create it) — the reports UI links trends by client id.
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)
        cid = sub.client_id
        if cid is None:
            c = Client(tenant_id=tid, name="Norwich Fire District",
                       contact_email="nfd@test.test")
            db.add(c); db.flush()
            cid = c.id
            sub.client_id = cid
            db.commit()
    # Hitting the endpoint with the CLIENT id resolves to the linked sub's trends.
    r = client.get(f"/v1/array-operator/billing/subscriptions/{cid}/trends",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customer_name"] == "Norwich Fire District"
    assert body["lifetime_kwh"] is not None  # real workbook → real trends


def test_trends_endpoint_client_without_workbook_empty_200(client):
    """A valid client that has no billing subscription yet → honest empty
    trends (200), NOT a 404, so the UI shows 'not enough history' cleanly."""
    from api.models import Client
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="No Workbook Co",
                   contact_email="nw@test.test")
        db.add(c); db.commit()
        cid = c.id
    r = client.get(f"/v1/array-operator/billing/subscriptions/{cid}/trends",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["years"] == []
    assert body["monthly_by_year"] == {}
    assert body["customer_name"] == "No Workbook Co"


def test_trends_endpoint_404_unknown_id(client):
    """An id matching neither a subscription nor a client → 404."""
    _, auth = _make_tenant()
    r = client.get("/v1/array-operator/billing/subscriptions/99999999/trends",
                   headers={"Authorization": auth})
    assert r.status_code == 404


# ─── GMP invoice attach hook (CONTRACT 2) ────────────────────────────────────

def test_generate_files_no_gmp_pdf_unchanged(tmp_path):
    match = build_match(types.SimpleNamespace(
        source_workbook=(FIX / "norwich.xlsx").read_bytes()))
    baseline = generate_files(match, ["pdf"], False, tmp_path)
    # Null gmp_invoice_pdf → identical attachment set, no GMP pdf.
    sub = types.SimpleNamespace(gmp_invoice_pdf=None)
    with_sub = generate_files(match, ["pdf"], False, tmp_path, sub=sub)
    assert [p.name for p in with_sub] == [p.name for p in baseline]
    assert not any(p.name.endswith("_GMP_invoice.pdf") for p in with_sub)


def test_generate_files_attaches_gmp_pdf_when_present(tmp_path):
    match = build_match(types.SimpleNamespace(
        source_workbook=(FIX / "norwich.xlsx").read_bytes()))
    blob = b"%PDF-1.4\n%fake GMP invoice\n"
    sub = types.SimpleNamespace(gmp_invoice_pdf=blob)
    paths = generate_files(match, ["pdf"], False, tmp_path, sub=sub)
    gmp = [p for p in paths if p.name.endswith("_GMP_invoice.pdf")]
    assert len(gmp) == 1
    assert gmp[0].read_bytes() == blob
    assert gmp[0].name.startswith("Norwich")
