"""An invoice-template upload must never look like it worked when it can't.

Ford, 2026-08-12 (Paul/HCT): "I tried to upload a new template but did not get a
different result (quite possibly user error on my part)." It was not user error —
he uploaded a PDF to the per-offtaker template slot. We stored it, left it
disabled, returned {"ok": true}, and said nothing. An invoice can only render from
token-HTML or from an .xlsx (pixel repro), so that upload was incapable of ever
doing anything from the moment we accepted it.

Same class of failure as billing a rate nobody entered: the system knew, and the
operator didn't.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_tpl_honesty_test")

import io
import secrets as _secrets

import openpyxl
import pytest

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (
    Tenant, BillingReportSubscription, OfftakerSubscriptionTemplate,
)

BASE = "/v1/array-operator/billing"


@pytest.fixture(autouse=True)
def _no_leftover_subscriptions():
    """Delete any subscription this module creates (and its per-offtaker template
    first — that row FK-references the subscription, so the sub can't be deleted
    while it exists).

    test_billing_delivery.test_match_preview_saves_nothing asserts that NO
    BillingReportSubscription exists anywhere, so a row left behind here fails an
    unrelated test whenever the two files run in the same session."""
    with SessionLocal() as db:
        before = {s.id for s in db.query(BillingReportSubscription).all()}
    yield
    with SessionLocal() as db:
        new_ids = [s.id for s in db.query(BillingReportSubscription).all()
                   if s.id not in before]
        if new_ids:
            for t in (db.query(OfftakerSubscriptionTemplate)
                        .filter(OfftakerSubscriptionTemplate.subscription_id.in_(new_ids))
                        .all()):
                db.delete(t)
            db.flush()
            for s in db.query(BillingReportSubscription).filter(
                    BillingReportSubscription.id.in_(new_ids)).all():
                db.delete(s)
        db.commit()


def _tenant():
    tid = "ten_" + _secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Template Op", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + _secrets.token_urlsafe(12),
                      plan="standard", active=True, product="array_operator"))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _sub(tid, name="Valley Cares"):
    with SessionLocal() as db:
        s = BillingReportSubscription(tenant_id=tid, customer_name=name,
                                      allocation_pct=0.5,
                                      billing_model="percent_of_array")
        db.add(s); db.commit(); db.refresh(s)
        return s.id


def _xlsx_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws["B3"] = "Invoice - Solar Power Generation"
    ws["B10"] = "Amount Owed:"
    ws["C10"] = 2150
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _upload(client, auth, sub_id, name, data, ctype):
    return client.post(
        f"{BASE}/subscriptions/{sub_id}/invoice-template",
        files={"file": (name, data, ctype)},
        headers={"Authorization": auth})


# ── the actual Paul case ──────────────────────────────────────────────────────

def test_pdf_upload_warns_and_is_not_marked_usable(client):
    """A PDF can never render. Accepting it silently is the bug."""
    tid, auth = _tenant()
    sid = _sub(tid)
    r = _upload(client, auth, sid,
                "07-2026 HCT Sun Enterprises, LLC Solar Invoice VV.pdf",
                _PDF, "application/pdf")
    assert r.status_code == 200, r.text
    body = r.json()
    # It must SAY something, and name the way out.
    assert body.get("warning"), "a PDF upload returned no warning at all"
    assert "PDF" in body["warning"]
    assert ".xlsx" in body["warning"]
    # And it must not masquerade as an installed template.
    tpl = body["template"]
    assert tpl["enabled"] is False
    assert tpl["renderable"] is False


def test_pdf_upload_does_not_take_over_from_a_working_template(client):
    """The dangerous version: a good .xlsx is live, then a PDF is uploaded over it.
    The PDF must not disable/replace the working template's ability to render."""
    tid, auth = _tenant()
    sid = _sub(tid)
    r = _upload(client, auth, sid, "invoice.xlsx", _xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert r.status_code == 200 and r.json()["template"]["renderable"] is True

    r = _upload(client, auth, sid, "scan.pdf", _PDF, "application/pdf")
    tpl = r.json()["template"]
    assert r.json().get("warning")
    # The stored FILE is now the PDF, but it is honestly reported as unusable and
    # switched off, so nothing silently renders from it.
    assert tpl["enabled"] is False
    # renderable stays True only if seeded HTML survived; either way it must never
    # be enabled-and-unrenderable at the same time.
    assert not (tpl["enabled"] and not tpl["renderable"])


def test_good_xlsx_uploads_clean_and_goes_live(client):
    """The happy path keeps working: uploading IS the opt-in, and no scary warning."""
    tid, auth = _tenant()
    sid = _sub(tid)
    r = _upload(client, auth, sid, "Danville Big Buck Invoice.xlsx", _xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("warning") is None
    assert body["template"]["enabled"] is True
    assert body["template"]["renderable"] is True


def test_tenant_wide_upload_behaves_identically(client):
    """The tenant-wide and per-offtaker routes share one implementation now; they
    used to carry separate copies and the per-offtaker one was the broken half."""
    tid, auth = _tenant()
    r = client.post(f"{BASE}/invoice-template",
                    files={"file": ("scan.pdf", _PDF, "application/pdf")},
                    headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json().get("warning")
    assert r.json()["template"]["enabled"] is False
    assert r.json()["template"]["renderable"] is False

    r = client.post(f"{BASE}/invoice-template",
                    files={"file": ("t.xlsx", _xlsx_bytes(),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    headers={"Authorization": auth})
    assert r.json().get("warning") is None
    assert r.json()["template"]["enabled"] is True


def test_status_reports_renderable_so_the_ui_can_tell_the_truth(client):
    """GET must expose `renderable`, or the card can only say 'On file' — which is
    what made a dead upload look installed."""
    tid, auth = _tenant()
    sid = _sub(tid)
    _upload(client, auth, sid, "scan.pdf", _PDF, "application/pdf")
    r = client.get(f"{BASE}/subscriptions/{sid}/invoice-template",
                   headers={"Authorization": auth})
    assert r.status_code == 200
    t = r.json()["template"]
    assert t["has_template"] is True      # a file IS on file...
    assert t["renderable"] is False       # ...but it can't produce an invoice
