# Agent F — Dashboard walkthrough / first-visit tour

## Goal
First time an operator lands on the dashboard after signup, show a guided tour
that teaches them how to use auto-populate per client. Dismissible. Replayable
from a "Show tour again" link in the dashboard header / settings.

## Scope — ONLY these files
- New: `web/app/src/components/WalkthroughOverlay.tsx` (new component, or use a
  small existing pattern if one exists — check `web/app/src/ui/` first)
- New: `web/app/src/lib/walkthrough.ts` (state helpers, localStorage keys)
- Edit: `web/app/src/screens/DashboardLayout.tsx` or wherever the main shell
  lives — to mount the overlay and add the replay link
- Edit: ONE existing card if a step needs to anchor to it (e.g. ClientCard for
  the "click here to expand" step) — minimal changes only

## Do NOT touch
- `web/onboarding/` — separate funnel
- `extension/` — separate
- `api/` — backend changes not needed for this
- Stripe code
- Other agents are running on:
  - `api/account.py`, `api/app.py`, `api/models.py`, `api/migrate.py` — VEC autopop
  - `web/app/src/lib/api.ts` and most cards — auto-refresh hooks
  KEEP CHANGES TO ANCHORED FILES MINIMAL (1-3 lines per file) and only for
  attaching a stable `data-tour-step="N"` attribute so the overlay can target
  it. Do NOT rewrite logic in those files.

## The tour steps (operator's first visit)

1. **Welcome.** "Quick 60-second tour — we'll show you the fastest path to
   working reports."
2. **Click a client to expand.** Anchor: the ClientCard header chevron. Copy:
   "Click any client to expand it and see their arrays."
3. **Enter the utility login for that client.** Anchor: the `gmp_email` /
   `vec_email` input on the expanded client (if VEC fields don't exist yet
   in the UI, just anchor to the GMP one — VEC autopop is a parallel agent's
   work). Copy: "Paste the email or username this client uses to log into
   their utility portal."
4. **Toggle auto-populate ON.** Anchor: the auto-populate checkbox. Copy:
   "Turn this on so we automatically pull this client's arrays the next time
   you log into their utility portal — no manual array entry."
5. **Go log in.** Copy: "Now open Green Mountain Power (or VEC) and sign in
   with that account. We'll capture the arrays in the background and they'll
   appear here automatically." [No anchor — close out, big CTA: "Open GMP" /
   "Open VEC".]
6. **You're done.** "When you come back, your client will have all their
   arrays auto-populated. Pricing reconciles automatically."

## Constraints

- Use plain CSS / inline styles. Do NOT add a tour library (no react-joyride,
  no shepherd.js). Bruce wants minimal surface area; a 150-LOC component is
  fine, a 30KB dependency is not.
- localStorage key: `so_walkthrough_v1_seen`. Set on dismiss or completion.
- Replay link: small "Show walkthrough again" text-link in the dashboard
  header, opens the overlay regardless of localStorage.
- Skippable: each step has a Skip button and a Close (X). Close = same as
  dismiss = sets the seen flag.
- Mobile: just hide the overlay below 768px. The dashboard is desktop-first.
- Anchoring: use `data-tour-step="N"` on target elements, then in the overlay
  compute the bounding rect and draw a dimmed mask with a hole. Standard
  spotlight pattern.

## Verification
- Onboard a fresh tenant (or just clear the localStorage key in your browser
  console), reload `/app/`, walk through all 6 steps, confirm dismiss persists
  across reloads.
- Click "Show walkthrough again" — confirm it reappears.
- Run `./build_app.sh` so `api/app_dist/` reflects the new bundle.

## Deliverable
- Branch `agent/dashboard-walkthrough`
- 5-line summary: (1) files touched, (2) build clean? (3) any cards where the
  anchor wasn't obvious, (4) anything Ford should know before merge, (5)
  confidence 1-10
