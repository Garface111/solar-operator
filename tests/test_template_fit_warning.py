"""The offtaker list flags a template <-> billing-model mismatch (a non-budget
offtaker on a fixed-budget template, or vice versa) so a silent fall-back to the
standard invoice is visible up front."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.models import (Base, Tenant, BillingReportSubscription,
                        OfftakerInvoiceTemplate)
from api.billing import routes as R


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _mk(db, budget):
    n = db.query(BillingReportSubscription).count()
    s = BillingReportSubscription(
        tenant_id="ten_x", customer_name=f"O{n}", cadence="monthly",
        send_mode="to_client", budget_amount_usd=budget)
    db.add(s); db.commit(); db.refresh(s)
    return s


def test_budget_cell_template_warns_only_the_mismatched_offtaker(db, monkeypatch):
    db.add(Tenant(id="ten_x", name="X", contact_email="x@x.com", tenant_key="k"))
    db.add(OfftakerInvoiceTemplate(tenant_id="ten_x", filename="t.xlsx",
                                   file_bytes=b"PK\x03\x04dummy", enabled=True))
    on = _mk(db, 1250.0)     # on a fixed budget
    off = _mk(db, None)      # NOT on a budget
    # template HAS a budget cell
    monkeypatch.setattr(R, "_attach_template_fit_warnings",
                        R._attach_template_fit_warnings)  # ensure real fn
    monkeypatch.setattr("api.billing.repro.template_repro.template_has_budget_cell",
                        lambda b: True)
    rows = [on, off]
    dicts = [{"id": on.id}, {"id": off.id}]
    R._attach_template_fit_warnings(db, "ten_x", rows, dicts)
    d_on = next(d for d in dicts if d["id"] == on.id)
    d_off = next(d for d in dicts if d["id"] == off.id)
    assert "template_fit_warning" not in d_on           # budget offtaker + budget template = OK
    assert "Fixed Monthly Budget Payment" in d_off.get("template_fit_warning", "")


def test_plain_template_warns_the_budget_offtaker(db, monkeypatch):
    db.add(Tenant(id="ten_x", name="X", contact_email="x@x.com", tenant_key="k"))
    db.add(OfftakerInvoiceTemplate(tenant_id="ten_x", filename="t.xlsx",
                                   file_bytes=b"PK\x03\x04dummy", enabled=True))
    on = _mk(db, 1250.0)
    off = _mk(db, None)
    monkeypatch.setattr("api.billing.repro.template_repro.template_has_budget_cell",
                        lambda b: False)   # template has NO budget cell
    dicts = [{"id": on.id}, {"id": off.id}]
    R._attach_template_fit_warnings(db, "ten_x", [on, off], dicts)
    d_on = next(d for d in dicts if d["id"] == on.id)
    d_off = next(d for d in dicts if d["id"] == off.id)
    assert "budget amount" in d_on.get("template_fit_warning", "")   # budget won't show
    assert "template_fit_warning" not in d_off                        # non-budget + plain = OK


def test_no_template_no_warning(db, monkeypatch):
    db.add(Tenant(id="ten_x", name="X", contact_email="x@x.com", tenant_key="k"))
    off = _mk(db, None)
    dicts = [{"id": off.id}]
    R._attach_template_fit_warnings(db, "ten_x", [off], dicts)   # no template rows
    assert "template_fit_warning" not in dicts[0]
