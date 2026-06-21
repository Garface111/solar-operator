# Repo topology — TWO frontends in TWO repos (read this before reading UI code)

The single biggest orientation trap in this project: assuming all UI lives in
`/root/solar-operator/web/`. It does not. There are two separate products with
two separate frontends in two separate git repos.

## The two frontends

| Repo | Product | Frontend | Deploy | What lives here |
|------|---------|----------|--------|-----------------|
| `/root/solar-operator` | **NEPOOL Operator** | `web/app` (React) + `web/onboarding` (React) | Railway serves `api/app_dist`; marketing on Netlify | Utility-bill dashboard: GMP/SmartHub capture, NEPOOL reports, clients/arrays table. Its array UI (`ArrayList.tsx`) is **SolarEdge-API-key-only** and intentionally has NO "Log in with <inverter>" buttons. |
| `/root/array-operator` | **Array Operator** (owner site) | `public/*.html` + `public/*.js` — **plain HTML/JS, NOT React** | **Netlify** (`array-operator-ea.netlify.app`, arrayoperator.com) | The owner canvas, "Add array" modal, and the live **"Log in with SolarEdge / Fronius / SMA / Chint"** one-click vendor buttons. Key files: `public/onboarding.html`, `public/sandbox.js` (Add-array modal), `public/app.js`. github.com/Garface111/array-operator |

Related sibling repos on the machine (not the AO owner site): `array-operator-pcc`,
`energyagent-site`, `solaroperator-site` (marketing), `interface`/`mindspace`
(Mindspace 3D canvas), `master-control`.

## How to tell which repo serves a given surface

- `api/app.py` CORS/origin list names the AO owner site as
  `array-operator-ea.netlify.app` / `arrayoperator.com` → that surface is the
  **Netlify** deploy from `/root/array-operator`, NOT solar-operator's bundle.
- solar-operator `web/app` routes `/arrays` and `/sandbox` → redirect to
  `/clients` (commit "Remove Arrays tab from dashboard nav"). `ArrayOverview.tsx`
  still sits in the tree but is **orphaned** (not imported/routed/built) — a
  deliberately-disconnected leftover, not damage. Don't mistake it for the live UI.

## The lesson (what cost three messages)

Searching only `/root/solar-operator` for the inverter "Log in with X" UI found
nothing → I wrongly reported "GAP: no UI entry point for any vendor" and
"an agent mangled your work." Both false. The buttons (Fronius, SMA, AND Chint)
are all live in `/root/array-operator/public/`. Ford even has a commit
"feat: Chint/CPS as a one-click-login vendor (onboarding + Add-array)".

Before concluding a feature/UI is "missing," "regressed," or "mangled":
1. `git log --oneline` the OTHER repo (`/root/array-operator`) — the work is
   often there, authored by Ford, intact.
2. Check `git status` / reflog / all branches in the suspected repo: clean tree +
   no force-push + Ford-authored commits = nothing was mangled.
3. Trust-check your own "an agent destroyed it" conclusion HARD — Ford finds that
   alarming. Verify across repos before raising it.

## Anonymous / signed-out arrayoperator.com behavior

Root `/` (public/index.html) is the Command Center DASHBOARD, not a login wall —
it never gates on auth or hard-redirects. `renderFromSession()` (public/app.js)
reads localStorage `so_session`; with no token it treats you as an anonymous
marketing visitor and renders the full dashboard in DEMO mode from the static
`public/inverter-truth.json` (fake fleet/KPIs so the product still tells its
story). A `#tabSignIn` "Sign in →" link (→ `/login`) un-hides for signed-out
visitors; the identity chip stays hidden. Same demo fallback fires on a stale
session or failed `/v1/array-owners/overview` fetch — design intent is "the
narrative still lands, never a blank page."

Entry points (Netlify `_redirects`): `/onboarding` (+ `/signup`, `/get-started`)
→ onboarding.html wizard (account create + connect inverter, the vendor login
buttons); `/login` (+ `/signin`) → login.html (email+password or magic-link
`?token=`). `/v1/*` and `/accounts` status-200 proxy to the Railway backend so
the whole owner flow is same-origin under arrayoperator.com.

FUNNEL FIX (shipped, confirmed): the dashboard root `/` used to show anonymous
visitors only a small "Sign in →" link — no signup CTA — a real funnel leak.
Fixed with a full-width DEMO ONBOARDING BANNER above the tab bar: gold "LIVE
DEMO" badge + "Make it yours in 60 seconds" copy + a big gold-gradient
"Make your own account →" button (→ `/onboarding`) and a quiet "Sign in" link.
Implementation pattern (Ford wants onboarding "way slicker"): banner markup +
scoped CSS in `public/index.html`'s inline `<style>` (uses the `--gold/--gold2`
theme vars); one line in `renderFromSession()` (public/app.js) toggles it by
session — `db.hidden = !!session` right beside the existing `tabSignIn` toggle,
so it shows ONLY to anonymous visitors and auto-hides the instant a session
exists. Keep the demo dashboard visible behind it (product sells itself) and
make the signup CTA the single most prominent element on the page.

## End-to-end inverter-capture chain (spans both repos + extension)

array-operator UI ("Log in with <vendor>") → extension arms `so_capture_intent`
(background.js, keyed by portal host) → vendor `*_content.js` scrapes the
logged-in portal → background.js → `SO_CAPTURE_LANDED {provider, sites}` → AO
page (onboarding.html / sandbox.js) POSTs to
`/v1/array-owners/inverter-capture` (in solar-operator's api/) → persisted.
Backend allowlist: `_CAPTURE_VENDORS = {"fronius","chint","sma"}` in
api/array_owners.py. Dual-auth via `_tenant_from_bearer` (session token first,
tenant key fallback).
