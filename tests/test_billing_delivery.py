"""Tests for the Array Operator billing endpoints + delivery pipeline.

Covers: /match preview, subscription create/list/patch/delete, the recipient
slider (to me / to client / to both), format selection, and a dry-run send that
mocks Resend so no real email goes out.
"""
from __future__ import annotations

import pathlib
import secrets
from datetime import date

import pytest
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, Client, BillingReportSubscription, Array, DailyGeneration

FIX = pathlib.Path(__file__).parent / "fixtures" / "billing"


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Billing Test Operator",
            contact_email=f"{tid}@operator.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _upload(client, auth, fixture="fairlee.xlsx", **form):
    data = (FIX / fixture).read_bytes()
    files = {"file": (fixture, data,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return client.post("/v1/array-operator/billing/subscriptions",
                       files=files, data=form, headers={"Authorization": auth})


# ─── /match ─────────────────────────────────────────────────────────────────

def test_match_preview_saves_nothing(client):
    _, auth = _make_tenant()
    data = (FIX / "norwich.xlsx").read_bytes()
    r = client.post("/v1/array-operator/billing/match",
                    files={"file": ("norwich.xlsx", data, "application/octet-stream")},
                    headers={"Authorization": auth})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"]
    assert body["match"]["customer"]["name"] == "Norwich Fire District"
    assert body["match"]["billing_model"] == "percent_of_array"
    # nothing persisted
    with SessionLocal() as db:
        assert db.execute(select(BillingReportSubscription)).first() is None


def test_match_requires_auth(client):
    data = (FIX / "norwich.xlsx").read_bytes()
    r = client.post("/v1/array-operator/billing/match",
                    files={"file": ("n.xlsx", data, "application/octet-stream")})
    assert r.status_code == 401


# ─── subscription lifecycle ─────────────────────────────────────────────────

def test_create_subscription_links_client_and_defaults_to_me(client):
    tid, auth = _make_tenant()
    r = _upload(client, auth, "fairlee.xlsx", cadence="monthly")
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    assert sub["customer_name"] == "Town of Fairlee"
    assert sub["send_mode"] == "to_me"          # safe default — no customer email yet
    assert sub["cadence"] == "monthly"
    assert sub["next_send_at"]
    # A Client row was created underneath the operator.
    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.tenant_id == tid)).scalar_one()
        assert c.name == "Town of Fairlee"
        s = db.execute(select(BillingReportSubscription)).scalar_one()
        assert s.source_workbook  # workbook bytes stored
        assert s.client_id == c.id


def test_list_and_patch_slider_and_formats(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth, "fairlee.xlsx").json()["subscription"]["id"]

    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    assert len(lst["subscriptions"]) == 1

    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"send_mode": "to_both", "client_email": "town@fairlee.gov",
                           "formats": ["pdf", "xlsx"], "cadence": "quarterly"},
                     headers={"Authorization": auth})
    assert r.status_code == 200
    s = r.json()["subscription"]
    assert s["send_mode"] == "to_both"
    assert s["client_email"] == "town@fairlee.gov"
    assert set(s["formats"]) == {"pdf", "xlsx"}
    assert s["cadence"] == "quarterly"


def test_patch_rejects_bad_send_mode(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"send_mode": "to_everyone"},
                     headers={"Authorization": auth})
    assert r.status_code == 400


def test_delete_is_soft(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    assert client.delete(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                         headers={"Authorization": auth}).status_code == 200
    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    assert lst["subscriptions"] == []


def test_tenant_isolation(client):
    _, auth_a = _make_tenant()
    _, auth_b = _make_tenant()
    sub_id = _upload(client, auth_a).json()["subscription"]["id"]
    # B cannot see or touch A's subscription.
    assert client.get("/v1/array-operator/billing/subscriptions",
                      headers={"Authorization": auth_b}).json()["subscriptions"] == []
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"enabled": False}, headers={"Authorization": auth_b})
    assert r.status_code == 404


# ─── preview ────────────────────────────────────────────────────────────────

def test_preview_invoice_pdf_streams(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth, "valley_cares.xlsx").json()["subscription"]["id"]
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sub_id}/preview",
                   params={"kind": "invoice", "fmt": "pdf"},
                   headers={"Authorization": auth})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


# ─── delivery (mocked Resend) ───────────────────────────────────────────────

