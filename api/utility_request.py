"""Operator-submitted "add this utility" requests.

When an operator's client uses a utility we don't list yet, the dashboard's
"Don't see your utility?" form POSTs here. We do two things:

  1. Always email Ford an internal alert (he reads these on his phone).
  2. If a Hermes agent webhook is configured (HERMES_UTILITY_WEBHOOK_URL),
     fire a signed POST that kicks off an autonomous agent run. That agent
     loads the solar-operator-saas skill and follows refs/adding-utility-
     providers.md to add the provider to the repo (SmartHub registry edit for
     co-op/muni portals, or an `in-progress` providers.py entry otherwise) and
     open a PR. The agent NEVER fabricates a bespoke scraper — see that ref.

The webhook is optional: with no URL configured the request still reaches Ford
by email, so the feature degrades gracefully (delivery beats automation).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request

from .notify import send_internal_alert

logger = logging.getLogger(__name__)

HERMES_UTILITY_WEBHOOK_URL = os.getenv("HERMES_UTILITY_WEBHOOK_URL", "")
HERMES_UTILITY_WEBHOOK_SECRET = os.getenv("HERMES_UTILITY_WEBHOOK_SECRET", "")


def _dispatch_to_hermes(payload: dict) -> bool:
    """POST a signed JSON payload to the Hermes webhook that triggers the
    add-a-utility agent run. Returns True if the POST returned 2xx.

    Signs the body with HMAC-SHA256 under the GitHub-style header the Hermes
    webhook adapter validates (`X-Hub-Signature-256: sha256=<hex>`)."""
    if not HERMES_UTILITY_WEBHOOK_URL:
        logger.info("utility-request: no HERMES_UTILITY_WEBHOOK_URL set — email only.")
        return False

    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if HERMES_UTILITY_WEBHOOK_SECRET:
        sig = hmac.new(
            HERMES_UTILITY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={sig}"

    req = urllib.request.Request(
        HERMES_UTILITY_WEBHOOK_URL, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.error("utility-request: webhook returned %s", resp.status)
            return ok
    except urllib.error.HTTPError as e:
        logger.error("utility-request: webhook HTTPError %s: %s", e.code, e.reason)
        return False
    except Exception as e:  # noqa: BLE001 — never let this break the request
        logger.error("utility-request: webhook dispatch failed: %s: %s", type(e).__name__, e)
        return False


def submit_utility_request(
    *,
    tenant_id: str,
    tenant_name: str | None,
    tenant_email: str | None,
    utility_name: str,
    portal_url: str | None,
    region: str | None,
    notes: str | None,
) -> dict:
    """Record + route an operator's utility-addition request.

    Returns {"ok": True, "agent_dispatched": bool}. Always emails Ford; also
    fires the Hermes agent webhook when configured."""
    utility_name = (utility_name or "").strip()
    portal_url = (portal_url or "").strip() or None
    region = (region or "").strip() or None
    notes = (notes or "").strip() or None

    lines = [
        "New utility-addition request from an operator.",
        "",
        f"Utility:  {utility_name}",
        f"Portal:   {portal_url or '(not provided)'}",
        f"Region:   {region or '(not provided)'}",
        f"Notes:    {notes or '(none)'}",
        "",
        f"Operator: {tenant_name or '(unnamed)'} <{tenant_email or 'no-email'}>",
        f"Tenant:   {tenant_id}",
        "",
        "Next step: add this provider per the solar-operator-saas skill",
        "(refs/adding-utility-providers.md). SmartHub co-op/muni → registry",
        "edit; investor-owned/unknown → providers.py 'in-progress' entry.",
        "NEVER fabricate a bespoke scraper without a live login.",
    ]
    body = "\n".join(lines)

    # 1. Always notify Ford by email.
    try:
        send_internal_alert(f"Utility request: {utility_name}", body)
    except Exception as e:  # noqa: BLE001
        logger.error("utility-request: internal alert failed: %s", e)

    # 2. Fire the autonomous agent webhook if configured.
    agent_payload = {
        "kind": "utility_addition_request",
        "utility": {
            "name": utility_name,
            "portal_url": portal_url,
            "region": region,
            "notes": notes,
        },
        "requested_by": {
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "tenant_email": tenant_email,
        },
    }
    dispatched = _dispatch_to_hermes(agent_payload)

    return {"ok": True, "agent_dispatched": dispatched}
