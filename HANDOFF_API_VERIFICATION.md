# Handoff: Inverter cloud-API verification + enrollment

> Written 2026-07-04 by the cloud Claude Code session (claude.ai/code) for the
> LOCAL Claude Code session on Ford's machine. The cloud sandbox's network
> policy blocks the vendor hosts (403 at the proxy) and Ford's Gmail, so this
> mission needs your real network and Ford's logged-in Chrome. General product
> context: read `CC_HANDOFF.md` and `CLAUDE.md` first if you haven't.

## Why this matters (the strategic picture)

Extension-scraped inverter data (Fronius / SMA / Chint) is only as fresh as the
last time a Chrome with the EnergyAgent extension was running — a structural
ceiling that no extension work can pass, and the #1 trust risk Ford has named
("data not being fresh"). Moving Fronius + SMA to their official cloud APIs
converts them to server-side pulls (the SolarEdge model: fresh 24/7, no Chrome).

The adapters are ALREADY WRITTEN — `api/inverters/fronius.py` and
`api/inverters/sma.py` — but both carry loud "UNVERIFIED AGAINST A LIVE
ACCOUNT" banners. Your mission: make them verified, and run the enrollment
steps that only a machine with real network + Ford's inbox can do.

## Authorization + hard limits

Ford has explicitly authorized (2026-07-04, in the cloud session):

- Driving Chrome, **including his Gmail** (ford.genereaux@gmail.com), to send
  the two enrollment emails below and to read the vendors' replies on later
  runs.

Hard limits — do NOT cross without a fresh, explicit, per-instance yes from
Ford (AskUserQuestion):

- **No purchases, order forms, or paid commitments.** The Fronius Query API is
  chargeable and its order form is a commercial contract; SMA production
  registration means accepting commercial terms for Dyson Swarm Technologies
  LLC. Verification and *inquiries* are authorized; *commitments* are Ford's.
  (CLAUDE.md: pricing decisions come LAST; flag caveats loudly.)
- **No credentials in the repo, ever.** API keys/secrets live in env vars or
  Ford's password manager. Production creds ultimately belong in
  `InverterConnection.config` (encrypted at rest via `api/crypto.py`), entered
  through the dashboard — not committed.
- Send email **only** the two drafts below (edited for accuracy as needed).
  Don't send anything else from his account.

## The tool you'll use

`scripts/verify_inverter_apis.py` (on main). It drives the REAL adapter code —
`validate()` → `fetch_live()` → `fetch_daily(7d)` — against live endpoints and
prints what parsed. Green run = adapter is production-ready.

```bash
cd ~/solar-operator && source venv/bin/activate   # local checkout may be /root/solar-operator
git pull
python -m scripts.verify_inverter_apis --vendor fronius
```

