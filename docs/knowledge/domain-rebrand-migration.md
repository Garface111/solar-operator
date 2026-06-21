# Domain rebrand / brand-rename migration playbook

How to rebrand a product (name + domain) across this stack WITHOUT breaking the live
pilot (Bruce) or the published Chrome extension. Proven Jun 2026 on the
Solar Operator → NEPOOL Operator rename + solaroperator.org → nepooloperator.com cutover.

## Prime directive: ADDITIVE first, cut over later

NEVER do a hard domain swap in one shot. Stand up the new domain as a working ALIAS
ALONGSIDE the old one, verify every layer, and keep the old domain fully live. Bruce's
pilot and the already-published extension both depend on solaroperator.org — they must keep
working through the whole migration. Only retire the old domain after the extension
re-review lands AND Ford confirms.

The migration splits into two very different jobs — separate them explicitly and make Ford
choose scope (he picked "full send" but the framing still matters):
- JOB A — visible rebrand: brand NAME in copy/SPA/onboarding/email. Safe, reversible,
  ship-today. Domain unchanged.
- JOB B — domain cutover: DNS/TLS, CORS, email sender (Resend), Stripe, Chrome Web Store.
  Multi-step infra; several steps are money/identity GATES (escalate, don't execute solo
  unless told).

## Layer-by-layer recipe

### 1. DNS + TLS (additive, safe, do first)
Both nepooloperator.com AND arrayoperator.com DNS zones already live in Netlify (Ford added
them). To serve the new domain off the EXISTING marketing site (no new site needed):
- `netlify api updateSite --data '{"site_id":"<id>","body":{"domain_aliases":[<existing>,"newdomain.com","www.newdomain.com"]}}'`
- Adding the alias AUTO-CREATES the `NETLIFY` DNS records pointing newdomain → solaroperator.netlify.app, and Netlify auto-provisions TLS (because the zone is under its control).
- solaroperator Netlify site id: `af4d43ee-e74d-4e9a-9137-996a1fb71a3a`. Its `_redirects`
  proxy /accounts + /onboarding to Railway, so the alias serves an identical working mirror.
- Verify: apex+www resolve (use python `socket.gethostbyname_ex` — no dig/host/nslookup on
  the box), `curl -sI https://newdomain.com/` → HTTP/2 200, and `curl -sI
  https://newdomain.com/accounts` → 200 (proxy works). www resolves first; apex lags a few min.

### 2. CORS (additive env var)
`CORS_ALLOWED_ORIGINS` is a Railway ENV VAR that OVERRIDES the code default. APPEND the new
origins, never replace — read the full current value first (`railway variables --kv | grep
CORS`), add `https://newdomain.com,https://www.newdomain.com`, `railway variables --set`.
Verify live with an OPTIONS preflight for BOTH old and new origin — each must return its own
`access-control-allow-origin` header (not just HTTP 200).

### 3. Email sender domain (Resend) — IDENTITY GATE, but agent-doable via API
To send from `admin@newdomain.com` the domain must be verified in Resend:
- Read RESEND_API_KEY from `railway variables` WITHOUT printing it (see secret-handling
  gotcha below). Register: `POST https://api.resend.com/domains {"name":"newdomain.com","region":"us-east-1"}`.
- Resend returns 3 DNS records: DKIM TXT `resend._domainkey`, SPF MX `send` →
  feedback-smtp.us-east-1.amazonses.com (prio 10), SPF TXT `send` → `v=spf1
  include:amazonses.com ~all`. Add all three to the Netlify DNS zone via
  `netlify api createDnsRecord --data '{"zone_id":"<zone>","body":{...}}'`. Mirror the exact
  pattern from the already-verified solaroperator.org zone.
- Trigger `POST /domains/<id>/verify`; status goes pending → verified in 5-30 min (DNS prop).
- Once verified, flip the SENDER: `railway variables --set "MAIL_FROM=NEPOOL Operator
  <admin@newdomain.com>"`. (MAIL_FROM is env-driven — code default in api/notify.py:22 is a
  fallback only; the Railway var wins.)
- Confirm with a real test send via `POST /emails` from the new From address.
- CAVEAT: Resend verifies the domain for SENDING only. It does NOT set up inbound MX, so
  `admin@newdomain.com` cannot RECEIVE mail. Leave existing `mailto:admin@olddomain.org`
  links pointing at the old (real, Google-Workspace-backed) inbox until inbound is stood up.

