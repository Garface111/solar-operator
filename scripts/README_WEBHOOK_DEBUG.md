# Webhook Self-Test

Diagnoses Stripe 400-Bad-Request errors on the production webhook endpoint by
sending a synthetic `checkout.session.completed` event signed with the local
`STRIPE_WEBHOOK_SECRET`, then a second request signed with a deliberately-wrong
secret, and compares which one the server accepts.

## Usage

```
STRIPE_WEBHOOK_SECRET=whsec_... python scripts/test_webhook_signature.py
```

If the variable is not passed explicitly, the script looks for it in a `.env`
file at the repo root. No new pip dependencies — uses only stdlib (`hmac`,
`hashlib`, `urllib`, `subprocess`).

## Interpreting the output

| valid-sig status | bad-sig status | Diagnosis |
|---|---|---|
| 200 | 400 | Signing secret is correct — 400s from Stripe are a different issue |
| 400 | 400 | **Secret mismatch** — Railway env var differs from Stripe endpoint secret |
| 200 | 200 | Secret empty on server — verification is disabled, fix immediately |

## Fixing a secret mismatch

1. Stripe Dashboard → Developers → Webhooks → your endpoint → Signing secret → Roll
2. Copy the new `whsec_...` value
3. `railway variables --set STRIPE_WEBHOOK_SECRET=<new value>`
4. Re-run this script to confirm 200 / 400 split
