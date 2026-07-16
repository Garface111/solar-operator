"""Eligibility for generation-report sends/digests (THE FOLD, Phase 1).

The scheduler + report digests used to gate on ``Tenant.product`` (skip
"array_operator"). Post-fold, MIGRATED AO tenants must keep receiving
scheduled sends, pre-send reviews, delivery receipts and the operator
directory. The AO arm keys on the EXPLICIT ``Tenant.generation_reports``
marker the fold migration flips — NOT on "has clients + cadence" inference,
which a 2026-07-16 prod probe proved unsafe (47 non-demo AO tenants already
have live capture-created Client rows + the default quarterly cadence):

  * nepool tenant w/ active clients + cadence          -> eligible (unchanged)
  * MIGRATED AO tenant (generation_reports) w/ clients -> eligible (NEW)
  * unmigrated AO tenant — even WITH capture clients   -> excluded (the
    load-bearing prod-safety pin; see the probe above)
  * demo tenant (even with seeded clients)             -> excluded, as today
  * inactive, non-comped/trialing tenant               -> excluded, as today

Also pins the one-brand direction: digests render in the TENANT's product
skin (an AO tenant gets AO-branded review/receipt emails, not NEPOOL ones).

NOTE: these jobs scan the whole (shared, session-scoped) test DB, so every
assertion here is scoped to THIS file's tenants/clients — other files' rows
may legitimately ride along in the same run.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, date

import pytest

from api.db import SessionLocal
from api.models import (Tenant, Client, Array, UtilityAccount, Bill,
                        ReportDelivery)
from api.report_eligibility import (tenant_reports_eligible,
                                    tenant_has_report_clients,
                                    tenant_in_reports_world)


def _mk_tenant(*, product: str = "nepool", active: bool = True,
               is_demo: bool = False, status: str | None = "active",
               frequency: str = "weekly",
               gen_reports: bool = False) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    email = f"{tid}@eligibility.test"
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name=f"Eligibility {tid[-4:]}", contact_email=email,
            tenant_key="k_" + secrets.token_hex(8), plan="standard",
            active=active, product=product, is_demo=is_demo,
            subscription_status=status, report_frequency=frequency,
            generation_reports=gen_reports,
        ))
        db.commit()
    return tid, email


def _mk_client(tid: str, *, name: str | None = None, active: bool = True,
               deleted: bool = False, contact_email: str | None = None) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name=name or ("Cl " + secrets.token_hex(3)),
                   active=active, contact_email=contact_email,
                   deleted_at=(datetime.utcnow() if deleted else None))
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    return cid


# ── the predicate itself ───────────────────────────────────────────────────────

def test_predicate_keys_on_the_migrated_marker_not_product():
    nep = Tenant(id="t1", name="n", contact_email="n@x", tenant_key="k1",
                 active=True, product="nepool")
    ao_migrated = Tenant(id="t2", name="a", contact_email="a@x", tenant_key="k2",
                         active=True, product="array_operator",
                         generation_reports=True)
    ao_plain = Tenant(id="t6", name="p", contact_email="p@x", tenant_key="k6",
                      active=True, product="array_operator",
                      generation_reports=False)
    assert tenant_in_reports_world(nep) is True         # legacy, unchanged
    assert tenant_in_reports_world(ao_migrated) is True # the fold marker
    # THE prod-safety pin: 47 non-demo AO tenants have live capture-created
    # Client rows in prod (2026-07-16 probe) — without the marker they must
    # stay exactly as excluded as under the old product gate.
    assert tenant_in_reports_world(ao_plain) is False
    assert tenant_reports_eligible(nep) is True
    assert tenant_reports_eligible(ao_migrated) is True
    assert tenant_reports_eligible(ao_plain) is False


def test_predicate_excludes_demo_and_inactive():
    demo = Tenant(id="t3", name="d", contact_email="d@x", tenant_key="k3",
                  active=True, product="array_operator", is_demo=True,
                  generation_reports=True)     # even migrated, demo never sends
    dead = Tenant(id="t4", name="i", contact_email="i@x", tenant_key="k4",
                  active=False, product="nepool", subscription_status="canceled")
    comped = Tenant(id="t5", name="c", contact_email="c@x", tenant_key="k5",
                    active=False, product="nepool", subscription_status="comped")
    assert tenant_reports_eligible(demo) is False
    assert tenant_reports_eligible(dead) is False
    assert tenant_reports_eligible(comped) is True      # comped survives inactive
    assert tenant_reports_eligible(None) is False


def test_has_report_clients_counts_only_live_active():
    tid, _ = _mk_tenant(product="array_operator")
    with SessionLocal() as db:
        assert tenant_has_report_clients(db, tid) is False
    _mk_client(tid, active=False)
    _mk_client(tid, deleted=True)
    with SessionLocal() as db:
        assert tenant_has_report_clients(db, tid) is False   # nothing live+active
    _mk_client(tid)
    with SessionLocal() as db:
        assert tenant_has_report_clients(db, tid) is True


# ── scheduler selection (_deliver_clients_with_frequency) ─────────────────────

def test_scheduler_picks_migrated_ao_tenant_and_excludes_the_rest(monkeypatch):
    from api import scheduler as sched

    nep_tid, _ = _mk_tenant(product="nepool")
    ao_tid, _ = _mk_tenant(product="array_operator", gen_reports=True)  # migrated
    plain_ao_tid, _ = _mk_tenant(product="array_operator")    # capture clients only
    demo_tid, _ = _mk_tenant(product="array_operator", is_demo=True,
                             gen_reports=True)
    dead_tid, _ = _mk_tenant(product="nepool", active=False, status="canceled")

    nep_cid = _mk_client(nep_tid)
    ao_cid = _mk_client(ao_tid)
    plain_ao_cid = _mk_client(plain_ao_tid)   # the 47-tenants-in-prod shape
    demo_cid = _mk_client(demo_tid)
    dead_cid = _mk_client(dead_tid)
    gone_cid = _mk_client(ao_tid, deleted=True)               # soft-deleted

    delivered: list[int] = []

    def fake_deliver(cid, **kw):
        delivered.append(cid)
        return {"ok": True, "email_sent": True, "client_id": cid,
                "client_name": f"c{cid}", "recipient": "x@y", "tenant": "t"}

    import api.delivery as delivery_mod
    monkeypatch.setattr(delivery_mod, "deliver_for_client", fake_deliver)
    monkeypatch.setattr(delivery_mod, "deliver_operator_directory",
                        lambda *a, **k: {"ok": True})
    import api.jobs.report_digests as rd
    monkeypatch.setattr(rd, "record_scheduled_batch", lambda *a, **k: 0)
    import api.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send_internal_alert", lambda *a, **k: True)

    sched._deliver_clients_with_frequency("weekly")

    assert nep_cid in delivered                     # nepool: unchanged
    assert ao_cid in delivered                      # MIGRATED AO: NEW, eligible
    assert plain_ao_cid not in delivered            # unmigrated AO w/ capture
                                                    # clients: MUST stay out
    assert demo_cid not in delivered                # demo: excluded as today
    assert dead_cid not in delivered                # inactive non-comped: excluded
    assert gone_cid not in delivered                # soft-deleted client: excluded


# ── pre-send review (run_presend_reviews) ─────────────────────────────────────

def test_presend_review_is_data_keyed_and_product_skinned(monkeypatch):
    import api.jobs.report_digests as rd

    nep_tid, nep_email = _mk_tenant(product="nepool")
    ao_tid, ao_email = _mk_tenant(product="array_operator", gen_reports=True)
    plain_ao_tid, plain_email = _mk_tenant(product="array_operator")  # unmigrated
    bare_ao_tid, bare_email = _mk_tenant(product="array_operator",
                                         gen_reports=True)            # no clients
    demo_tid, demo_email = _mk_tenant(product="array_operator", is_demo=True,
                                      gen_reports=True)

    _mk_client(nep_tid, contact_email="n-client@x.test")
    _mk_client(ao_tid, contact_email="a-client@x.test")
    _mk_client(plain_ao_tid, contact_email="p-client@x.test")  # capture client
    _mk_client(demo_tid, contact_email="d-client@x.test")

    # Freeze "now" so now()+2d lands on a Monday -> the weekly cadence fires.
    frozen = datetime(2026, 7, 11, 9, 0, 0)          # Saturday
    assert (frozen + timedelta(days=2)).weekday() == 0
    monkeypatch.setattr(rd, "now", lambda: frozen)

    sends: dict[str, dict] = {}

    def fake_send(to, subject, html, text=None, **kw):
        sends[to] = {"subject": subject, "html": html, "text": text,
                     "product": kw.get("product")}
        return True

    monkeypatch.setattr(rd, "_send_via_resend", fake_send)

    out = rd.run_presend_reviews()
    assert out.get("cadences") == ["weekly"]

    assert nep_email in sends                        # nepool operator reviewed
    assert ao_email in sends                         # migrated AO reviewed (NEW)
    assert plain_email not in sends                  # unmigrated AO w/ capture
                                                     # clients: MUST stay out
    assert bare_email not in sends                   # no clients -> no review
    assert demo_email not in sends                   # demo excluded

    # One-brand direction: the digest renders in the TENANT's product skin.
    assert sends[ao_email]["product"] == "array_operator"
    assert "arrayoperator.com" in sends[ao_email]["html"]
    assert "nepooloperator.com" not in sends[ao_email]["html"]
    assert sends[nep_email]["product"] == "nepool"
    # Post-fold (sunset lane) the nepool dashboard_url ALSO defaults to the
    # folded home — the dying domain must not be emitted for anyone.
    assert "arrayoperator.com" in sends[nep_email]["html"]
    assert "nepooloperator.com" not in sends[nep_email]["html"]


# ── delivery receipt (run_delivery_receipts) ──────────────────────────────────

def test_delivery_receipt_reaches_migrated_ao_and_stamps_the_rest(monkeypatch):
    import api.jobs.report_digests as rd

    ao_tid, ao_email = _mk_tenant(product="array_operator", gen_reports=True)
    plain_ao_tid, plain_email = _mk_tenant(product="array_operator")
    nep_tid, nep_email = _mk_tenant(product="nepool")
    demo_tid, demo_email = _mk_tenant(product="array_operator", is_demo=True,
                                      gen_reports=True)

    aged = datetime.utcnow() - timedelta(hours=3)    # past the 90-min window
    row_ids: dict[str, int] = {}
    with SessionLocal() as db:
        for key, tid in (("ao", ao_tid), ("plain", plain_ao_tid),
                         ("nep", nep_tid), ("demo", demo_tid)):
            r = ReportDelivery(tenant_id=tid, client_id=None,
                               client_name=f"{key} client", recipient="c@x.test",
                               cadence="weekly", status="sent", sent_at=aged)
            db.add(r)
            db.flush()
            row_ids[key] = r.id
        db.commit()

    sends: dict[str, dict] = {}

    def fake_send(to, subject, html, text=None, **kw):
        sends[to] = {"html": html, "product": kw.get("product")}
        return True

    monkeypatch.setattr(rd, "_send_via_resend", fake_send)

    rd.run_delivery_receipts()

    assert ao_email in sends                          # migrated AO receipt (NEW)
    assert sends[ao_email]["product"] == "array_operator"
    assert "arrayoperator.com" in sends[ao_email]["html"]
    assert nep_email in sends                         # nepool unchanged
    assert sends[nep_email]["product"] == "nepool"
    assert plain_email not in sends                   # unmigrated AO: stamped only
    assert demo_email not in sends                    # demo never emailed

    with SessionLocal() as db:                        # every row stamped once
        for key, rid in row_ids.items():
            assert db.get(ReportDelivery, rid).receipt_sent_at is not None, key


# ── operator directory (deliver_operator_directory) ───────────────────────────

def test_operator_directory_keys_on_client_presence(monkeypatch):
    from api.delivery import deliver_operator_directory
    import api.delivery as delivery_mod

    ref = date(2024, 4, 1)                            # window Q4'22..Q1'24

    ao_tid, _ = _mk_tenant(product="array_operator", gen_reports=True)
    cid = _mk_client(ao_tid)
    with SessionLocal() as db:
        a = Array(tenant_id=ao_tid, client_id=cid, name="Dir Arr",
                  nepool_gis_id="777")
        db.add(a)
        db.flush()
        ua = UtilityAccount(tenant_id=ao_tid, array_id=a.id, provider="gmp",
                            account_number="DIR_" + secrets.token_hex(3))
        db.add(ua)
        db.flush()
        db.add(Bill(tenant_id=ao_tid, account_id=ua.id,
                    bill_date=datetime(2024, 1, 15),
                    period_start=datetime(2024, 1, 1), kwh_generated=2000,
                    document_number="doc-" + secrets.token_hex(3)))
        db.commit()

    sent = {}
    monkeypatch.setattr(delivery_mod, "send_workbook_email",
                        lambda **kw: sent.update(kw) or True)

    # MIGRATED AO tenant with clients + data -> directory goes out
    # (used to be product-refused: "not a NEPOOL tenant")
    out = deliver_operator_directory(ao_tid, reference_date=ref)
    assert out["ok"] is True and out["sheet_count"] == 1
    assert sent.get("to")

    # UNMIGRATED AO tenant — even with a capture client -> refused
    plain_tid, _ = _mk_tenant(product="array_operator")
    _mk_client(plain_tid)
    out1 = deliver_operator_directory(plain_tid, reference_date=ref)
    assert out1["ok"] is False and out1["reason"] == "generation reports not enabled"

    # migrated AO tenant WITHOUT clients -> data-presence refusal
    bare_tid, _ = _mk_tenant(product="array_operator", gen_reports=True)
    out2 = deliver_operator_directory(bare_tid, reference_date=ref)
    assert out2["ok"] is False and out2["reason"] == "no report clients"

    # demo tenant (even migrated, even with a client) -> refused
    demo_tid, _ = _mk_tenant(product="array_operator", is_demo=True,
                             gen_reports=True)
    _mk_client(demo_tid)
    out3 = deliver_operator_directory(demo_tid, reference_date=ref)
    assert out3["ok"] is False and out3["reason"] == "demo tenant"
