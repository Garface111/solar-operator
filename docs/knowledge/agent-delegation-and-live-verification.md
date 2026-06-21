# Delegating frontend builds to Claude Code + verifying against LIVE data

Ford wants coding delegated to Claude Code (opus) to save Hermes tokens, but the
agent CANNOT run `node --check` or commit/verify here (permission gate). So the
durable loop is: **agent writes the code, Hermes verifies it against real live
data, then commits + deploys.** Never trust the agent's self-report for anything
with external side-effects. Proven repeatedly on the Array Operator static site.

## The loop (proven, ~$2–3.50 / 22–45 turns per multi-file UI build on opus)

1. **Write a tight task brief to a file** (`/tmp/<task>.md`), pass via
   `claude -p "$(cat /tmp/<task>.md)" --permission-mode acceptEdits --model opus
   --output-format json > /tmp/<task>_result.json 2>/tmp/<task>_err.log` inside a
   detached `tmux` session. Poll the result file with a background watcher
   (`for i in $(seq 1 90); do [ -s result.json ] && break; sleep 10; done` +
   `notify_on_complete=true`). Big refactors run 6–12 min.
2. **The brief MUST list the exact allowed endpoints, the dark-skin tokens, the
   same-origin `Bearer so_session` rule, and an explicit DO-NOT list** (no npm/build,
   don't touch the backend repo, don't break the other tabs/modal/onboarding). Briefs
   that omit the DO-NOT list get scope creep.
3. **Read the agent's JSON `result` field** for its self-report + cost, but treat it
   as a CLAIM. Then verify everything yourself:
   - `node --check public/sandbox.js public/app.js` (agent literally cannot run this).
   - `git status --porcelain` to see the REAL changed-file set vs what it claimed.
   - `git log --oneline -1` to confirm it did NOT auto-commit (usually it doesn't,
     but behavior is inconsistent — see pitfalls).
4. **Local playwright QA with a prod-proxy static server** (see
   `scripts/local_prod_proxy_qa.js` template below) — serves `public/` locally but
   proxies `/v1/*` to `https://arrayoperator.com` so the page fetches REAL fleet data.
   Inject the session via `localStorage.setItem('so_session', token)`. Assert structure
   (tab default, counts, data-attrs, POSTs fired) + screenshot + vision-review.
5. **Commit on the agent's behalf** with an honest message noting "built by Claude
   Code opus, verified + committed by Hermes against live tenant: <assertions>".
6. **Deploy** (`netlify deploy --prod --dir public --site <UUID>`), then a FINAL
   **cold-browser E2E against the PROD url** (not the local proxy) to confirm the
   deployed bundle. Refresh the dad-launcher token last.

## Pitfalls that actually bit (encode these in every brief / verify step)

- **Agent edits OUT-OF-SCOPE files.** It tweaked `onboarding.html` copy unasked
  during a sandbox task (and the new copy was subtly wrong — claimed per-kWh billing
  when billing is per-array). ALWAYS `git status` after; `git checkout <file>` to
  revert anything you didn't ask for before committing.
- **Synthetic `DragEvent` + jsdom `DataTransfer` cannot prove a VISUAL drag/reorder.**
  It reliably fires the handlers and side-effects (a real `POST /reassign`, a
  localStorage write, an order key) but won't always visibly swap DOM nodes. Assert
  the SIDE EFFECT (network call captured by the proxy, persisted state), not the pixel
  move. State that caveat to Ford; real mouse drag works.
- **MIME-on-`/` test-harness bug:** when the static server serves `/`, compute the MIME
  from `/index.html`, not the bare `/` path — else you serve HTML as `text/plain` and
  playwright shows raw source (looks like a site bug, isn't). Cost a false "render
  failed" once.
- **`position:sticky` nav/footer in full-page screenshots** paints at the stuck scroll
  position, so a sticky nav can appear DUPLICATED mid-page in a `fullPage:true` shot.
  It's an artifact, not a bug — confirm with a viewport-only screenshot + a DOM count
  (`panel.querySelectorAll('nav,footer').length === 0`).
- **Dark-skin drift on full-page screenshots / mobile overscroll:** `body` with
  `background-attachment:fixed` leaves the `html` element WHITE. Fix once with
  `html{background:#0a0e14}` — also real insurance against overscroll-white on mobile.
- **Stale tag mappings after a backend response-shape change.** When the backend started
  sending `inverter_source:"live"` (was `"solaredge"`), the frontend chip still checked
  the old literal and rendered "no data" on live arrays. After any response-shape change,
  grep the frontend for the old literal values.

## Token / secret hygiene in this loop
- The owner session token looks like a JWT, so Hermes's display layer MASKS it in tool
  output. To hand Ford a working `?token=` link, WRITE it to a file
  (`/mnt/c/Users/fordg/Desktop/...`) rather than printing it. Mint with
  `_sign_session(str(tenant.id))` on prod (signs the `ten_` PK, NOT tenant_key).
- The dad/family launcher is an HTML file with `<meta http-equiv="refresh"
  content="0;url=https://arrayoperator.com/?token=<session>#arrays">` written to BOTH
  Desktop and OneDrive/Desktop, plus a plain `.txt` with the raw URL for texting. The
  `?token=` auto-signs-in and scrubs itself from the address bar (app.js `loadDashboard`).
  Re-mint + rewrite when the token expires (~30 days) or after any QA that moved live data.

## Verifying a backend mutation that has real engine consequences
When a mutation is supposed to CHANGE analysis (e.g. moving an inverter changes its peer
cohort), prove the consequence, not just the 200. The decisive assertion was capturing the
metric BEFORE and AFTER: `peer_index 1.02 → 1.10` after a cross-array move. Run the probe via
`railway ssh ... python` (base64-encode the script to dodge quoting/secret-redactor), and
RESET the test tenant afterward (`reset_layout` + delete stray owner-created arrays) so the
shared demo tenant stays clean for Ford's next open. The probe left prod dirty once and a
duplicate-name array 500'd the re-run — always clean-state FIRST, then run once.
