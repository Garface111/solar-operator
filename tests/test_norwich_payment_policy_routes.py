from datetime import date
from api.db import SessionLocal
from api.models import Tenant
from tests.test_offtaker_upload import _make_tenant

BASE = "/v1/array-operator/billing"


def test_policy_default_optin_audit_and_tenant_isolation(client):
    tid, auth = _make_tenant()
    foreign, foreign_auth = _make_tenant()
    headers = {"Authorization": auth}
    assert client.get(BASE + "/payment-policy", headers=headers).json()["policy"] == "online_required"
    response = client.patch(BASE + "/payment-policy", headers=headers, json={"policy": "offline"})
    assert response.status_code == 200
    record = response.json()["audit"][-1]
    assert record["from"] == "online_required" and record["to"] == "offline"
    assert record["actor"].startswith("tenant:" + tid + ":") and record["at"].endswith("Z")
    assert client.get(BASE + "/payment-policy", headers={"Authorization": foreign_auth}).json()["policy"] == "online_required"
    assert client.patch(BASE + "/payment-policy", headers=headers, json={"policy": "offline", "actor": foreign}).status_code == 422
    assert client.patch(BASE + "/payment-policy", headers=headers, json={"policy": "anything"}).status_code == 400
    assert client.get(BASE + "/payment-policy").status_code == 401


def test_offline_route_requires_policy_and_derives_identity(client, monkeypatch):
    from api.billing import payments
    tid, auth = _make_tenant(); headers = {"Authorization": auth}
    seen = []
    monkeypatch.setattr(payments, "record_offline_payment", lambda db, **kw: seen.append(kw) or {"ok": True}, raising=False)
    body = {"amount_cents": 1234, "request_key": "receipt-unique-key", "received_on": str(date.today()), "note": "Check 123"}
    path = BASE + "/invoices/99/offline-payments"
    assert client.post(path, headers=headers, json=body).status_code == 409
    client.patch(BASE + "/payment-policy", headers=headers, json={"policy": "offline"})
    assert client.post(path, headers=headers, json=body).status_code == 200
    assert seen[0]["tenant_id"] == tid and seen[0]["actor"].startswith("tenant:" + tid)
    assert seen[0]["amount_cents"] == 1234
    assert client.post(path, headers=headers, json=dict(body, actor="forged")).status_code == 422
    assert client.post(path, headers=headers, json=dict(body, amount_cents=1.5)).status_code == 422
    assert client.post(path, headers=headers, json=dict(body, note="")).status_code == 400
    def missing(*a, **kw): raise ValueError("Invoice not found")
    monkeypatch.setattr(payments, "record_offline_payment", missing)
    assert client.post(path, headers=headers, json=body).status_code == 404
