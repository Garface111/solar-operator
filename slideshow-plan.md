# Architecture Slideshow — Frame Plan

Source of truth: `architecture-slideshow.html` (single self-contained file, dark theme, SVG).
One conceptual increment per frame; the diagram accumulates within each chapter.
Color code (legend always visible): **cyan** = capture/extension · **violet** = backend API ·
**amber** = state/persistence · **green** = background jobs · **rose** = external services ·
**white** = people/deliverables.

## Chapter 1 — The Big Picture (frames 1–7)

| # | What appears | Caption |
|---|--------------|---------|
| 1 | Title + color legend | Solar Operator: quarterly NEPOOL-GIS credit reports, automated |
| 2 | `extension/` box (EnergyAgent, Chrome MV3) | Layer 1: a Chrome extension rides the operator's own utility logins |
| 3 | `api/app.py` box (FastAPI on Railway) + arrow `POST /v1/sync` | Layer 2: captures land in one FastAPI backend |
| 4 | Postgres box (`api/models.py`) + read/write arrow | Everything durable lives in Postgres on Railway |
| 5 | External services cluster: GMP API, inverter clouds, Stripe, Resend | The backend talks out to utilities, inverters, payments, email |
| 6 | Deliverable: GMCS `.xlsx` → REC agent (arrow via Resend) | The product: a NEPOOL-GIS-ready workbook in the right inbox |
| 7 | Highlight ring around backend: "one Railway process" | Web, scheduler, and worker all run in a single process (`railway.toml`) |

## Chapter 2 — The Web App: `api/app.py` (frames 8–14)

| # | What appears | Caption |
|---|--------------|---------|
| 8 | `FastAPI(title="NEPOOL Operator API")` core (app.py:234) | One app object serves two products: NEPOOL Operator + Array Operator |
| 9 | Router chips attach: `ingest` `account` `onboarding` `billing.routes` `array_owners` `stripe_webhook` `solaredge` `verification` | Features are routers, included at app.py:361-402 |
| 10 | Middleware ring: `CORSMiddleware` + `_security_headers` (CSP) | CORS allows `chrome-extension://` origins; every response gets hardened headers |
| 11 | Auth path 1: `tenant_from_bearer` (app.py:597), key `sol_live_…` | Extension routes authenticate with the tenant's API key |
| 12 | Auth path 2: `_sign_session`/`_verify_session` HMAC tokens (account.py) | Dashboard sessions are self-signed HMAC blobs — no session table |
| 13 | `SPAStaticFiles` mounts: `/onboarding`, `/app`, `/accounts` | Prebuilt React SPAs are served by the same process |
| 14 | Startup hook: `init_db()` + `scheduler.start()` (app.py:410) | Boot wires the DB and starts the entire background engine in-process |

## Chapter 3 — The Capture Layer: `extension/` (frames 15–22)

| # | What appears | Caption |
|---|--------------|---------|
| 15 | Browser frame: utility portal tab (greenmountainpower.com) + extension | Capture happens where the credentials already are: the operator's browser |
| 16 | `content.js`: reads `gmp-vue` localStorage → JWT + accounts → `GMP_TOKEN_CAPTURED` | The GMP content script lifts the portal's own API token |
| 17 | `background.js` service worker: `postSync()` → `POST /v1/sync` (Bearer tenant_key) | The service worker is the hub — it alone talks to the backend |
| 18 | Portal fan-in: `smarthub_content.js` (every `*.smarthub.coop`), `gmp_meter_content.js`, `solaredge/solarweb/sunnyportal/chint_content.js` | One universal SmartHub script covers every co-op; inverter portals get their own scripts |
| 19 | `so_bridge.js` ↔ dashboard SPA (`SO_PAIR`, `SO_CAPTURE_LANDED`) | A postMessage bridge pairs the extension and auto-advances the wizard |
| 20 | `vault.js`: AES-GCM credential vault in `chrome.storage.local` | Portal credentials stay client-side — never sent to the backend |
| 21 | Backend: `sync()` (app.py:610) → `adapters/gmp.py:parse_extension_payload` | Each provider's adapter normalizes the raw capture |
| 22 | Writes: `UtilitySession` + `UtilityAccount` upserts, `Array` autopop, `CaptureEvent` timeline | One login click becomes durable, queryable state |