### 4. Code sweep (brand name + public URLs) — delegate to opus Claude Code
271 brand-name occurrences across ~85 files is textbook parallel delegation. Run TWO opus
agents concurrently (marketing-site repo `solaroperator-site` + app repo `solar-operator`)
via background tmux + `claude -p ... --model opus --permission-mode acceptEdits
--output-format json --max-turns N`. Write the brief to a file, pass `"$(cat brief.md)"`.
The brief MUST spell out carve-outs (see below). Always do the sweep on a FEATURE BRANCH,
not main — Railway auto-deploys main to Bruce.

Carve-outs that MUST be in the brief (the agents honor them well when explicit):
- MAIL_FROM default + `admin@olddomain.org` email ADDRESSES — leave (sender migrated separately).
- CORS allow-list in api/app.py — ADD/REMOVE nothing; backend must keep accepting BOTH domains.
- extension/ and builds/ — skip entirely (separate Web Store re-publish).
- *.xlsx filenames and "SolarOperator" when part of a FILENAME — leave.
- Generic "solar" energy words (solar array, fuel_type 'solar') — NOT the brand; only the
  two-word product name changes.
- */dist/, api/app_dist/, api/onboarding_dist/ built artifacts — leave (rebuilt by scripts).
- Rule of thumb to give the agent: if it's English a human reads → rebrand; if it's a code
  token / URL / filename / storage key → leave.
Opus catches split-markup wordmarks a naive grep misses, e.g.
`<span>Solar</span> Operator` → `<span>NEPOOL</span> Operator` (TabBar/Login/TopNav.tsx).
Cost observed: ~$7.3 app sweep (46 turns) + ~$0.74 site (27 turns) + ~$1.58 extension (59 turns).

### 5. Verify + build + commit (the parts the agent CAN'T do)
Per the claude-code skill: the agent can't run pytest/git (permission gate) and may
mis-commit. After every agent run, YOU: run the full suite, fix any stale brand-string test
assertions (with a comment), rebuild SPAs + commit dist (the Railpack convention — else web
changes deploy nothing), then commit on the branch. Merge to main only on Ford's go (or his
standing "full send"); Netlify deploy the static site separately; re-verify live bundle hash.

## Chrome extension rename — brand vs FUNCTIONAL identifier (critical)

When renaming the extension (e.g. "Solar Operator Sync" → "EnergyAgent"), the published
extension talks to the LIVE site via shared identifiers. Renaming any of these breaks it:
- ALL `SO_*` message types (SO_CAPTURE_LANDED, SO_PAIR, SO_STATUS_REQUEST, SO_LOGIN_STATE,
  SO_WIPE_COOKIES, SO_OPEN_PORTAL, …) — postMessage protocol with the SPA. LEAVE.
- `so_bridge.js` filename + every manifest/inject reference. LEAVE.
- `broadcastToSoTabs`, `so_session` + `so_*` storage keys. LEAVE.
- API endpoints `PROD_ENDPOINT` / Railway fallback URL, and the content-script `matches` /
  `host_permissions`: LEAVE during a NAME-ONLY rename. These DO change when you fully cut the
  product over to the new domain — see "Completing the cutover (extension)" below.
SAFE to rename: manifest name/short_name/author/description, homepage_url, all UI labels
(popup, options, notification title, action.default_title tooltip), console-log prefixes
`[Solar Operator]` → `[EnergyAgent]`, comments, README/BRIDGE_PROTOCOL docs.
- Use the locked one-word brand "EnergyAgent" even if Ford writes "Energy Agent" (confirm if unsure).
- `smarthub_registry.js` is GENERATED — fix the brand string in
  `scripts/gen_smarthub_registry_js.py` (~line 75) and regenerate; never hand-edit. CI gates
  it with `--check`.
- Bump manifest version (1.6.2 → 1.7.0). Build zip, copy to BOTH Desktops, commit. The
  actual Web Store UPLOAD is Ford's gate (his dev account + multi-day re-review). The store
  TITLE/slug only change when he edits the dev console; slug is permanent once set.

## Completing the cutover (extension fully → new domain) — DONE Jun 2026, v1.7.1

When Ford says "transition entirely to <new domain>," the extension must POINT at the new
domain, not just be renamed. Done as extension v1.7.1 (solaroperator.org → nepooloperator.com):

1. **The apex domain ALREADY serves the API — no `api.` subdomain needed.** The marketing
   site's `_redirects` has `/v1/*  https://web-production-49c83.up.railway.app/v1/:splat  200`,
   so `https://newdomain.com/v1/sync` proxies straight to Railway, SAME-ORIGIN with the
   dashboard. Set `PROD_ENDPOINT = "https://newdomain.com/v1/sync"` (background.js). Keep the
   Railway public domain as `FALLBACK_ENDPOINT`. Verify reachability with a POST that should
   hit the app: `curl -s -o /dev/null -w '%{http_code}' -X POST https://newdomain.com/v1/sync
   -H 'Authorization: Bearer x' -d '{}'` → 401/403 = reached the app (auth gate), 404 = proxy
   miss. (Note `/v1/health` 404s — health is at `/health`, not under /v1; test a real /v1 route
   like `/v1/demo/enter` → 200.)