def test_send_now_test_goes_to_operator(client, monkeypatch):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "fairlee.xlsx",
                     send_mode="to_client", client_email="town@fairlee.gov",
                     formats='["pdf","xlsx"]').json()["subscription"]["id"]

    captured = {}

    def fake_send(to, subject, html, text, attachments=None, from_addr=None,
                  reply_to=None, product="nepool"):
        captured.update(to=to, subject=subject, attachments=attachments, product=product)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "true"}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    # Test send always goes to the operator, never the customer.
    to = captured["to"]
    to_list = to if isinstance(to, list) else [to]
    assert any("operator.test" in addr for addr in to_list)
    assert all("fairlee.gov" not in addr for addr in to_list)
    # Both formats produced invoice + summary attachments.
    names = [a["filename"] for a in captured["attachments"]]
    assert any(n.endswith("_invoice.pdf") for n in names)
    assert any(n.endswith("_invoice.xlsx") for n in names)
    assert any("summary" in n for n in names)


def test_send_now_live_to_client_stamps_schedule(client, monkeypatch):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx",
                     send_mode="to_both", client_email="nfd@norwich.gov",
                     formats="pdf").json()["subscription"]["id"]

    captured = {}
    monkeypatch.setattr("api.notify._send_via_resend",
                        lambda **kw: captured.update(kw) or True)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "false"}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    # to_both → client is primary, operator cc'd.
    to = captured["to"]
    to_list = to if isinstance(to, list) else [to]
    assert any("norwich.gov" in a for a in to_list)
    # Live send stamps the schedule fields.
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        assert s.last_sent_at is not None
        assert s.next_send_at is not None
        assert s.last_invoice_number  # e.g. "2026-05"


def test_send_now_to_client_without_email_errors(client, monkeypatch):
    _, auth = _make_tenant()
    # Fairlee workbook has no customer email; force to_client with none set.
    sub_id = _upload(client, auth, "fairlee.xlsx").json()["subscription"]["id"]
    client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                 json={"send_mode": "to_client", "client_email": ""},
                 headers={"Authorization": auth})
    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "false"}, headers={"Authorization": auth})
    assert r.status_code == 422


# ─── manual customer-input path (no workbook) ───────────────────────────────


def _make_array_with_generation(tid: str, kwh_per_day: float = 100.0,
                                days: int = 30) -> int:
    """An array with a recent full month of DailyGeneration rows. Returns its id."""
    from datetime import date, timedelta
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Manual Co", active=True)
        db.add(c)
        db.flush()
        arr = Array(tenant_id=tid, name="Manual Array", client_id=c.id,
                    fuel_type="solar")
        db.add(arr)
        db.flush()
        aid = arr.id
        # Most-recent complete month: anchor on the 1st of last month.
        today = date.today()
        first_this = today.replace(day=1)
        anchor = (first_this - timedelta(days=1)).replace(day=1)  # 1st of last month
        for i in range(days):
            d = anchor + timedelta(days=i)
            if d.month != anchor.month:
                break
            db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=d,
                                   kwh=kwh_per_day, source="csv"))
        db.commit()
    return aid


def _create_manual(client, auth, **form):
    """POST the subscriptions endpoint as multipart/form-data WITHOUT a file."""
    return client.post("/v1/array-operator/billing/subscriptions",
                       data=form, headers={"Authorization": auth})


def test_manual_subscription_no_file_creates_and_stores_allocation(client):
    """The manual customer-input path: NO xlsx, just typed fields. Asserts 200,
    the typed allocation is stored, and the sub appears in GET /subscriptions.

    Old behavior (proof this would FAIL pre-change): create_subscription did
    `if file is None: raise HTTPException(400, "Upload the billing workbook …")`,
    so this same request returned 400 and stored nothing.
    """
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid, kwh_per_day=100.0, days=30)

    r = _create_manual(client, auth,
                       customer_name="Paul Bozuwa", array_id=aid,
                       allocation_pct="0.25", cadence="monthly",
                       delivery_mode="approval", send_mode="to_me",
                       client_email="paul@example.com")
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    assert sub["customer_name"] == "Paul Bozuwa"
    assert sub["array_id"] == aid
    assert abs(sub["allocation_pct"] - 0.25) < 1e-9
    assert sub["billing_model"] == "percent_of_array"
    assert sub["cadence"] == "monthly"
    assert sub["next_send_at"]

    # Stored without a workbook, with the typed allocation.
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub["id"])
        assert s.source_workbook is None
        assert s.allocation_pct == 0.25
        assert s.array_id == aid

    # Appears in the list.
    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    assert sub["id"] in [x["id"] for x in lst["subscriptions"]]