## Chapter 4 — State: the Postgres Model (frames 23–29)

| # | What appears | Caption |
|---|--------------|---------|
| 23 | `Tenant` (id `ten_…`, tenant_key, product, send_mode) | The paying operator — everything hangs off the tenant |
| 24 | `Client` + FK `tenant_id` | Reports are generated per client, not per tenant (Phase 1) |
| 25 | `Array` + FK `client_id` (`nepool_gis_id`, `excluded`, `bill_offset_months`) | The REC-minting unit; `nepool_gis_id` is the canonical ID |
| 26 | `UtilityAccount` (FK `array_id`) + `UtilitySession` (captured JWTs) | Accounts map meters to arrays; sessions hold the captured auth |
| 27 | `Bill` (FK `account_id`): kWh + costs + `raw_json` + `pdf_bytes` | The data sponge: every bill field GMP exposes is kept |
| 28 | `DailyGeneration` keyed `(array_id, day)` with `source` tag; `generation_sources.is_measured()` | All generation converges into one daily table; measured always beats estimate |
| 29 | `Job` queue + support tables (`StripeEvent`, `ReportDelivery`, `LoginToken`, `InverterDaily`…) | 33 tables total — these are the reporting core |

## Chapter 5 — The Background Engine (frames 30–36)

| # | What appears | Caption |
|---|--------------|---------|
| 30 | APScheduler `BackgroundScheduler` (scheduler.py:36) inside the web process | No worker dyno: cron and queue-drain are threads in the web app |
| 31 | `enqueue_pull_for_all_tenants` (every 6h) → `Job(kind="pull_bills")` rows | The only queued job type — one pull job per active tenant |
| 32 | `run_pending_jobs` (every 1 min) → `worker.run_job` | A one-minute drainer walks the Job table |
| 33 | `pull_bills_for_tenant` → `fetch_bills_json` with stored JWT → `_upsert_bill` | JSON-first: full bill history in one authenticated API pass |
| 34 | PDF fallback: `_pull_via_pdf` (currentBillUrl → parse) | Only when the JSON path fails — expired JWT or GMP downtime |
| 35 | Nightly pipeline: 03:00 `inverter_pull` → 03:30 snapshot → 03:45 `generation_watchdog` → 05:00 `gmp_daily_backfill` → 05:30 `bill_to_daily` | A deliberately ordered overnight data pipeline |
| 36 | `bill_to_daily.transform_all_tenants`: bills prorated → `DailyGeneration(source="bill_prorate")` | Bills become daily rows so dashboards work — real readings always win |

## Chapter 6 — The Report Pipeline (frames 37–45)

| # | What appears | Caption |
|---|--------------|---------|
| 37 | `CronTrigger(month="1,4,7,10", day=1, hour=9)` → `deliver_quarterly_reports` | Quarter-start, 09:00 UTC: the run this company exists for |
| 38 | `_deliver_clients_with_frequency` → per-`Client` fan-out | Cadence resolves Client.report_frequency, falling back to the tenant's |
| 39 | `deliver_for_client` (delivery.py:61) → `report_has_data` guard | Blank workbooks are never mailed — same window, same sources, read-only |
| 40 | `writers/registry.build_workbook` dispatch on `Array.fuel_type` | solar → `gmcs_writer`; wind/hydro/digester/storage → `rec_writer` |
| 41 | `gmcs_writer.build_workbook`: `_rolling_quarters(ref, 6)` | Six most recent *complete* quarters — never the in-progress one |
| 42 | Merge per month: `distribute_kwh_by_calendar_day(bill)` + `DailyGeneration` (daily wins) | Two sources, one precedence rule, no double counting |
| 43 | Sheet render: `A1:C1` = "Name (nepool_gis_id)", producing-array filter, `recs = int(mwh)`, footnote pinned row 31 | Pixel-matches Bruce's GMCS.xlsx; non-producing arrays get no sheet |
| 44 | `notify.send_workbook_email` → Resend attachment; recipients via `send_mode` | to_client / to_me / to_both — the operator controls who receives |
| 45 | `ReportDelivery` rows → `resend_webhook` → receipts (11:00) + pre-send reviews (14:00) | The pipeline audits itself: delivery confirmed, next batch previewed |