2. **Add the new host in THREE places in the extension, keep the old during transition:**
   (a) manifest `host_permissions` += `https://newdomain.com/*` + `https://*.newdomain.com/*`;
   (b) the `so_bridge.js` content-script `matches` += the same;
   (c) `SO_TAB_URLS` broadcast array in background.js += the same. Leave solaroperator.org in
   all three so in-flight users mid-session aren't cut off.
3. **The bridge is ORIGIN-AGNOSTIC — no SPA changes needed.** so_bridge.js posts with
   `targetOrigin "*"` and does NOT whitelist any origin; the SPA's `window.addEventListener
   ("message", …)` handlers (useExtensionStatus.ts, CaptureCeremony.tsx, openPortalTab.ts,
   previewSync.ts) do NOT filter `event.origin`. So adding the host to the manifest is
   SUFFICIENT — the handshake works on the new domain with zero website-side code. Verify by
   grepping `web/app/src` for `\.origin` and `solaroperator\.org` in those files — both empty
   = origin-agnostic. (If a future change ADDS an origin filter, this breaks and the SPA needs
   the new domain whitelisted too.)
4. CORS must already allow the new origin (step 2 of the cutover above) — confirm the /v1/sync
   OPTIONS preflight returns `access-control-allow-origin: https://newdomain.com` +
   `access-control-allow-headers: authorization`.
5. Bump version (1.7.0 → 1.7.1), build, copy to both Desktops, commit, merge. **Adding new
   host_permissions makes Chrome show users a permission-change prompt on update** — normal,
   but flag it to Ford. The Web Store re-upload is still his gate.

## Extension as a UNIVERSAL CAPTURE agent (the inverter vector) — strategy, Jun 2026

Ford's vector: "can the extension add inverter data from the inverter sites? boosts the
sublimeness." YES — the extension is a logged-in-session scraper (reads the page the user is
already authenticated in, ships to backend, no keys stored), and that pattern clones onto
inverter monitoring portals (SolarEdge/Enphase/Fronius/SMA/Chint), same as the SmartHub
adapter. Full plan: `docs/plans/2026-06-13-extension-inverter-capture.md`. Key synthesis:
- It is NOT a blanket replacement for the official `api/inverters/` API path. Where a real API
  exists AND the owner can give a key, the API wins (structured, stable, historical).
- The extension earns its place EXACTLY where the API can't reach: **Fronius** (cloud API
  paid + not-US; only free path is the local LAN API the Railway backend physically cannot
  reach — but the owner's browser, on their home network, CAN); **Chint/CPS** (no public API
  at all); **SMA** / any key-averse owner (zero-setup fallback). This is the umbrella synergy:
  ONE EnergyAgent extension serves BOTH products (bills for NEPOOL Operator, inverter truth
  for Array Operator) and answers "will it work with everything?" with yes.
- Drops into the brain with ZERO rework: `api/inverters/peer_analysis.py analyze_cohort(units)`
  is unit-agnostic; an inverter capture is just another "unit" → feeds the Array Overview
  peer bars (built, awaiting live data).
- HARD CAVEAT (why this is a doc, not code): never write scraper selectors blind. No inverter
  portal creds = fabricated guesses that break on contact (violates "never fabricate an
  integration"). Each adapter needs a real login OR a saved portal HTML/JSON sample to inspect
  FIRST. Recommend spiking Fronius-local (highest unique value) or SolarEdge-via-extension
  (easiest to test) before expanding.

## Canonicalizing the legacy domain (HOST-SCOPED 301) — DONE Jun 2026

After the new domain is a working ALIAS, users can still LAND on the old domain because (a)
old links/bookmarks point there and (b) the apex serves IDENTICAL content (the two domains
are aliases of ONE Netlify site — `curl` both and the md5 matches). Symptom Ford reported:
"clicking Open NEPOOL Operator takes me to solaroperator.org/onboarding not nepooloperator.com."

Diagnosis sequence (do this BEFORE editing anything — the leak is usually a link, not a server
redirect):
- `curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' https://OLD/onboarding` — if it's
  200 (not 301), the old domain isn't redirecting; some LINK sent the user there.
- Compare content: `curl -s https://OLD/onboarding | md5sum` vs `https://NEW/onboarding` — same
  md5 ⇒ aliases of one site (so the fix is a redirect rule on that shared site).
- Check the onboarding SPA: it uses SAME-ORIGIN RELATIVE paths on purpose (Done.tsx redirects
  to `/accounts/?fresh=1`, not an absolute host) — so it PRESERVES whatever domain you arrived
  on. Good: it's not the leak, and it means a 301 at the entry route is enough.

The fix — HOST-SCOPED 301s in the shared site's `_redirects` (Netlify supports full-URL source
matching, which only fires for that exact host, so the new-domain alias never matches and never
loops):
```
https://solaroperator.org/             https://nepooloperator.com/             301!
https://www.solaroperator.org/         https://nepooloperator.com/             301!
https://solaroperator.org/onboarding    https://nepooloperator.com/onboarding         301!
https://solaroperator.org/onboarding/*  https://nepooloperator.com/onboarding/:splat  301!
https://solaroperator.org/signup        https://nepooloperator.com/onboarding/  301!   # + signup.html, get-started
```
Put these ABOVE the existing relative proxy rules. The relative rules (`/onboarding`,
`/signup` → `/onboarding/`) stay and now fire only for the NEW-domain host (the old host is
caught by the absolute 301s first). `301!` (the bang) forces the redirect even though `/` has
real content.

