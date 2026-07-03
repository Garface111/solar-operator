"""Offtaker invoice email MASS TEMPLATE (Anna-scale ask, 2026-07-03).

The letter at the top of every offtaker invoice email renders from a
tenant-wide merge-tag template (same engine as the NEPOOL report-email
customizer): the DEFAULT is personalized ("Hi <first name>" / "Dear <org>")
and warm; a stored custom template replaces it; the operator's per-draft note
still overrides everything for that one send.
"""
from __future__ import annotations

import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_offtaker_tpl_test")

import secrets
from datetime import datetime

from api.db import SessionLocal, init_db
from api.models import (Tenant, Array, UtilityAccount, Bill,
                        BillingReportSubscription, ReportDraft)
from api.email_templates import (
    DEFAULT_OFFTAKER_BODY_TEMPLATE, OFFTAKER_ALLOWED_MERGE_TAGS,
    build_offtaker_context, render_merge, unknown_tags,
)


def _mk_world(db, offtaker_name="Abigail Quimby"):
    tid = "ten_" + secrets.token_hex(4)
    db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Tpl Test Co",
                  company_name="Sunrise Commons Energy",
                  contact_email=f"{tid}@e.com", active=True,
                  product="array_operator"))
    db.flush()
    arr = Array(tenant_id=tid, name="Tpl Array", region="VT")
    db.add(arr)
    db.flush()
    host = UtilityAccount(tenant_id=tid, provider="gmp", array_id=arr.id,
                          account_number="HOST-TPL")
    db.add(host)
    db.flush()
    db.add(Bill(tenant_id=tid, account_id=host.id,
                period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 30),
                kwh_generated=10000, kwh_sent_to_grid=10000.0,
                solar_credit_usd=1700.0, is_net_metered=True, parse_status="parsed"))
    own = UtilityAccount(tenant_id=tid, provider="gmp", account_number="OWN-TPL")
    db.add(own)
    db.flush()
    db.add(Bill(tenant_id=tid, account_id=own.id,
                period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 30),
                kwh_generated=500, kwh_sent_to_grid=500.0,
                solar_credit_usd=85.0, is_net_metered=True, parse_status="parsed"))
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name=offtaker_name, array_id=arr.id,
        allocation_pct=1.0, array_share_pct=0.05, utility_account_id=own.id,
        billing_model="percent_of_array", cadence="monthly",
        client_email="offtaker@example.com", enabled=True)
    db.add(sub)
    db.commit()
    return tid, sub


def _cleanup(db, tid):
    for model in (ReportDraft, BillingReportSubscription, Bill,
                  UtilityAccount, Array):
        db.query(model).filter(model.tenant_id == tid).delete(
            synchronize_session=False)
    t = db.get(Tenant, tid)
    if t is not None:
        db.delete(t)
    db.commit()


def test_default_letter_is_personalized_and_longer():
    """Default email: 'Hi Abigail,' + warm multi-paragraph letter + real
    figures — and the figures table still carries kwh + $ (the anna800 send
    harness spot-checks those exact strings)."""
    from api.billing.delivery import build_match, _email_html
    init_db()
    with SessionLocal() as db:
        tid, sub = _mk_world(db)
        try:
            m = build_match(sub)
            subject, html, text = _email_html(m, sub, is_test=False)
            assert "Hi Abigail," in html, html[:600]
            assert "Thanks for going solar!" in html
            assert "reply to this email" in html
            # real-math kwh = 0.05 × 10000 = 500 → "500 kWh"; amount in both.
            assert "500 kWh" in html and "$" in html
            assert "Your full invoice is attached" in html
            assert subject.startswith("Your solar credit invoice — Abigail Quimby")
            # plain-text alternative carries the personalized letter too
            assert "Hi Abigail," in text
        finally:
            _cleanup(db, tid)


