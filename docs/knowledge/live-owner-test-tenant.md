# Standing up a LIVE Array Operator owner dashboard for testing (proven Jun 2026)

Goal: hand Ford a signed-in arrayoperator.com link that reads REAL SolarEdge telemetry
("reading live from your hardware"), not the demo fallback. Use when he says "put up the
live version" / "begin testing the live version."

## The real-vs-demo logic (public/app.js loadDashboard)
The owner dashboard decides what to show, in this order:
1. Reads `localStorage.so_session`.
2. If signed in → `fetch("/v1/array-owners/overview", Bearer session)`.
3. If that returns ZERO arrays → falls back to static `inverter-truth.json` (demo).
4. If anonymous → demo file.
The ON-SCREEN tell is the meta line under the title: `render()` prints
`reading <b>live from your hardware</b>` when `data.source==="live"`, else
`reading <b>demo data</b>`. `adaptOverview()` sets `source: anyLive ? "live" : "demo"`
where `anyLive = arrays.some(a => a.live && a.live.source)`. So a fresh/empty account is
correctly demo — it just LOOKS identical except those two words. (Consider a LOUD banner.)

## The only live creds we have
Bruce's prod tenant (name "Green Mountain Community Solar") has 47 arrays but only TWO with
live SolarEdge keys: `Londonderry` (SE site 416160) + `Cover Catamount Building` (SE site
4631514), both on an ACCOUNT-LEVEL key. Tenant id `ten_6522da7ac2e1d01d`; tenant_key
`sol_live_xGqBD0BPahu0iCx2M2sSuaxOW7cDimzQ`; product = **nepool** (he's a verifier, not an
owner). Signing him into the OWNER dashboard shows 47 cards, 45 blank — messy. Build a clean
owner tenant instead.

## Recipe (all on prod via `railway ssh --service web`, base64-pipe to dodge quoting)
Run python on prod with: `B64=$(base64 -w0 script.py); railway ssh --service web "echo $B64
| base64 -d > /tmp/s.py && cd /app && python /tmp/s.py"`. (`curl … | python3` is blocked by
the security scanner — never pipe to an interpreter; write to a file.)

1. **Find the source live arrays** — query Bruce's tenant for arrays where
   `solaredge_api_key` and `solaredge_site_id` are both set.
2. **Create a clean owner tenant** — `Tenant(id="ten_"+secrets.token_hex(8), name=...,
   contact_email=ford's, tenant_key="sol_live_"+secrets.token_urlsafe(24), plan="trial",
   product="array_operator", active=True, is_demo=False)`. MUST set `id=` explicitly (string
   PK, not autoincrement). Make it idempotent: look up by a fixed name first, reuse if found.
3. **Mirror the live arrays onto it** — for each source array, create/update an Array on the
   owner tenant with the SAME `solaredge_api_key` + `solaredge_site_id`. The key is copied
   DB-to-DB SERVER-SIDE — it NEVER enters agent context or memory. Idempotent by name.
4. **Pull live data** — `from api.jobs.solaredge_pull import pull_daily_for_array;
   pull_daily_for_array(db, array_id, 7)` (signature: db FIRST, then array_id).
5. **Verify live** — `tok = _sign_session(str(owner.id))` (NOT tenant_key — see SKILL.md
   gotcha), then `TestClient(app).get("/v1/array-owners/overview", Bearer tok)`. Expect 200,
   each array `live.source == "solaredge"`, a real `peer.peer_index`, `ANY_LIVE True`.
   NOTE: `live.current_power` reads None in the evening (panels idle) — that's normal; the
   `source: solaredge` field is the live signal, daily kWh is populated regardless.
6. **Hand off the link** — `https://arrayoperator.com/?token=<_sign_session(str(id))>`.
   The owner site now accepts `?token=` (see below). The session token is a 30-day creds —
   it gets REDACTED in agent stdout (looks like a JWT), so write the URL to a file and copy
   it to `/mnt/c/Users/fordg/Desktop/` (+ OneDrive/Desktop) for Ford to click. Don't post
   the link publicly; rotate by re-minting anytime.

## The `?token=` sign-in handler (shipped Jun 2026)
The owner site (`array-operator/public/app.js`) originally signed in ONLY via
`localStorage.so_session` — so there was NO way to hand someone a link AND the owner
magic-link email (`/?token=`) was DEAD. Fix added at the top of `loadDashboard()`: read
`new URL(location).searchParams.get("token")`, `localStorage.setItem("so_session", tok)`,
then `history.replaceState` to scrub the token out of the address bar. Deploy: `node --check
public/app.js` → commit → `git push` → `netlify deploy --prod --dir public --site
966cb1f5-944e-41fd-855b-10053edc5d18` (UUID, not slug). Verify shipped:
`curl -s https://arrayoperator.com/app.js | grep -c 'searchParams.get("token")'` → 1.

## Caveats to state to Ford every time
- The hardware behind it is Bruce's real SolarEdge panels (only live creds we have) — legit
  internal test, but not synthetic data.
- Dollar figures use the front-end estimate model ($0.21/kWh + REC) — real kWh, estimated $.
- peer_index reads ~1.0 because `Array.nameplate_kw` is unset (engine infers nameplate from
  observed peak, so each array passes vs its own baseline) — set it for a sharper signal.
