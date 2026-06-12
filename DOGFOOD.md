# DOGFOOD — Customer Zero (OCICBB Solar Agency)

Provisioned 2026-06-11 by the customer-zero Hearth agent.
Script: `scripts/provision_customer_zero.py` (idempotent — safe to re-run).

## What exists (local sqlite, storage/solar.db)

| Entity  | Value |
|---------|-------|
| Tenant  | `ten_ocicbb_customer_zero` — "OCICBB Solar Agency", plan=**comped** (no Stripe objects touched) |
| Client  | id=**2** — "OCICBB Solar Agency" (CUSTOMER ZERO note on record) |
| Array 3 | "WEC – Evans (Maple Corner)" → UtilityAccount provider=`wec` **#982501** (real, live-discovered) |
| Array 4 | "Lyndonville – O'Connor-Genereaux" → UtilityAccount provider=`lyndonville` `#LYND-PENDING-DISCOVERY` (placeholder) |

Verified by query-back on every run; re-running creates zero dupes
(tenant/client/array/account all check-exists-before-create; placeholder
account numbers are upgraded in place by `--live`).

## Live ingest result (REAL, not faked)

`python scripts/provision_customer_zero.py --live --pull`

- WEC SmartHub auth with the staged credentials (`~/solar-operator-logins/credentials/wec.json`):
  **SUCCEEDED** against washingtonelectric.smarthub.coop.
- Account discovery: 1 service location — acct **982501**, svc-loc **9825**,
  1519 Wrights Mtn Road, Bradford VT (Richard G Evans). UtilitySession stored,
  so `api/jobs/smarthub_pull.py` runs server-side.
- Generation pull: **91 days fetched, 91 rows upserted** (re-run: 0 inserted /
  91 updated — idempotent).

### Honest data gap: WEC shows ZERO generation

The Evans meter (29747) reports `flowDirection: NET` and every daily value is
**positive** (~23.5 kWh/day consumed; 729.6 kWh over the last 31 days; zero
negative/export days). Per the adapter's NET convention (negative = export),
that means **no solar export is visible on this account** — it looks like a
plain consumption account, not a generation meter. So DailyGeneration rows
exist but are all 0.0 kWh. This is the truthful reading of the live API, not
an adapter bug. To dogfood real generation we need either (a) a WEC login
that owns an actual net-metered/generation meter, or (b) confirmation that
Evans' solar is registered on a different account/meter.

## Adapter/credential gaps found (and what was fixed)

1. **FIXED — `fetch_account_list` crashed on real WEC** (`'list' object has no
   attribute 'get'`): `/services/secured/user-data` returns a LIST of account
   objects, and the account number lives at the account-object level
   (`"account": "982501"`), not in the location summaries. Adapter now handles
   both shapes; the previously-UNVERIFIED response structure is now VERIFIED
   against live WEC and documented in `api/adapters/smarthub.py`.
2. **FIXED — `api/migrate.py` broke every sqlite dev DB**:
   `ADD COLUMN IF NOT EXISTS` is Postgres-only syntax (the `column_exists()`
   guard already made it idempotent), and `utility_accounts.is_residential`
   existed in models.py but had **no migration at all** — any pre-existing DB
   (prod Postgres included, unless created fresh via `create_all`) would 500 on
   first UtilityAccount SELECT. Both fixed.
3. **Lyndonville: NO adapter exists.** It is not SmartHub — the portal is
   InvoiceCloud (registry `needs_adapter: true`, VT.csv `in-progress`). The
   array+account are provisioned with a placeholder number; real ingest needs
   a bespoke InvoiceCloud adapter or manual PDF upload.
4. **xpansiv_ma: NOT provisioned.** `xpansiv_ma` is not a provider code
   (`PROVIDER_CODES` check fails), and ms.xpansiv.com is a REC
   market-settlement registry, not a utility billing portal. Needs its own
   adapter + provider row before it can become an array. Deliberately skipped
   rather than forced in with a fake provider.
5. Staged credential files carry no utility account numbers — discovery via
   `--live` is the only way to get them (worked for WEC; impossible for
   Lyndonville until an adapter exists).

## Not done / out of scope

- No Stripe objects (tenant is `plan=comped`).
- No deploy, no push to main.
- Prod (Railway Postgres) provisioning — run the same script via
  `railway ssh "cd /app && python scripts/provision_customer_zero.py --live"`
  when we choose to promote customer zero to prod.
