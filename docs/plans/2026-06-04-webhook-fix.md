# Stripe Webhook Signature 400 — Diagnose & Fix

## Symptom
Production Railway logs show `POST /v1/stripe/webhook → 400 Bad Request`
on every Stripe event delivery, as of 2026-06-05.

Last action taken: orchestrator set
`STRIPE_WEBHOOK_SECRET=whsec_TwEU3WJpr6X37PXngm15uNukr6pzCAAv`
via `railway variables --set`. Railway reports the variable IS set.
Still 400s.

## Possible causes (investigate in this order)
1. **Wrong secret value.** The orchestrator was given a secret; it may
   be the WRONG endpoint's secret (Stripe has separate secrets per
   webhook endpoint). The actual production endpoint should match.
2. **Test-mode vs live-mode mismatch.** The secret might be for the
   test-mode endpoint while production receives live events (or vice
   versa). Project is "Stripe dual-mode" — see
   `.stripe-keys-test.env` / `.stripe-keys-live.env` at repo root.
3. **Code-path bug** in `api/stripe_webhook.py` or wherever the
   verification happens — wrong header parsing, wrong `construct_event`
   usage, raw vs JSON body, etc.
4. **Reverse proxy stripping `Stripe-Signature` header.** Railway's
   ingress sometimes mangles headers — check what arrives.

## Tasks

### Task 1 — Locate webhook handler + verification logic
- Find the webhook endpoint (likely `api/app.py` or
  `api/stripe_webhook.py`). Read it.
- Identify the secret env var name and how it's loaded.
- Identify how `stripe.Webhook.construct_event` is called (raw body
  bytes? string? signature header source?).

### Task 2 — Inspect what production is actually receiving
- `railway logs --json | grep -i stripe -i webhook | tail -30` — what
  payloads / headers do we see?
- Add temporary debug logging to the webhook handler that prints
  (a) the FIRST 40 chars of `STRIPE_WEBHOOK_SECRET` as seen at request
  time, (b) the first 40 chars of `Stripe-Signature` header, (c) the
  raw body length. Commit + push + wait for deploy.
- Trigger a webhook via `stripe trigger checkout.session.completed`
  or `railway logs` until the next real event lands.

### Task 3 — Compare Stripe dashboard vs Railway
- The Stripe dashboard's webhook endpoint config lists the SECRET for
  THIS endpoint. The user can pull it via `stripe webhook_endpoints list`
  if `stripe` CLI is logged in. Verify the secret on Railway matches.
- If MISMATCH: surface the correct secret in the summary so the
  orchestrator can update Railway. Do NOT update Railway variables
  yourself.

### Task 4 — Fix the actual problem
- If it's a code bug, fix it.
- If it's a secret mismatch, do NOT change Railway — flag for
  orchestrator.
- If it's header stripping, add a workaround or escalate.

### Task 5 — Verify
After fix is deployed:
- `railway logs --json | grep stripe.*webhook | tail -10` should show 200s
- Trigger a test event if possible: `stripe trigger checkout.session.completed`
- Confirm tenant.active flips on real signup

## Constraints
- ONLY touch `api/stripe_webhook.py`, `api/app.py` (webhook route),
  and possibly small ops scripts under `scripts/`.
- DO NOT modify other backend modules.
- DO NOT touch `web/`.
- May `git push` — webhook fix is urgent.
- After tasks: emit 5-line summary including any required orchestrator
  action (e.g. "update Railway STRIPE_WEBHOOK_SECRET to <new value>").
