# Onboarding Flow Rebuild — Implementation Plan

**Date:** 2026-06-03
**Goal:** Replace the existing signup flow (`api/signup.py` + ad-hoc legacy frontend) with a polished 5-screen wizard. Operator → pay → install extension → add clients with optional GMP auto-populate → done. Minimum data entry. GMP-only for now.

**For Hermes:** Dispatch each Task below to Claude Code via `claude -p` from `~/solar-operator/`. Use `subagent-driven-development` discipline — one task per dispatch, verify, commit, move on.

---

## Architecture Decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| Frontend stack | React 18 + Vite + TailwindCSS | Fast scaffold, Claude knows it cold, easy static deploy |
| Where it lives | `~/solar-operator/web/onboarding/` (new), built to `web/onboarding/dist/`, served by FastAPI `StaticFiles` at `/onboarding/*` | Single repo, single deploy, no Netlify changes |
| Routing | React Router, hash-based or BrowserRouter w/ FastAPI catch-all | Avoid Railway 404s on deep links |
| Visual style | Stripe/Linear minimalism — white, generous space, single accent (`#10b981` solar green or existing brand), Inter font, tight typography, no illustrations | Matches B2B trade buyer; signals trust |
| State | Local React state per screen + `sessionStorage` for partial wizard state | No backend until they pay; one Stripe Checkout call, then magic-link auth |
| Auth model | Single-tier (Tenant only, no Client login). Existing magic-link `/v1/auth/request` reused for post-onboarding sessions. | Confirmed: Clients are records, not users |
| Payment timing | Stripe Checkout BEFORE extension install. Setup $250 (one-time) + $45/array/mo subscription. No-card → no further steps. | Confirmed |
| Replacement scope | `api/signup.py` rewritten end-to-end. Old `/v1/signup` POST removed. New `/v1/onboarding/*` endpoints added. Legacy `/v1/checkout/{sid}` kept ONLY long enough to redirect old links to new flow. | Confirmed |
| GMP autopop | Extension `/v1/sync` payload already carries `user.email`, `user.username`, `user.fullName`, `accounts[].{accountNumber, nickname, customerNumber, currentBillUrl, currentBillUrlBinary, serviceAddress, solarNetMeter, groupNetMetered, isPrimary}`. We match `Client.gmp_email = user.email` and append/update Arrays + UtilityAccounts. | Uses existing pipeline; no extension changes needed for v1 |

---

## Data Model Changes

```python
# api/models.py — add to Client:
gmp_email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
gmp_autopopulate: Mapped[bool] = mapped_column(Boolean, default=False)
gmp_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

Migration in `api/migrate.py`: add `clients.gmp_email` (nullable VARCHAR(200)), `clients.gmp_autopopulate` (BOOLEAN default false), `clients.gmp_last_sync_at` (TIMESTAMP nullable). Index on `(tenant_id, gmp_email)`.

---

## API Surface (new)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/v1/onboarding/checkout` | `{email, full_name, company}` | `{checkout_url, onboarding_token}` |
| GET | `/v1/onboarding/status?token=` | — | `{stage, tenant_id?, activation_code?}` (`pending_payment|extension|clients|done`) |
| POST | `/v1/onboarding/extension-installed?token=` | — | `{ok}` (marks stage advanced) |
| POST | `/v1/onboarding/clients?token=` | `[{name, contact_email, gmp_email?, gmp_autopopulate, arrays:[{name, nepool_gis_id?, bill_offset_months?}]}, ...]` | `{client_ids}` |
| POST | `/v1/onboarding/complete?token=` | — | `{ok, magic_link_email_sent}` |
| GET | `/v1/onboarding/extension-ping?token=` | — | `{installed: bool, last_capture_at?}` (poll target during step 3) |

The `onboarding_token` is a 32-char random string stored on the pending Tenant row (`Tenant.onboarding_token` + `Tenant.onboarding_stage`). Expires 24h.