## Chapter 7 — Money & Integrations (frames 46–50)

| # | What appears | Caption |
|---|--------------|---------|
| 46 | `onboarding.py`: `/v1/onboarding/start` → trial tenant (no upfront charge) | Five-screen wizard mints tenant_key + onboarding_token |
| 47 | `stripe_webhook.py:422`: signature-verified, `StripeEvent` idempotency | checkout.completed, setup_intent, subscription lifecycle → Tenant state |
| 48 | `pricing.py` graduated tiers + `nameplate_sync`/`usage_report` push to Stripe | $15→$10.50/array tiers; daily jobs keep Stripe quantities honest |
| 49 | `api/inverters.VENDORS` (9 vendors) + `poll_all_sources_job` every 5 min | One vendor interface: validate / fetch_live / fetch_daily |
| 50 | Two products, one backend: `Tenant.product` + `branding.from_address()` | NEPOOL Operator and Array Operator share every layer below the email |

## Chapter 8 — Full Trace: One Bill's Journey to a REC (frames 51–63)

| # | What appears (highlight on full-system map) | Caption |
|---|--------------|---------|
| 51 | Full-system map fades in, dimmed; operator icon at GMP portal | Follow one real path: Bruce's operator login to a REC report |
| 52 | Portal tab lights up: `content.js` reads JWT + 9 accounts | The extension notices a signed-in GMP session |
| 53 | `GMP_TOKEN_CAPTURED` → `background.js.postSync()` | The service worker takes over |
| 54 | Arrow: `POST /v1/sync` Bearer `sol_live_…` → `sync()` app.py:610 | The capture crosses into the backend |
| 55 | `adapters/gmp.py` normalize → `UtilitySession` + `UtilityAccount` rows; `SO_CAPTURE_LANDED` back | Session stored; the onboarding wizard auto-advances |
| 56 | +6h: `enqueue_pull_for_all_tenants` → `Job(pull_bills)` queued | The scheduler notices the new tenant on its next sweep |
| 57 | `run_pending_jobs` → `pull_bills_for_tenant` → GMP `/api/v2/accounts/{n}/bills` | The stored JWT pulls the full bill history, JSON-first |
| 58 | `_upsert_bill` × N → `Bill` rows (kWh, costs, raw_json, pdf_bytes) | Every bill is sponged in — not just solar months |
| 59 | `bill_to_daily` → `DailyGeneration(source="bill_prorate")` | Bills become the daily stream; Trends light up immediately |
| 60 | Oct 1, 09:00 UTC: quarterly `CronTrigger` fires | Quarter boundary — the report run begins |
| 61 | `deliver_for_client` → `build_workbook(client_id=…)` merges 6 quarters | Daily data beats bill proration month by month |
| 62 | Sheet appears: "Starlake (NEPOOL-GIS id)" — monthly MWh, `RECs = int(mwh)` | One sheet per producing array, in Bruce's exact format |
| 63 | `send_workbook_email` → Resend → REC agent inbox; `ReportDelivery` + webhook confirm | ~$135 of consultant work per array per quarter, automated |

**Total: 63 frames · 8 chapters**
