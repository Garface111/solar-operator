# Array Operator Reports/Billing — local stand-up + visual QA harness

How to probe/QA the AO **Reports tab** (a.k.a. automatic billing) end-to-end on
localhost before touching it. Use this whenever asked to "probe", screenshot, or
extend Reports/invoices/quarterly/drafts. The system is large and mostly built —
ALWAYS stand it up and exercise the real API before claiming anything is
"lost/stubbed/broken".

## Where the code lives (two repos, do not conflate)
- FRONTEND: `/root/array-operator/public/reports.js` (~830 lines, vanilla JS,
  exposes `window.__aoLoadReports`; the tab panel is `#reportsRoot` in index.html).
  Classes are `.rb-*`; styles in `public/command-center.css`.
- BACKEND: `/root/solar-operator/api/billing/` — `routes.py` (mounted at
  `/v1/array-operator/billing`), `matcher.py`, `invoice.py`, `invoice_writer.py`,
  `summary.py`, `delivery.py`. Models: `BillingReportSubscription`, `ReportDraft`,
  `Client`, `Array`, `DailyGeneration` in `api/models.py`.
- The proxy that makes /v1 reachable from the static site locally:
  `/root/array-operator/dev_proxy.py` (mirrors Netlify _redirects → Railway).

## The workflow the product implements (Paul Bozuwa's ask)
Manual customer (no workbook): `POST /subscriptions` with
`customer_name + array_id + allocation_pct` (fraction 0–1) → `percent_of_array`.
Math: array period kWh × allocation_pct × rate. Defaults in delivery.py:
`MANUAL_TARIFF=0.18398`, `MANUAL_BILLING_RATE=0.9`. Read path =
`build_manual_match` → `_array_period_kwh` (DailyGeneration first, Bill fallback).
Then `POST /subscriptions/{id}/draft` → pending ReportDraft → attach GMP PDF →
`POST /drafts/{id}/approve` sends via Resend. NOTHING auto-sends; every path ends
at a human "Approve & send". Quarterly already exists as a per-subscription
cadence (`next_send_at` computes quarter boundaries).

## Reports tab subtab architecture (LIVE — three subtabs)
`shell()` in reports.js renders three `.rb-subtab` buttons → three `.rb-subpanel`
divs, switched by `wireSubtabs()` (generalize that function for ALL panels when
adding a 4th — it toggles each `#rbSub*` display + lazy-renders on click):
  1. **Invoice generator** (`#rbSubInvoice`, `data-sub=invoice`) — upload/match +
     manual "Add a customer" form + global rate + the `.rb-sub` schedule cards.
  2. **Quarterly reports** (`#rbSubQuarterly`, `data-sub=quarterly`) — `renderQuarterly()`;
     per-customer quarter invoice math + Trends visuals via `window.AOTrends`.
  3. **Customers** (`#rbSubCustomers`, `data-sub=customers`) — `renderCustomers()`;
     edit OFFTAKERS. One `.rb-cust` card per subscription with `[data-f="…"]`
     fields (customer_name, client_email, cc_emails, array_id, allocation_pct,
     rate_per_kwh). "Save changes" → `saveCustCard()` → PATCH only changed fields.

KEY: editing a customer's details needs NO backend change. `PATCH
/v1/array-operator/billing/subscriptions/{id}` (SubscriptionPatch in routes.py)
ALREADY accepts customer_name, client_email, cc_emails, array_id, allocation_pct
(fraction 0–1), rate_per_kwh (null clears → global default), etc. So a
"customer editor" UI is frontend-only. The customer record is shared across all
three subtabs (name/rate show in the Invoice cards + Quarterly selector too) —
after a save, call `refreshList()` to keep the Invoice tab in sync.
NOTE the operator's OWN company name is separate: `POST /v1/account/company-name`
(api/account.py), edited in the Master Account tab — NOT in Reports.

## Stand-up recipe (verified working)
1. venv + deps:
   `cd /root/solar-operator && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
   (playwright for screenshots: `pip install playwright`; chromium is usually
   already cached under ~/.cache/ms-playwright).
