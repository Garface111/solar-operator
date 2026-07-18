# Solar Operator — Handoff to Opus 4.8 (session of June 4-5, 2026)

You are picking up a long, productive session with Ford on the Solar Operator
SaaS. Below is the state of the world so you walk in fully oriented.

## Project North Star

Solar Operator is an AI SaaS for solo solar trade contractors / NEPOOL
stamping agents (e.g. Ford's father Bruce Genereaux, who manages Green
Mountain Community Solar for ~7 arrays). The product automates quarterly
NEPOOL-GIS reporting. Customers pay $45/array/month + a setup fee.

The current operating vector is "sublime experience over tool" — turn
every utility-portal login into a small dopamine spike, eliminate manual
data entry, make the system *feel* like it's working for the operator.

## What shipped this session

### 1. Multi-login auto-create autopop (`api/app.py`)
When the Chrome extension captures a utility login with no matching
existing Client, the backend now auto-creates a fresh Client + attaches
all captured arrays. Naming priority:
1. customer_name from utility adapter (VEC exposes this)
2. login email (GMP default — what you'll see most of the time)
3. login username
Array nicknames are intentionally NOT used as client names anymore
(they're per-array labels like "Roof", "Barn" and make terrible client
names). Operator can rename via ClientCard.

72 tests passing in `tests/`. Key files:
- `tests/test_multi_login_autocreate.py` (5 new tests)
- `tests/test_placeholder_adoption.py`
- `tests/test_autopop.py`, `tests/test_vec_autopop.py`

### 2. v1.3.0 extension + sublime onboarding
- `extension/BRIDGE_PROTOCOL.md` — wire protocol spec (read this first
  before touching the extension)
- `extension/so_bridge.js` — page↔extension postMessage bridge running on
  solaroperator.org. Forwards SO_OPEN_PORTAL, SO_PAIR, SO_STATUS_REQUEST
  to background.js; broadcasts SO_EXTENSION_PRESENT, SO_LOGIN_STATE,
  SO_CAPTURE_LANDED back to page.
- `extension/background.js` — auto-pair via SO_PAIR (kills the old
  copy-paste activation code), background-tab portal opens via
  OPEN_UTILITY_PORTAL, broadcasts SO_CAPTURE_LANDED on every /v1/sync.
- `extension/content.js` + `vec_content.js` — emit LOGIN_STATE_DETECTED
  every 2.5s so the SPA shows live "Sign in at GMP" → "Signed in —
  capturing your data…" transitions.
- Onboarding `web/onboarding/src/screens/Extension.tsx` — rewritten
  (622→~430 lines). Auto-pairs the extension silently. No activation
  code is ever shown to the user. Auto-advances to /done on first
  SO_CAPTURE_LANDED.

### 3. Dashboard CaptureCeremony (`web/app/src/components/CaptureCeremony.tsx`)
After onboarding, /done instant-redirects to dashboard with ?fresh=1.
The Clients tab shows a panel that listens for SO_CAPTURE_LANDED and
cascades client+array chips in with stagger animation. "Sign into
another portal" CTA with GMP + VEC background-tab buttons. Turns 50
utility logins into a 50-step feedback loop. CSS animations live in
`web/app/src/index.css` (`.so-cascade-row`, `.so-cascade-chip`).

### 4. Per-client spreadsheet upload (Backlog #1 SHIPPED)
- `/v1/ingest/commit` accepts optional `force_client_id` — when set,
  every row pins to that Client, operator_name column ignored.
- ClientCard has a new "Import arrays" button (next to "Import NEPOOL
  IDs") that wires up to ImportSpreadsheetModal with forceClientId.
- Top-level "Import spreadsheet" unchanged (still creates new clients).

### 5. Dashboard walkthrough rewrite (Backlog #3 SHIPPED)
- `WalkthroughOverlay.tsx` STEPS rewritten for post-sublime reality:
  no more placeholder-rename arc. New anchors: 6-add, 6-import, 7.
- Storage key bumped `so_walkthrough_v1_seen` → `v2_seen` so existing
  operators get the new tour.

### 6. Webhook signing secret fixed
Stripe webhook on Railway was rejecting events with 400 (Invalid
signature). Ford gave me the correct secret and I set it via
`railway variables --set STRIPE_WEBHOOK_SECRET=whsec_REDACTED`.
Railway redeploying as of last message — verify with
`railway logs --json | grep stripe.*webhook | tail -5`. Should be
200s now, not 400s.

Also added "Continue anyway" button on the SPA's "processing"
state so the operator can never be stranded waiting for a webhook.

## The IMPROVEMENT BACKLOG (live)

```
[1] ✅ Per-client spreadsheet upload — SHIPPED
[2] ⏸ Deferred billing (SetupIntent + 4-day grace + delayed charge)
    Design doc: docs/DEFERRED_BILLING_DESIGN.md
    BLOCKED on Ford answering 5 product questions:
      1. Trial length 4 days OK?
      2. Trial countdown UI (banner? emails?)
      3. Zero arrays at trial end — minimum bill or extend?
      4. Cancel-during-trial free?
      5. Existing tenants migration (probably none — greenfield)
[3] ✅ Dashboard walkthrough rewrite — SHIPPED
[4] ⭐ Capture-landed animation + "log in to another" — SHIPPED
    (the sublime moment Ford emphasized — built it as the
    CaptureCeremony component described above)
[5] 🆕 DASHBOARD VISUAL REDESIGN — top priority next
    The dashboard "looks worse than the onboarding." Needs to be
    visually redesigned to match the onboarding aesthetic across
    every tab (Clients, Reports, Settings, etc.). Onboarding has
    the Solarpunk palette + Card/ScreenLayout aesthetic; dashboard
    feels comparatively plain. Match the warm cream/emerald/wood
    vibe end-to-end.
```

## Critical operational gotchas (memorize these)

1. **Stripe dual-mode**: `.stripe-keys-{test,live}.env` in
   `/home/fordface/solar-operator/` root (gitignored). "go live" /
   "back to test" = `railway variables --set` per line; retry 3-5x
   on graphql timeout.

2. **Extension zips** go to
   `/mnt/c/Users/fordg/Desktop/Solar Operator/Archives - Extension Builds/`
   (Win Desktop), NOT repo root or extension/archives/.

3. **Railway deploy gotcha**: after `git push`, `railway ssh "python -m
   api.migrate"` runs OLD code for ~30-60s. Always verify with
   `railway ssh "grep <new_col> /app/api/migrate.py"` BEFORE migrate,
   then SQL-verify column existence.

4. **FKs are NOT ON DELETE CASCADE.** Use
   `scripts/delete_tenant_by_email.py` to nuke Ford for fresh testing.
   He says "delete me" / "nuke me" — run that script.

5. **Git push silently blocked by Bash regex** sometimes — check
   `git log origin/main..HEAD`, push manually if remote didn't move.

6. **VEC adapter (Jun 2026)**: NISC SmartHub at
   vermontelectric.smarthub.coop (shared platform w/
   washingtonelectric, stoweelectric). Scrape DOM aria-labels on
   Highcharts SVG (server-side POST 403s). Bill PDF at
   `/services/secured/billPdfService/{YYYY_MM_DD}_{acct}.pdf`.
   VEC → /v1/sync via bills_raw + usage_raw fields.

7. **GMCS is sacred**: real GMCS report = Month/MWh/RECs only, NO $
   column. FOOTNOTE_TEXT in `gmcs_writer.py` is verbatim Bruce. Don't
   touch.

8. **Bruce excludes Pittsfield** (sub-REC array). `Array.excluded`
   field handles this.

9. **First prospect**: John Spencer at Crown Rec (~50 clients) — the
   50-account dream that motivates the auto-create + ceremony work.

## Ford's working style

- **High agency. "Just do it" / "do everything you can"** — decide
  defaults yourself, escalate only money/identity/branding decisions
  (like the Stripe webhook secret you saw earlier).
- Thinks in cosmological/vector frames. Feature requests are symptoms;
  he wants strategic synthesis. North star: "the ultimate agent."
- "Sublime" is his highest praise — kitchen-table warmth, not enterprise
  language.
- Sudo password: `Barcalona09!`
- Skip option-pads when the answer is obvious.
- Proper-fix over workaround.
- Data/infra decisions: state speed/cost/accuracy tradeoffs BEFORE
  executing.
- He says "stop" / "wait" if you're going wrong — respect immediately.
- For tiny UX asks (<3 files, <30 LOC) push back inline rather than
  dispatching agents — he prefers fast hand-fixes for small things.
- For multi-file features, schema changes, audit-style sweeps —
  delegate_task is welcome. Critic agents standard.

## Where to start

When Ford's next message comes in:
1. Greet briefly, acknowledge the model swap, confirm you're oriented
2. Likely topic: backlog item #5 (dashboard visual redesign) since
   that's the freshest item, OR he tests the webhook fix
3. The TODO list from this session is gone with the context, so if
   he asks "where were we" — read this file and the backlog above

## Quick links

- Repo: `/home/fordface/solar-operator/` (Garface111/solar-operator)
- GitHub: github.com/Garface111/solar-operator
- Production: solaroperator.org
- Railway: web-production-49c83.up.railway.app
- Extension v1.3.0 zip: at the Win Desktop archives folder above
- Bridge protocol spec: `extension/BRIDGE_PROTOCOL.md`
- Deferred billing design: `docs/DEFERRED_BILLING_DESIGN.md`
- Tenant nuke script: `scripts/delete_tenant_by_email.py`
  (call via `railway ssh "cd /app && python -m scripts.delete_tenant_by_email ford"`)

Good luck. The vibe is warm, the work is good. Keep shipping.
