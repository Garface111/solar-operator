# Solar Operator Payment Audit — 2026-06-06

---

## Table of Contents

1. [Topology](#1-topology)
2. [Code Surface](#2-code-surface)
3. [Trial Lifecycle](#3-the-trial-lifecycle)
4. [Webhook Surface](#4-webhook-surface)
5. [Edge Cases & Risks](#5-edge-cases--risks)
6. [Observability Gaps](#6-observability-gaps)
7. [Bruce-Specific](#7-bruce-specific)
8. [Top 5 Concrete Action Items](#8-top-5-concrete-action-items)

---

## 1. Topology

**Happy-path flow (new onboarding, June 2026):**

```
Operator fills onboarding form
  → POST /v1/onboarding/checkout
      - Creates Tenant(active=False, status="pending", stage="pending_payment")
      - Creates Stripe Checkout session (mode="setup" — card capture only, NO charge)
      - Returns checkout_url + onboarding_token to SPA
  → Operator completes Stripe Checkout (enters card)
  → Stripe fires POST /v1/stripe/webhook  [checkout.session.completed]
      - Webhook retrieves SetupIntent → stores stripe_payment_method_id
      - Sets trial_ends_at = now() + 14 days
      - Sets subscription_status = "trialing", active = True
      - Internal alert fires. Welcome email NOT sent yet.
  → Operator finishes onboarding wizard screens
  → POST /v1/onboarding/complete
      - Sends deferred welcome email + magic-link sign-in email
  → 14 days pass
  → finalize_expired_trials() runs (APScheduler, hourly interval)
      - Queries Tenant WHERE trial_ends_at <= now AND status="trialing"
      - Calls stripe.Subscription.create(customer, items=[setup_fee+array_fee], default_payment_method)
      - On success: status="active", trial_ends_at=None, stripe_subscription_id stored
      - On failure: internal alert only — tenant stays "trialing" (retried next hour)
  → Stripe generates first invoice (setup_fee + array_fee * array_count)
  → Future array changes: reconcile_subscription_quantity() keeps Stripe quantity in sync
```

**Self-heal path** (webhook lag): If Stripe webhook is slow, `/v1/onboarding/reconcile-checkout` and `/v1/onboarding/extension-installed` both call `_activate_from_paid_session()` which polls Stripe directly and activates the tenant without waiting for the webhook.

**Legacy path** (retired): `_legacy_signup.py` had `POST /v1/signup` → Stripe Checkout (mode="subscription") → immediate charge. Router unmounted at v1.1.0. Dead code, kept for reference. The webhook still handles any in-flight legacy sessions tagged with bare `tenant_id` metadata.

---

## 2. Code Surface

| File | Money Responsibility |
|------|---------------------|
| `api/onboarding.py` | Checkout session creation (mode=setup), 5-screen wizard, cancel-trial, reconcile-checkout self-heal |
| `api/stripe_webhook.py` | All webhook event handlers; idempotency via `stripe_events` table; signature verification |
| `api/stripe_helpers.py` | `reconcile_subscription_quantity()` — syncs Stripe subscription item qty when arrays change |
| `api/scheduler.py` | `finalize_expired_trials()` (hourly) — creates subscription at trial end; zero-array grace extension |
| `api/account.py` | `/v1/account/billing-summary`, `/v1/account/next-invoice`, `/v1/account/billing-portal`; array CRUD endpoints call `reconcile_subscription_quantity` on every add/delete |
| `api/notify.py` | `send_payment_failed_email`, `send_cancellation_email`, `send_trial_charged_email`, `send_add_first_array_email`, `send_internal_alert` |
| `api/models.py` | `Tenant` (trial_ends_at, stripe_customer_id, stripe_subscription_id, stripe_payment_method_id, subscription_status, trial_extended, plan); `StripeEvent` (idempotency log) |
| `api/_legacy_signup.py` | Dead code (unmounted). Legacy $75/mo flat-rate checkout. Kept for emergency re-mount only. |
| `tests/test_deferred_billing_setup_mode.py` | Checkout uses setup mode, PM stored, trial_ends_at set, no welcome email at webhook time |
| `tests/test_trial_finalization.py` | finalize_expired_trials creates subscription for expired trialing tenants |
| `tests/test_trial_zero_arrays.py` | Zero-array grace extension (3 days, once only) |
| `tests/test_trial_cancel.py` | cancel-trial detaches PM and deactivates tenant |
| `tests/test_stripe_webhook_sig.py` | Signature verification, missing-sig 400, duplicate event idempotency |

---

## 3. The Trial Lifecycle

### a. Operator signs up → Checkout → SetupIntent succeeds

**WHAT:** POST /v1/onboarding/checkout creates a pending Tenant row. Stripe Checkout session is created with `mode="setup"` — this captures a card but charges nothing. The `_line_items()` helper in onboarding.py exists but is **never called** in the checkout endpoint (confirmed at line 300-323). The $250 setup fee and $15/array/mo appear in that helper but are NOT used at checkout time.

**WHO triggers:** User action (operator completes Stripe Checkout UI).

**Tenant row on creation:**
```
active = False
subscription_status = "pending"
onboarding_stage = "pending_payment"
trial_ends_at = NULL
stripe_customer_id = NULL
stripe_payment_method_id = NULL
```

**After webhook fires** (`checkout.session.completed`):
```
active = True
subscription_status = "trialing"
onboarding_stage = "extension"
trial_ends_at = now() + 14 days
stripe_customer_id = cus_xxx
stripe_payment_method_id = pm_xxx
stripe_subscription_id = NULL (no subscription yet)
```

**WHAT COULD GO WRONG:**
- Stripe Checkout session creation fails (network, Stripe outage): the orphaned pending Tenant is deleted inline (onboarding.py line 327-329). Good.
- Webhook never fires: self-heal path via `/v1/onboarding/reconcile-checkout` kicks in when operator advances to Screen 3. If even that fails, the operator is stuck at "pending_payment" indefinitely.
- SetupIntent retrieval fails in the webhook (line 95, bare `except Exception`): tenant is activated but `stripe_payment_method_id` stays NULL. At trial end, `stripe.Subscription.create(..., default_payment_method=None)` will likely fail, firing an internal alert. Operator is not notified.

### b. 14 days pass — what runs?

**WHAT:** `finalize_expired_trials()` in api/scheduler.py runs on an **hourly interval** (not cron). It queries `Tenant WHERE trial_ends_at <= now() AND subscription_status = "trialing"`.

**WHO triggers:** APScheduler background thread, started by api/app.py on server startup.

**Tolerance:** Up to 60 minutes. If the server is down during the exact trial-end moment, APScheduler catches up on restart and processes all expired trials on the next run.

**Stripe is NOT involved in the trial period.** There is no Stripe-side trial. trial_ends_at is tracked entirely in our Postgres. If the scheduler never ran, Stripe would not automatically charge — the operator gets free service indefinitely.

### c. Trial ends — subscription creation

**WHAT:** `stripe.Subscription.create()` is called with:
```python
customer = t.stripe_customer_id
items = [
    {"price": STRIPE_SETUP_PRICE_ID, "quantity": 1},   # $250 one-time
    {"price": STRIPE_ARRAY_PRICE_ID, "quantity": max(array_count, 1)},  # $15/arr/mo
]
default_payment_method = t.stripe_payment_method_id
```

**The $250 setup fee is charged here, on the first invoice at trial end, not at checkout.** It is a one-time price attached to the subscription's first billing cycle.

**WHO triggers:** APScheduler `finalize_expired_trials()` running hourly.

**If card declined:**
- `stripe.Subscription.create()` raises `StripeError` (caught at scheduler.py line 204).
- `send_internal_alert` fires: "Trial-end charge FAILED: {t.id}".
- Tenant stays `subscription_status = "trialing"`, `trial_ends_at` unchanged (still in the past).
- **The scheduler retries every hour** (the condition `trial_ends_at <= now` remains true). This provides implicit retry but with no cap and no operator notification. Operator is NOT told their card failed.

**If payment method was removed/detached between signup and trial end:**
- Same outcome: `stripe.Subscription.create()` fails, internal alert fires, hourly retry.
- No `payment_method.detached` webhook handler exists to detect this proactively.

**On success:**
- `subscription_status = "active"`, `trial_ends_at = None`, `stripe_subscription_id` stored.
- `send_trial_charged_email` fires to operator: "Your card was charged $X for N arrays."
- Internal alert fires.

### d. Array count at trial end — is there a race?

The array count is taken **at the moment finalize_expired_trials runs**, not at a fixed snapshot from day 1 or day 14. Specifically (scheduler.py lines 135-140):

```python
array_count = db.execute(
    select(func.count()).select_from(Array).where(
        Array.tenant_id == t.id,
        Array.deleted_at.is_(None),
        Array.excluded.is_(False),
    )
).scalar() or 0
```

**Consequence:** An operator who adds 10 arrays on day 13 of a 14-day trial will be charged for 10 arrays, not for whatever count was present at day 0. There is no "day-1 snapshot" locking the quantity.

**Race condition:** The hourly scheduler run and a concurrent array-add via `/v1/account/clients/{client_id}/arrays` could theoretically execute simultaneously. The array-add calls `reconcile_subscription_quantity` AFTER the subscription exists. If the scheduler creates the subscription at the same moment the array-add fires, there is a brief window where the subscription exists but has the pre-add count. The reconcile call will correct it within the same request. Not a real problem in practice but worth noting.

### e. Operator cancels during trial

**WHO triggers:** `POST /v1/onboarding/cancel-trial` (operator action, requires session Bearer token).

**WHAT happens:**
- `stripe.PaymentMethod.detach(pm_id)` is called if Stripe is configured and PM exists.
- Tenant set to: `active=False, subscription_status="cancelled", trial_ends_at=None, stripe_payment_method_id=None`.
- Internal alert fires. No operator-facing confirmation email is sent (unlike post-trial cancellation which uses `send_cancellation_email`).
- Note the spelling inconsistency: cancel-trial sets `"cancelled"` (two l's), while `_process_subscription_deleted` sets `"canceled"` (one l). Both are treated identically by access-gating checks (which check `not in ("active","trialing","comped")`), so there is no functional impact.

**Is the flag actually used to gate access?**
YES. From account.py lines 813, 870, 2130, 2157:
```python
if not t.active and t.subscription_status not in ("active", "trialing", "comped"):
    raise HTTPException(402, "Reactivate your subscription to send reports")
```
And delivery.py line 73:
```python
is_active = (tenant.active or tenant.subscription_status in ("comped", "trialing")) and client.active
```
A cancelled tenant (active=False) cannot send reports. They CAN still reach `/v1/account` to view their status (account.py line 157-161 explicitly allows inactive tenants to reach /account for export).

---

## 4. Webhook Surface

### Handled events (api/stripe_webhook.py lines 351-356)

| Event | Line | Handler | Purpose |
|-------|------|---------|---------|
| `checkout.session.completed` | 352 | `_process_checkout_completed` | Activate tenant, store Stripe IDs, set trial |
| `customer.subscription.updated` | 353 | `_process_subscription_updated` | Sync subscription_status from Stripe |
| `customer.subscription.deleted` | 354 | `_process_subscription_deleted` | Deactivate tenant, send cancellation email |
| `invoice.payment_failed` | 355 | `_process_invoice_payment_failed` | Email operator + Ford; don't deactivate yet |

### Required for happy path

- `checkout.session.completed` — **required**; self-heal fallback exists but webhook is primary.

### Required for failure modes

| Event | Handled? | Risk if missing |
|-------|----------|----------------|
| `invoice.payment_failed` | ✅ Yes | Operator and Ford are notified on each retry |
| `customer.subscription.deleted` | ✅ Yes | Tenant deactivated; cancellation email sent |
| `customer.subscription.updated` | ✅ Yes | Covers past_due → canceled progression |

### MISSING — events with no handler

| Event | Risk |
|-------|------|
| `payment_method.detached` | If operator detaches their card via the Stripe billing portal between signup and trial end, we have no DB record. `finalize_expired_trials()` attempts `stripe.Subscription.create(default_payment_method=None_or_detached)` → fails → internal alert only. No proactive warning to operator. |
| `invoice.payment_succeeded` | No audit trail in `stripe_events`. Not critical for operations but useful for debugging billing history. |
| `customer.deleted` | If the Stripe customer object is deleted (e.g., admin mistake in Stripe dashboard), the tenant is never deactivated in our DB. |
| `invoice.marked_uncollectible` | Stripe marks invoices uncollectible after all retries fail before canceling the subscription. We would see `customer.subscription.updated` → canceled eventually, so this is LOW risk but creates a gap in operator notification timing. |

**The `payment_method.detached` gap is the most relevant missing event** for the current 5-operator scale. The others are defensive coverage.

---

## 5. Edge Cases & Risks

### a. Trial-end job doesn't run on time — H impact

**Scenario:** Server is down for 2 hours straddling a trial-end moment.

**Outcome:** APScheduler uses `"interval"` scheduling. On restart, it runs immediately. All tenants with `trial_ends_at <= now()` are processed. The operator gets extended free service (up to downtime duration + 1 hour max) but IS charged once the server recovers.

**CRITICAL:** Stripe does NOT catch this. There is no Stripe-side trial (no `trial_end` set on a subscription). If the scheduler never runs again (server permanently down), the operator gets free service forever. Railway is the only safeguard — if Railway is permanently down, we have bigger problems.

**Risk: MEDIUM.** Short downtime is handled gracefully. Permanent scheduler death is undetected.

### b. Webhook delivered twice — is idempotency enforced? — L impact

YES, idempotency is enforced via the `stripe_events` table (models.py, StripeEvent, primary key = event_id). On first delivery: event stored with `status="received"`, handler runs, status updated to `"processed"`. On duplicate delivery: `existing.status == "processed"` → returns `{ok: True, duplicate: True}` immediately (stripe_webhook.py lines 345-346).

**Race condition:** Two concurrent deliveries of the same event could both pass the initial `existing = db.get(StripeEvent, event_id)` check before either writes. The second `db.add(StripeEvent(...))` would fail with a unique-constraint violation (event_id is PK), causing a 500 response. Stripe would retry the 500 and eventually succeed on a sequential attempt. Not a correctness problem, just noisy.

### c. Webhook event lost — does anything reconcile? — M impact

For `checkout.session.completed`: YES. Self-heal exists in `_activate_from_paid_session()`, called by both `/v1/onboarding/reconcile-checkout` and `/v1/onboarding/extension-installed`. The SPA calls these on Screen 3, so even if the webhook was never delivered, the tenant activates when the operator clicks "I've installed it."

For `invoice.payment_failed`: NO reconciliation. If this event is lost, the operator is not notified of the decline. Stripe retries for 3 days, so a lost event means the operator goes unwarned for that retry cycle. If ALL retry events are lost, the subscription moves to canceled via `customer.subscription.deleted` — which IS handled.

For `customer.subscription.deleted`: NO reconciliation. If lost, tenant stays `active=True` in our DB even though Stripe canceled. Operator continues receiving reports and is not billed. Duration: until the next `customer.subscription.updated` or a manual DB fix.

### d. test_deferred_billing_setup_mode.py — SILENTLY BROKEN CONTRACT — H

**Why it's skipped:** The test file has NO `@pytest.mark.skip` decorator and no xfail marker visible in the source. The "auto-skipped" behavior described in prior sessions is not explained by any marker in the code. It is possible the test is excluded by a pytest.ini configuration, a `-k` filter in a CI command, or collected but erroring at import time in a specific test-run configuration. **However, the tests WOULD FAIL if run in isolation** — this is the real finding.

**The broken assertion** (test_deferred_billing_setup_mode.py lines 127-129):
```python
expected = datetime.utcnow() + timedelta(days=4)
diff = abs((t.trial_ends_at - expected).total_seconds())
assert diff < 60, f"trial_ends_at too far off: {t.trial_ends_at}"
```

**The actual code** (stripe_webhook.py line 98):
```python
t.trial_ends_at = now() + timedelta(days=14)
```

The test expects ~4 days; the code sets 14 days. The diff would be ~864,000 seconds (10 days). The assertion would fail with margin `864000 > 60`. **This is a broken contract.** At some point the trial was extended from 4 days to 14 days without updating the test.

**Contracts that ARE still upheld** (tested by the other 3 tests in the file):
- Checkout must use `mode="setup"` with no `line_items` — ✅ upheld
- Webhook stores `stripe_payment_method_id` and activates tenant — ✅ upheld
- Welcome email NOT sent at webhook time — ✅ upheld
- No `stripe_subscription_id` at webhook time — ✅ upheld

**Bottom line: YES, the test represents a silently broken contract.** The trial is 14 days in the code but the test asserts 4 days. No test currently verifies the correct 14-day trial window.

### e. The $250 setup fee — when is it charged?

**At trial END, not at checkout.** The Checkout session uses `mode="setup"` (card-capture only). The `_line_items()` helper function in onboarding.py is defined but not called from the `checkout()` endpoint. At trial end, `finalize_expired_trials()` creates a Stripe Subscription with `STRIPE_SETUP_PRICE_ID` (quantity=1) + `STRIPE_ARRAY_PRICE_ID` (quantity=array_count). Both appear on the first subscription invoice.

**Risk:** If `STRIPE_SETUP_PRICE_ID` env var is not set in production, only the array fee is on the first invoice. The scheduler silently skips the setup fee line item (lines 162-165):
```python
if setup_price_id:
    items.append({"price": setup_price_id, "quantity": 1})
if array_price_id:
    items.append({"price": array_price_id, "quantity": quantity})
```
No alert fires if setup_price_id is missing. Verify this env var is set in Railway.

### f. 1-array minimum — enforced where?

**At billing time, not at the API level.** Two enforcement points:
1. `reconcile_subscription_quantity()` in stripe_helpers.py line 64: `target_qty = max(array_count, 1)`
2. `finalize_expired_trials()` in scheduler.py line 159: `quantity = max(array_count, 1)`

An operator can delete all their arrays. The reconcile call would set the Stripe quantity to 1 (the minimum). They would continue to be billed $15/month for 1 virtual array. There is no API-level block preventing deletion of all arrays, nor is there a dashboard warning. This is acceptable product behavior but worth being explicit about.

### g. Bruce is comped — analysis of every path

**How comp is implemented:** Bruce's `subscription_status = "comped"` and/or `plan = "comped"`. There is no dedicated `is_comped` boolean flag. Comp is checked at every gating point via string comparison.

**Every code path that checks for comped status:**
- `delivery.py:73` — `tenant.active or tenant.subscription_status in ("comped", "trialing")` — report delivery gate
- `account.py:813` — `t.subscription_status not in ("active", "trialing", "comped")` — send-report gate
- `account.py:870` — same — send-sample-report gate
- `account.py:2130` — same — send-one-client-report gate
- `account.py:2157` — same — resend-report gate
- `scheduler.py:71` — `t.active or t.subscription_status in ("comped", "trialing")` — scheduled delivery gate
- `notify.py:219` — `PLAN_LABELS = {"comped": "Solar Operator (comped)"}` — email display name

**Risk: `reconcile_subscription_quantity` fires an internal alert for Bruce.** Every time Bruce adds or removes an array, `reconcile_subscription_quantity` is called (account.py lines 2095, 2486, 2566, 2637, 2704, 2765). Since Bruce has no `stripe_subscription_id`, the function hits:
```python
if not subscription_id:
    send_internal_alert("⚠️ Billing not reconciled — missing subscription id", ...)
    return
```
This is a **false-positive alert** on every array change for Bruce. Currently 7 arrays, unlikely to change frequently, but it creates alert noise.

**Risk: Webhook overwrite.** The `_process_subscription_updated` handler sets `t.subscription_status = new_status` for any tenant matching by `stripe_subscription_id` OR `stripe_customer_id`. If Bruce has a `stripe_customer_id` on file and a stray Stripe subscription is created under that customer, the webhook would overwrite `subscription_status = "comped"` with `"active"` or `"trialing"`. This would not charge Bruce (no PM on file for billing) but could affect access gating. Unlikely, but the comp is NOT stored in a write-protected field.

**Risk: Charge path.** The only code that could actually charge Bruce is `finalize_expired_trials()`. Bruce's `subscription_status = "comped"` would NOT appear in the query `WHERE subscription_status = "trialing"`. Bruce cannot accidentally be charged via the trial-end path. **He is safe on that vector.**

### h. Stripe key rotation

**How the key is sourced:** `STRIPE_SECRET_KEY` is read from `os.getenv()` at module import time (stripe_webhook.py line 44, onboarding.py line 54) and assigned to `stripe.api_key`. The scheduler re-reads it at runtime on each `finalize_expired_trials()` call (scheduler.py lines 115-122). The webhook handler re-reads `STRIPE_WEBHOOK_SECRET` for debug logging (line 310) but uses the module-level constant for actual verification (line 330).

**If the key rotates without a process restart:** The module-level `stripe.api_key` and `STRIPE_WEBHOOK_SECRET` would be stale. Webhook verification would fail (400 to Stripe → Stripe retries → event backlog). Railway deploys restart the process, picking up new env vars. Manual secret rotation without a restart is the risk. test_stripe_webhook_sig.py documents this exact failure mode.

**Source:** Environment variable only. No secret manager, no file. Standard Railway pattern.

### i. Trial extension — operator-facing or admin path?

**Operator-facing:** None. No UI exists for an operator to request a trial extension.

**Automated extension:** Only the zero-array 3-day grace (scheduler.py lines 142-146). Gated by `trial_extended = False`. Happens exactly once per tenant.

**Admin path:** None exists in the API. Ford would need to run a direct DB update:
```sql
UPDATE tenants SET trial_ends_at = '2026-07-01' WHERE id = 'ten_xxx';
```

**Flagging as operational risk:** If an operator is a genuine high-value lead and has a technical problem during their trial (e.g., extension doesn't install on their corporate-locked Chrome), there is no product path to extend their trial. This requires a DB SSH session. For 5 customers this is fine; for 50 it becomes unsustainable.

### j. Sub-array billing precision — prorated or full month?

**Prorated.** `reconcile_subscription_quantity()` calls:
```python
stripe.SubscriptionItem.modify(
    recurring_item["id"],
    quantity=target_qty,
    proration_behavior="create_prorations",
)
```
Stripe creates a proration credit/charge for the remainder of the current billing cycle. An operator who adds an array on day 14 of a 30-day cycle is charged 16/30 of $15 = ~$8 immediately, then full $15 on the next cycle.

**Caveat:** During the trial period, there is no Stripe subscription yet, so no reconcile call is made. Arrays added during the trial are simply counted at trial-end subscription creation. The first invoice reflects the full count at that moment — no prorating of trial-period additions.

---

## 6. Observability Gaps

### Failure notification matrix

| Failure Type | Operator Notified? | Ford Notified? | How? |
|---|---|---|---|
| Card declined on recurring invoice | ✅ Yes | ✅ Yes | `send_payment_failed_email` + `send_internal_alert` via `invoice.payment_failed` webhook |
| Trial-end subscription creation fails | ❌ No | ✅ Yes | `send_internal_alert` in `finalize_expired_trials()` exception handler only |
| Webhook handler crashes | ❌ No | ✅ Yes | `send_internal_alert` + 500 response (Stripe retries) |
| Card detached between signup and trial | ❌ No | ❌ No (until failure) | Only discovered at trial end when subscription creation fails |
| Checkout webhook lost | ❌ No | ✅ Yes | `send_internal_alert` for unknown onboarding token; self-heal on Screen 3 |

### Stripe-side vs Solar Operator-side logging

**Stripe side:**
- All events visible in Stripe Dashboard → Events tab (7-day searchable window)
- Webhook delivery attempts and response codes visible per event
- No proactive alerting for failed deliveries

**Solar Operator side:**
- `stripe_events` table logs every webhook: event_id, event_type, status (received / processed / ignored / error), note (truncated error text), processed_at
- APScheduler runs are not logged to the DB — only to stdout (visible via `railway logs`)
- `send_internal_alert` goes to `ford.genereaux@dysonswarmtechnologies.com` via Resend

### If a charge fails today, who finds out and when?

**Recurring subscription charge:**
1. Stripe fires `invoice.payment_failed` → webhook handler → `send_payment_failed_email` to operator + `send_internal_alert` to Ford. This happens within minutes of the charge attempt.
2. Stripe retries up to 4 times over several days. Each retry fires `invoice.payment_failed` again.
3. After all retries, Stripe fires `customer.subscription.deleted` → tenant deactivated, `send_cancellation_email` to operator.

**Trial-end subscription creation failure:**
1. `finalize_expired_trials()` catches the exception and fires `send_internal_alert` to Ford.
2. Operator is NOT notified. They continue to have `active=True` and `subscription_status="trialing"` in the DB, so their access is unaffected.
3. The hourly retry will attempt again on the next hour.

**GAP:** An operator whose card fails at trial end will not know their subscription didn't start. They'll appear to have a working account (active=True, trialing) but are not paying. Ford will receive hourly alerts until manually resolved.

---

## 7. Bruce-Specific

**Tenant ID:** `ten_14b76982523a3b47`

**How comp is stored:** `subscription_status = "comped"` (and likely `plan = "comped"` based on `PLAN_LABELS` and model comment). There is no boolean `is_comped` field. The comp is a string value in two nullable columns, not a protected flag.

### All code paths that special-case Bruce's status

1. **delivery.py:73** — `tenant.active or tenant.subscription_status in ("comped", "trialing")` — allows comped tenants to have reports delivered even if active=False
2. **account.py:813** — blocks report delivery for non-comped/trialing/active tenants
3. **account.py:870** — same for sample reports
4. **account.py:2130, 2157** — same for per-client report sends and resend
5. **scheduler.py:71** — includes comped tenants in scheduled delivery runs
6. **account.py:159** — explicitly notes comped tenants can access /account for data export

### Is the comp persistent?

**Mostly yes.** The comp is set directly in the DB (`subscription_status = "comped"`) and is not subject to being overwritten by normal webhooks because:
- Bruce has no `stripe_subscription_id` on file → `_process_subscription_updated` won't match him
- Bruce won't appear in `finalize_expired_trials()` query (subscription_status ≠ "trialing")

**However**, `_process_subscription_updated` (stripe_webhook.py:207-213) matches by `stripe_customer_id` OR `stripe_subscription_id`:
```python
select(Tenant).where(
    (Tenant.stripe_subscription_id == sub_id) |
    (Tenant.stripe_customer_id == customer_id)
)
```

If Bruce has a `stripe_customer_id` on file (possible from legacy signup or manual entry) and any subscription is created under that Stripe customer (by accident or a future admin mistake), the webhook would overwrite `subscription_status = "comped"` with the subscription's status. **This would not charge Bruce** (no payment method on file) but would affect access gating.

### Alert noise: reconcile fires for Bruce on every array change

Every array add/delete/exclude via the account portal calls `reconcile_subscription_quantity(t.stripe_subscription_id, ...)`. For Bruce, `stripe_subscription_id` is null, so the function logs an error and fires:
```
⚠️ Billing not reconciled — missing subscription id
Tenant ten_14b76982523a3b47 (bruce.genereaux@gmail.com) changed array count to N but has no stripe_subscription_id. Fix the subscription quantity manually.
```
This is a false-positive internal alert. With 7 stable arrays, this fires infrequently today, but it should be suppressed for comped tenants.

### Path where Bruce could accidentally get charged

**None via normal product flows.** The charge paths are:
1. `finalize_expired_trials()` — requires `subscription_status = "trialing"`, Bruce is "comped"
2. `stripe.Subscription.create()` — only called from finalize_expired_trials
3. `reconcile_subscription_quantity()` — calls `stripe.SubscriptionItem.modify()`, which requires an existing subscription. Bruce has none, so this is a no-op (with the false-positive alert).

**Theoretical risk:** A future admin script or manual Stripe operation could create a subscription under Bruce's Stripe customer, which the `_process_subscription_updated` webhook would then sync to our DB. This is not a product code path.

---

## 8. Top 5 Concrete Action Items

---

### #1 — Trial-end charge fails silently for operator (trial_end scheduler exception)

**Risk:** When `finalize_expired_trials()` fails to create the subscription (card declined, PM detached, Stripe error), the operator is NOT notified. They receive hourly internal alerts to Ford but think their account is active. The operator has no signal to update their card.

**Fix:** After catching the exception in scheduler.py, call `send_payment_failed_email` (or a purpose-built "trial charge failed" email) to the operator:
```python
# scheduler.py ~line 208, inside the except block
try:
    send_payment_failed_email(
        to=t.contact_email, name=t.name,
        amount_dollars=0, next_attempt_unix=None
    )
except Exception:
    pass
```
(Reuse or add a `send_trial_charge_failed_email` variant with trial-specific copy.)

**Location:** `api/scheduler.py:204-210`
**Impact: H | Effort: S | Ship before launch: YES**

---

### #2 — test_deferred_billing_setup_mode.py has a broken assertion

**Risk:** The contract "trial is 14 days" is untested. If the trial duration changes again, nothing will catch it. The test asserts 4 days while code sets 14 days.

**Fix:** Update `test_webhook_setup_mode_stores_pm_and_trial` (line 127):
```python
# Change:
expected = datetime.utcnow() + timedelta(days=4)
# To:
expected = datetime.utcnow() + timedelta(days=14)
```
Also ensure the test is not excluded from the test run (check `pytest.ini` / CI command for `-k` filters).

**Location:** `tests/test_deferred_billing_setup_mode.py:127`
**Impact: H | Effort: S | Ship before launch: YES**

---

### #3 — Missing `payment_method.detached` webhook handler

**Risk:** If an operator removes their card via the Stripe billing portal between day 0 and day 14, we have no record of this. At trial end, `stripe.Subscription.create(default_payment_method=detached_pm)` fails. Ford gets an internal alert; the operator does not.

**Fix:** Add a handler for `payment_method.detached` in stripe_webhook.py:
```python
def _process_payment_method_detached(pm: dict) -> dict:
    pm_id = pm.get("id")
    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.stripe_payment_method_id == pm_id)
        ).scalars().first()
        if not t:
            return {"ignored": f"no tenant for pm={pm_id}"}
        t.stripe_payment_method_id = None
        db.commit()
        ...
    send_internal_alert("⚠️ Payment method detached mid-trial", ...)
    # Optionally: email operator to re-add card
    return {"tenant": t.id}
```
Register it in the `handlers` dict.

**Location:** `api/stripe_webhook.py:351-356` (add to handlers dict)
**Impact: M | Effort: M | Ship before launch: YES**

---

### #4 — Bruce gets false-positive "billing not reconciled" alerts on every array change

**Risk:** Operational noise. Every array add/delete for Bruce fires an internal alert that looks like a billing error but isn't. As the tenant count grows and this pattern is copied for future comped accounts, alert fatigue sets in and real billing errors get ignored.

**Fix:** Skip reconcile silently for comped tenants. In `reconcile_subscription_quantity` (stripe_helpers.py) or in every caller, check for comped status before calling:
```python
# In stripe_helpers.py, add an early return before the alert:
if not subscription_id:
    # Silence for comped/free accounts — they have no subscription by design
    # (callers should check tenant.subscription_status == "comped" before calling)
    logger.info("reconcile: no subscription_id for tenant %s — likely comped, skipping", tenant_id)
    return
```
Or pass a `is_comped` flag from callers, or check `Tenant.plan == "comped"` in the helper.

**Location:** `api/stripe_helpers.py:35-43` (the first early-return block)
**Impact: L (Bruce) / M (future scale) | Effort: S | Ship before launch: NO, but soon**

---

### #5 — No admin path to extend a trial

**Risk:** When a legitimate operator has a technical problem during their trial (corporate Chrome policy blocking extension, IT firewall, etc.), Ford has no product path to extend their trial. Requires an SSH DB session, which is slow and error-prone.

**Fix:** Add an admin-only endpoint:
```python
# api/app.py or a new api/admin.py
@router.post("/admin/tenants/{tenant_id}/extend-trial")
def extend_trial(tenant_id: str, days: int = Query(default=7), admin_key: str = Header(...)):
    if admin_key != os.getenv("ADMIN_SECRET_KEY"):
        raise HTTPException(403)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t or t.subscription_status != "trialing":
            raise HTTPException(404)
        t.trial_ends_at = max(t.trial_ends_at, datetime.utcnow()) + timedelta(days=days)
        t.trial_extended = False  # allow the zero-array extension to re-fire if needed
        db.commit()
    send_internal_alert("Trial extended", f"{tenant_id} extended by {days} days")
    return {"ok": True, "trial_ends_at": t.trial_ends_at.isoformat()}
```

**Location:** `api/app.py` or a new `api/admin.py` module
**Impact: M (operationally) | Effort: S | Ship before launch: NO, but before first customer complaint**

---

*Audit conducted by automated agent on 2026-06-06. Read-only: no code changes made.*
*Report length: ~430 lines.*