Existing Stripe webhook (`/v1/stripe/webhook`) already activates tenant on `checkout.session.completed`. Modify to also set `onboarding_stage = 'extension'` and NOT send the welcome email until `/v1/onboarding/complete` is hit.

---

## Screen Specs

### Screen 1 — Welcome & Agreement (`/onboarding/`)
- Hero: "Quarterly solar reports, on autopilot."
- Sub: 1-line value prop.
- Plan card: `$250 one-time setup · $45/array/month · cancel anytime`.
- Bulleted services: "Auto-pull GMP bills · NEPOOL-format Excel · Email delivery to your clients · Multi-client portal".
- Legalese accordion (collapsed by default): TOS + Privacy. Text from `store_assets/privacy_policy.md` + new TOS.md.
- Checkbox: "I agree to the Terms of Service and Privacy Policy".
- CTA: "Continue →" (disabled until checkbox).

### Screen 2 — Operator Info (`/onboarding/info`)
- 3 fields: Full name, Email, Company (optional).
- Inline validation. Email format.
- Back / Continue. Continue calls `POST /v1/onboarding/checkout` → 302 to Stripe Checkout URL.

### Screen 3 — Install Extension (`/onboarding/extension`)
- Detects `?onboarding_token=` in URL (return path from Stripe).
- Big CTA: "Install Solar Operator Sync from Chrome Web Store" (target=_blank).
- Below: "We're waiting…" with live status (polls `/v1/onboarding/extension-ping` every 3s).
- Auto-advance when ping returns `installed: true`.
- Manual "I've installed it →" fallback.

### Screen 4 — Clients (`/onboarding/clients`)
- Heading: "Add your reporting clients."
- Repeatable client card:
  - Name (required)
  - Contact email (required — where reports get sent)
  - Toggle: "Auto-populate arrays from GMP"
    - When ON: "GMP login email" field appears. Helper text: "When this client logs into GMP with this email through the extension, we'll auto-add their arrays."
    - When OFF: collapsible "Add arrays manually" with array name + NEPOOL-GIS ID (optional).
- "+ Add another client" button.
- "Finish setup →" → `POST /v1/onboarding/clients` then `/v1/onboarding/complete`.

### Screen 5 — Done (`/onboarding/done`)
- Confetti or subtle success state.
- "You're all set. Check your inbox for your login link."
- Summary: N clients, M arrays-so-far (could be 0 if autopop pending).
- Link to dashboard (magic-link-gated).

---

## Tasks

### Task 1 — Repo scaffold + Vite + Tailwind

**Objective:** Get the static SPA scaffold standing up in `web/onboarding/` with the design system primitives ready.

**Files:**
- Create: `web/onboarding/package.json`, `vite.config.ts`, `tsconfig.json`, `tailwind.config.js`, `postcss.config.js`, `index.html`
- Create: `web/onboarding/src/main.tsx`, `src/App.tsx`, `src/index.css`
- Create: `web/onboarding/src/ui/` with `Button.tsx`, `Card.tsx`, `Input.tsx`, `Checkbox.tsx`, `Toggle.tsx`, `Stepper.tsx`
- Modify: `.gitignore` to add `web/onboarding/node_modules`, `web/onboarding/dist`

**Design tokens (tailwind.config.js):**
- Font: `Inter`, system fallback.
- Primary: `#10b981` (solar green) with full Tailwind palette mapping.
- Neutrals: zinc scale.
- Radius: `rounded-xl` everywhere by default.
- Shadow: `shadow-sm` cards; `shadow-lg` on the active step.

**UI primitives behavior:**
- `Button`: variants `primary` (filled green), `secondary` (outlined), `ghost`. Disabled state opacity-50, cursor-not-allowed.
- `Card`: white bg, border zinc-200, rounded-xl, p-8.
- `Stepper`: numbered pill list 1–5 across top showing current step.

