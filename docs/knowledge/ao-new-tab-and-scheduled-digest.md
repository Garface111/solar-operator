# Array Operator — adding a new SPA tab end-to-end + a scheduled client digest

CLASS pattern (instance built Jun'26: the **Audit** tab + weekly settlement-audit
email). Use whenever adding a new owner-facing tab to the Array Operator SPA
(`/root/array-operator/public/`, vanilla JS, Netlify) backed by a new
`/v1/array-owners/*` route, and/or a recurring email to the client.

## The wiring contract (5 touch points — miss one and the tab silently no-ops)

The AO SPA is `index.html` + per-tab `*.js`/`*.css`, with tab switching driven by
**`sandbox.js`** (NOT app.js). To add a tab you MUST edit all five:

1. **`index.html` nav** — add `<a class="tab" id="tab<Name>" href="#<name>" role="tab">Name</a>`
   in the `.tabbar` (place it where it belongs in the journey, e.g. Audit went
   between Arrays and Trends).
2. **`index.html` panel** — add `<section class="panel" id="panel<Name>" role="tabpanel">`
   with an inner `<div id="<name>Root">…Loading…</div>`.
3. **`index.html` includes** — add `<link rel="stylesheet" href="<name>.css">`
   BEFORE `mobile.css` (mobile.css MUST load last — see frontend-bug-patterns),
   and `<script src="<name>.js"></script>` after the other view scripts.
4. **`sandbox.js` TABS map** — add `<name>: { panel: "panel<Name>", tab: "tab<Name>" }`.
5. **`sandbox.js` tabFromHash() + applyView()** — add `if(h==="#<name>") return "<name>";`
   and an `else if(active==="<name>"){ if(window.__aoLoad<Name>) window.__aoLoad<Name>(); }`.

The view's JS exposes its loader as `window.__aoLoad<Name> = load;` (mirror
trends.js). applyView() calls it on activate. Session token is read from
`localStorage.getItem("so_session")` (NOTE: `so_`, not `ao_`); fetch with
`Authorization: "Bearer " + s`. 401/403 → "session expired" empty state.

## Backend route — mirror fleet-trends

Add `@router.get("/v1/array-owners/<thing>")` in `api/array_owners.py`, right
after the function it's modeled on. Auth via `tenant = _tenant_from_bearer(authorization)`.
Loop `Array` rows `where(tenant_id==tenant.id, deleted_at.is_(None), excluded.is_(False))`.
Return `{summary:{…}, <rows>:[…]}`. Per-row try/except so ONE bad array can't 500
the whole response. PITFALL: when inserting a function before a `# ── section ──`
comment header, the patch can swallow the header — re-add it.

## Slick on-brand CSS (matches trends/reports)

Pull the palette from `public/styles.css :root` (NOT hardcoded): `--card`/`--card2`
gradient panels, radii 18–22px, `--good #3fd68a` / `--good2 #7ff0bb` green,
`--gold/--gold2`, `--sky`, `--bad #ff6b6b`, `--line`, `--ink/--muted/--faint`.
Use CSS vars only so day/night themes inherit automatically. Reusable motifs that
read as "the product": a conic coverage ring, a fill meter, ambient breathing
glow (`@keyframes …breathe`), staggered row entrance, status spines (left 4px bar
tinted per status), status filter chips with colored swatches + counts.
Add a `@media (max-width:600px)` block + verify 0px overflow at 390px.

PITFALLS found in QA this session:
- **Ring/disc center text unreadable**: a `conic-gradient` ring with a `::before`
  inner disc needs `isolation:isolate` on the ring + `z-index:-1` on `::before`
  and `z-index:1` on the text, with a SOLID disc bg (e.g. `#0c1018`), not a
  translucent card gradient (which lets the conic bleed through).
- **Status-aware semantics**: a money-gap variance (leak/unconfirmed) must NOT be
  tinted green-positive. Color variance by STATUS (leak=red, unconfirmed=gold),
  not by sign. Green only for genuinely-good clean rows.

## Demo-tenant seeding for screenshots (when the real fleet is too sparse to QA)

Paul's local test tenant often has near-empty data, so a new tab renders blank.
To exercise every state for a screenshot, seed a temporary tenant directly via
models, screenshot, then DELETE it. Required NOT-NULL columns the seed must set
(learned by hitting IntegrityErrors): `Tenant.tenant_key` (+ set `active=True`,
`product="array_operator"` if a scheduler/filter will pick it up), `Bill.tenant_id`,
`GmpDailyGeneration.tenant_id`/`account_number`/`array_id`, `DailyGeneration.tenant_id`,
`Inverter.serial`. The reconcile `independent_feed` gate keys off the
DailyGeneration **source** (`solaredge`/`csv`/`manual` = independent; `gmp_api` =
utility-only), NOT off inverter rows — so you usually don't need Inverter rows at
all; drop them to dodge the serial NOT-NULL. ALWAYS clean up the demo tenant +
all its child rows in the same pass (Ford prizes honesty on real vs fabricated;
a stray demo tenant on prod is a landmine).

## Scheduled client digest (weekly email)

Add the job fn to `api/scheduler.py` near `reconcile_warranty_claims`, register
in `start()` with `CronTrigger(day_of_week="mon", hour=13, minute=0)` (13:00 UTC
≈ 8–9am ET; the existing weekly-reports job is Mon 09:00). Recipient = each
`Tenant.contact_email` where `active==True, product=="array_operator"`. Send via
`api.notify._send_via_resend(to, subject, html, text, product="array_operator")`
wrapped in `email_skin.render_email_skin(... product="array_operator")` for the
dark AO theme. Per-tenant try/except so one bad fleet can't stall the batch.

HONESTY rules Ford expects baked into the digest:
- Send an explicit "all clear" when nothing's flagged (silence ≠ reassurance).
- Never assert a leak the engine didn't confirm; label unconfirmed gaps as such
  with the "connect inverter monitoring to confirm" line.
- SKIP owners with nothing to audit yet (e.g. `have_settlement==0`) — no noise.

VERIFY the email truly renders+sends, don't trust the code path: the LOCAL dev DB
has no `RESEND_API_KEY`, so `_send_via_resend` just LOGS and returns False. To
really test, set `os.environ["RESEND_API_KEY"]` from `~/.hermes/secrets/resend_api_key`
+ `notify.RESEND_API_KEY = …` then call it against a demo tenant whose
contact_email is Ford's inbox; expect `sent ok: True`.

## Deploy + verify (same as the rest of AO)

Backend: `git push origin HEAD:main` → Railway auto-deploys (~75s) → `curl /health`
200 + the new route returns **401 (auth-gated), not 404/500**. Confirm a scheduler
job landed by reading the deployed `api/scheduler.py` over `railway ssh` (NOT the
migrate/boot log). Frontend: MANUAL — `python3 scripts/netlify_api_deploy.py`
(the CLI auth is wedged; see ao-deploy-and-frontend-debugging.md), then
`curl https://arrayoperator.com/<name>.js` 200 + grep the tab id in index.html.
Stage ONLY your own hunks on the shared tree; the index.html/sandbox.js diffs
should contain nothing but your tab additions.