def test_manual_subscription_rejects_bad_allocation(client):
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid)
    # > 1.0 (caller must pass a fraction).
    r = _create_manual(client, auth, customer_name="Bad Pct", array_id=aid,
                       allocation_pct="1.5")
    assert r.status_code == 400
    # Missing array_id.
    r = _create_manual(client, auth, customer_name="No Array",
                       allocation_pct="0.5")
    assert r.status_code == 400


def test_manual_subscription_computes_customer_share_on_send(client, monkeypatch):
    """A manual sub with no workbook still produces a real invoice: the
    customer share = allocation_pct × the array's period generation."""
    tid, auth = _make_tenant()
    # 30 days × 100 kWh = 3000 kWh for the array's recent month.
    aid = _make_array_with_generation(tid, kwh_per_day=100.0, days=30)
    sub_id = _create_manual(client, auth, customer_name="Share Test",
                            array_id=aid, allocation_pct="0.10",
                            send_mode="to_me").json()["subscription"]["id"]

    captured = {}

    def fake_send(to, subject, html, text, attachments=None, from_addr=None,
                  reply_to=None, product="nepool"):
        captured.update(to=to, attachments=attachments)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "true"}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["ok"]
    # 10% of 3000 kWh = 300 kWh → priced at the default VT net rate (0.21,
    # provider default) with the default 10% discount: 300 × 0.21 × 0.9 = 56.70.
    assert result["amount_owed"] == pytest.approx(56.70, abs=0.5)
    # An invoice attachment was produced from the synthesized match.
    names = [a["filename"] for a in captured["attachments"]]
    assert any(n.endswith("_invoice.pdf") for n in names)


def test_manual_subscription_array_must_be_owned(client):
    tid_a, auth_a = _make_tenant()
    tid_b, auth_b = _make_tenant()
    aid = _make_array_with_generation(tid_a)
    # Tenant B cannot attach a manual customer to tenant A's array.
    r = _create_manual(client, auth_b, customer_name="Cross Tenant",
                       array_id=aid, allocation_pct="0.2")
    assert r.status_code == 404


# ─── scheduler ──────────────────────────────────────────────────────────────


def test_scheduler_monthly_billing_delivers(client, monkeypatch):
    """The scheduler job picks up THIS tenant's enabled monthly sub. With
    delivery_mode='auto' it sends straight to the recipient. (Asserts on our own
    sub id — the session-scoped test DB accumulates subs from other tests, so
    exact counts aren't meaningful.)"""
    from api import scheduler
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx", cadence="monthly",
                     delivery_mode="auto", send_mode="to_me").json()["subscription"]["id"]

    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    result = scheduler.deliver_billing_reports("monthly")
    assert sub_id in result["sent"]
    assert sub_id not in result["failed"]
    # And it stamped the schedule on our sub.
    with SessionLocal() as db:
        assert db.get(BillingReportSubscription, sub_id).last_sent_at is not None


# ─── billing rate ($/kWh): global default + per-customer override ────────────


def _math(client, auth, sub_id):
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sub_id}/preview-math",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    return r.json()