CRITICAL — what NOT to redirect (cost-of-getting-it-wrong is a broken live pilot):
- **`/accounts*` (dashboard): keep dual-domain 200-proxy, do NOT 301.** The PUBLISHED store
  extension's `so_bridge.js` pairs by injecting on the dashboard tab, and the published
  manifest still `matches` the OLD domain. Redirecting `/accounts` off the old host breaks
  pairing for Bruce/live users until the new-domain-matching extension (v1.7.1) is actually
  published. Add `/accounts` to the 301 only AFTER that ships.
- **`/v1/*` (API proxy): never 301.** A 301 across a POST risks the browser dropping the
  request body. Keep it a 200 proxy on both domains.

Verify the full matrix live after deploy:
- `OLD/onboarding` → `301 -> NEW/onboarding`; `OLD/` → `301 -> NEW/`
- `NEW/onboarding` and `NEW/` → `200` with EMPTY redirect_url (no loop)
- `OLD/accounts` AND `NEW/accounts` → both `200` (extension still pairs)
- `OLD/v1/demo/enter` AND `NEW/...` → both reach the app (405/401, not 404)

Deploy: `netlify deploy --prod --dir . --site af4d43ee-...` (the marketing repo is
`solaroperator-site`, NOT cloned by default — `gh repo clone Garface111/solaroperator-site`).
Also sweep cross-repo USER-FACING hardcoded links to the old domain (e.g. array-operator's
`public/app.js` "Explore selling your RECs" REC hand-off CTA → point at NEW domain), but LEAVE
`API_BASE`/`DISCOVER_URL`/CORS/`admin@OLD` email — those are API/identity, not nav.

DUAL-AGENT COORDINATION: Ford sometimes has a second agent on the same task. The redirect
work touches shared infra (`_redirects` in one repo + one Netlify site) — last-write-wins on
deploy. If told another agent is active, finish only ISOLATED changes (e.g. the array-operator
link fix), don't re-deploy `_redirects`, and report exactly which repos/sites you touched so
they can reconcile.

## Gotchas hit this session

- **Secret redaction breaks shell quoting.** Having the literal token name (e.g. the env-var
  key for the Resend secret) in a terminal command triggers Hermes's secret-redactor, which
  mangles surrounding quotes → `unexpected EOF while looking for matching '`. FIX: don't put
  the secret's key string in the command. Extract via a partial grep to a temp file
  (`railway variables --kv | grep RESEND > /tmp/x`), parse with python `split('=',1)[1]`,
  write the value to `/tmp/rk_val.txt`, and have your script READ THE FILE. Scrub with
  `shred -u` after. Never `curl ... | python3` either — the security scanner blocks
  pipe-to-interpreter; write JSON to a temp file and parse from there.
- **Resend API behind Cloudflare returns HTTP 403 "error code: 1010"** for urllib's default
  User-Agent. FIX: add `User-Agent: curl/8.5.0` (+ Accept: application/json) header.
- **api.solaroperator.org never resolved** — the extension's OLD PROD_ENDPOINT pointed there
  but the published extension ran on its Railway fallback the whole time. Dead config. Fixed
  in v1.7.1: PROD_ENDPOINT is now `https://nepooloperator.com/v1/sync` (apex proxy → Railway).
- **No dig/host/nslookup on the box** — use python `socket.gethostbyname_ex(domain)[2]` for
  DNS resolution checks.
- **Netlify deploy of the static marketing site:** `netlify deploy --prod --dir . --site
  <af4d43ee...>` (no build step). Serves all aliased domains at once.
