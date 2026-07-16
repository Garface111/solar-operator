"""Auto-linking cross-product siblings on signup (Ford 2026-07-16).

When the same email signs up for BOTH NEPOOL and Array Operator, the two tenants
should be linked as siblings automatically (bidirectional linked_tenant_id), so
they work together — no manual admin step.
"""
from __future__ import annotations
import secrets

from api.onboarding import _create_trial_tenant
from api.db import SessionLocal
from api.models import Tenant


def _signup(email: str, product: str) -> str:
    _tok, tid = _create_trial_tenant(
        email=email, full_name="Link Test", company="LT",
        password=None, array_count=None, product=product,
        consent_version="2026-06-27",
    )
    return tid


def test_second_product_signup_autolinks_both_ways():
    email = f"linktest-{secrets.token_hex(5)}@example.com"  # @example.com skips the alert
    nep_id = _signup(email, "nepool")
    # Before the second product exists, nothing to link.
    with SessionLocal() as db:
        assert db.get(Tenant, nep_id).linked_tenant_id is None

    ao_id = _signup(email, "array_operator")
    with SessionLocal() as db:
        nep = db.get(Tenant, nep_id)
        ao = db.get(Tenant, ao_id)
        assert nep.linked_tenant_id == ao_id, "nepool tenant links to AO sibling"
        assert ao.linked_tenant_id == nep_id, "AO tenant links back to nepool sibling"


def test_single_product_signup_stays_unlinked():
    email = f"solo-{secrets.token_hex(5)}@example.com"
    nep_id = _signup(email, "nepool")
    with SessionLocal() as db:
        assert db.get(Tenant, nep_id).linked_tenant_id is None
