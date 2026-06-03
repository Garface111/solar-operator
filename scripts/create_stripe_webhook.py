"""Create/update Stripe webhook endpoint pointing at production.
Run via: railway ssh "cd /app && python scripts/create_stripe_webhook.py"
"""
import os, stripe
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

URL = "https://web-production-49c83.up.railway.app/v1/stripe/webhook"
EVENTS = [
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
]

existing = [e for e in stripe.WebhookEndpoint.list(limit=100).data if e.url == URL]
if existing:
    e = existing[0]
    print(f"found existing: {e.id} (status={e.status})")
    stripe.WebhookEndpoint.modify(e.id, enabled_events=EVENTS)
    print(f"updated event list to {len(EVENTS)} events")
    print(f"secret (already set): use existing STRIPE_WEBHOOK_SECRET")
else:
    e = stripe.WebhookEndpoint.create(url=URL, enabled_events=EVENTS)
    print(f"created: {e.id}")
    print()
    print("Set on Railway:")
    print(f"  STRIPE_WEBHOOK_SECRET={e.secret}")