Adapter contract (don't break it): `validate(config) -> dict`,
`fetch_live(config) -> dict | None`, `fetch_daily(config, start, end) ->
list[dict]`, errors raised as `InverterError` / `InverterAuthError` from
`api/inverters/base.py`.

---

## Task 1 — Verify Fronius against the public demo system (~10 min, do first)

No account needed: the harness defaults to the demo PV system whose
credentials Fronius publishes in its own API docs.

```bash
python -m scripts.verify_inverter_apis --vendor fronius
```

- **PASS** → edit the banner at the top of `api/inverters/fronius.py`: replace
  "has not been exercised against a live US account" with a dated note
  ("verified against Fronius's public demo system on <date> via
  scripts/verify_inverter_apis"). Also update the stale claim that the API "is
  NOT currently offered in the United States" — US access exists now (Fronius
  enables REST API per installer account via pv-support-usa@fronius.com; the
  API remains chargeable, pay-per-data-point). Update the module `NOTE`
  constant to match. Branch → commit → PR → tell Ford.
- **401/403 auth error** → the published demo key may have rotated. Browse
  Fronius's Solar.web Query API docs (you can!) for the current demo
  AccessKeyId/AccessKeyValue/pvSystemId, update `FRONIUS_DEMO` in the script,
  re-run.
- **Parse/shape errors** → capture the raw JSON (add a quick `print` or use
  httpx in a REPL with the demo headers), fix the parsing in
  `fronius.py::_channels` / `fetch_daily`, keep the error contract, extend the
  unit tests (see `tests/` for the existing inverter test patterns), re-run
  until green.
- `fetch_live` returning `None` at night, or 0 daily rows on the demo system,
  is a WARN not a failure — check the demo system has recent production before
  chasing ghosts.

## Task 2 — Fronius US enablement + pricing inquiry (Gmail, authorized)

Prereq: Ford's Solar.web account email. **Ask Ford for it** (AskUserQuestion)
if you don't have it; if he has no Solar.web installer account yet, say so in
the email and ask Fronius what account type is required.

From ford.genereaux@gmail.com → **pv-support-usa@fronius.com**:

> **Subject:** Solar.web Query API — US access enablement + pricing
>
> Hello — we're Dyson Swarm Technologies LLC (EnergyAgent / Array Operator,
> arrayoperator.com), monitoring and reporting software for US community-solar
> operators. Please enable REST API / Solar.web Query API access on our
> Solar.web account [FORD'S SOLAR.WEB EMAIL] and send the current order form
> and pricing. Our usage: once-daily aggregated energy pulls plus occasional
> live-power reads for roughly 20–50 PV systems, growing — we'd like to know
> which data-package tier that lands in. Contact: Ford Genereaux,
> ford.genereaux@gmail.com. Thank you!

When the reply arrives (check on later runs): summarize the pricing to Ford in
chat. **Do not return the signed order form yourself** — that's a commitment
(see limits).

## Task 3 — SMA sandbox enrollment + verification

1. Browse https://developer.sma.de/sma-sandbox-apis and the FAQ to find the
   current API Developer Support contact route (contact form or email).
2. Send (or submit via form) from Ford's account:

> **Subject:** Sandbox client credentials request — Monitoring API
>
> Hello — we're Dyson Swarm Technologies LLC (EnergyAgent / Array Operator,
> arrayoperator.com), a fleet-monitoring and reporting platform for
> community-solar operators in the US. We'd like sandbox client credentials
> for the Monitoring API to validate our OAuth token flow and
> measurement/energy endpoints ahead of a production app registration. Planned
> use: read-only monitoring (live power + daily energy) for plants whose
> owners grant consent — initially a few dozen systems in Vermont. Contact:
> Ford Genereaux, ford.genereaux@gmail.com. Thank you!

3. When credentials arrive:

```bash
SMA_SANDBOX=1 SMA_CLIENT_ID=… SMA_CLIENT_SECRET=… SMA_SYSTEM_ID=… \
  python -m scripts.verify_inverter_apis --vendor sma
```

Known rough edges you'll likely hit (by design — fix as you go):

- The sandbox URL layout in the script (`sandbox.smaapis.de/oauth2/token`,
  `…/monitoring/v1`) is best-effort from SMA's docs. If it 404s, get the real
  paths from https://sandbox.smaapis.de/monitoring/index.html and SMA's
  Postman collection (downloadable from the developer portal), then fix the
  script defaults and note the production-vs-sandbox mapping in `sma.py`.
- The sandbox simulates the plant-owner consent flow: per SMA docs, POST
  `sandbox.smaapis.de/oauth2/v2/bc-authorize` then PUT
  `…/bc-authorize/apiTestUser@apiSandbox.com/status` with body status
  `accepted`. You may need this before monitoring calls return data. If the
  client-credentials grant in `sma.py::_get_token` doesn't match the sandbox's
  expected flow, adapt — and mirror whatever you learn into the adapter's
  docstring, since the production consent flow will follow the same pattern.
- SMA ROTATES refresh tokens on every refresh — `sma.py` already handles this
  (`_TOKEN_CACHE` + config mutation); don't regress it.

On green: update `sma.py`'s banner with a dated "verified against SMA sandbox"
note, branch → commit → PR, and tell Ford it's ready for the production app
registration decision (pricing: ~€12/system/yr base + €0.09/kWac — e.g.
€16.50/yr for a 50 kW system, per developer.sma.de/api-plans).

