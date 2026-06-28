"""
PER-OFFTAKER invoice template precedence (Ford, 2026-06-28).

Each offtaker can have its OWN invoice template; it OVERRIDES the tenant-wide
default. The render layer resolves which template to use via
delivery._effective_template_row with precedence:
    per-offtaker (enabled) → tenant default (enabled) → None (standard PDF).
`force` (preview) ignores the enabled gate so an operator can preview before
turning it on.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_per_offtaker_tpl")

import secrets as _secrets

import pytest
from sqlalchemy import select, delete

from api.db import SessionLocal
from api.models import (Tenant, BillingReportSubscription,
                        OfftakerInvoiceTemplate, OfftakerSubscriptionTemplate)
from api.billing.delivery import _effective_template_row


@pytest.fixture(autouse=True)
def _cleanup_test_rows():
    """Delete this file's rows after each test (FK-safe order) so it never pollutes
    the shared test DB / count-sensitive tests regardless of run order."""
    yield
    with SessionLocal() as db:
        tids = [t.id for t in db.execute(
            select(Tenant).where(Tenant.id.like("ten_tpl_%"))).scalars()]
        if not tids:
            return
        sids = [s.id for s in db.execute(select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id.in_(tids))).scalars()]
        if sids:
            db.execute(delete(OfftakerSubscriptionTemplate).where(
                OfftakerSubscriptionTemplate.subscription_id.in_(sids)))
        db.execute(delete(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id.in_(tids)))
        db.execute(delete(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id.in_(tids)))
        db.execute(delete(Tenant).where(Tenant.id.in_(tids)))
        db.commit()


def _seed():
    tid = "ten_tpl_" + _secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="Tpl Test",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        sub = BillingReportSubscription(tenant_id=tid, customer_name="Valley Cares",
                                        allocation_pct=0.5, billing_model="percent_of_array")
        db.add(sub); db.flush()
        sid = sub.id
        db.commit()
    return tid, sid


def _row(tid, sid, force=False):
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        return _effective_template_row(db, sub, force)


def test_no_templates_returns_none():
    tid, sid = _seed()
    assert _row(tid, sid) is None


def test_tenant_template_used_when_no_per_offtaker():
    tid, sid = _seed()
    with SessionLocal() as db:
        db.add(OfftakerInvoiceTemplate(tenant_id=tid, html="<p>TENANT</p>", enabled=True))
        db.commit()
    row = _row(tid, sid)
    assert row is not None and row.html == "<p>TENANT</p>"


def test_per_offtaker_overrides_tenant():
    tid, sid = _seed()
    with SessionLocal() as db:
        db.add(OfftakerInvoiceTemplate(tenant_id=tid, html="<p>TENANT</p>", enabled=True))
        db.add(OfftakerSubscriptionTemplate(subscription_id=sid, tenant_id=tid,
                                            html="<p>OFFTAKER</p>", enabled=True))
        db.commit()
    assert _row(tid, sid).html == "<p>OFFTAKER</p>"   # per-offtaker wins


def test_disabled_per_offtaker_falls_back_to_tenant():
    tid, sid = _seed()
    with SessionLocal() as db:
        db.add(OfftakerInvoiceTemplate(tenant_id=tid, html="<p>TENANT</p>", enabled=True))
        db.add(OfftakerSubscriptionTemplate(subscription_id=sid, tenant_id=tid,
                                            html="<p>OFFTAKER</p>", enabled=False))
        db.commit()
    # disabled + not forced → skip per-offtaker, use the tenant default
    assert _row(tid, sid).html == "<p>TENANT</p>"
    # force (preview) → the per-offtaker template is shown even when disabled
    assert _row(tid, sid, force=True).html == "<p>OFFTAKER</p>"


def test_empty_per_offtaker_row_falls_back_to_tenant():
    """A per-offtaker row that exists but carries no content (no html/file) must not
    shadow the tenant template."""
    tid, sid = _seed()
    with SessionLocal() as db:
        db.add(OfftakerInvoiceTemplate(tenant_id=tid, html="<p>TENANT</p>", enabled=True))
        db.add(OfftakerSubscriptionTemplate(subscription_id=sid, tenant_id=tid,
                                            html=None, file_bytes=None, enabled=True))
        db.commit()
    assert _row(tid, sid).html == "<p>TENANT</p>"
