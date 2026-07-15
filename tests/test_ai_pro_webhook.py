"""Energy Agent Pro add-on must not ride the fleet subscription lifecycle."""
from __future__ import annotations

from unittest.mock import patch

from api.db import SessionLocal
from api.models import Tenant
from api.stripe_webhook import (
    _process_checkout_completed,
    _process_subscription_deleted,
    _process_subscription_updated,
)


def _tenant(**kw) -> Tenant:
    t = Tenant(
        id="ten_aipro_test_" + (kw.pop("suffix", "1")),
        tenant_key="sol_test_aipro_" + kw.pop("key_suffix", "1"),
        name="AI Pro Test",
        company_name="AI Pro Test Co",
        contact_email="aipro@example.com",
        product="array_operator",
        active=True,
        subscription_status="active",
        stripe_customer_id=kw.pop("stripe_customer_id", "cus_aipro_1"),
        stripe_subscription_id=kw.pop("stripe_subscription_id", "sub_fleet_1"),
        ai_pro=kw.pop("ai_pro", False),
        **kw,
    )
    with SessionLocal() as db:
        db.add(t)
        db.commit()
        db.refresh(t)
        return t


def test_checkout_completed_routes_ai_pro_not_welcome():
    t = _tenant(suffix="chk", key_suffix="chk")
    with patch("api.stripe_webhook.send_welcome_email") as welcome, \
         patch("api.stripe_webhook.send_internal_alert"):
        out = _process_checkout_completed({
            "id": "cs_ai_pro",
            "customer": t.stripe_customer_id,
            "subscription": "sub_ai_pro_1",
            "metadata": {
                "tenant_id": t.id,
                "product": "energy_agent_pro",
            },
        })
    assert out.get("ai_pro") is True
    assert out.get("tenant") == t.id
    welcome.assert_not_called()
    with SessionLocal() as db:
        row = db.get(Tenant, t.id)
        assert row.ai_pro is True
        # Fleet subscription id must stay untouched
        assert row.stripe_subscription_id == "sub_fleet_1"
        assert row.active is True
        assert row.subscription_status == "active"


def test_ai_pro_subscription_deleted_clears_flag_only():
    t = _tenant(suffix="del", key_suffix="del", ai_pro=True)
    with patch("api.stripe_webhook.send_cancellation_email") as cancel_mail, \
         patch("api.stripe_webhook.send_internal_alert"):
        out = _process_subscription_deleted({
            "id": "sub_ai_pro_x",
            "customer": t.stripe_customer_id,
            "status": "canceled",
            "metadata": {
                "tenant_id": t.id,
                "product": "energy_agent_pro",
            },
        })
    assert out.get("ai_pro") is False
    cancel_mail.assert_not_called()
    with SessionLocal() as db:
        row = db.get(Tenant, t.id)
        assert row.ai_pro is False
        assert row.active is True  # fleet still live
        assert row.stripe_subscription_id == "sub_fleet_1"
        assert row.subscription_status == "active"


def test_ai_pro_subscription_updated_reactivates():
    t = _tenant(suffix="up", key_suffix="up", ai_pro=False)
    with patch("api.stripe_webhook.send_internal_alert"):
        out = _process_subscription_updated({
            "id": "sub_ai_pro_y",
            "customer": t.stripe_customer_id,
            "status": "active",
            "metadata": {
                "tenant_id": t.id,
                "product": "energy_agent_pro",
            },
        })
    assert out.get("ai_pro") is True
    with SessionLocal() as db:
        row = db.get(Tenant, t.id)
        assert row.ai_pro is True
        assert row.stripe_subscription_id == "sub_fleet_1"