## Task 4 — after verification (Ford decisions, not yours)

- SMA production app registration (terms acceptance) + real plant-owner
  consent flow (OAuth redirect handling — new work, scope it separately).
- Fronius order form / pricing acceptance.
- Then wiring real systems: creds go into `InverterConnection.config` via the
  dashboard connect UI (`FIELDS` in each adapter drive the form), server pulls
  take over from extension scraping per array, and the freshness ceiling for
  those vendors is gone.

## Reporting back

Append a dated entry to the **Status log** below on every run (commit it), and
give Ford the harness output + a plain-language verdict in chat. If you get
blocked, say exactly where and why — no silent stalls.

## Status log

- 2026-07-04 (cloud session): Harness shipped (#26). Fronius/SMA unreachable
  from the cloud sandbox (network policy 403) — everything above is pending
  first local run.
- 2026-07-04 (LOCAL session, Ford's machine): First live run. **Fronius = PARTIAL
  PASS (adapter is NOT broken).** The auth + request path reach the live API
  correctly, but Task 1 as written is impossible now — Fronius RETIRED the public
  demo system.
  - The harness's baked-in demo key returned `401 {"responseError":1102,
    "responseMessage":"AccessKey not found."}` (rotated/deleted, not a network
    block — we reached Fronius fine).
  - Chased the fallback: Fronius no longer publishes a self-serve public demo
    *system*. A currently-working community demo key
    (`FKIAB4CDA71C…`, github.com/drc38/Fronius_solarweb) AUTHENTICATES (200 on
    `/pvsystems`) but has ZERO systems attached (`totalItemsCount:0`), and it's
    403 `responseError 1013 "User not authorized"` for the old demo system id.
    So: auth + request shape VERIFIED live; response PARSING (flowdata/aggrdata
    channels) still only doc-verified — needs a real producing system.
  - Updated the harness (`scripts/verify_inverter_apis.py`): swapped the dead key
    for the working community key, added a `/pvsystems` preflight that resolves a
    real system when creds have one and otherwise reports an honest "auth OK, no
    system — set FRONIUS_PV_SYSTEM_ID" partial instead of a confusing 403.
  - Corrected `api/inverters/fronius.py` banner to the real dated status. NOTE:
    the FAQ/country-list still shows the Query API is NOT self-serve in the USA —
    the "US access exists now" claim in Task 1 is UNVERIFIED, so I did NOT assert
    it. Confirming it is exactly what Task 2's email is for.
  - **Tasks 2 & 3 SENT (Ford authorized + confirmed live, 2026-07-04):**
    - Task 2 — Fronius email SENT from ford.genereaux@gmail.com →
      pv-support-usa@fronius.com ("Message sent" confirmed). Key correction:
      Ford has no Solar.web account of his own — the Fronius systems live in his
      DAD's account, **bruce.genereaux@gmail.com** (Green Mountain Community
      Solar). The email names that account and asks (1) whether US Query API
      access is possible + what's required to enable it on that account, and (2)
      the order form + pricing tier for ~20-50 systems. Commits to nothing.
    - Task 3 — SMA sandbox-credentials request SUBMITTED via the developer-
      portal contact FORM (developer.sma.de/contact — there's no support email;
      "Thank you for your message" success page confirmed). Selected "SMA
      Monitoring API"; company Dyson Swarm Technologies LLC; contact Ford.
  - **Now waiting on vendor replies (check on later runs, both land in Ford's
    Gmail):** Fronius → summarize pricing/US-availability to Ford, do NOT sign
    the order form (commitment = Ford/Bruce). SMA → when sandbox creds arrive,
    run `SMA_SANDBOX=1 SMA_CLIENT_ID=… SMA_CLIENT_SECRET=… SMA_SYSTEM_ID=…
    python -m scripts.verify_inverter_apis --vendor sma` to fully verify that
    adapter (see Task 3 rough edges above).
