# Standing up AO Reports/backend LOCALLY to probe + visually QA before building

When asked to "probe what exists" or build/QA Array Operator Reports (or any AO
owner-site tab that hits the FastAPI backend), DON'T scrape blind or trust the
diff — stand the real stack up locally, drive the real API, and screenshot the
real UI. This recipe got a full Reports-tab probe + multi-feature build done with
visual QA at every step. Ford's hard rules this satisfies: localhost http never
file://, Playwright screenshot + vision_analyze every UI change, never fabricate.

## Repo layout reminder (where the pieces live)
- Frontend: `/root/array-operator/public/` — `reports.js` (the Reports tab,
  `window.__aoLoadReports`), `command-center.css`, `index.html`. Vanilla JS, no build.
- Backend: `/root/solar-operator/api/` — `billing/{routes,delivery,matcher,invoice,
  summary,invoice_writer}.py`, `models.py`, `migrate.py`, `account.py`.
  Reports frontend calls `/v1/array-operator/billing/*` and `/v1/array-owners/*`.
- `/root/array-operator/dev_proxy.py` mirrors Netlify locally: serves `public/` and
  reverse-proxies `/v1`, `/accounts`, `/onboarding`, `/health` to BACKEND.

## The stack (3 processes + a seeded DB)
1. **venv + deps** (first run only; deps are usually pip-cached so it's fast):
   `cd /root/solar-operator && python3 -m venv venv && source venv/bin/activate &&
   pip install -r requirements.txt`. Then `pip install playwright` (browsers are
   already cached under ~/.cache/ms-playwright; no `playwright install` needed).
2. **Fresh sqlite DB** so `Base.metadata.create_all` picks up CURRENT columns.
   CRITICAL: the default dev DB (`storage/solar.db`) is usually STALE — it predates
   recent columns, and create_all does NOT ALTER existing tables, so you'll hit
   `sqlite3.OperationalError: no such column: <table>.<col>` on the first query.
   Fix = point at a brand-new dir: `export SOLAR_DATA_DIR=/tmp/ao_probe_db && rm -rf
   /tmp/ao_probe_db` BEFORE seeding, so create_all builds the schema from scratch.
3. **Pin SESSION_SECRET** so a minted token verifies against the running server.
   `account.mint_session_for_tenant(tid)` signs with `SESSION_SECRET` (HMAC); if the
   server and the mint use different secrets you get `401 Session expired`. Put it in
   an env file sourced by BOTH the server and the mint:
   ```
   # /tmp/ao_env.sh
   export SESSION_SECRET="probe-dev-secret-stable"
   export SOLAR_DATA_DIR="/tmp/ao_probe_db"
   export RESEND_API_KEY="dummy-probe-key-no-send"   # no real email goes out
   ```
4. **Seed a realistic tenant + array + a month of DailyGeneration**, then mint the
   token. Use a `seed_probe.py` scratch script (keep it in the repo root; it's
   harmless and handy). Models needed: `Tenant(product="array_operator")`, `Client`,
   `Array(tenant_id, client_id, name, ...)`, `DailyGeneration(tenant_id, array_id,
   day, kwh, source="manual")`. Write the token to `/tmp/ao_token.txt`.
5. **Run backend** on 8788 and **dev_proxy** on 8089, both sourcing the env file:
   ```
   cd /root/solar-operator && source venv/bin/activate && source /tmp/ao_env.sh && \
     uvicorn api.app:app --host 127.0.0.1 --port 8788 --log-level warning   # background
   cd /root/array-operator && BACKEND=http://127.0.0.1:8788 python3 dev_proxy.py 8089  # background
   ```
   Health check both: `/health` on 8788, `/index.html` on 8089 → 200.

## Driving the API (and a masker gotcha)
The terminal secret-masker MANGLES inline `$(cat token)` and inline python with
quotes/JSON in bash one-liners ("syntax error near unexpected token"). RELIABLE
patterns instead:
- Header file for curl: `printf 'Authorization: Bearer ' > /tmp/h.txt; tr -d '\n' <
  /tmp/ao_token.txt >> /tmp/h.txt; curl -s -H @/tmp/h.txt $B/v1/...`
- Better: write a `.py` probe using `urllib.request` (multipart helper for
  form-POST subscriptions) and run it through the venv. No quoting hell, exact JSON.
  (NOTE: write_file occasionally drops content as a serialization artifact — always
  read_file the script back to confirm it landed before running it.)

## Visual QA (the part Ford checks hardest)
Playwright headless, set the session in localStorage on the SAME origin first:
```python
pg.goto(BASE + "/index.html", wait_until="domcontentloaded")
pg.evaluate("t => localStorage.setItem('so_session', t)", TOKEN)   # auth key is so_session
pg.goto(BASE + "/index.html#reports", wait_until="networkidle")
pg.wait_for_timeout(2500)                # let render + fetches settle
pg.screenshot(path="/tmp/shots/NN_state.png", full_page=True)
```
Then `vision_analyze` each PNG and fix clipping/overflow before calling it done.
Also collect console errors (`pg.on("console", ...)`) — a clean build has zero.

## Reusing Trends charts in a report (the spiral/ridgeline)
The Trends views self-register on `window.AOTrends` (see TRENDS-VIEWS-CONTRACT.md).
To embed them anywhere (e.g. a quarterly report) WITHOUT reimplementing:
```js
const C = window.AOTrends;
const data = await (await fetch("/v1/array-owners/fleet-trends", {headers})).json();
const prepped = C.prep(data);
const view = C.getView("spiral");      // or "ridgeline" | "liquid" | "heatfield"
host.style.position = "relative";
const stop = view.mount(host, prepped, C);   // returns a cleanup fn — call it on teardown
```
index.html already loads trends-core + all four view files, so `AOTrends` is
available app-wide at click-time. Ford likes the spiral + ridgeline for reports.
With only 1 month of seeded data the charts render sparse (one point) — that's
honest/correct, not a bug; they shine with multi-year data.

## Cleanup
The seed script, `/tmp/ao_env.sh`, `/tmp/ao_token.txt`, `/tmp/h.txt`, `/tmp/shots/`,
and any probe `.py` are scratch — leave them in /tmp (NOT committed). `seed_probe.py`
in the solar-operator root is fine to leave untracked; do NOT `git add` it.