**Verify:** `cd web/onboarding && npm install && npm run dev` → http://localhost:5173 shows a blank App with the Stepper rendering 5 steps. Commit.

---

### Task 2 — Backend: data model + migration

**Files:**
- Modify: `api/models.py` (add `Client.gmp_email`, `gmp_autopopulate`, `gmp_last_sync_at`; add `Tenant.onboarding_token`, `onboarding_stage` if not present)
- Modify: `api/migrate.py` (idempotent ALTER TABLE for both)

**Verify:** `python -m api.migrate` runs clean on a local sqlite. Commit.

---

### Task 3 — Backend: onboarding router

**Files:**
- Create: `api/onboarding.py` with router + endpoints from the API table above
- Modify: `api/app.py` to `include_router(onboarding.router)` and REMOVE the old `signup.router` mount (delete `api/signup.py` AFTER the new flow is proved out — for now leave the file and just unmount)
- Modify: `api/signup.py` — webhook handler: on `checkout.session.completed`, look up tenant by `onboarding_token` (now passed as Stripe metadata), activate, set `onboarding_stage='extension'`, do NOT send welcome email here.

**Tests:** `tests/test_onboarding.py` — at minimum: checkout creates pending tenant, status returns `pending_payment`, after webhook fires status returns `extension`, complete moves to `done` and triggers magic link.

**Verify:** `pytest tests/test_onboarding.py -v` passes. Commit.

---

### Task 4 — Backend: extension sync upgrade for autopop

**Files:**
- Modify: `api/app.py` `/v1/sync` handler (lines 82+): after persisting the `UtilitySession`, look up any `Client` where `tenant_id == tenant_id AND gmp_autopopulate == true AND gmp_email == payload.user.email`. For each captured account, upsert an `Array` (one per `accountNumber` unless an existing array already has it linked) and a `UtilityAccount` row. Update `Client.gmp_last_sync_at`.
- Test: `tests/test_autopop.py` — synthesize a `/v1/sync` payload with 3 accounts, assert 3 arrays + 3 utility_accounts created, linked to the right Client.

**Pitfall (flag loudly in code comment):** Bruce's Starlake = 3 accounts → 1 array. Autopop creates 3 separate arrays. Operator must manually merge later via the dashboard. UI on Screen 4 should warn about this case if more than 1 client has autopop on.

**Verify:** `pytest tests/test_autopop.py -v` passes. Commit.

---

### Task 5 — FE: Screen 1 (Welcome & Agreement)

**File:** `web/onboarding/src/screens/Welcome.tsx`

Layout per spec above. TOS + Privacy text inline (placeholder lorem now; real content in Task 11). Checkbox state → enable Continue. Route push to `/onboarding/info`.

**Verify:** Manual — `npm run dev`, click through, button disables/enables correctly. Commit.

---

### Task 6 — FE: Screen 2 (Operator Info) + Stripe redirect

**File:** `web/onboarding/src/screens/Info.tsx`

3 fields, validation, on submit `fetch('/v1/onboarding/checkout', ...)` → `window.location = checkout_url`. Persist `onboarding_token` in `sessionStorage` before redirect (Stripe `success_url` will carry it back too).

**Verify:** Submitting redirects to a real Stripe Checkout in test mode. Commit.

---

### Task 7 — FE: Screen 3 (Extension install + polling)

**File:** `web/onboarding/src/screens/Extension.tsx`

Read `onboarding_token` from URL or sessionStorage. Show Chrome Web Store link (env var for the actual URL once published; placeholder for now). Poll `/v1/onboarding/extension-ping` every 3s. When `installed: true` → auto-route to `/onboarding/clients`.

**Server side:** `extension-ping` should return `installed: true` if a `UtilitySession` exists for this tenant in the last 24h. Plus a manual `extension-installed` POST endpoint as fallback.

**Verify:** Manual. Without the extension, polling shows "waiting"; after a fake `/v1/sync` call, status flips. Commit.

---

