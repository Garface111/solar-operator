# Dashboard Welcome-Back Reveal

## Goal
On EVERY dashboard load (not just `?fresh=1`), unfurl the operator's
world for them. Warm greeting → cascade their clients & arrays in →
land on a fully-populated dashboard. Sublime moment, every visit.

## Scope (own ONLY these)
- NEW: `web/app/src/components/WelcomeReveal.tsx`
- NEW: anything under `web/app/src/components/welcome/` if you need helpers
- EDIT: `web/app/src/components/ClientsSection.tsx` (mount the reveal)
- EDIT: `web/app/src/index.css` (animations only, use existing tokens)

## DO NOT TOUCH
- `web/app/src/components/TopNav.tsx` (deferred-billing is putting a
  banner here in parallel)
- `web/app/src/screens/SettingsTab.tsx` (deferred-billing owns it)
- `web/app/src/screens/ReportsTab.tsx` (reports-polish agent owns it)
- `api/`, `extension/`, `web/onboarding/`
- `web/app/src/ui/` (use the existing primitives as-is)

## Visual language
Use the tokens already in `web/app/src/styles/tokens.css` (cream,
emerald, wood). The redesign just merged — read it first.

## Behavior
1. On Clients tab mount, fetch clients+arrays (existing API call).
2. Show "Welcome back, {operator_name}." in serif heading. 700ms hold.
3. Cascade client cards in with 80ms stagger (reuse `.so-cascade-row`
   if defined in index.css, otherwise add similar).
4. After last card lands, show one-line footer:
   "{N} clients · {M} arrays · last update {Z} ago" — small, warm.
5. Total time ≤ 1.4s. Clicking anywhere skips remaining animation
   and jumps to final state.
6. Throttle: skip the reveal entirely if `localStorage.so_last_reveal`
   was set within the last 2 hours. Just fade-in normally.
   Always run if `?fresh=1` (post-onboarding) regardless of throttle.

## Tasks
1. Read tokens.css, ClientsSection.tsx, CaptureCeremony.tsx (similar
   pattern — borrow from it but don't break it).
2. Build WelcomeReveal.tsx.
3. Wire it into ClientsSection.tsx as the wrapper around the existing
   client list. Existing list logic untouched.
4. Throttle logic with localStorage.
5. Run `npm --prefix web/app run build` clean.
6. Commit per task ('reveal: <what>'). Do NOT push.
7. 5-line summary.

## Constraints
- No new npm deps.
- TS strict, no `any`.
- Skip-click must always work — never trap user in animation.
