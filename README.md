# Solar Operator

End-to-end utility data scraping + quarterly report drafting service for
community-solar operators. Layer 1 (extension) captures the user's utility
session. Layer 2 (API + worker) pulls bill PDFs, parses kWh + billing days,
and persists everything per tenant.

## Repo layout

```
solar-operator/
├── extension/             ← Chrome Manifest V3 extension (Solar Operator Sync)
├── api/                   ← FastAPI app + worker + adapter registry
│   ├── app.py             ← endpoints
│   ├── models.py          ← SQLAlchemy schema (Tenant/Array/Account/Bill/Job)
│   ├── db.py              ← engine + session helpers
│   ├── worker.py          ← pulls bills, parses, writes Bill rows
│   ├── scheduler.py       ← APScheduler — every 6h, all tenants
│   └── adapters/          ← per-utility plugins
│       └── gmp.py         ← Green Mountain Power (only adapter today)
├── storage/               ← SQLite DB + downloaded PDFs (gitignored)
│   ├── solar.db
│   └── bills/<tenant_id>/*.pdf
├── captures/              ← optional, raw dev_server captures
├── dev_server.py          ← stub HTTP server used during extension dev
├── requirements.txt
└── README.md
```

## Quickstart (full pipeline locally)

```bash
# 1. install
cd ~/solar-operator
pip install -r requirements.txt

# 2. start the API
uvicorn api.app:app --host 127.0.0.1 --port 8788

# 3. create a tenant
curl -X POST http://127.0.0.1:8788/admin/tenants \
  -H "Content-Type: application/json" \
  -d '{"name":"My Solar Co","contact_email":"me@example.com"}'
# → returns tenant_id + tenant_key

# 4. load the Chrome extension (chrome://extensions → Load unpacked → ./extension)
#    open Options, paste tenant_key, save.

# 5. log into greenmountainpower.com normally
#    → content.js posts JWT + accounts to POST /v1/sync

# 6. trigger a bill pull
curl -X POST http://127.0.0.1:8788/v1/tenants/<tid>/pull \
  -H "Authorization: Bearer <tenant_key>"
# → 6 bills pulled, parsed, persisted in ~5s

# 7. check what we have
curl http://127.0.0.1:8788/v1/tenants/<tid>/bills \
  -H "Authorization: Bearer <tenant_key>"
```

The scheduler also auto-queues a pull every 6 hours per active tenant, and a
1-minute job drainer runs the queue.

## API reference

### Ingest (called by extension)

`POST /v1/sync` — body shape documented in `extension/README.md` → API contract.
Requires `Authorization: Bearer <tenant_key>`. Upserts UtilityAccount rows
keyed on (tenant_id, provider, account_number). Persists a UtilitySession
row for the JWT.

### Tenant-facing

- `GET  /v1/tenants/{id}/status` — accounts, latest session, bill count
- `GET  /v1/tenants/{id}/bills` — every parsed bill, descending
- `POST /v1/tenants/{id}/pull` — force a pull-bills run (synchronous)

### Admin (lock these down before deploying)

- `POST /admin/tenants` — create a tenant, returns tenant_key
- `GET  /admin/tenants` — list all
- `POST /admin/jobs/run` — drain pending Job rows immediately

## Data model

- **Tenant** — a paying customer
- **Array** — a logical solar array (Bruce's "Starlake" = 1 Array, even though
  it maps to 3 UtilityAccounts). `bill_offset_months` codifies the
  "Starlake is same-month, others are prior-month" rule.
- **UtilityAccount** — one account number at one provider. Owns its bills.
- **UtilitySession** — captured JWT + refresh + expiry. Latest wins.
- **Bill** — one parsed bill PDF + extracted kWh / days / period. De-duped by
  `(account_id, document_number)`.
- **Job** — queued background work (pull_bills, generate_report).

## Adapter architecture

A new utility is one new file in `api/adapters/`. Two functions required:

```python
def parse_extension_payload(payload: dict) -> dict: ...
def fetch_bill_pdf(current_bill_url: str, out_path: Path) -> tuple[Path, str]: ...
def extract_bill_metrics(pdf_path: Path) -> dict: ...
```

Then add it to `api/adapters/__init__.py`. The Chrome extension's
`content.js` also needs a per-utility content-script block in `manifest.json`.

Target adapters next (by addressable market):
1. National Grid (NY + MA)
2. Eversource (CT + MA + NH)
3. PSE&G (NJ)

## What's NOT here yet (the next builds)

- **Sheet mapper** — wires a tenant's xlsm so writes hit the right cells per
  array per month. Right now the API has the data but doesn't push it into
  the customer's spreadsheet.
- **Report templater** — learns the customer's voice + structure from 2-3
  prior reports and drafts the next quarterly. The Q2 2026 generator we
  built for Bruce is hardcoded for his style; productizing it is the next
  big lift.
- **Customer dashboard** — minimal HTML UI for tenants to review pulls,
  approve drafts, edit before send.
- **Stripe billing** — subscription tiers, plan limits.

## Test verification (May 29 2026)

Full end-to-end run against Bruce's real GMP login:

```
tenant: Green Mountain Community Solar
accounts ingested: 8
bills pulled: 8
parse success: 8/8
data extracted:
  Tannery Brook   18,398 kWh  30 days  04/08-05/08
  Chester         25,560 kWh  30 days  04/13-05/13
  Timberworks     23,250 kWh  30 days  04/15-05/15
  Waterford       22,440 kWh  30 days  04/15-05/15
  Londonderry     53,520 kWh  32 days  04/17-05/19
  Starlake North   2,409 kWh  29 days  04/23-05/22
  Starlake South   2,395 kWh  29 days  04/23-05/22
  Starlake Center  2,393 kWh  29 days  04/23-05/22
elapsed: ~6 seconds
```

Same numbers as the manual run from the `green-mountain-solar-quarterly`
skill — proves the productized pipeline reproduces what the skill does.