### Task 8 — FE: Screen 4 (Clients + autopop UI)

**File:** `web/onboarding/src/screens/Clients.tsx`

Repeatable client card component, toggle, conditional GMP-email field, conditional manual-arrays section. Local array state, "+ Add another client" appends a blank card. Submit posts the whole list to `/v1/onboarding/clients` then `/v1/onboarding/complete`. Navigate to `/onboarding/done`.

**Verify:** Manual flow — add 2 clients, one autopop one manual, finish, both persisted in DB correctly. Commit.

---

### Task 9 — FE: Screen 5 (Done) + magic-link integration

**File:** `web/onboarding/src/screens/Done.tsx`

Success state. Fetch `/v1/onboarding/status` to display client/array counts. Email-link CTA: "Open your inbox →".

Server: `/v1/onboarding/complete` sends a magic link via the existing `account.py` auth flow.

**Verify:** End-to-end manual run from welcome screen → inbox link works → dashboard opens. Commit.

---

### Task 10 — FastAPI static mount + production build

**Files:**
- Modify: `api/app.py` — `from fastapi.staticfiles import StaticFiles; app.mount("/onboarding", StaticFiles(directory="web/onboarding/dist", html=True), name="onboarding")`
- Modify: `railway.toml` or build hook — pre-deploy step `cd web/onboarding && npm ci && npm run build`
- Modify: `Procfile` if needed

**Verify:** Deployed Railway URL `/onboarding/` serves the SPA. Deep links work. Commit.

---

### Task 11 — Real legal copy + final design polish

**Files:**
- Create: `web/onboarding/public/tos.md`, `privacy.md` (port from `store_assets/privacy_policy.md` + new TOS draft)
- Polish pass: spacing audit, focus rings, transitions on buttons, loading states on async actions, error toasts for network failures, mobile responsive check

**Verify:** Run Lighthouse — aim for Accessibility ≥ 95, Performance ≥ 90 on the Welcome page. Commit.

---

### Task 12 — Cutover

- Remove `signup.router` mount + `api/signup.py` (move to `api/_legacy_signup.py`).
- Marketing site "Get Started" buttons → `https://api.solaroperator.org/onboarding/`.
- Smoke test live with a real test-mode Stripe card.
- Tag release `v1.1.0-onboarding`.

---

## Open Questions for Ford (non-blocking, ask before relevant task)

1. **Pricing math on Screen 1.** "$45/array/month" but operator doesn't know array count yet. Show "$45/array/month — first month prorated based on arrays added" or just "from $45/month"? — **Default: "from $45/array/month, billed monthly".**
2. **Stripe price model.** $45/array/mo as metered usage, or fixed quantity adjusted post-onboarding? — **Default: subscription with quantity = array count, updated via Stripe API after Screen 4.**
3. **What if extension never gets installed?** Time-out the onboarding session at 24h, send a "finish setup" email. **Default: yes, 24h timeout.**
4. **Refund on cancel during onboarding.** If they pay then bail on Screen 4 — auto-refund the setup fee or not? — **Default: NO auto-refund, but flag for manual review.**

---

## File map (final state after Task 12)

```
api/
  onboarding.py        ← NEW: wizard endpoints
  app.py               ← MODIFIED: mount /onboarding static, /v1/sync autopop hook
  models.py            ← MODIFIED: Client.gmp_email etc.
  migrate.py           ← MODIFIED: new columns
  _legacy_signup.py    ← RENAMED from signup.py, unmounted
web/onboarding/
  src/screens/{Welcome,Info,Extension,Clients,Done}.tsx
  src/ui/{Button,Card,Input,Checkbox,Toggle,Stepper}.tsx
  src/App.tsx, main.tsx, index.css
  public/{tos.md,privacy.md}
  dist/                ← build output, gitignored
  package.json, vite.config.ts, tailwind.config.js, etc.
tests/
  test_onboarding.py
  test_autopop.py
```
