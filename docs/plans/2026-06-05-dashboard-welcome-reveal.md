# Dashboard Welcome Reveal (queued — do after dashboard-redesign merges)

## Why
Existing CaptureCeremony only fires on `?fresh=1` (post-onboarding handoff).
On normal login the dashboard just appears, flat. Ford never sees it.

The product vector is "sublime moment every time you arrive." Every login
should feel like the system unfurling itself FOR you — not a static table.

## What
After the dashboard-redesign branch merges (giving us the new
Card/ScreenLayout/tokens), build a "welcome back" reveal that runs on
EVERY dashboard load (not just `?fresh=1`):

- Brief warm greeting using operator name ("Welcome back, Ford.")
- Cascade in client + array chips with stagger animation (reuse
  `.so-cascade-row` / `.so-cascade-chip` from index.css)
- Summary line: "X clients, Y arrays, last captured Z ago"
- Total reveal time ≤1.2s; never gates interaction (clicking through
  skips the animation, doesn't queue)
- Throttle: only animate if last reveal was >2h ago OR fresh data
  arrived since last visit. Otherwise just fade-in. Don't make it
  annoying.

## Notes
- Build on top of the new tokens from `agent/dashboard-redesign` —
  do NOT start before that branch is merged or you'll fight tokens.
- The existing CaptureCeremony in `web/app/src/components/` keeps its
  current job (post-onboarding cascade). This new component is the
  every-login version. Probably called `DashboardReveal` or
  `WelcomeBackReveal`.
- Hook into wherever the Clients tab first mounts.

## Status
QUEUED. Do not dispatch yet — waits for dashboard-redesign merge.