2. Use a FRESH sqlite DB dir so schema is current (see pitfall below):
   `export SOLAR_DATA_DIR=/tmp/ao_probe_db && rm -rf $SOLAR_DATA_DIR`
3. Pin the session secret so a minted token verifies against the running server:
   `export SESSION_SECRET=<stable-string>` — set it identically for BOTH the
   seed/token-mint AND the uvicorn process. The token is HMAC-signed with it.
4. Seed tenant + Client + Array + a month of DailyGeneration, then mint a token
   with `api.account.mint_session_for_tenant(tenant_id)`. See
   `scripts/seed_ao_billing_probe.py`.
5. Run backend: `uvicorn api.app:app --host 127.0.0.1 --port 8788` (dev_proxy
   defaults to this backend). Set a dummy `RESEND_API_KEY` so imports are happy.
6. Run proxy from the AO repo: `BACKEND=http://127.0.0.1:8788 python3 dev_proxy.py 8089`.
7. Browser: the UI reads the token from `localStorage["so_session"]`. Visit
   `http://127.0.0.1:8089/index.html` first (same-origin), set the key, THEN
   `goto(.../index.html#reports)`. See `scripts/ao_reports_screenshot.py`.

## API smoke checks (curl through :8788 with `Authorization: Bearer <token>`)
- `GET  /v1/array-operator/billing/subscriptions`            → list
- `GET  /v1/array-owners/fleet-tree`                         → arrays for the form
- `POST /v1/array-operator/billing/subscriptions` (-F fields) → create manual
- `GET  /v1/array-operator/billing/subscriptions/{id}/preview-math` → auditable math
- `POST /v1/array-operator/billing/subscriptions/{id}/draft` → pending draft
- `GET  .../subscriptions/{id}/preview?kind=invoice&fmt=pdf`  → invoice PDF blob

## Pitfalls (each cost real time)
- **Stale dev sqlite DB → 500 "no such column".** `Base.metadata.create_all`
  does NOT ALTER existing tables. A pre-existing `solar.db` from an older schema
  will be missing newer columns (e.g. `billing_report_subscriptions.allocation_pct`,
  `array_id`, `gmp_invoice_pdf`) and every list/query 500s. FIX: point
  `SOLAR_DATA_DIR` at a fresh empty dir and re-seed. (Prod runs real migrations;
  this is a dev-only artifact — do NOT "fix" it by editing models.)
- **Secret-masker mangles terminal commands.** The runtime redacts secret-looking
  substrings; inline `$(cat token)`, bearer headers, and quoted secrets get
  corrupted → bash "unexpected EOF/`)`" or a wrong-looking token. FIX: write the
  token to a file, build a curl header file (`Authorization: Bearer ` + token via
  `tr -d '\n'`), and put multi-step curl probes in a `.sh` script rather than
  one-lining them. Mint/verify tokens inside a sourced env file, never echo them.
- **fetchArrays shape bug (fixed f3e62d5, watch for regressions).** The manual
  "Add a customer" dropdown reads `/v1/array-owners/fleet-tree`, which returns
  `{columns:[{array_id, array_name, ...}]}` — NOT `{arrays:[{array_id, name}]}`.
  Reading the wrong shape silently yields "No arrays yet" and an unusable form.
- **Visual QA every state.** Screenshot signed-in Reports, the manual form (assert
  the array `<option>`s are real names, not the empty placeholder), and post-submit
  (assert the new customer appears in `.rb-sub-name`). For the Customers subtab,
  assert `.rb-cust-title` cards render and exercise a real edit→Save→re-fetch to
  prove PATCH persisted (don't trust the in-card status span — it can read empty
  if the selector grabs a sibling card's status; verify via GET /subscriptions).
  `scripts/ao_reports_screenshot.py` now seeds two customers via the API, shoots
  all three subtabs, and exercises a rename-save. Use vision_analyze for
  clipping/overflow.

## Deploy reminder
AO deploy is MANUAL: `netlify deploy --prod --dir=public` from /root/array-operator.
`git push` updates GitHub only; live stays stale. Confirm with Ford before push/deploy.
Backend changes live in /root/solar-operator (the cron auto-commit-trap repo) —
avoid touching it for a frontend-only task.
