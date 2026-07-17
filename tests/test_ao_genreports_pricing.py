"""Metered generation-reports billing — $15/client/QUARTER on first OUTPUT (THE FOLD).

Ford's FINAL model: building + previewing + auto-propagating the fleet is FREE; the
$15 fires on the FIRST real OUTPUT for a (client, calendar quarter) — a report SEND
(auto or manual) OR a DOWNLOAD of the deliverable (per-client or all-clients
directory) — then unlimited that quarter. Idempotent per (tenant, client, quarter).
The scheduler only auto-sends clients enrolled with auto_send=True. Pushing charges
to Stripe is a separate job, INERT until STRIPE_AO_GENREPORTS_PRICE_ID is minted.

These queries scan the shared session-scoped test DB, so every assertion is scoped to
THIS file's own tenants.
"""
from __future__ import annotations

import secrets
from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import select, func

from api.db import SessionLocal
from api.models import Tenant, Client, GenReportCharge
from api import pricing_ao_genreports as genrep
from api.delivery import (
    record_genreport_output, record_genreport_directory, deliver_for_client,
)

# Two reference dates that resolve to DIFFERENT complete quarters (Q2 vs Q1 2026),
# so quarter-idempotency tests are deterministic (see gmcs_writer._rolling_quarters).
REF_Q2 = date(2026, 7, 1)   # last complete quarter = 2026-Q2
REF_Q1 = date(2026, 4, 1)   # last complete quarter = 2026-Q1


@pytest.fixture(autouse=True)
def _always_has_data(monkeypatch):
    """Default: every client "has data" for the quarter, so the require_data gate
    passes. Individual tests override to False to prove the empty-output case."""
    monkeypatch.setattr("api.writers.gmcs_writer.report_has_data",
                        lambda *a, **k: True)


# ── fixtures ────────────────────────────────────────────────────────────────────

def _mk_tenant(*, product: str = "array_operator", gen_reports: bool = True,
               active: bool = True, is_demo: bool = False,
               status: str | None = "active", frequency: str = "weekly") -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name=f"Genrep {tid[-4:]}", contact_email=f"{tid}@genrep.test",
            tenant_key="k_" + secrets.token_hex(8), plan="standard",
            active=active, product=product, is_demo=is_demo,
            subscription_status=status, generation_reports=gen_reports,
            report_frequency=frequency,
        ))
        db.commit()
    return tid


def _mk_client(tid: str, *, contact_email: str | None = "client@genrep.test",
               auto_send: bool = False) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Cl " + secrets.token_hex(3),
                   active=True, contact_email=contact_email, auto_send=auto_send)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    return cid


def _rows(tid: str) -> list[tuple]:
    with SessionLocal() as db:
        return db.execute(
            select(GenReportCharge.quarter, GenReportCharge.amount_cents,
                   GenReportCharge.first_source, GenReportCharge.pushed_at)
            .where(GenReportCharge.tenant_id == tid)
            .order_by(GenReportCharge.id)
        ).all()


def _count(tid: str) -> int:
    with SessionLocal() as db:
        return int(db.execute(
            select(func.count()).select_from(GenReportCharge)
            .where(GenReportCharge.tenant_id == tid)
        ).scalar() or 0)


# ── the unit price ──────────────────────────────────────────────────────────────

def test_price_is_fifteen_dollars():
    assert genrep.PRICE_CENTS == 1500
    assert genrep.PER_CLIENT_CENTS == 1500


@pytest.mark.parametrize("n,cents", [
    (0, 0), (1, 1500), (2, 3000), (3, 4500), (10, 15000), (-5, 0), (None, 0),
])
def test_compute_monthly_cents(n, cents):
    assert genrep.compute_monthly_cents(n) == cents


# ── first output bills once; repeat outputs free (record_genreport_output) ──────

def test_first_download_records_one_15_dollar_row():
    tid = _mk_tenant()
    cid = _mk_client(tid)
    assert record_genreport_output(tid, cid, reference_date=REF_Q2,
                                   first_source="download") is True
    rows = _rows(tid)
    assert len(rows) == 1
    quarter, amount, source, pushed = rows[0]
    assert amount == 1500
    assert quarter == "2026-Q2"
    assert source == "download"
    assert pushed is None            # not pushed to Stripe yet


def test_second_output_same_client_quarter_is_free():
    tid = _mk_tenant()
    cid = _mk_client(tid)
    for src in ("download", "send", "directory", "download"):
        record_genreport_output(tid, cid, reference_date=REF_Q2, first_source=src)
    assert _count(tid) == 1          # first output billed; the rest free


def test_send_then_download_same_quarter_charges_once():
    """A send and a download of the SAME client-quarter = one $15 (either can be first)."""
    tid = _mk_tenant()
    cid = _mk_client(tid)
    assert record_genreport_output(tid, cid, reference_date=REF_Q2, first_source="send") is True
    assert record_genreport_output(tid, cid, reference_date=REF_Q2, first_source="download") is False
    assert _count(tid) == 1


def test_different_quarter_is_a_new_charge():
    tid = _mk_tenant()
    cid = _mk_client(tid)
    record_genreport_output(tid, cid, reference_date=REF_Q2, first_source="download")
    record_genreport_output(tid, cid, reference_date=REF_Q1, first_source="download")
    rows = _rows(tid)
    assert len(rows) == 2
    assert {r[0] for r in rows} == {"2026-Q2", "2026-Q1"}


def test_empty_output_records_nothing(monkeypatch):
    """A client with no report data for the quarter is a preview/empty output — free."""
    monkeypatch.setattr("api.writers.gmcs_writer.report_has_data", lambda *a, **k: False)
    tid = _mk_tenant()
    cid = _mk_client(tid)
    assert record_genreport_output(tid, cid, reference_date=REF_Q2,
                                   first_source="download") is False
    assert _count(tid) == 0


