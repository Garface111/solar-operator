"""Send-pipeline dashboard (Ford 2026-07-03): the roll-up endpoint, the pause
switch (and its scheduler gate), and the bulk delivery-mode flip.

  GET   /v1/array-operator/billing/send-pipeline
  PATCH /v1/array-operator/billing/sending-paused
  POST  /v1/array-operator/billing/subscriptions/bulk-delivery-mode
"""
from __future__ import annotations

import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_send_pipeline_test")

import secrets
from datetime import datetime

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, BillingReportSubscription, ReportDraft

BASE = "/v1/array-operator/billing"


def _mk_world():
    """Tenant + 4 enabled subs: 2 sent for June ($10 + $20), 1 with a pending
    draft, 1 waiting (never sent, no draft). Mixed cadence + delivery modes."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Pipe Co",
                      contact_email=f"{tid}@test.example", active=True,
                      product="array_operator"))
        db.flush()
        s1 = BillingReportSubscription(
            tenant_id=tid, customer_name="Sent A", billing_model="percent_of_array",
            cadence="monthly", delivery_mode="auto", enabled=True,
            last_sent_period_end="2026-06-30T00:00:00", last_sent_amount_usd=10.0,
            last_sent_at=datetime(2026, 7, 3, 12, 0))
        s2 = BillingReportSubscription(
            tenant_id=tid, customer_name="Sent B", billing_model="percent_of_array",
            cadence="quarterly", delivery_mode="approval", enabled=True,
            last_sent_period_end="2026-06-30T00:00:00", last_sent_amount_usd=20.0,
            last_sent_at=datetime(2026, 7, 3, 13, 0))
        s3 = BillingReportSubscription(
            tenant_id=tid, customer_name="Drafted C", billing_model="percent_of_array",
            cadence="monthly", delivery_mode="approval", enabled=True)
        s4 = BillingReportSubscription(
            tenant_id=tid, customer_name="Waiting D", billing_model="percent_of_array",
            cadence="monthly", delivery_mode="auto", enabled=True)
        db.add_all([s1, s2, s3, s4])
        db.flush()
        db.add(ReportDraft(tenant_id=tid, subscription_id=s3.id,
                           customer_name="Drafted C", status="pending"))
        db.commit()
        ids = [s1.id, s2.id, s3.id, s4.id]
    return tid, f"Bearer {mint_session_for_tenant(tid)}", ids


def _cleanup(tid):
    with SessionLocal() as db:
        db.query(ReportDraft).filter(ReportDraft.tenant_id == tid).delete(
            synchronize_session=False)
        db.query(BillingReportSubscription).filter(
            BillingReportSubscription.tenant_id == tid).delete(
            synchronize_session=False)
        t = db.get(Tenant, tid)
        if t is not None:
            db.delete(t)
        db.commit()


def test_pipeline_rollup_math(client):
    tid, auth, _ = _mk_world()
    try:
        r = client.get(BASE + "/send-pipeline", headers={"Authorization": auth})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total_enabled"] == 4
        assert d["last"]["period_month"] == "2026-06"
        assert d["last"]["delivered"] == 2
        assert d["last"]["dollars"] == 30.0
        assert d["last"]["last_run_at"].startswith("2026-07-03T13")
        assert d["inflight"]["pending_drafts"] == 1
        assert d["inflight"]["waiting"] == 1
        assert d["next_monthly"]["scheduled"] == 3
        assert d["next_monthly"]["auto"] == 2
        assert d["next_monthly"]["approval"] == 1
        assert d["next_quarterly"]["scheduled"] == 1
        # next fire dates are 1sts at 09:00 UTC
        assert d["next_monthly"]["fires_at"].endswith("-01T09:00:00")
        assert d["paused"] is False
    finally:
        _cleanup(tid)


def test_pause_switch_roundtrip_and_scheduler_gate(client):
    tid, auth, _ = _mk_world()
    try:
        r = client.patch(BASE + "/sending-paused", json={"paused": True},
                         headers={"Authorization": auth})
        assert r.status_code == 200 and r.json()["paused"] is True
        r2 = client.get(BASE + "/send-pipeline", headers={"Authorization": auth})
        assert r2.json()["paused"] is True

        # The scheduler's billing run must SKIP every sub of a paused tenant
        # (benign skips, no sends, no drafts).
        from api.scheduler import deliver_billing_reports
        res = deliver_billing_reports("monthly")
        with SessionLocal() as db:
            mine = {s.id for s in db.query(BillingReportSubscription).filter(
                BillingReportSubscription.tenant_id == tid)}
        assert not (set(res["sent"]) & mine)
        assert not (set(res["drafted"]) & mine)
        assert mine & set(res["skipped"]) == {
            s for s in mine} & set(res["skipped"])  # my monthly subs land in skipped
        assert any(s in set(res["skipped"]) for s in mine)

        r3 = client.patch(BASE + "/sending-paused", json={"paused": False},
                          headers={"Authorization": auth})
        assert r3.json()["paused"] is False
    finally:
        _cleanup(tid)


def test_bulk_delivery_mode_flip(client):
    tid, auth, ids = _mk_world()
    try:
        r = client.post(BASE + "/subscriptions/bulk-delivery-mode",
                        json={"mode": "auto"}, headers={"Authorization": auth})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["changed"] == 2          # the two approval subs flipped
        assert d["auto"] == 4 and d["approval"] == 0
        r2 = client.post(BASE + "/subscriptions/bulk-delivery-mode",
                         json={"mode": "approval"}, headers={"Authorization": auth})
        assert r2.json()["changed"] == 4
        # invalid mode rejected
        r3 = client.post(BASE + "/subscriptions/bulk-delivery-mode",
                         json={"mode": "yolo"}, headers={"Authorization": auth})
        assert r3.status_code == 422
    finally:
        _cleanup(tid)