def test_rate_global_and_per_customer_override(client):
    """Legacy flat-rate back-compat under the discount model. A flat rate (per
    customer or global) is treated as a net rate with 0 discount, so the billed
    amount == kWh × flat_rate (unchanged dollars), and rate_source reflects the
    legacy_flat provenance. With NO rate set, the new default is 10% off the VT
    net rate."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid, kwh_per_day=100.0, days=30)  # 3000 kWh
    pct = 0.40
    cust_kwh = round(3000 * pct, 2)  # 1200.0

    sub_id = _create_manual(client, auth, customer_name="Rate Co", array_id=aid,
                            allocation_pct=str(pct), cadence="monthly",
                            send_mode="to_me").json()["subscription"]["id"]

    # A) no rate anywhere → default discount (10%) off the resolved VT net rate.
    #    No UtilityAccount on this array → provider default 0.21 (api/rates.py).
    a = _math(client, auth, sub_id)
    assert a["discount_source"] == "default"
    assert abs(a["discount_pct"] - 0.10) < 1e-9
    NET_DEFAULT = 0.21
    assert abs(a["net_rate_per_kwh"] - NET_DEFAULT) < 1e-6
    assert abs(a["effective_rate_per_kwh"] - round(NET_DEFAULT * 0.9, 6)) < 1e-6
    assert a["customer_kwh"] == cust_kwh
    # savings = kWh × net × discount
    assert abs(a["solar_savings_usd"] - round(cust_kwh * NET_DEFAULT * 0.10, 2)) < 0.02

    # B) legacy global flat rate 0.20 → billed at exactly 0.20 (0 discount).
    r = client.put("/v1/array-operator/billing/global-rate",
                   json={"default_billing_rate_per_kwh": 0.20},
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    b = _math(client, auth, sub_id)
    assert b["rate_source"] == "legacy_flat"
    assert abs(b["rate"] - 0.20) < 1e-9
    assert abs(b["amount_usd"] - round(cust_kwh * 0.20, 2)) < 0.01

    # C) per-customer legacy flat override wins over the global rate.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"rate_per_kwh": 0.14},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert abs(r.json()["subscription"]["rate_per_kwh"] - 0.14) < 1e-9
    c = _math(client, auth, sub_id)
    assert abs(c["amount_usd"] - round(cust_kwh * 0.14, 2)) < 0.01

    # D) clearing the override (null) falls back to the global flat rate.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"rate_per_kwh": None},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["subscription"]["rate_per_kwh"] is None
    d = _math(client, auth, sub_id)
    assert abs(d["amount_usd"] - round(cust_kwh * 0.20, 2)) < 0.01


def test_discount_model_global_and_per_customer(client):
    """The discount billing model: invoice = kWh × net_rate × (1 − discount).
    Default 10% off; editable globally and per-customer; savings reported."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid, kwh_per_day=100.0, days=30)  # 3000 kWh
    pct = 0.50
    cust_kwh = round(3000 * pct, 2)  # 1500.0
    NET = 0.21   # no UtilityAccount → provider default net rate (api/rates.py)

    sub_id = _create_manual(client, auth, customer_name="Disc Co", array_id=aid,
                            allocation_pct=str(pct), cadence="monthly",
                            send_mode="to_me").json()["subscription"]["id"]

    # A) default 10% off the VT net rate.
    a = _math(client, auth, sub_id)
    assert abs(a["amount_usd"] - round(cust_kwh * NET * 0.90, 2)) < 0.02

    # B) set a GLOBAL discount of 25% (and an explicit global net rate of 0.20).
    r = client.put("/v1/array-operator/billing/global-rate",
                   json={"default_discount_pct": 0.25, "default_net_rate_per_kwh": 0.20},
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    b = _math(client, auth, sub_id)
    assert b["net_rate_source"] == "global"
    assert b["discount_source"] == "global"
    assert abs(b["discount_pct"] - 0.25) < 1e-9
    assert abs(b["amount_usd"] - round(cust_kwh * 0.20 * 0.75, 2)) < 0.02

    # C) per-customer discount override (40%) wins over the global 25%.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"discount_pct": 0.40},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    c = _math(client, auth, sub_id)
    assert c["discount_source"] == "customer"
    assert abs(c["amount_usd"] - round(cust_kwh * 0.20 * 0.60, 2)) < 0.02
    assert abs(c["solar_savings_usd"] - round(cust_kwh * 0.20 * 0.40, 2)) < 0.02

    # D) clearing the per-customer discount falls back to the global 25%.
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"discount_pct": None},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    d = _math(client, auth, sub_id)
    assert d["discount_source"] == "global"
    assert abs(d["amount_usd"] - round(cust_kwh * 0.20 * 0.75, 2)) < 0.02

    # E) a discount ≥ 1 is rejected (would zero/inverse the bill).
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"discount_pct": 1.5},
                     headers={"Authorization": auth})
    assert r.status_code == 400


def test_rate_rejects_out_of_range(client):
    """A fat-fingered rate (negative or absurdly high) is rejected, so a units
    mistake can't silently produce a wild invoice."""
    tid, auth = _make_tenant()
    aid = _make_array_with_generation(tid)
    r = _create_manual(client, auth, customer_name="Bad Rate", array_id=aid,
                       allocation_pct="0.25", rate_per_kwh="99")
    assert r.status_code == 400
    r = client.put("/v1/array-operator/billing/global-rate",
                   json={"default_billing_rate_per_kwh": -1},
                   headers={"Authorization": auth})
    assert r.status_code == 400


