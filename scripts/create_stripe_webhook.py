"""Create/update Stripe webhook endpoint pointing at production.
Run via: railway ssh "cd /app && python scripts/create_stripe_webhook.py"

Diagnostic mode (lists all endpoints + compares Railway secret prefix):
  railway ssh "cd /app && python scripts/create_stripe_webhook.py --list"

To roll the signing secret (if secret is wrong/unknown):
  Stripe Dashboard → Developers → Webhooks → click endpoint → Signing secret → Roll key
  Then: railway variables --set "STRIPE_WEBHOOK_SECRET=<new_secret>"
"""
import os, sys, stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
mode = "live" if stripe.api_key.startswith("sk_live") else "test"

URL = "https://web-production-49c83.up.railway.app/v1/stripe/webhook"
EVENTS = [
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
    "setup_intent.succeeded",
    "payment_method.detached",
    # Offtaker pay-link lifecycle (Sep 2026) — api/stripe_webhook.py handles
    # all four; without the subscription an expired link looks unpaid, an ACH
    # payment never lands, and a refund still counts as collected.
    "checkout.session.expired",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "charge.refunded",
    # Connect Express KYC progress (account.updated arrives on a CONNECT
    # endpoint with its own signing secret when it comes from connected
    # accounts — see the audit note in SHARED-BACKLOG 2026-09-18).
    "account.updated",
]


def list_all():
    """Print all webhook endpoints for the current API key mode."""
    endpoints = stripe.WebhookEndpoint.list(limit=100).data
    print(f"=== Stripe webhook endpoints ({mode} mode, {len(endpoints)} total) ===")
    for e in endpoints:
        match = " ← TARGET" if e.url == URL else ""
        print(f"  {e.id}  status={e.status}  url={e.url}{match}")
    print()
    env_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    print(f"STRIPE_WEBHOOK_SECRET in Railway (first 40 chars): {env_secret[:40]!r}")
    print()
    print("Note: Stripe only exposes the signing secret at endpoint-creation time.")
    print("If the secret is wrong, roll it in the Stripe Dashboard:")
    print("  Developers → Webhooks → <endpoint> → Signing secret → Roll key")
    print("Then update Railway:")
    print("  railway variables --set 'STRIPE_WEBHOOK_SECRET=whsec_...'")


def create_or_update():
    existing = [e for e in stripe.WebhookEndpoint.list(limit=100).data if e.url == URL]
    if existing:
        e = existing[0]
        print(f"found existing: {e.id} (status={e.status})")
        stripe.WebhookEndpoint.modify(e.id, enabled_events=EVENTS)
        print(f"updated event list to {len(EVENTS)} events")
        print()
        print("Secret: Stripe does not re-expose the signing secret for existing endpoints.")
        print("Run with --list to compare what Railway has vs what's expected.")
        print("If mismatched, roll it: Stripe Dashboard → Developers → Webhooks → Roll key")
    else:
        e = stripe.WebhookEndpoint.create(url=URL, enabled_events=EVENTS)
        print(f"created: {e.id}")
        print()
        print("Set on Railway:")
        print(f"  STRIPE_WEBHOOK_SECRET={e.secret}")


# ── Connect endpoint (connected-account events) ────────────────────────────
# Offtaker pay links are DIRECT charges (Sep 2026): the Checkout Session and
# charge live on the operator's connected account, so their events are only
# delivered to an endpoint created with connect=True — which has its own
# signing secret. Same handler url, distinguished by a query flag so the two
# endpoints can be told apart in the Dashboard and in list_all().
CONNECT_URL = URL + "?connect=1"
CONNECT_EVENTS = [
    "checkout.session.completed",
    "checkout.session.expired",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "charge.refunded",
    "account.updated",
]


def create_or_update_connect():
    endpoints = stripe.WebhookEndpoint.list(limit=100).data
    existing = [e for e in endpoints if e.url == CONNECT_URL]
    if existing:
        e = existing[0]
        stripe.WebhookEndpoint.modify(e.id, enabled_events=CONNECT_EVENTS)
        print(f"connect endpoint {e.id}: updated event list to {len(CONNECT_EVENTS)} events")
    else:
        e = stripe.WebhookEndpoint.create(
            url=CONNECT_URL, enabled_events=CONNECT_EVENTS, connect=True,
            description="Array Operator — connected-account events (offtaker pay links)")
        print(f"connect endpoint created: {e.id}")
        print()
        print("Set on Railway:")
        print(f"  STRIPE_CONNECT_WEBHOOK_SECRET={e.secret}")


if "--list" in sys.argv:
    list_all()
else:
    create_or_update()
    create_or_update_connect()
