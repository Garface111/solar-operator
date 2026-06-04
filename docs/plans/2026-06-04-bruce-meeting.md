# Bruce Meeting — Action items not yet shipped (2026-06-04)

Father-and-son meeting transcript distilled to code tasks. The handwritten-notes
pass already shipped (Pittsfield, spreadsheet import, kill bill-timing, etc.).
This plan tackles what the verbal conversation added beyond the notes.

## Hard rule: stay in the dashboard SPA + delivery code

ALL changes must live in:
- `web/app/src/` — dashboard SPA (post-signup)
- `api/delivery.py`, `api/notify.py`, `api/email_templates.py` — outbound email
- `api/account.py` only if a small field needs adding/exposing

DO NOT TOUCH:
- `web/onboarding/` — onboarding SPA (separate domain of work)
- `extension/`, `api/adapters/` — utility integration
- Stripe code, marketing site
- The GMCS writer (`api/writers/gmcs_writer.py`) — formatting is sacred

After ANY web/app/src edit, run `./build_app.sh` so `api/app_dist/` reflects
the new bundle.

## Tasks (priority order)

### 1. VERIFY (do not blindly re-fix): Pittsfield exclusion

Bruce said in the meeting: "We've got to pull that [Pittsfield] out of my
thing and put it somewhere else, because although it's interesting, it's
really a Pittsfield garage. We actually have a little solar right there.
But we can't sell the RECs on it, because you have to be a certain size
to sell the RECs."

Agent B's recent commit (`c4540ee`) "fixed" Pittsfield being missed by the
spreadsheet importer. **This may have re-introduced an array Bruce
intentionally excludes.**

What to do:
- Read `tests/test_pittsfield.py` and `api/ingest.py` recent changes
- Determine if the "fix" makes Pittsfield show up in Bruce's tenant
- If yes, add an explicit per-array "exclude from reports / hidden" flag
  on the Array model (boolean column `excluded`, default false; migrate)
- Expose a toggle on the dashboard ClientCard array list: "Hide from
  reports (e.g. below-REC-threshold arrays)"
- Hidden arrays still capture data but are NOT included in the GMCS writer
  output and NOT billed (subscription quantity excludes them)
- If Pittsfield ISN'T currently appearing in Bruce's reports, leave the
  importer fix alone, just expose the `excluded` flag for future use

### 2. Dashboard "Recent activity" copy

Bruce: "Why do I care that you scrape Waterford five times?... maybe you
could just say recent activity, and it can say just now it collected data
from all the X number of arrays that you've signed in."

Find wherever the dashboard shows "Waterford scraped 5 times" / per-array
scrape counters. Likely in `ClientCard.tsx`, `ArrayList.tsx`, or a
RecentActivity card.

Reframe to:
- "Just now: collected data from N arrays (Waterford, Tannery Brook, …)"
- Or "Last sync: 2 min ago — N arrays updated"

Aggregate, don't enumerate redundant entries. If the same array was scraped
5 times in the last hour, show it once with the latest timestamp.

### 3. Send-from email auto-fill from operator login

Bruce: "Your email is pre-filled. Oh, you should force it to pre-fill based
on my login already."

In whatever Reports / Send-from configuration UI exists (likely
`ReportsCard.tsx` or a Send modal), the "Send from" email field should
default to the operator's own login email (the email on their tenant
record). It's editable but pre-filled — not blank.

### 4. Sub-client emails: white-label as the operator, not solar operator

Bruce: "Solar Operator should be invisible to the client [meaning the
sub-client]. To the sub client for sure."

When the operator (e.g. Bruce / GMCS) sends a quarterly report TO a
sub-client (a solar array owner), the email should appear FROM the operator
(name + email), with `admin@solaroperator.org` only as the technical
sender (Reply-To: operator).

Status check first: commit `f0fee57` claimed to default Send-from to
operator's own email. Verify this still works post-merge. If it doesn't, fix
in `api/delivery.py` / `api/notify.py`. The "From:" header should be
`"<Operator Name>" <operator@example.com> via Solar Operator` or, if Resend
allows it cleanly, just `"<Operator Name>" <operator@example.com>` with
Reply-To set.

If Resend requires a verified sending domain (likely), we can't truly send
AS the operator — in that case use `"<Operator Name> via Solar Operator"
<admin@solaroperator.org>` with `Reply-To: operator@example.com` and a
prominent "Sent by <Operator> via Solar Operator" footer. Be honest with
Ford in the summary about which path you took.

### 5. ClientCard "expand" affordance

Bruce: "You got to make that more clear that expand."

The little arrow that expands a ClientCard to show arrays is too subtle.
Make it more obvious:
- Larger affordance, label "Show arrays" or "Expand"
- Or make the entire card header clickable to expand
- Hover/focus state should clearly indicate clickability

### 6. Clickable array name affordance

Bruce: "You raised, look at that, but that's not obvious that you can click
on that until you hover over it."

Wherever array names appear as clickable links in the dashboard, surface
the link affordance without requiring hover:
- Subtle underline or color change at rest
- Arrow icon `→` next to clickable items
- Don't rely on hover-only styling

### 7. Move preview button to the top

(Handwritten notes also flagged this; the verbal meeting confirmed.)

In whatever screen has a "Preview report" button buried below configuration,
move the button to the top of the card / form so the operator can preview
without scrolling. Likely `ReportsCard.tsx` or a Send-report form.

### 8. "Extension code for reference" reframe

Bruce: "Maybe just say extension code, like for reference."

Currently the dashboard shows the extension activation code with copy-button
prominence that implies the user might need to do something. Reframe as a
passive reference:
- Section heading: "Extension code (reference)"
- Smaller / collapsed by default with a "Show" toggle
- Helper text: "You already pasted this into the extension. Kept here in
  case you reinstall or set it up on another computer."

Likely lives in `ActivationCodeCard.tsx`.

### 9. (If time permits) Multiple email recipients per client

Bruce: "I've put in multiple email addresses here — let's say I wanted to
put you and Jamie and Steve and Caleb."

Verify the Client contact_email field accepts multiple addresses
(comma-separated or array). If not, change `Client.contact_email` schema
to support a list and update the AddClient modal + send logic.

If this requires a migration, do it (drop column + add JSON column, or
keep the original `contact_email` for primary and add `cc_emails` JSON).
Mention the migration step in the summary so Ford runs it on Railway.

### 10. Capture data freshness: per-client date stamp

Bruce on the report list: "But you could put a date stamp on that easily,
right?"

Wherever the dashboard shows "Latest report" / "Last bill captured", make
sure it's a real date stamp (e.g. "Captured 2026-05-26") not just "latest".
Agent B made a partial pass on this; verify it stuck everywhere and is
consistent.

## Deliverables

- Branch `agent/bruce-meeting` with the work
- Re-built `api/app_dist/` (run `./build_app.sh`)
- All commits pushed to origin
- 5-line summary: (1) tasks completed vs. deferred and why, (2)
  verification results, (3) any migration steps Ford must run, (4)
  deviations from the plan, (5) confidence 1-10

## Method

For each task: read the relevant file(s), identify the change, make it,
build if frontend, run tests if backend. Don't over-engineer. Bruce's
feedback is consistently about removing friction and making invisible
choices visible — favor small precise changes over rewrites.