def test_kwh_source_prefers_gmp_else_falls_back(client):
    """Source-agnostic period generation: when GMP daily-read has coverage the
    invoice is sourced from it (kwh_source='gmp_api'); otherwise it falls back
    to DailyGeneration ('daily_csv'). Verified via the live preview-math route."""
    from api.models import Client as ClientM, Array, UtilityAccount, \
        DailyGeneration, GmpDailyGeneration
    from datetime import date, timedelta

    tid, auth = _make_tenant()
    # Array with a month of DailyGeneration (the fallback source).
    today = date.today()
    anchor = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    with SessionLocal() as db:
        c = ClientM(tenant_id=tid, name="Src Co", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="Src Array", client_id=c.id, fuel_type="solar")
        db.add(arr); db.flush()
        aid = arr.id
        for i in range(28):
            db.add(DailyGeneration(tenant_id=tid, array_id=aid,
                                   day=anchor + timedelta(days=i), kwh=50.0, source="csv"))
        db.commit()

    sid = _create_manual(client, auth, customer_name="Src Cust", array_id=aid,
                         allocation_pct="0.50", send_mode="to_me",
                         ).json()["subscription"]["id"]

    # No GMP rows yet → falls back to DailyGeneration.
    a = _math(client, auth, sid)
    assert a["kwh_source"] == "daily_csv"
    assert a["has_data"] is True

    # Now add GMP daily-read coverage for a DIFFERENT, later month via a GMP
    # utility account — the adapter must PREFER it.
    gmp_anchor = anchor  # same month is fine; distinct source table
    with SessionLocal() as db:
        ua = UtilityAccount(tenant_id=tid, array_id=aid, provider="gmp",
                            account_number="GMP-TEST-1", enabled=True)
        db.add(ua); db.flush()
        for i in range(28):
            db.add(GmpDailyGeneration(
                tenant_id=tid, account_id=ua.id, account_number="GMP-TEST-1",
                array_id=aid, day=gmp_anchor + timedelta(days=i),
                kwh=70.0, interval_count=96, source="gmp_api"))
        db.commit()

    b = _math(client, auth, sid)
    assert b["kwh_source"] == "gmp_api", b
    # GMP month total 28*70=1960 → 50% share = 980 kWh (distinct from the 50/day CSV).
    assert abs(b["array_total_kwh"] - 1960.0) < 1.0
    assert abs(b["customer_kwh"] - 980.0) < 1.0


def test_auto_attach_gmp_bill_when_captured_else_nothing(client, monkeypatch):
    """The auto-attach toggle: when a durable GMP bill PDF is captured for the
    array+period, it rides the email automatically; when none is captured,
    nothing is attached (never fabricated). Manual upload is unaffected."""
    import pathlib, tempfile
    from datetime import date as _date
    from api.billing import delivery
    from api.models import Client as ClientM, Array, BillingReportSubscription
    from api.billing.matcher import BillingMatch, Period

    tid, auth = _make_tenant()
    with SessionLocal() as db:
        c = ClientM(tenant_id=tid, name="Auto Co", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="Auto Array", client_id=c.id, fuel_type="solar")
        db.add(arr); db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="Auto Cust",
            array_id=arr.id, allocation_pct=0.5, billing_model="percent_of_array",
            auto_attach_gmp=True, cadence="monthly", send_mode="to_me")
        db.add(sub); db.commit()
        sub_id, array_id = sub.id, arr.id

    period = Period(month="2026-05", start=_date(2026, 5, 1), end=_date(2026, 5, 31),
                    array_kwh=1000.0, customer_kwh=500.0)
    match = BillingMatch(
        matched=True, confidence=1.0, source="manual",
        customer={"name": "Auto Cust"}, billing_model="percent_of_array",
        periods=[period], latest_period=period,
        computed_invoice={"invoice_number": "2026-05", "period_start": "2026-05-01",
                          "period_end": "2026-05-31", "amount_owed": 100.0, "kwh": 500},
    )

    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)

        # 1) No captured PDF → nothing attached (read seam returns None today).
        #    formats=[] so only the GMP-attach branch runs (no invoice render).
        with tempfile.TemporaryDirectory() as tmp:
            paths = delivery.generate_files(match, [], False,
                                            pathlib.Path(tmp), sub=sub)
            assert not any("GMP_bill" in p.name for p in paths)

        # 2) Simulate ingestion having landed a durable PDF → auto-attached.
        monkeypatch.setattr(
            "api.reports.gmp_bill_pdf_read.get_bill_pdf_for_period",
            lambda aid, ps=None, pe=None, **kw: {
                "bytes": b"%PDF-1.4\nGMP bill\n", "filename": "GMP_bill_2026-05.pdf",
                "content_type": "application/pdf", "account_id": 1,
                "period_start": None, "period_end": None})
        with tempfile.TemporaryDirectory() as tmp:
            paths = delivery.generate_files(match, [], False,
                                            pathlib.Path(tmp), sub=sub)
            assert any("GMP_bill" in p.name for p in paths), [p.name for p in paths]
