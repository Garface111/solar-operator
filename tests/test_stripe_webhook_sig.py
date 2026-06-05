"""
Tests for Stripe webhook signature verification in api/stripe_webhook.py.

The conftest.py clears STRIPE_WEBHOOK_SECRET before any api.* imports, so the
module-level STRIPE_WEBHOOK_SECRET is always "" in the test suite. Tests that
need the signed code path monkeypatch the module-level variable directly.

Covered:
  1. Signed path + good (mocked) signature → 200
  2. Signed path + SignatureVerificationError → 400
     (replicates the production failure mode when the cached module secret
      diverges from the secret Stripe uses to sign events)
  3. Signed path + no Stripe-Signature header → 400
  4. Duplicate event_id (already processed) → 200 with duplicate=True
"""
from __future__ import annotations

import json
import secrets

import stripe

import api.stripe_webhook as _sw


def _event(**overrides) -> dict:
    """Build a minimal checkout.session.completed event dict with no tenant
    metadata so the handler silently returns ignored (no side-effects)."""
    base = {
        "id": "evt_sig_" + secrets.token_hex(4),
        "type": "checkout.session.completed",
        "data": {"object": {
            "metadata": {},
            "customer": None,
            "subscription": None,
            "customer_email": None,
        }},
    }
    base.update(overrides)
    return base


def _post(client, body, sig: str | None = None):
    headers = {"content-type": "application/json"}
    if sig is not None:
        headers["stripe-signature"] = sig
    payload = json.dumps(body).encode() if isinstance(body, dict) else body
    return client.post("/v1/stripe/webhook", content=payload, headers=headers)


# ── (1) Good signature → 200 ─────────────────────────────────────────────────

def test_signed_path_200_on_good_signature(client, monkeypatch):
    """construct_event succeeds (mocked) → 200 ok."""
    event = _event()
    monkeypatch.setattr(_sw, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)

    resp = _post(client, event, sig="t=1,v1=goodsig")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


# ── (2) Bad signature → 400 ──────────────────────────────────────────────────
# Production failure mode: the module caches STRIPE_WEBHOOK_SECRET at import
# time (before Railway injects env vars, or after a secret rotation that didn't
# restart the process).  Every incoming event that was signed with the real
# secret fails verification → 400 → Stripe retries indefinitely → event backlog.

def test_signed_path_400_on_bad_signature(client, monkeypatch):
    monkeypatch.setattr(_sw, "STRIPE_WEBHOOK_SECRET", "whsec_test")

    def _raise(*a, **k):
        raise stripe.error.SignatureVerificationError(
            "No signatures found matching the expected signature for payload",
            sig_header="t=1,v1=badsig",
        )

    monkeypatch.setattr(stripe.Webhook, "construct_event", _raise)

    resp = _post(client, b'{"id":"evt_bad","type":"test"}', sig="t=1,v1=badsig")
    assert resp.status_code == 400


# ── (3) Missing Stripe-Signature header with secret set → 400 ────────────────

def test_signed_path_400_on_missing_signature(client, monkeypatch):
    """Stripe-Signature header absent → construct_event receives None → 400."""
    monkeypatch.setattr(_sw, "STRIPE_WEBHOOK_SECRET", "whsec_test")

    def _raise(*a, **k):
        raise stripe.error.SignatureVerificationError(
            "No stripe-signature header value was provided.",
            sig_header="",
        )

    monkeypatch.setattr(stripe.Webhook, "construct_event", _raise)

    resp = _post(client, b'{"id":"evt_nosig","type":"test"}', sig=None)
    assert resp.status_code == 400


# ── (4) Duplicate event_id → ok=True with duplicate=True ────────────────────

def test_duplicate_event_returns_duplicate_flag(client, monkeypatch):
    """Second delivery of the same event_id short-circuits with duplicate=True."""
    event = _event(id="evt_dup_" + secrets.token_hex(4))
    monkeypatch.setattr(_sw, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)

    body = json.dumps(event).encode()

    resp1 = _post(client, body, sig="t=1,v1=ok")
    assert resp1.status_code == 200
    assert resp1.json()["ok"] is True

    # Second delivery: event already processed → duplicate flag
    resp2 = _post(client, body, sig="t=1,v1=ok")
    assert resp2.status_code == 200
    assert resp2.json().get("duplicate") is True
