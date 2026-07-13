# V2 Offtaker Pay-Links (Stripe Connect + platform fee)

**Status:** building (2026-07-13)  
**Product:** Array Operator only  
**Goal:** offtakers pay an invoice via a link on the email + PDF; Array Operator
scrapes a tiny platform fee off each payment; the rest lands in the *owner's*
Stripe Connect account.

## Why

Today `api/billing/delivery.py` generates the offtaker invoice (PDF/XLSX) and
emails it with "please make payment to {operator}". Money collection is outside
the product. V2 closes that loop:

```
owner sends invoice
  → Checkout Session (destination charge + application_fee)
  → offtaker pays with card at hosted URL
  → Stripe: fee → platform (EnergyAgent); net → owner's Connect account
  → webhook stamps OfftakerPayment paid
```

This is **not** the owner's SaaS subscription (nameplate / offtaker-count
metering). That path is unchanged. This is offtaker → owner money movement,
with a platform cut.

## Architecture

### Stripe Connect Express per owner tenant
- `Tenant.stripe_connect_account_id` — Express account id (`acct_…`)
- `Tenant.stripe_connect_charges_enabled` — True once Stripe says they can receive
- Onboarding: `POST /v1/array-operator/billing/payments/connect` → Account Link
  URL; owner finishes KYC on Stripe; return URL hits
  `GET …/payments/connect/status` which refreshes charges_enabled from Stripe.

### Per-invoice Checkout Session (not reusable Payment Links)
Variable monthly amounts → one Checkout Session per send, mode=`payment`.
Destination charge:

```
payment_intent_data.application_fee_amount = fee_cents
payment_intent_data.transfer_data.destination = connect_account_id
```

Metadata (on both session + payment_intent):
`kind=offtaker_invoice`, `tenant_id`, `subscription_id`, `payment_id`,
`invoice_number`, `period_key`.

### Platform fee ("scrape a tiny bit")
Source of truth: env `AO_OFFTAKER_FEE_BPS` (basis points, default **150** = 1.5%).
Optional floor: `AO_OFFTAKER_FEE_MIN_CENTS` (default **0** — pure percentage).

  fee_cents = max(min_cents, amount_cents * bps // 10_000)

Money decision: 1.5% is the engineering default. Ford can lower/raise via env
without a code change. Flag any live rate change before minting.

### When the pay link is created
Inside `deliver_subscription` **before** render + email, only when ALL of:
1. tenant is `product=array_operator`
2. Connect account exists AND `charges_enabled`
3. amount_due > 0
4. not a test send (test sends stay pure preview)

Best-effort: Stripe failure never blocks the send — invoice still goes out
with the classic "payable to" wording.

### Surfaces that show the link
- **Email** — green "Pay invoice" CTA above the figures table
- **PDF** — Payment section links the URL (when present)
- **XLSX** — "Pay online:" cell with the URL
- Result dict gains `pay_url` / `payment_id` for the Reports UI later

### Persistence: `offtaker_payments`
One row per pay link created:

| col | purpose |
|-----|---------|
| id | PK |
| tenant_id | owner |
| subscription_id | offtaker |
| invoice_number / period_key | idempotency + display |
| amount_cents / fee_cents / currency | money snapshot |
| stripe_checkout_session_id / stripe_payment_intent_id | Stripe ids |
| pay_url | hosted Checkout URL |
| status | `open` → `paid` / `expired` / `failed` |
| paid_at | when webhook confirms |

Unique: `(subscription_id, period_key)` soft — re-send reuses an open session
if amount matches; otherwise creates a new one (force re-send).

### Webhook
`checkout.session.completed` with `metadata.kind == "offtaker_invoice"`:
mark payment paid, store PI id, alert owner (optional v1.1). Existing
onboarding/subscription handlers are unchanged (kind-gated first).

Also handle `account.updated` (Connect) to flip `charges_enabled`.

## What is NOT in V1 of this build
- Frontend Master Account "Connect bank" button polish (endpoint exists; UI can
  follow — Reports can show a "enable online pay" banner later)
- ACH / bank transfer (card Checkout only)
- Partial payments / multi-currency
- Refunds UI (Stripe Dashboard for now)
- Replacing the SaaS offtaker-count subscription fee with take-rate only

## Env vars
| var | default | meaning |
|-----|---------|---------|
| `AO_OFFTAKER_FEE_BPS` | `150` | platform fee in basis points (1.5%) |
| `AO_OFFTAKER_FEE_MIN_CENTS` | `0` | floor fee in cents |
| `AO_OFFTAKER_PAYMENTS` | `1` | set `0` to hard-disable pay-link creation |

Uses existing `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` (Energy Agent acct).

## Go-live gates (Ford)
1. Enable **Stripe Connect** on the Energy Agent Stripe account (dashboard).
2. Confirm fee bps (default 1.5% — escalate if he wants different).
3. Add webhook events if not already: `checkout.session.completed` (already),
   `account.updated` (new).
4. Owner completes Express onboarding once → invoices get pay buttons.

## Files
- `api/billing/payments.py` — fee math, Connect, Checkout create, mark paid
- `api/models.py` — Tenant Connect cols + OfftakerPayment
- `api/migrate.py` — ALTER tenants + create_all table
- `api/billing/delivery.py` — create link + email CTA
- `api/billing/invoice.py` — PDF/XLSX pay link
- `api/stripe_webhook.py` — offtaker + account.updated handlers
- `api/billing/routes.py` — connect + status endpoints
- `tests/test_offtaker_payments.py`