def test_demo_and_unmigrated_tenants_never_charge():
    demo = _mk_tenant(is_demo=True)
    dcid = _mk_client(demo)
    assert record_genreport_output(demo, dcid, reference_date=REF_Q2) is False
    unmig = _mk_tenant(gen_reports=False)          # AO not in reports world
    ucid = _mk_client(unmig)
    assert record_genreport_output(unmig, ucid, reference_date=REF_Q2) is False
    assert _count(demo) == 0 and _count(unmig) == 0


def test_nepool_tenant_is_charged():
    tid = _mk_tenant(product="nepool", gen_reports=False)   # always reports-world
    cid = _mk_client(tid)
    assert record_genreport_output(tid, cid, reference_date=REF_Q2) is True
    assert _count(tid) == 1


# ── directory download: one row per client with data; re-download free ──────────

def test_directory_download_records_one_row_per_client():
    tid = _mk_tenant()
    cids = [_mk_client(tid) for _ in range(3)]
    n = record_genreport_directory(tid, reference_date=REF_Q2)
    assert n == 3
    assert _count(tid) == 3
    # re-download the SAME quarter -> zero new
    assert record_genreport_directory(tid, reference_date=REF_Q2) == 0
    assert _count(tid) == 3
    # a different quarter -> 3 new
    assert record_genreport_directory(tid, reference_date=REF_Q1) == 3
    assert _count(tid) == 6


def test_directory_skips_clients_without_data(monkeypatch):
    tid = _mk_tenant()
    c_data = _mk_client(tid)
    c_empty = _mk_client(tid)
    monkeypatch.setattr("api.writers.gmcs_writer.report_has_data",
                        lambda cid, **k: cid == c_data)
    n = record_genreport_directory(tid, reference_date=REF_Q2)
    assert n == 1
    assert _count(tid) == 1


# ── the send path (deliver_for_client) charges on success ───────────────────────

def _fake_build_workbook(client_id, year=None, out_path=None, reference_date=None):
    wb = Workbook()
    wb.active.title = "Array1"
    wb.save(out_path)
    return out_path


def _drive_send(monkeypatch, *, sent: bool, triggered_by: str = "self-serve",
                skip_if_empty: bool = False):
    tid = _mk_tenant()
    cid = _mk_client(tid, contact_email="dest@genrep.test")
    monkeypatch.setattr("api.delivery.build_workbook", _fake_build_workbook)
    monkeypatch.setattr("api.delivery.send_workbook_email", lambda **kw: sent)
    result = deliver_for_client(cid, triggered_by=triggered_by,
                                skip_if_empty=skip_if_empty)
    return tid, cid, result


def test_successful_send_records_one_charge(monkeypatch):
    tid, cid, result = _drive_send(monkeypatch, sent=True)
    assert result["ok"] is True and result["email_sent"] is True
    assert _count(tid) == 1
    assert _rows(tid)[0][2] == "send"


def test_failed_send_records_no_charge(monkeypatch):
    tid, cid, result = _drive_send(monkeypatch, sent=False)
    assert result["email_sent"] is False
    assert _count(tid) == 0


def test_sample_preview_send_records_no_charge(monkeypatch):
    tid, cid, result = _drive_send(monkeypatch, sent=True, triggered_by="sample")
    assert result["email_sent"] is True
    assert _count(tid) == 0


def test_repeated_auto_sends_same_quarter_charge_once(monkeypatch):
    tid = _mk_tenant()
    cid = _mk_client(tid, contact_email="dest@genrep.test", auto_send=True)
    monkeypatch.setattr("api.delivery.build_workbook", _fake_build_workbook)
    monkeypatch.setattr("api.delivery.send_workbook_email", lambda **kw: True)
    for _ in range(3):
        deliver_for_client(cid, triggered_by="sched-weekly")
    assert _count(tid) == 1


# ── scheduler auto-send is gated on auto_send=True ──────────────────────────────

def test_scheduler_only_auto_sends_enrolled_clients(monkeypatch):
    from api import scheduler as sched

    tid = _mk_tenant(frequency="weekly")
    enrolled = _mk_client(tid, auto_send=True)
    not_enrolled = _mk_client(tid, auto_send=False)     # capture-artifact default

    delivered: list[int] = []
    import api.delivery as delivery_mod
    monkeypatch.setattr(delivery_mod, "deliver_for_client",
                        lambda cid, **kw: delivered.append(cid) or {
                            "ok": True, "email_sent": True, "client_id": cid,
                            "client_name": f"c{cid}", "recipient": "x@y", "tenant": tid})
    import api.jobs.report_digests as rd
    monkeypatch.setattr(rd, "record_scheduled_batch", lambda *a, **k: 0)
    import api.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send_internal_alert", lambda *a, **k: True)

    sched._deliver_clients_with_frequency("weekly")

    assert enrolled in delivered
    assert not_enrolled not in delivered


# ── the metered Stripe push is INERT until the price is minted ───────────────────

def test_usage_job_is_inert_without_price(monkeypatch):
    monkeypatch.delenv("STRIPE_AO_GENREPORTS_PRICE_ID", raising=False)
    from api.jobs.genreports_usage import report_genreport_charges_to_stripe
    tid = _mk_tenant()
    cid = _mk_client(tid)
    record_genreport_output(tid, cid, reference_date=REF_Q2, first_source="download")
    out = report_genreport_charges_to_stripe()
    assert out == {"reported": 0, "tenants": 0, "skipped": 0, "errors": [], "inert": True}
    assert _rows(tid)[0][3] is None      # pushed_at still NULL — nothing billed
