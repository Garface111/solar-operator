# Stripe Webhook Self-Test Script

## Goal
A script we can run on demand against production that:
1. Reads the current Stripe API key + webhook secret from env
2. Signs a synthetic checkout.session.completed event using the secret
3. POSTs it to https://web-production-49c83.up.railway.app/v1/stripe/webhook
4. Reports back the HTTP status + response body
5. Tails `railway logs --json` for the corresponding WARNING debug
   line (from the webhook handler we just deployed) and prints it

This turns "wait for a real Stripe event" into "click button get
answer" for the current 400-Bad-Request mystery.

## Scope (own ONLY these)
- NEW: `scripts/test_webhook_signature.py`
- NEW: `scripts/README_WEBHOOK_DEBUG.md` (short usage notes)

## DO NOT TOUCH
- `api/stripe_webhook.py` (already touched by webhook-fix branch)
- Anything in `web/`, `extension/`, `api/` other than READ
- Railway env vars (read-only)

## Behavior
```
$ python scripts/test_webhook_signature.py
=== Stripe Webhook Self-Test ===
Using STRIPE_WEBHOOK_SECRET prefix: whsec_REDACTED...
Target: https://web-production-49c83.up.railway.app/v1/stripe/webhook

→ POST with valid signature ...
  Response: 200 OK
  Body: {"received": true}

→ POST with INTENTIONALLY-WRONG signature ...
  Response: 400 Bad Request
  Body: {"error": "Invalid signature"}

→ Tailing railway logs for matching event ...
  [WARNING] webhook debug: secret=whsec_REDACTED... sig=t=1717... len=512
  [WARNING] SignatureVerificationError: No signatures found matching
            the expected signature for payload.

Verdict: SECRET MISMATCH (90%+ confidence). The webhook secret on
Railway does NOT match what Stripe is signing with.

Next action: roll the signing secret in Stripe Dashboard, then
  railway variables --set STRIPE_WEBHOOK_SECRET=<new value>
```

## Tasks
1. Read `api/stripe_webhook.py` to understand the expected sig format.
2. Write the script using `requests` + `hmac` + `time` (vanilla
   Python, no new deps). Use stripe SDK only if available locally.
3. Add README_WEBHOOK_DEBUG.md with one-paragraph usage.
4. Commit ('ops: webhook self-test script'). Do NOT push.
5. Run the script ONCE locally — capture its output verbatim in the
   5-line summary so the orchestrator has the diagnosis.

## Constraints
- No new pip deps.
- Don't actually fire real Stripe events from the CLI tool.
- DO NOT mutate Railway state.