def test_org_offtaker_gets_dear_greeting():
    from api.billing.delivery import build_match, _email_html
    init_db()
    with SessionLocal() as db:
        tid, sub = _mk_world(db, offtaker_name="Hartland Feed & Grain LLC")
        try:
            m = build_match(sub)
            _s, html, _t = _email_html(m, sub, is_test=False)
            assert "Dear Hartland Feed &amp; Grain LLC," in html \
                or "Dear Hartland Feed & Grain LLC," in html
        finally:
            _cleanup(db, tid)


def test_custom_template_and_note_override():
    """A stored tenant template replaces the default letter; a per-draft note
    replaces even the custom template (explicit beats mass)."""
    from api.billing.delivery import build_match, _email_html
    init_db()
    with SessionLocal() as db:
        tid, sub = _mk_world(db)
        try:
            t = db.get(Tenant, tid)
            t.offtaker_email_body_template = (
                "<p>{{greeting}},</p><p>Custom letter: {{kwh}} for {{amount}} "
                "this {{period}}.</p>{{signoff}}")
            t.offtaker_email_subject_template = "Invoice {{invoice_number}} — {{offtaker_name}}"
            db.commit()
            m = build_match(sub)
            subject, html, _t2 = _email_html(m, sub, is_test=False)
            assert "Custom letter: 500 kWh" in html
            assert subject.startswith("Invoice 2026-06 — Abigail Quimby")
            # The note wins over the custom template.
            _s3, html3, _t3 = _email_html(m, sub, is_test=False,
                                          note="My hand-written words.")
            assert "My hand-written words." in html3
            assert "Custom letter" not in html3
        finally:
            _cleanup(db, tid)


def test_attachments_line_never_overclaims():
    """With a real attachment list, the letter only claims files present."""
    from api.billing.delivery import build_match, _email_html
    init_db()
    with SessionLocal() as db:
        tid, sub = _mk_world(db)
        try:
            m = build_match(sub)
            _s, html_min, _t = _email_html(
                m, sub, is_test=False, attachment_names=["invoice_x.pdf"])
            assert "GMP source bill" not in html_min
            _s2, html_gmp, _t2 = _email_html(
                m, sub, is_test=False,
                attachment_names=["invoice_x.pdf", "gmp_utility_bill_x.pdf"])
            assert "GMP source bill" in html_gmp
        finally:
            _cleanup(db, tid)


def test_offtaker_tag_allowlist():
    """The offtaker tag set validates saves; the default template is clean."""
    assert unknown_tags(DEFAULT_OFFTAKER_BODY_TEMPLATE,
                        OFFTAKER_ALLOWED_MERGE_TAGS) == set()
    assert unknown_tags("<p>{{quarter_total_mwh}}</p>",
                        OFFTAKER_ALLOWED_MERGE_TAGS) == {"quarter_total_mwh"}
    ctx = build_offtaker_context(
        offtaker_name="Tess Lamson", tenant_name="Op Co",
        period="2026-06-01 → 2026-06-30", kwh="1,265 kWh", amount="$190.20",
        invoice_number="2026-06", attachments_line="Your full invoice is attached.")
    out = render_merge(DEFAULT_OFFTAKER_BODY_TEMPLATE, ctx)
    assert "Hi Tess," in out and "{{" not in out


def test_draft_letter_default_matches_send():
    """The drafts payload's email_letter_default is the plain-text mass letter
    for that draft — what the send would use with no note."""
    from api.billing.routes import _draft_letter_default
    from api.billing.delivery import _offtaker_email_fields
    init_db()
    with SessionLocal() as db:
        tid, sub = _mk_world(db)
        try:
            d = ReportDraft(tenant_id=tid, subscription_id=sub.id,
                            customer_name=sub.customer_name, status="pending",
                            period_label="2026-06-01 → 2026-06-30",
                            customer_kwh=500.0, amount_usd=76.5,
                            invoice_number="2026-06")
            db.add(d)
            db.commit()
            r = _draft_letter_default(d, sub, _offtaker_email_fields(tid))
            assert r and "Hi Abigail," in r["letter"]
            assert "500 kWh" in r["letter"] and "$76.50" in r["letter"]
            assert r["subject"].startswith("Your solar credit invoice — Abigail Quimby (2026-06)")
        finally:
            _cleanup(db, tid)
