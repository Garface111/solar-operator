# SolarEdge Monitoring API — integration reference (verified live Jun 2026)

The gold path for SolarEdge arrays (better than extension scraping where a key exists).
Adapter: `api/adapters/solaredge.py` (HTTP truth) wrapped by `api/inverters/solaredge.py`.
Base: `https://monitoringapi.solaredge.com`. Auth is a simple `?api_key=<KEY>` query param
(NOT OAuth, unlike Locus/SMA). Rate budget: **300 req/day per key** → the 5-min server cache
is mandatory.

## The key-tier reality (THE thing that bites)
SolarEdge has TWO key tiers, and which one you get decides whether "one credential, all
arrays" works:
- **Account-level key** (generated at the company/account admin → API Access, NOT inside a
  single site): `GET /sites/list` returns EVERY site under the account. This is the
  "one credential, all arrays" jackpot.
- **Site-level key** (generated per-site under Site Access → API Access): `/sites/list`
  returns ONLY that one site. Everything else still works for that site, but it's one array.
Verified Jun 2026: Bruce's key was site-level → saw only "Londonderry Community Solar"
(site 416160), not his ~7 arrays. ALWAYS call `/sites/list` first and check `sites.count`
to know which tier you're holding before promising fleet-wide discovery. If count looks low,
ask for an account-level key — and confirm whether the arrays even live under ONE account
(they may be split across installer accounts → one key per account).

## Login vs API key (the access-control trap)
Driving the owner's web login (playwright) is the WRONG path for SolarEdge — and often fails:
- The monitoring login is AWS Cognito (`login.solaredge.com`, redirects to `/mfe/auth/`).
  Form fields: `input[type=email]` name=`username`, `input[type=password]` name=`password`,
  submit = the "Sign in" button. No CAPTCHA/2FA seen Jun 2026, but creds are commonly wrong.
- Owners often have a **viewer/homeowner role**, not admin. Symptom when the human logs in:
  "you are not allowed to view this page" → that means creds are VALID but the role lacks
  permission. API key generation is an ADMIN privilege — a viewer account literally cannot
  create one. The installer usually holds the admin account. So: don't burn cycles on the
  login; ask whoever has account admin (owner-upgraded or the installer) to generate the key.
- Stop at ≤2-3 failed automated login attempts — repeated Cognito failures risk a lockout /
  security email on a LIVE pilot account. Verify "did we actually get in" by reusing the saved
  storageState to hit `/solaredge-web/p/home`; if it bounces to `/mfe/auth/`, you're NOT in
  (the post-submit screenshot is unreliable — it catches the page mid-reset).

## Endpoints (all verified returning real data Jun 2026)
- `GET /sites/list?size=100` → `{sites:{count, site:[{id,name,status,peakPower,location}]}}`.
  Account-level lists all; site-level returns its one.
- `GET /site/{id}/details` → `details.{name,status,peakPower(kW),installationDate,type}`.
- `GET /site/{id}/overview` → `overview.{currentPower.power(W), lastDayData.energy,
  lastMonthData.energy, lifeTimeData.energy}` — energy fields are **Wh** (÷1000 → kWh).
- `GET /site/{id}/inventory` → `Inventory.inverters[{name, SN, model, connectedOptimizers}]`.
  Model encodes nameplate: regex `(\d+(?:\.\d+)?)K` → `SE20K`=20kW, `RSE33.3K-USR48BNU4`=33.3kW.
- `GET /equipment/{id}/{SN}/data?startTime=&endTime=` (format `YYYY-MM-DD HH:MM:SS`, URL-encoded)
  → per-inverter telemetry. **7-DAY SPAN CAP per call** — window longer ranges into 7-day chunks.
  Key fields: `totalEnergy` (lifetime Wh COUNTER → daily kWh = (max−min that day)/1000),
  `inverterMode` (STARTING/MPPT/PRODUCING = healthy; FAULT/ERROR/SHUTDOWN/LOCKED = fault →
  map to `error_code`), `date`, `totalActivePower`.

## Feeding the peer engine (proven end-to-end)
Build one `analyze_cohort` unit per inverter: `{id:name, nameplate_kw:(from model),
daily:[{date,kwh}](from totalEnergy diff), error_code:(from inverterMode), last_report:(last
telemetry ts)}`. cohort = the inverters on one site (or arrays under one Client). Verified
result: Londonderry's 6 inverters → 6/6 ok, peer_index 0.96–1.02 (healthy tight cluster).
peer_index normalizes by nameplate share, so a 10kW unit reading 0.96 is fine, not a fault.

Re-runnable proof: `scripts/solaredge_live_peer_proof.py` (key via `SE_KEY` env, never commit).
Full result write-up: `docs/proofs/2026-06-13-solaredge-live-peer-proof.md` in the repo.

## Credential handling
Treat the API key like any live secret: never write it to a committed file, never store in
agent memory, scrub temp JSON after. For production it must live on the Array/tenant record
or an env var (Ford's storage-decision call). Recommend the owner rotate a key that crossed
a chat.
