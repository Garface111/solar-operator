# Array Operator static-site build → verify → deploy loop (proven 4× Jun 2026)

The repeatable loop for shipping a feature to the `/root/array-operator` static site
(plain HTML/CSS/vanilla-JS, NO build step, Netlify site `array-operator-ea` UUID
`966cb1f5-944e-41fd-855b-10053edc5d18`). Ford wants frontend work DELEGATED to Claude Code
(opus) to save Hermes tokens, but the agent CANNOT reliably commit or run verification
(permission gate) — so Hermes owns verify + deploy. Bruce's family is live-testing, so an
unverified self-report is never enough.

## Sandbox restructure SHIPPED (Jun 2026, commit cae07b0)
UX follow-ups SHIPPED on top (commit a785ee2): (1) the WHOLE array card expands on click —
`wireInvToggle` binds a click on `.sb-array` calling `toggleArray(col,host)`, excluding
`a, .sb-drag, [contenteditable='true'], input, textarea` so origin links / rename / drag keep
their behaviour. (2) `#sbOrient` toolbar toggle flips `localStorage.ao_sandbox_orient` between
"vertical" (default: arrays side-by-side, inverters below) and "horizontal" (arrays stacked
LEFT, inverters spread RIGHT) — render() adds `sb-orient-<o>` to `.sb-canvas`, CSS
`.sb-orient-horizontal .sb-col{flex-direction:row}`, `drawFleetConnectors` skips wires in
horizontal. getOrient/setOrient by getExpandedSet. Verified via local proxy QA + vision.
Further follow-ups SHIPPED (commit 67f2d58, from a handwritten Ford note photo in Downloads):
(a) command-center KPI strip trimmed to Sites·Inverters·Healthy·Flagged now — REMOVED the
'At risk / mo' + 'Recovered YTD' tiles (command-center.js render + paintKpis + the live-guard
that referenced [data-kpi="risk"]); (b) sandbox now DEFAULTS to horizontal (getOrient returns
horizontal unless localStorage ao_sandbox_orient==="vertical"); (c) faster perceived startup —
'Loading your fleet tree…' banner replaced with a light .sb-skeleton shimmer; (d) the floating
inverter detail pop-up card is MOVABLE via makeDetailDraggable(host,card): pointer-drag the
.sb-dc-head, repins #sbDetail from right/bottom to left/top, clamped inside #sbWrap.
NOTE: Ford communicates change lists via PHOTOS of handwritten notes dropped in
/mnt/c/Users/fordg/Downloads — "check latest download" = newest file there (vision-read it).
READ THE SKETCH FOR INTENT, NOT LITERAL WIDGETS — confirm the interaction model BEFORE
building (cost a full rebuild Jun 2026). His sketch of an inverter card showed a chart-like
shape labeled with min/current/max; I built a HISTORY GRAPH (sparkline of daily kWh), but he
actually wanted a LIVE %-OF-MAX BAR (current ÷ rated nameplate, card tinting orange as it
drifts below max). A hand-drawn "graph" can mean a live gauge, a bar, or a trend — they look
similar on paper. When a sketch maps to >1 plausible interaction (live gauge vs historical
trend vs static stat), ASK one tight clarifying question (offer the 2-3 concrete readings) or
state the assumption loudly before committing the build. Bonus: when he later said "the
history is smart, have BOTH," the fix was additive (keep the graph, ADD the bar) — so a
wrong-guess build isn't always thrown away, but confirming up front would have saved the
round-trip. Also: a "live bar" is current vs a MAX REFERENCE — use rated nameplate kW as the
ceiling (the honest "what % of capacity right now"), and make night/offline an "idle" neutral
state, never a false 0%-of-max alarm.
Inverter-CARD redesign SHIPPED (frontend 5173f6f + backend 7ebafe5, from a Ford sketch):
each inverter card = name + nameplate kW size, an SVG output graph (invSpark() area-line of
the daily kWh series; red dots on zero-output days), a Min/Current/Max stat row (Min/Max =
REAL lowest/highest DAILY kWh in the window, Current = live kW), and a status-coloured alert
line. Inverters sorted by nameplate kW DESC within each array. Backend: build_fleet_tree
inv_rows now emit daily[{date,kwh}] + min_kwh + peak_kwh (derived from the SolarEdge equipment
telemetry / InverterDaily already pulled for peer analysis) — REAL data, never fabricated;
test_inverter_daily_series_and_min_peak. fleet-store passes daily/min_kwh/peak_kwh through
adaptTree + toColumns, and simulateFleet seeds a synthetic 14-day series for the anon demo
(fleet-tree-demo.json is ORPHANED/unused — don't bother editing it). ROLLOUT ORDER MATTERS:
deploy backend (Railway) BEFORE frontend (Netlify) or real owners see "no history yet"/"—"
until the fields exist. Verified live on Bruce's owner tenant (real Min/Max kWh, Current "—"
off-peak which is correct, 0 JS errors).
CARD ITERATION (commit 9902da2): Ford clarified he wanted a LIVE %-OF-MAX BAR, not the
Min/Current/Max text — current power ÷ rated nameplate, bar fills to that %, and the WHOLE
inverter card tints progressively orange the further live output drifts BELOW max (pctTone:
>=80 ok/green, >=55 warn/amber, else bad/orange; "idle" when not reporting so night/offline
isn't a false alarm). KEEP the history graph above it ("history is smart, have both").
outputBar() builds it; startLiveTicker drives bar fill + % + card data-tone in place; the old
.sb-inv-now/.sb-now-val card element is GONE so _liveKW/_invNowKW/detail-card live-kW were
rewired to read the bar's .sb-ob-cur / data-curw.
DEPLOY-CLEAN UNDER A CONCURRENT AGENT (important — happened repeatedly): when Ford's other
agent has UNCOMMITTED WIP in the array-operator working tree (e.g. a deleteArray in
fleet-store.js + a .sb-ctxmenu block in styles.css), `git add` ONLY your own files, commit,
then DEPLOY FROM A CLEAN WORKTREE of your commit (`git worktree add /tmp/ao-deploy HEAD;
netlify deploy --dir /tmp/ao-deploy/public`) — NOT `--dir public`, which would ship their
uncommitted WIP live. Verify the deploy tree has your feature but NOT their markers, curl the
live bundle after, then remove the worktree.
ARRAY-CARD AT-A-GLANCE + SPACING (commit 50d83f1): array card carries an "Inverters at a
glance" strip — arrayGlance() builds an auto-wrapping grid of one miniSpark() per inverter
(46x18 SVG, no axis), each cell tinted by live output-vs-max pctTone (green/amber/orange/idle,
red dots on zero days) so the whole array's health reads in one look without expanding. Array
spacing bumped in BOTH views: .sb-canvas gap 108->160px (vertical/side-by-side), .sb-canvas
.sb-orient-horizontal gap 34->64px (stacked). ISOLATING YOUR PUSH FROM A CONCURRENT AGENT'S
COMMITTED-BUT-UNPUSHED WORK: when the other agent has COMMITTED locally (HEAD ahead of origin
with their commit) and you must not push it, capture your own uncommitted diff to a patch
(`git diff files > /tmp/mine.patch`), `git worktree add /tmp/ao-mine origin/main`, `git apply`
the patch there, commit, `git push origin HEAD:main`, and deploy `--dir /tmp/ao-mine/public`.
That lands ONLY your commit on origin; their commit stays local for them to ship. Verify the
live bundle has your markers and NOT theirs (curl).
PER-INVERTER GRAPH DATA / API-INDEPENDENT HISTORY STORE (commit 378e738, backend): the
per-inverter graphs were vanishing because they read straight from the LIVE vendor API.
Bruce's 62-inverter fleet = Fronius 31, SMA 14, SolarEdge 13, Chint 4. SolarEdge was
LIVE-API-ONLY (0 persisted history) so its 13 graphs disappeared whenever the SE API was
slow/down/off-peak. Fix in api/inverter_fleet.py: storage (InverterDaily table, keyed
(inverter_id,day)) is now the SOURCE OF TRUTH. _persist_daily_series() snapshots WHATEVER
daily readings build_fleet_tree sees (any vendor) on read (persist-on-read), keeping the
LARGER kWh on re-read (a day only climbs). _merged_daily() = stored history + fresh live
merged on top → graph never vanishes. The commit at the end of build_fleet_tree is
try/except-guarded so a storage hiccup never breaks the tree. NEW scheduled job
api/jobs/inverter_history_snapshot.py (snapshot_all_inverter_history) registered in
scheduler.py at 03:30 UTC (after 03:00 inverter_daily_pull) forces capture for every active
array_operator tenant so history grows with zero dashboard traffic. A graph needs >=2 stored
days to draw; single-day inverters (Chint, fresh captures) fill in after the next snapshot.
After deploy SolarEdge went 0->13/13 stored, 57/62 inverters render a graph (remaining 5 =
4 Chint + 1 SMA with <2 days). Audit Bruce's fleet via railway ssh running build_fleet_tree
against TENANT ten_544fd6541eb8405b and counting InverterDaily rows per inverter.
LIVE PRODUCTION BAR EMPTY FOR NON-SOLAREDGE (extension fix v1.9.29, commit 6eea769): the
per-inverter production bar = live current_power_w ÷ nameplate. Only SolarEdge filled it
(polled live every dashboard load); Fronius/SMA/Chint (49 of Bruce's 62) were null because
their live-power capture in the EXTENSION had each broken/drifted. Backend was already ready:
array_owners.py inverter_capture accepts CaptureSite.current_power_w and SPLITS it across
inverters by energy share into iv.last_power_w — so a reliable SITE-LEVEL current_power_w
(watts) is all the extension must send. Per-vendor fixes (each grounded on a fresh daytime
HAR Ford dropped on OneDrive Desktop): FRONIUS solarweb_content.js — GetActualValues works,
call it withOnlineState=False (not True) and stop the silent catch-swallow; TotalPower(W).
SMA sunnyportal_content.js — the /overview/<plant>/devices pvPower field DRIFTED to null;
added site live-power fetch: PRIMARY GET /api/v1/widgets/gauge/power?componentId=<plant>&type=PvProduction
-> {value: W} (the portal's own gauge, single clean number), FALLBACK POST /measurements/search
channel Measurement.GridMs.TotW.Pv last finite sample; killed the `liveW||null` bug.
CHINT chint_content.js — per-inverter numeric currentPower gone; read site busTypeDevices
.data.currentPowerWithUnit ("72.7 KW") and parse to watts. CAVEAT: extension power is a
SNAPSHOT at manual-capture time (unlike SolarEdge live), so it's only as fresh as the last
capture — a "live as of Xh ago" stamp or auto-recapture is the honest follow-up. HAR TRAP hit
again: Ford's "SMA dashboard" HAR did NOT contain the /overview/devices call the extension
depends on (only dashboard widgets) — could not verify the inverter-identity path from it, only
the live-power path; flagged. Build: bump extension/manifest.json version, run
scripts/build_extension_zip.sh (auto-copies energyagent-extension-vX.Y.Z.zip to BOTH Desktop
roots). Delegated the 3 vendor scripts to parallel subagents, then VERIFIED each parse myself
by replaying the real HAR payload in execute_code (Fronius 99.9+44.4kW, SMA 40.7kW via gauge,
Chint 72.7kW via gauge) — don't trust subagent self-reports on parse correctness.
ZERO-vs-NULL LIVE POWER (sandbox card tint, fixed Jun2026): SolarEdge reports
current_power_w: 0 (a literal zero, NOT null) in the evening when panels are idle. Any UI
that derives a tone/alarm from "live output % of nameplate" will FALSE-ALARM every healthy
inverter every night (0/maxW = 0% → orange "underperforming"). Fix pattern: treat a
near-zero reading (<=1% of rated / 25W) as IDLE (calm, "not producing right now"), and only
let output drive an orange/red tint when the inverter's HEALTH status is already flagged.
Apply the SAME health-aware logic to any live ticker that re-paints tone on an interval, or
it re-introduces the bug on re-render. The peer-based health status (not instantaneous
output) is the real underperformance signal.
SILENT AUTO-RECAPTURE (extension v1.9.30, commit 3e7bfcc, background.js): extension-vendor
live power is a SNAPSHOT at manual-capture time, so bars froze. Made it hands-off: a
chrome.alarms timer (id "inverter-recapture", every 180min, persists across SW sleeps) opens
each OWNED vendor portal (fronius/sma/chint) in a BACKGROUND inactive tab, arms the existing
so_capture_intent (10-min TTL, self-expires), and the existing content-script poll rides the
owner's logged-in session + grabs fresh power exactly like the manual flow. KEY ENABLER: the
/v1/array-owners/inverter-capture endpoint is DUAL-AUTH (session token OR tenant key), so the
background worker POSTs the captured sites DIRECTLY with the stored tenant key — no open
dashboard page needed (the old flow required an AO page to POST). On *_CAPTURED the new hook
POSTs + auto-closes the tab; 90s watchdog closes a hung tab. Only refreshes vendors that have
captured before (so_recap_last map) — never opens portals the owner lacks. Graceful degrade:
expired portal session => ONE "quick reconnect" notification per vendor per DAY (so_recap_nudges),
never nagging. Also kicks one cycle ~8s post install/update. The new code is an IIFE appended to
background.js (NOT a separate file — MV3 single service worker; separate files need importScripts
and the global STORAGE_KEYS/PROD_ENDPOINT must already exist). Manual page-driven capture flow
untouched (new POST only fires when so_recap_state.running). Content scripts DO run in background/
inactive tabs in MV3. Bump manifest + run scripts/build_extension_zip.sh (auto-copies to both
Desktop roots). VERIFY: unzip -p the built zip's background.js and node --check it.
LIVE-FEEL LAYER (frontend 1ff1df7 + extension v1.9.31 c832e25): Ford asked "refresh every
second?" — DON'T poll portals that fast (inverters report every 5-15min; 1/sec would
rate-limit/ban the vendor portals + melt battery). Correct pattern = decouple VISUAL liveness
from DATA refresh: (a) real data refreshes hourly (extension auto-recapture RECAP_PERIOD_MIN
60); (b) the production bar ANIMATES every 1s client-side — startLiveTicker setInterval 1000ms
breathes each .sb-outbar's current around its last REAL data-curw reading with a smooth
continuous sine (phase = Date.now()/9000 + base%997, ±1.2%, NO per-tick Math.random jitter so
1s glides not twitches), paired with CSS .sb-ob-fill transition:width 1s linear so the fill
glides. Heavier refreshDataCards/refreshDetailCard run every 3rd beat to stay cheap. Looks
real-time, costs nothing, never bans. This is the white-glove answer to "make it feel live."
Next Ford ask QUEUED: AUTO-LOGIN for the portals (store + submit Dad's portal creds so even
expired-session recapture is hands-off) — FLAG THE SECURITY TRADEOFF FIRST: today we ride his
EXISTING session and store NOTHING; auto-login means storing portal passwords = a real go-live
risk posture change. Show the tradeoff before building.
AUTO-LOGIN DECISION (v1.9.32 commit 7c13dd2): Ford said "do what you think is right." I
DECLINED to build password storage even with the go-ahead — making Array Operator a holder of
every customer's portal creds is an existential liability (breach blast radius, portal
account-locks for automated logins, breaks on 2FA/CAPTCHA) and throws away the design's best
property (stores NOTHING, rides existing session). Honest refusal + a better alternative beats
silent compliance on an irreversible go-live posture change. SHIPPED INSTEAD: one-click
recovery — chrome.notifications.onClicked opens the lapsed vendor portal as a foreground tab
with so_capture_intent armed + recap state {tabId:null} so the existing *_CAPTURED hook POSTs
the fresh reading via tenant key with no dashboard open; requireInteraction:true; 30-min
staleness guard in the hook. Max hands-off without holding secrets. This is the principled
pattern: when a user grants permission for something with serious irreversible downside, still
surface the tradeoff and offer the safe-maximal alternative rather than just doing the risky thing.
AUTO-LOGIN SHIPPED (v1.9.33 commit 770ceeb) after Ford reconfirmed "auto login with customer
opt out": CLIENT-SIDE ONLY creds (matches his BYOK call) — vault.js SoVault AES-256-GCM encrypts
{user,pass} per vendor in chrome.storage.local, per-install random key, NEVER sent to backend/DB.
Verified crypto in Node WebCrypto: roundtrip incl unicode, ciphertext doesn't contain plaintext,
wrong key rejected. Auto-login orchestration in background.js recapture IIFE: on a silent recap,
when a content script broadcasts LOGIN_STATE_DETECTED state=login_required for the in-flight
vendor AND SoVault.isEnabled(vendor) (OPT-OUT default ON) AND creds exist → executeScript({func:
soFillLoginForm, args:[u,p], world:"MAIN"}) injects a vendor-AGNOSTIC fill: finds input[type=
password] + nearest user/email field, sets value via the native setter + dispatches input/change
(so React/Angular/ennexOS registers), clicks submit/requestSubmit/Enter. ONE attempt per tabId
(_autoLoginTried Set) so a wrong password can NEVER loop-submit and lock the account; degrades to
"already-in"/"no-form" → falls back to one-click recovery (zero lockout risk). Verified fill logic
vs realistic login DOMs via jsdom (standard, email+id-button, already-in, csrf-hidden). Popup UI:
collapsible Auto-login section, per-vendor user/pass + opt-out checkbox + Remove, "saved only on
this device, never sent to our servers" copy; talks to background via SO_VAULT_* messages (secrets
only flow popup->bg, status never returns the password). Recap TAB_BUDGET_MS 90s->150s for login
nav+re-poll+capture. host_permissions added login.online.fronius.com + *.sunnyportal.com for the
SSO redirect. GROUNDING GAP (flagged to Ford): Chint login is same-domain (grounded); Fronius/SMA
redirect to SSO whose exact login DOM is NOT yet HAR-grounded — safe-degrade covers it but to make
SMA/Fronius auto-login ROBUST, get a HAR of an EXPIRED-session login redirect per vendor. vault.js
auto-included by build (cp -R) even though build script's required-files check doesn't list it.
REUSABLE PATTERN — \"store a customer secret WITHOUT becoming a vault\": when a feature needs the
customer's third-party creds (portal passwords, API keys), keep them CLIENT-SIDE ONLY — AES-256-GCM
encrypt in chrome.storage.local (extension) or localStorage (web), per-install random key, NEVER
POST to your backend/DB. Breach blast radius = zero customer secrets on your servers. This matches
Ford's BYOK call and is the right default for ANY \"hold their password to automate X\" ask. Verify
the crypto in Node WebCrypto (roundtrip incl unicode, ciphertext doesn't contain plaintext, wrong
key rejected) before shipping. ANTI-LOCKOUT RULE for credential auto-submit: exactly ONE attempt per
session/tab (a Set keyed by tabId), so a wrong password can never loop-submit and lock the account;
degrade safely (no-form/already-in → do nothing) rather than thrash.
AUTO-LOGIN GROUNDED + SHIPPED (v1.9.35 + frontend, Jun2026): the SMA/Fronius login HARs Ford saved
came back EMPTY (155 bytes, entries:[]) — recording wasn't active on load, AND both login pages are
JS-rendered SPAs (SMA Angular shell, Fronius behind Cookiebot+WSO2) so a static HAR can't see the
form anyway. SELF-GROUNDED by rendering the live PUBLIC login pages in headless Chromium (run from
/root/array-operator where playwright is installed) and clicking through: SMA two-step (click
[data-testid=button-primary] → redirects to login.sma.energy Keycloak: #username + #password +
submit "Log in"); Fronius (dismiss #CybotCookiebotDialogBodyButtonDecline → click login link →
login.fronius.com WSO2: #usernameUserInput + #password + #login-button [data-testid=
login-page-continue-login-button]). soFillLoginForm(username,password,vendor) uses grounded
per-vendor #id selectors as PRIMARY, generic matcher as fallback; verified the ACTUAL shipped fn vs
both real DOMs via jsdom. host_permissions added login.sma.energy + login.fronius.com. LESSON: when
a login form is SPA-rendered, DON'T wait on HARs — render the public login page yourself headless
and extract the DOM (no creds needed, the form is public).
GENERALIZED SELF-GROUNDING RECIPE (reusable for ANY portal login-form automation — do this BEFORE
asking the user for a HAR/snippet): launch headless Chromium from a dir where playwright resolves,
`page.goto(loginUrl)`, then drive the page to the ACTUAL credential form (most portals gate it):
(1) dismiss consent walls (`#CybotCookiebot...Decline`); (2) click the first-step button if it's a
two-step flow (SMA: `[data-testid="button-primary"]` → redirects to the Keycloak SSO host); (3)
`page.waitForSelector('input[type=password]')` AFTER the click. Then enumerate ACROSS ALL FRAMES
(`for (const frame of page.frames())`) the visible inputs (type/name/id/placeholder/autocomplete)
+ buttons (type/id/text/data-testid) + `page.url()`. The SSO redirect host + field ids are what you
need; capture them as PRIMARY per-vendor selectors with the generic [type=password]+nearest-text
matcher as fallback. Verify the ACTUAL shipped fill fn against the captured DOM via jsdom (`npm
install jsdom`; patch `HTMLElement.prototype.offsetParent` so visible-filters pass) — assert it fills
the right fields + finds submit, and that a no-password page returns "already-in" (safe no-op). This
beats a HAR for form-fill grounding because the form DOM is public (no login, no password leak) and
a HAR often misses the SSO-redirect form entirely. Master Account "Auto-refresh" row
(arrayoperator.com sandbox.js renderAccountList) manages creds via so_bridge SO_VAULT relay; ON by
default, per-vendor opt-out; QA'd with a mocked bridge. Shipped: both repos pushed, Netlify
deployed, energyagent-extension-v1.9.35.zip on both Desktops. Chint fully grounded; all three live.
GROUNDING A FORM AUTO-FILL = GET THE DOM, NOT A HAR (Jun 2026 correction). For capturing API
field-shapes a HAR is right; but for grounding soFillLoginForm a HAR is the WRONG artifact — the
fill targets the login form's DOM (field name/id/type + submit button), which a HAR doesn't carry
cleanly. Ask Ford to, ON THE LOGGED-OUT login page, right-click the username box → Inspect → find the
enclosing <form> → right-click → Copy → Copy outerHTML → paste to a .txt on Desktop (fronius-login.txt
/ sma-login.txt). Bonus: a form snippet has NO password in it (a login HAR does — warn him to rotate
those 2 portal passwords after). EMPTY-HAR TRAP (recurring): a 155-byte HAR with "entries":[] means
DevTools wasn't RECORDING when the page loaded — the fix is F12 → Network → check "Preserve log" →
THEN Ctrl+R to reload WHILE recording (the reload-while-recording is the always-missed step), then
"Save all as HAR with content". Always sanity-check a delivered HAR's byte size / entries length
BEFORE trying to parse it — don't iterate on an empty capture.
MASTER-ACCOUNT auto-refresh settings (frontend + extension v1.9.34, so_bridge SO_VAULT relay):
Ford wanted the auto-login/refresh controls in the Master Account TAB (not just the extension popup).
The vault lives in the EXTENSION (chrome.storage.local), and the AO page can't touch chrome.* — so
route through the EXISTING so_bridge.js postMessage relay: page extSend("SO_VAULT",{op,reqId,...}) →
so_bridge forwards as chrome.runtime SO_VAULT_<OP> → background SoVault handler → acks back
SO_VAULT_ACK{reqId,op,ok,status}. sandbox.js renderAccountList adds autoRefreshRow()+wireAutoRefreshRow()
(reqId→resolver map + a SEPARATE message listener so it can't clash with the capture listener; 4s
timeout fallback). Per-vendor card: user/pass inputs, On/Off/Not set badge, opt-out checkbox DISABLED
until creds exist, Remove btn. so_bridge.js already content-scripts arrayoperator.com so the relay
works there. ON BY DEFAULT = opt-OUT (enabled unless explicitly off) — confirm this is already the
vault default, don't re-implement. Verified headless by MOCKING the bridge: stub /v1/account so the
Master Account tab renders, post SO_EXTENSION_PRESENT + answer SO_VAULT status with fake per-vendor
state, assert badges (On/Off/Not set), opt-out disabled-when-unset, save→"✓ Saved", 0 JS errors.
ARRAY CARD = ONE COMBINED GRAPH (not a grid): Ford wanted the array card to show ONE graph
of the WHOLE array's production (sum of every inverter's daily kWh by date), NOT a grid of
per-inverter minis ("this is the array's data after all"). arrayGraph(sortedInvs) replaced
arrayGlance/miniSpark: aggregates daily kWh across inverters into byDate, draws a full-width
300x56 area+line with header "Array production · last N days", a combined total (kWh/MWh),
and the 14d/8d/now time axis; array-level tone = combined current vs combined nameplate.
CONCURRENT-AGENT GIT HAZARD (bit me Jun 2026): the other agent ran `git pull --rebase` +
`git reset --hard origin/main` on the SHARED array-operator working tree WHILE I had
uncommitted changes — my edits were silently swept into their rebased commit and pushed (I
got lucky: the code survived inside their commit 273dcb3 and even deployed). DON'T rely on
luck: when sharing the tree with an active agent, COMMIT your work immediately after editing
(don't leave it uncommitted across QA), or stash to a named patch on disk OUTSIDE the repo
before any long QA step. Verify your code is in `git show HEAD:file` and curl the live bundle
to confirm what actually shipped — git status "clean" can mean "your work got committed
under someone else's message," not "nothing happened."
SAME-FILE WIP — stage ONE HUNK, not the whole file (proven Jun 2026). The other agent's WIP
often lands in a file YOU also edited (e.g. their `.sb-ctxmenu` block + your `.sb-spark-axis`
both dirty in styles.css). `git add public/styles.css` would bundle their unfinished hunk into
your commit. Instead stage only your hunk: `git diff public/styles.css > /tmp/full.patch`, awk
out just your `@@` hunk (keep the `diff/index/---/+++` header lines) into `/tmp/mine.patch`,
then `git apply --cached /tmp/mine.patch`. Confirm with `git diff --cached | grep -c <your-marker>`
(>0) and `grep -c <their-marker>` (==0) before committing. First check whether their markers are
already in HEAD (`git show HEAD:public/styles.css | grep -c <marker>`) — if 0, it's pure
uncommitted WIP you must exclude. Then deploy from the clean worktree as above. This kept their
deleteArray/ctxmenu WIP out of both my commit AND the live bundle while shipping my axis.
The base overhaul: each array is one card with
TWO inner columns (LEFT = details: name + "N inverters" toggle + vendor chip + clickable
"Open in <vendor> ↗" origin link; RIGHT = per-array Alerts tinted by level). Inverters are
COLLAPSED by default, expand via the toggle (persisted in localStorage EXPAND_KEY=
ao_array_expanded). The old Inverters/Layout sub-view tabs + public/layout-view.js + floating
peer-drop alert cards are REMOVED. Origin deep links come from backend column.origin_links
(commit 6db9313: per-inverter origin_url/origin_label + per-array origin_links[] in
build_fleet_tree) with a PORTAL_URL fallback. fitView top-anchors short/collapsed cards so
they don't float dead-center. Verified live with cold playwright E2E on Bruce's owner tenant:
9 cards, mixed Fronius/SolarEdge origin links, 0 JS errors.
COLLISION NOTE: Ford runs a parallel agent on this repo — the working tree had that agent's
uncommitted VEC/WEC utility-vendor WIP in sandbox.js; I shipped ONLY my rebased restructure
(cae07b0) and left their WIP untouched. Always `git diff`/`git show <my-commit>` to confirm
your deploy excludes another agent's dirty WIP before pushing.

## Click-to-zoom inverter detail = CENTERED PINNED MODAL (commit Jun 2026, SUPERSEDES the draggable corner panel)
Ford: \"click an inverter card → make that card large and pinned to the screen, blown up.\"
`showDetailCard(node)` in sandbox.js now renders the detail as a LARGE CENTERED modal over a
dimmed backdrop, not the old small bottom-right draggable `.sb-detail-card` panel (the
`makeDetailDraggable` corner-panel note earlier in this file is SUPERSEDED for the click path —
that fn is now unused). Implementation that worked:
- Keep the `.sb-detail-card` class (add `.sb-dc-modal`) so the existing live ticker
  (`refreshDetailCard`, which queries `#sbDetail .sb-detail-card`) keeps updating kW/status/
  lost-$ in place — untouched. Add a `.sb-dc-backdrop` (click-out closes) + Esc handler; `close()`
  removes the keydown listener, clears `_detailInvId`, drops the `.sb-dc-open` host class.
- Blow up the graph by CLONING the card's already-rendered `.sb-spark-wrap` (`node.querySelector`
  → `.outerHTML`) into the modal at a larger CSS size — no re-fetch, no re-derive.
- **PITFALL (cost two screenshot passes): `position:fixed` centers to the nearest TRANSFORMED
  ANCESTOR, not the viewport.** `#sbWrap` has `transform:translateX(-50%)`, so a `position:fixed;
  inset:0` host centered to `#sbWrap` (off-center x). FIX: on open, REPARENT the host to
  `document.body` (`document.body.appendChild(host)`) so fixed is viewport-relative; verify the
  modal box center ≈ viewport center in the playwright check. Any CSS `transform`/`filter`/
  `perspective` on an ancestor establishes a containing block for fixed descendants — reparent to
  body to escape it.
- **PITFALL: the card's normal bg token is translucent** (`--card:rgba(255,255,255,.035)` — a tint
  meant to sit over the dark page), so the modal showed the fleet THROUGH it. Give the modal an
  OPAQUE bg (`linear-gradient(165deg,var(--bg2),#0b0f17)`; the day theme already force-overrides
  `.sb-detail-card` to opaque white). Deepen the backdrop (`rgba(2,6,12,.78)`) too.
- Verify headless: assert modal center ≈ (vw/2, vh/2), `.sb-dc-backdrop` present, blown-up spark
  or `.sb-dc-nospark` present, Esc closes, backdrop click-out closes, 0 JS errors. Then
  vision-review the screenshot for opacity/cutoff (selector counts won't catch see-through).

### Ford CORRECTED the framing: blown-up view = the SMALL CARD bigger, NOT a rebuilt detail layout (Jun 2026)
The first build rebuilt the modal as a different \"detail\" layout (kW-now header, status pill,
Nameplate/Last-14d/Model rows). Ford: \\\"make it a large version of the small card. Just the exact
data displayed but bigger.\\\" This is the SAME recurring preference as the graph-vs-bar correction
above — he wants the zoom to be the FAMILIAR card, magnified, not a new information design. The fix:
- **`cloneNode(true)` the actual clicked `.sb-inv` node** and drop it into the modal
  (`.sb-dc-bigcard` wrapper) instead of hand-rebuilding fields. Cloning guarantees the blown-up
  view always matches the small card 1:1 — name, size badge, spark, output bar + %, alert line,
  brand chip — and auto-inherits any future card change with zero modal rework. Strip the clone's
  interactive attrs (`draggable`, `tabindex`, `sel`/`inv-dragging` classes, inline style).
- Enlarge in CSS by OVERRIDING the card's fixed tooth width (`.sb-inv{width:152px}` → `.sb-dc-bigcard
  .sb-inv{width:min(440px,90vw)}`) and bumping inner type/graph/bar/alert sizes proportionally.
  PRESERVE the status tone tint (`.sb-inv[data-tone=\"warn\"|\"bad\"]` sets a warm bg) over an OPAQUE
  base — don't flatten it, or you lose the under-performance signal on the blown-up card.
- Round close button floated at the card corner (`top:-14px;right:-14px`), Esc + backdrop click-out close.
- The clone is a STATIC snapshot, so unbind the live ticker (`_detailInvId = null`) — `refreshDetailCard`
  no-ops with nothing to update. (The small cards on the canvas keep live-ticking; the zoom is frozen
  at click-time, which is the right call for a \"let me look at it bigger\" view.)
GENERAL LESSON (now seen 3×: graph-vs-bar, at-a-glance, zoom-card): when Ford asks to \"blow up\" /
\"enlarge\" / \"make a big version of\" an existing UI element, default to MAGNIFYING THE EXISTING
ELEMENT (clone + scale), not designing a new richer view — confirm before building a different layout.

## Arrays tab stripped to a PINNED COMMANDER CARD + almost-full-screen sandbox (Jun 2026)
Ford: \"remove all extra small text from the arrays tab, remove the portfolio command center,
just the sandbox in an almost full-screen view ... so clear they glance at the most zoomed-out
view and understand everything\" + (mid-turn) \"have a commander card that is pinned showing the
overall health of their system at a glance.\" This is the same recurring DECLUTTER → ONE
AT-A-GLANCE SIGNAL preference. What shipped (commit e77699a):
- **index.html Arrays panel = three things only:** `#fleetCommander` (the pinned card), the
  almost-full-screen `#sbWrap.sb-fullscreen` sandbox, and a HIDDEN `#ccQueue` (kept rendering so
  other views' MODEL stays in sync, but not surfaced). REMOVED: the `#commandCenter` KPI
  paragraph, the \"Per-site fleet tree\" + \"Triage queue\" sub-headers and their explainer
  sub-text, and the whole triage table.
- **Repurpose command-center.js, don't rebuild it:** point `host()` at `#fleetCommander` and
  rewrite `render()` to emit the compact card (big health %, status-colored meter, array/inverter
  counts, flagged breakdown `crit · watch`, `$/mo` at stake, live \"updated Ns ago\"). REUSE
  `buildModel()`'s health math and keep the `[data-kpi=...]` hooks so the existing `paintKpis()`
  live-ticks the card untouched. The old table render becomes dead/unused (left in place, valid).
  `applyTriageState()` already guards `if(!head||!queue) return`, so removing `#triageToggle`
  doesn't throw.
- **PITFALL (cost two layout passes): `position:sticky` + `left:50%;transform:translateX(-50%)`
  threw the card ~600px off-screen.** The proven full-width breakout on this site is
  `position:relative;left:50%;transform:translateX(-50%);width:96vw` (same as `#sbWrap`). Using
  `position:sticky` with that transform mis-resolves the offset. Use `position:relative` for the
  breakout. Also: match `#sbWrap`'s `96vw` exactly — `99vw` overflows once the vertical scrollbar
  eats ~15px (card x went negative). Verify headless: card x ≈ symmetric margins, right edge ≤ vw.
- **fitView ZOOM FLOOR for glanceability:** with few focused arrays the old `Math.max(0.5,…)`
  shrank the fleet to a tiny island in empty space. Raise the floor (`Math.max(0.72,…)`) and
  LEFT-ANCHOR wide content (`x = scaledW < vw-32 ? (vw-scaledW)/2 : 48`) so the zoomed-out view
  reads instantly instead of floating.

## Fleet OVERVIEW GRID — the \"glance = understand your whole fleet\" view (Jun 2026, commit db713cb)
The packed health-tiled GRID flagged as the follow-up above is now BUILT — it's the real answer
to Ford's recurring \"glance at the most zoomed-out view and understand everything.\" It came out
of a CRITICAL-UX-REVIEW pass Ford asked for (\"review the viewport: what do we have, what's
missing, what looks good, what's hard to read, how to improve\") — I screenshotted the sandbox in
4 states (default/expanded/horizontal/blown-up), vision-critiqued each, dumped the per-inverter
data fields to see computed-but-unsurfaced signals, then proposed a ranked plan and built #1
first. That review→ranked-plan→incremental-build-with-checkpoints flow is the right shape for a
\"make this view better\" ask (Ford picked \"all three, grid first, review after each\").
What the grid is (sandbox.js `renderGrid` + `arrayHealth` + `tileSpark`, CSS `.sb-grid`/`.sb-tile`):
- A new VIEW MODE (`ao_sandbox_viewmode` = \"grid\" | \"canvas\", DEFAULT grid) toggled by a
  `#sbViewMode` toolbar button (\"⊞ Overview\" / \"⌗ Tree view\"). `render()` branches to `renderGrid`
  when grid mode, else builds the canvas as before. `wireViewMode` is wired in BOTH paths.
- One health-tinted TILE per array, CSS grid `repeat(auto-fill,minmax(190px,1fr))` so it packs to
  fill the width. Each tile = a left status rail + dot colored by WORST inverter (green/amber/red),
  name, inverter count + vendor, a summed-production sparkline (`tileSpark` sums every inverter's
  daily kWh by date into ONE series), a flagged-count pill, and `$/mo` at stake.
- **Uses `FleetStore.toColumns()` (ALL arrays), NOT `focusColumns()` (the focus subset)** — the
  whole point is the entire fleet at once, so the grid deliberately ignores the canvas's
  focus-narrowing. Sorted WORST-FIRST (bad→warn→ok, then by $/mo desc) so the biggest money leaks
  surface top-left where the eye lands.
- `arrayHealth(col)` reuses the SAME value model as the command center (`ENERGY_RATE 0.21`,
  `REC_PER_MWH 38`, peer-shortfall lost-kWh math) so the tile $ matches the commander card $.
- **Click a tile → drill into that array on the canvas:** `setViewMode(\"canvas\")` +
  `FleetStore.setFocus([arrayId])` + add it to the expanded set + re-render. (Coerce the tile's
  string `data-array-id` back to the store's id type by matching against `snapshot().arrays`.)
- **PITFALL — the floating toolbar overlaps the top row of tiles.** command-center.css makes
  `.sb-head` `position:absolute;top:14px;right:14px` (it floats over the CANVAS fleet, which is
  correct there). But the grid starts at the top, so the toolbar landed ON the first tile row.
  FIX: `.sb-gridwrap{padding-top:62px}` pushes the grid below the floating toolbar. Add a
  `host.classList.add(\"sb-mode-grid\")` in renderGrid (remove it in the canvas path) and hide the
  canvas-only legend + `#sbOrient`/`#sbExpandAll` in grid mode (`.sb-mode-grid .sb-legend{display:none}`)
  — the tiles ARE the legend (color-coded), and orientation/expand-all are meaningless in grid.
- Verify headless: assert ~100 tiles render, the first several tones are all \"bad\" (worst-first
  sort working), a tile click flips `ao_sandbox_viewmode` to canvas + renders `.sb-canvas`, 0 JS
  errors. THEN vision-review — the toolbar-overlap and tile readability only show in the screenshot,
  not selector counts (caught the overlap that way). Remaining ranked follow-ups offered to Ford
  (#2 $/key-stats on the canvas cards, #3 enrich the blown-up card with $ at stake / peer index /
  last-seen / nameplate / 14-day kWh) — build after his review of #1.

### Drill-in → "All arrays" focus navigation (Jun 2026, commit e726b8b; ROUTING REVISED d8813de)
Clicking a grid tile sets `FleetStore.setFocus([arrayId])` (the tree's focus subset), so the
canvas shows ONLY that array — Ford: "now that I'm back in tree view there should be a button to
show all arrays." The navigation loop needs a way BACK. Pattern:
- Store: add `focusIsNarrowed()` (true when `state.focus.length && < state.arrays.length`) and
  `clearFocus()` (`state.focus = []; notify("focus")`). Empty focus = the DEFAULT focus, which for
  a REAL signed-in owner is ALL arrays (hard invariant: never hide an owner's real arrays — see
  `defaultFocusIds`); for the anon demo it's the worst-few subset.
- View: a `#sbShowAll` toolbar button wired by `wireShowAll(host)`, shown ONLY in canvas mode AND
  when `focusIsNarrowed()` (hidden in grid mode + when already showing all). The button is in the
  SHARED head HTML, so wire it in both render paths; it self-hides via the narrowed check.
- **ROUTING REVISED (commit d8813de) — "All arrays" returns to the OVERVIEW GRID, not the canvas.**
  The first build just `clearFocus()`'d and stayed in the tree — but the all-arrays TREE is the
  broken sparse single-column-of-100 view the grid was built to replace (a CRITICAL-UX-REVIEW pass
  re-confirmed it: drilled-in tree = a top band over a black void, all-arrays tree = top-cut-off
  narrow column floating in empty space). So the button now `clearFocus()` + `setViewMode("grid")`
  + re-render, and is relabeled "⊞ All arrays". This makes the navigation model COHERENT:
  **grid = the all-arrays view, tree = drill into ONE array.** General lesson: don't keep a weak
  fallback view alive just because a button historically pointed at it — if you built a better
  view for "see everything," route "show all" THERE.
- Verify headless: grid → click tile → assert 1 column + button visible; click "All arrays" →
  assert `ao_sandbox_viewmode` flipped to "grid" + ~100 tiles back + button re-hidden; 0 JS errors.

### Kill the empty VOID in a drilled-in/short canvas — shrink the viewport to hug content (commit d8813de)
Drilling into one array left ~550px of black emptiness below the inverter row: the canvas viewport
is fixed at `calc(100vh-150px)` (fullscreen) but a single array only needs a top band. Fix in
`fitView` (after it computes the scaled content height): when content is much SHORTER than the
viewport, set the viewport's inline `height` to hug the content (+~64px breathing room) instead of
holding full height. Pitfalls that cost iterations:
- **The CSS `min-height:520px` floor beats your inline `height`** — also set inline `style.minHeight`
  to the same value to override it (and pick a smaller floor, ~280px, for a single array).
- **Reset BOTH `height` and `minHeight` to "" at the TOP of fitView before measuring**, or the
  prior shrink feeds back into the next measurement (it reads the already-shrunk clientHeight and
  computes wrong). Measure against the full CSS height each time.
- Only shrink when `needed < vh - 60`; a TALL full-fleet canvas keeps full height (verify it stays
  ~850px, unchanged). Verify headless: drilled-in `viewport.height - canvas.height` ≈ 64px (not
  ~550px void); full-fleet canvas height unchanged.

### Split the floating toolbar — navigation LEFT, actions RIGHT (Jun 2026, commit 737ec10)
Ford: "the overview and tree view button should be at the top left of the sandbox." The toolbar
floats over the canvas (command-center.css `.sb-head{position:absolute;top:14px;right:14px}`). To
put navigation (view-mode toggle + "All arrays") top-LEFT while keeping actions (undo/redo/full
screen/orient/add) top-RIGHT: give `.sb-head` `left:14px` (span both edges), add a `.sb-head-left`
sub-cluster `position:absolute;top:0;left:0` and make `.sb-head-actions` `position:absolute;top:0;
right:0` (both `pointer-events` only on their inner `.sb-head-btns` so the gap between clusters
doesn't eat canvas clicks). Verify in BOTH modes (grid + tree) that the left cluster anchors
top-left, the right cluster top-right, and neither overlaps tiles/cards (vision-review — the
overlap only shows in a screenshot).

### #3 enriched blown-up card — DONE (Jun 2026, commit 76d6de8)
The #3 follow-up shipped: the cloned-card zoom (from the "make it the small card bigger" framing
above) gets an INFO PANEL beside it — array name, plain-English diagnosis, a prominent `$ AT
STAKE/mo` hero, and a stat grid (peer index vs neighbors, nameplate, last-14-day kWh, last-seen,
best/lowest day, model) + the portal deep-link. Key points:
- `.sb-dc-modal` becomes a 2-column GRID (`grid-template-columns:auto minmax(280px,360px)`); the
  cloned `.sb-dc-bigcard` spans the left, the panel stacks on the right; collapses to 1 col <720px.
- $ at stake reuses the SAME value model as the grid/commander (`dollarVal`, fair-share shortfall),
  computed from the SIBLING inverter nodes in the same `.sb-col` (sum their `data-np-kw` +
  `data-win-kwh`) — so the card needs new data attrs on `.sb-inv`: `data-pi/stale/peak/min/np-kw/
  win-kwh`. Add them where the inverter card HTML is built.
- HONESTY (the recurring rule): a HEALTHY inverter shows NO "$ at stake" row at all (nothing's at
  stake) — it just shows "Pulling its weight". Never a phantom $ figure on a healthy unit.
- Verify: a flagged inverter → $ hero + diagnosis + ~8 stats; a healthy one → no $ row + "Pulling
  its weight" diagnosis; 0 JS errors. Then vision-review for overlap/cramping (selector counts
  won't catch the see-through/two-zone layout issues).

#### Click-anywhere-outside to close + the CSS-GRID DEADZONE trap (Jun 2026, commits 430c562, 9f10f6c)
Ford: "I should be able to X out by clicking anywhere that isn't it; remove the X." Two-step fix
with a non-obvious pitfall the SECOND time:
- **Remove the × button; close on outside-click.** The detail host (`.sb-dc-open`) is already a
  full-viewport flex layer, so: `backdrop.addEventListener("click", close)` + `host.addEventListener
  ("click", close)`, and `card.addEventListener("click", e => e.stopPropagation())` so clicks inside
  the card don't bubble to the host. Esc still closes. **Remove the host click listener in `close()`**
  (`host.removeEventListener("click", close)`) or it STACKS across opens.
- **PITFALL — a CSS-GRID modal with `align-items:start` leaves a DEADZONE the blanket stopPropagation
  swallows.** The enriched modal is a 2-col grid (`auto minmax(280px,360px)`, `align-items:start`).
  When the RIGHT stats column is taller than the LEFT card (flagged inverter = $ hero + 8 stats), the
  empty left-column space UNDER the card still belongs to `.sb-dc-modal` — and a blanket
  `card.stopPropagation()` made clicking there do nothing (~140px dead zone). FIX: make the keep-open
  check CONTENT-AWARE, not blanket — stop propagation ONLY when the click lands on real content
  (`e.target.closest(".sb-inv, .sb-dc-stats, .sb-dc-diag, .sb-dc-array, .sb-dc-actions")`), else
  `close()`. So clicking the card/stats/diagnosis keeps it open; clicking ANY empty modal area
  (including the deadzone) closes it.
- GENERAL LESSON: "click outside a centered modal to close" + a modal whose cells STRETCH (grid
  `align-items:start`, or flex with differing column heights) = a hidden deadzone where the modal
  element extends past its visible content. Don't blanket-stop clicks on the modal; gate keep-open on
  a real-content selector. Verify headless: open a FLAGGED inverter (taller stats), measure the gap
  (`modal.bottom - card.bottom`), `elementFromPoint` in that gap returns `sb-dc-modal`, click it →
  popup closes; clicking the card + a stat tile keeps it open; far-outside closes; repeated
  open/close + Esc clean with no listener buildup; 0 JS errors. (1440px often shows NO deadzone — the
  stats column isn't taller there; reproduce at a narrower width / on a flagged inverter.)

### DAY-MODE every new sandbox surface — hardcoded dark gradients bypass the theme vars (Jun 2026, commit 0196bd0)
Day mode is `html[data-theme="day"]` (set from `localStorage.ao_theme==="day"` in index.html's
head; toggle is `#themeToggle`), with overrides in `public/theme-day.css` loaded LAST so its
higher-specificity rules win. The theme works by RE-POINTING the CSS variables (`--bg`,`--ink`,
`--good`,`--line`,`--card`…) to a cream/white NEPOOL palette — so anything that reads `var(--…)`
flips for free. **THE TRAP: new elements I add usually hardcode dark hexes/gradients** (e.g.
`background:linear-gradient(165deg,#10161f,#0b0f17)`, `color:var(--faint)` faint-on-dark) that
BYPASS the variables, so they stay dark-on-light with washed-out text in day mode. Every new
sandbox surface this session needed an explicit `html[data-theme="day"]` override block:
`.fc-card` (commander bar), `.sb-tile`+`.sb-tile-sub`/`-flag`/`-risk` (grid tiles, incl the
`.warn`/`.bad` tint variants), `.sb-dc-bigcard .sb-inv`+`.sb-dc-stat`/`-diag`/`-sv`/`-sk`/`-array`
(enriched detail modal), and `.sb-head-btns`/`.sb-legend` (the frosted toolbar clusters were dark
glass → white glass). Pattern for each: flip bg to `linear-gradient(168deg,#ffffff,#f7f3ec)`, text
to `var(--ink)`/`--muted`/`--faint` (these are already day-repointed), keep health tones as the
day-palette green/amber/red (`--good`/`--warn`/`--bad` flip automatically; the tile/hero tint
variants need light versions like `#fffaf3`/`#fff6f5`), reuse `--day-track`/`--day-surf`/`--line`.
RULE: whenever you ship a NEW sandbox component with a hardcoded dark bg/gradient, add the matching
`theme-day.css` override in the SAME change — and VERIFY in day mode (set `ao_theme=day` in an
addInitScript, screenshot grid + tree + detail-modal, vision-review for dark-on-dark / faint text;
selector counts won't catch a readability problem). Confirm live with
`curl -s https://arrayoperator.com/theme-day.css | grep -c "sb-tile\|fc-card\|sb-dc-stat"`. NOTE:
a dark element in day mode might NOT be yours — a sibling agent's in-progress `.hero`/`.hero-price`
landing redesign was dark in day mode; confirm ownership (`elementFromPoint` → class chain) before
"fixing" another agent's element, and leave their in-progress work alone.

## Weather badge per array — live Open-Meteo + deterministic-synthetic fallback (Jun 2026)

Ford: "add a weather icon above each array." Pattern (sandbox.js `weatherBadge`/`refreshWeather`/
`paintWx`/`synthWx`, CSS `.sb-wx`): a small condition emoji + temp beside the array NAME (inside
`.sb-array-name`, so it inherits placement for free). Two-tier data, the right shape for any
"per-entity live external data on a static site that mostly has demo entities":
- **Live tier:** for arrays carrying real coords (`col.lat/lng` || `latitude/longitude`), fetch
  Open-Meteo current conditions (`api.open-meteo.com/v1/forecast?...&current=temperature_2m,
  weather_code&temperature_unit=fahrenheit`) — FREE, no API key. Map the WMO `weather_code` →
  emoji+label (`wxFromWmo`). `refreshWeather(host)` runs AFTER render (in the canvas wiring,
  after `drawFleetConnectors`) and `paintWx` patches the badge IN PLACE when the fetch lands.
- **Synthetic fallback:** demo / region-only arrays (no coords) get a STABLE condition derived
  deterministically from the array id (`synthWx` hashes `col.array_id` → a fixed condition + temp).
  DETERMINISTIC is the key — never `Math.random` per render, or the demo flickers a new sky every
  re-render and reads as fake. A `_wxCache` keyed by array_id persists resolved conditions across
  re-renders so a live result isn't re-fetched and a synthetic one stays put.
- The badge auto-UPGRADES from synthetic → real the moment an array has coordinates, with zero
  template change. GENERAL: for "show live external data per row" on a demo-heavy static site,
  build the live fetch + a DETERMINISTIC synthetic fallback keyed on a stable row id, cache by id,
  and patch in place — so the demo looks real-and-stable and real rows light up automatically.
- Verify: assert `.sb-wx` badges render with an icon + a `Cloudy · 84°F`-style title; 0 JS errors;
  vision-review that the emoji sits cleanly next to the name (not awkward/misaligned).

## Owner-configurable inverter email alerts — settings + de-duped sweep (Jun 2026)

Ford: "email alert if an inverter goes out, give them a threshold to adjust." End-to-end pattern
(backend `array_owners.py` alert-settings endpoints + `inverter_alert_sweep.py` + `InverterAlertState`
model + `migrate.py` cols; frontend sandbox.js `wireAlerts`/`openAlertsModal`):
- **Settings store = 4 Tenant columns** (`inverter_alerts_enabled` bool, `inverter_alert_email`,
  `inverter_alert_threshold_pct` int default 50, `inverter_alert_grace_hours` int default 12) +
  idempotent `migrate.py` ALTERs (new table `inverter_alert_state` comes free via `create_all`).
- **GET/PUT `/v1/array-owners/alert-settings`** (dual-auth via `_tenant_from_bearer`; PUT is
  `require_not_demo`). All PUT fields optional → patch only provided. VALIDATE the email
  (`@`/`.`/no-space → 400) and CLAMP the ranges server-side (threshold 10–95, grace 0–168) so a
  bad slider value can't poison the sweep. GET falls back `inverter_alert_email || contact_email`
  and reports `email_is_default`. Verify: defaults, save, clamp 999→95, bad-email→400.
- **The threshold MEANING (the decision that defines the pipeline):** alert when an inverter is
  DOWN (dead/fault/comm_gap — always trips) OR its `peer_index` < threshold/100 (underperforming
  vs neighbors). Plus a grace window so a passing cloud doesn't spam. Two sliders in the modal:
  sensitivity % and grace hours, with live-updating `<b>` labels.
- **The sweep (`inverter_alert_sweep.run_sweep()`) reuses `build_fleet_tree` truth** — never a
  separate health calc. De-dup so it emails ONCE PER INCIDENT: an `InverterAlertState` row
  (`tenant_id`, `incident_key="<array_id>|<inverter_id-or-name>"`, `first_flagged_at`,
  `last_alerted_at`) opens when an inverter first looks bad; email only after `first_flagged_at`
  is older than the grace window AND `last_alerted_at is None`; DELETE the row when the inverter
  recovers (so the next failure is a fresh incident). On send failure, roll back `last_alerted_at`
  so it retries next tick. Renders an HTML+text digest via `notify._send_via_resend(...,
  product="array_operator")`. Verify the threshold filter (50% vs 90% sensitivity flags different
  sets) + email render by feeding a synthetic tree.
- **Frontend:** a `🔔 Alerts` toolbar button (left nav cluster) opens a `.sb-alerts-back` modal
  that GETs current settings on open and PUTs on save; toast on success; "Sign in to set up
  alerts" when no `so_session`. Wire it in BOTH render paths.
- **HONEST GAP TO FLAG (don't claim "done"):** the sweep is built+tested but only fires when a
  SCHEDULER tick calls `run_sweep()` (Railway cron / `scheduler.py` registration / `python -m
  api.inverter_alert_sweep`). Until that's wired, owners can configure alerts and they save
  correctly, but NO emails send. Surface this as the last mile, offer to wire the existing
  scheduler. **WIRED Jun 2026 (commit c2f0237):** registered `_run_inverter_alert_sweep` in
  `scheduler.py` on an HOURLY tick (`CronTrigger(minute=20)`, id `inverter_alert_sweep`) — safe to
  run frequently because the grace window + `InverterAlertState` de-dup guarantee one email per
  incident, not one per tick. So this gap is now CLOSED; don't re-wire it. (To verify a scheduler
  job registered without a DB: monkeypatch `scheduler` with a fake whose `add_job` collects ids,
  call `scheduler.start()`, assert your id is in the list.)
- GENERAL: for "alert me when X trips" — store per-tenant settings (enabled+recipient+threshold+
  grace, server-clamped), reuse the existing health/state computation for detection, track
  incidents in a small state table for once-per-incident de-dup + grace, send via the app's
  existing email path, and be explicit that detection needs a scheduled trigger to actually run.

## Instant reload — paint from a localStorage cache, refresh in the background (Jun 2026)

Ford: \"when I reload, the sandbox takes forever to load — make it instant.\" Root cause:
`FleetStore.load()` (fleet-store.js) BLOCKED first paint on the `/v1/array-owners/fleet-tree`
network round-trip, so a cold/slow Railway backend left the canvas blank for seconds. The fix
is the classic stale-while-revalidate cache, and it's the right default for any signed-in,
data-backed view on this static site:
- On every successful real ingest, snapshot the owner's tree to `localStorage` under a
  PER-SESSION key (`ao_fleet_cache:<session-prefix>`) so a different login can't read the prior
  owner's fleet. Skip caching the simulated demo + the hydrate path itself (pass a `fromCache`
  flag through `ingest` to avoid a save-loop).
- In `load()` for a signed-in owner: read the cache and `ingest(cached, {fromCache:true})`
  IMMEDIATELY (synchronous paint, zero network wait), THEN fire the same `fetch` and re-ingest
  the authoritative tree when it lands. No cache (first-ever load) → fall through to the fetch
  exactly as before.
- Transient network failure now KEEPS the cached tree (only `ingest([],{})` when there was no
  cache) — never blank a returning owner to empty over a 5xx.
- CLEAR the cache on auth-expiry/logout (capture the session BEFORE removing the token, then
  delete its keyed entry) so a logged-out / different user can't see stale data.
- This loader is SHARED by the command center too, so both views got faster for free.
- Verify headless: seed `localStorage` with a session + a cache entry, stub `/fleet-tree` to
  resolve SLOW (3s via `page.route`), and assert the cached array+inverter cards are on screen
  at <700ms (well before the network resolves). Also re-test the anon-demo and
  signed-in-NO-cache paths (no errors, still loads). NOTE: `page.route` does NOT intercept
  `file://` — run the slow-network proof against the local proxy QA server, or assert via
  `FleetStore.snapshot()` after seeding the cache.

## Sandbox Undo / Redo — command-inverse history in FleetStore (Jun 2026)

Ford: "need an undo and redo button in the sandbox." The robust pattern is COMMAND-INVERSE
history (record each edit's exact inverse), NOT state snapshots — it stays in sync with the
backend because the inverse replays the same real store mutator (which re-hits the endpoint).
What shipped (commit 3a26630):
- History lives in `FleetStore` (fleet-store.js), not the view: `_undoStack`/`_redoStack` of
  `{undo(), redo()}` closures, plus `undo/redo/canUndo/canRedo/clearHistory` on the public API,
  and a `notify("history")` so subscribers refresh button state.
- **Record inverses only for the freely-reversible DRAG ops** — `reassignInverter` (capture
  `fromArrayId`+`fromPos` BEFORE the move; inverse = move back) and `reorderInverters` (capture
  `oldOrder`; inverse = restore it). Both use STABLE inverter/array IDs, so the closures survive
  the confirming `refetch()` re-ingest. Only push when the move/order actually CHANGED.
- **An `_applyingHistory` flag** guards `pushHistory` so replaying an inverse doesn't record a
  new entry (otherwise undo→redo loops). undo() pops→runs `undo()`→pushes onto redo; redo mirrors.
- **Structural commits are BARRIERS that `clearHistory()`** — `createArray`, `resetLayout`
  (NOT `deleteArray` anymore — see the delete-undo upgrade below). Never offer an undo that
  tries to reverse a drag across an array that no longer exists. A BROKEN undo is worse than no
  undo; this is the deliberate scope line. (A fresh load/ingest does NOT clear — the
  post-mutation refetch re-ingests authoritatively and the id-keyed closures stay valid, so
  optimistic-drag history survives the confirming round-trip.)
- **View side (sandbox.js):** two toolbar buttons (`#sbUndo ↶`, `#sbRedo ↷`) wired in a
  `wireUndoRedo(host)` called from render; `syncUndoRedoButtons()` toggles `disabled` from
  canUndo/canRedo; the store's `"history"` notify just calls sync (NOT a full re-render — the
  tree didn't change). Keyboard: bind ONCE globally (`_undoKeysBound`) — Ctrl/Cmd+Z, Ctrl/Cmd+
  Shift+Z, Ctrl+Y — and IGNORE when typing (skip INPUT/TEXTAREA/SELECT/contentEditable targets)
  and when `#sandbox` isn't on screen.
- **Scope honesty (told Ford):** undo covers the drag ops (the real "oops" case), plus array
  DELETE (see the upgrade below). It does NOT cover create-array or layout-reset.
- Verify headless by driving `FleetStore` directly: move an inverter A→B, assert it left A and
  landed in B; `undo()` → back in A; `redo()` → back in B; assert `canUndo/canRedo` flip
  correctly at each step; then `createArray()` and assert BOTH buttons disable (barrier cleared
  history). 0 JS errors. (Driving the store is cleaner than synthesizing real HTML5 drags.)

### Undo for accidental DELETES — leverage the existing soft-delete + a restore endpoint (Jun 2026)
Ford: "we need to be able to undo accidental deletes too." Deletes are the scariest accidental
action, and this one was cheap because the delete was ALREADY a soft-delete. Pattern (backend
commit 369b94e + frontend 31eb1ba):
- **Backend already soft-deleted** — `inverter_fleet.delete_array` sets `deleted_at` on the
  Array AND its Inverters at one timestamp (its docstring literally said "NEVER hard-deletes (an
  undo/restore can revive the rows)"). So the inverse already existed in latent form. Added
  `restore_array(db, tenant, array_id)`: clears `deleted_at` on the array and on EXACTLY the
  inverters that shared the array's deletion timestamp (`Inverter.deleted_at == arr.deleted_at`)
  — so inverters removed BEFORE the array was deleted stay removed; no straggler revival.
  Ownership-checked → FleetError→404; not-currently-deleted → 404 (idempotent). Endpoint:
  `POST /v1/array-owners/arrays/{id}/restore` (mirror the delete route's `require_not_demo`).
  Test: delete→restore round-trips the array + its 2 inverters, leaves a pre-deleted inverter
  dead, re-restore 404s. (Test gotcha: `Inverter` rows need `vendor` AND `serial` — both NOT NULL.)
- **Frontend `deleteArray` becomes UNDOABLE instead of a barrier** (fleet-store.js): capture the
  removed array object + its original index BEFORE removal; `pushHistory({undo, redo})` where
  `undo` re-inserts the array LOCALLY at its old index (`splice`) AND revives it server-side via
  `apiPost(.../restore)`, and `redo` re-deletes (local filter + `apiDelete`). The local re-insert
  makes undo feel instant; the restore POST reconciles the backend.
- **Deploy ORDER matters + VERIFY the route is live before the frontend relies on it:** push
  backend first, then POLL until the restore route is actually registered on Railway — a missing
  route and a real "array not found" BOTH return 404, so disambiguate by hitting it with NO auth:
  a registered route reaches the auth layer and returns **401**, a missing route returns 404. Only
  deploy the frontend once the no-auth probe flips 404→401.
- **UX:** soften the delete confirm copy (it's undoable now) and `toast("Deleted … — press ↶ Undo
  (Ctrl/Cmd+Z) to bring it back")` after delete. Verify headless: delete→undo→redo round-trips the
  array, count restored, buttons track, 0 JS errors.
GENERAL LESSON: before building "undo delete" from scratch, CHECK whether the delete is already a
soft-delete (a `deleted_at` column) — if so the data was never destroyed and "undo" is just a
restore endpoint that clears the flag, scoped to the exact rows the delete touched (match by the
shared deletion timestamp). Far cheaper and safer than re-creating destroyed rows.
GENERAL PATTERN for undo/redo on a store-backed canvas: keep history IN the store next to the
mutators (so every entry point — drag, keyboard, programmatic — is covered), record inverses
with stable IDs (not object refs, not deep snapshots), guard re-entrancy, and make
irreversible/structural edits explicit barriers rather than faking their inverse.

## Prospect-funnel audit + pricing disclosure (Jun 2026, commit 9909da4)

Ford: "audit arrayoperator.com as a potential customer." Drive the LIVE site as a cold
prospect with playwright (run from /root/array-operator so `require('playwright')` resolves):
landing → onboarding → connect step; capture console errors, load time, mobile overflow at
390px, and vision-review each screenshot. Walk the funnel READ-ONLY — browse/inspect, don't
create junk accounts or hit payment. The arrayoperator.com root is the APP itself (only 3
pages: index.html = app/dashboard, onboarding.html, login.html) — a cold visitor lands
straight in the live demo dashboard, so the #1 finding is usually top-of-funnel POSITIONING
(no "what is this / who's it for / what's it cost" before the demo), not bugs. Report findings
ranked by severity with the customer's-eye "what would make me bounce" framing.

What shipped to fix the two highest-impact gaps (value-prop + price):
- The anonymous-only `#demoBanner` (toggled by app.js renderFromSession via the [hidden] attr
  on session presence) was a thin strip; REPLACE it with a real value-prop hero (`.hero` =
  headline + benefit subtext + CTA + trust line on the left, a pricing card on the right) PLUS
  a `.db-demostrip` framing the live demo below. No JS change needed — it auto-hides for
  signed-in owners via the existing session toggle. The hero styles are INLINE in index.html's
  `<head>` (not styles.css) — that's where `.demo-banner`/`.db-*` live.
- **PRICING — get the rate from the AUTHORITATIVE billing config, never a nearby number.**
  api/pricing_array_operator.py TIERS is the real SaaS fee: 0.5¢/kWh generated (FULL_UNIT_CENTS
  = 0.50 decimal-cents), graduated discounts 0.45¢ >20k kWh/mo, 0.40¢ >200k; no setup fee;
  14-day no-card trial. DO NOT grab the `RATE = 0.21` / `$0.24/kWh` in sandbox.js — that's the
  ENERGY-VALUE rate (what the owner's power is worth, used for $-at-stake), a COMPLETELY
  different number from the SaaS fee. Showing it as the price would be wrong and a trust killer.
- **PITFALL — I almost shipped a 10×-wrong example.** "$5/mo per 100 kW array" was off by 10×.
  Honest math: monthly kWh = kW × ~0.14 capacity factor × 730 h; ×$0.005 ⇒ a 10 kW HOME array
  ≈ $5/mo, a 100 kW array ≈ $50/mo. When you state an illustrative price, COMPUTE it (run the
  arithmetic in execute_code) against a realistic capacity factor — don't eyeball it.
- Verify headless: stub /v1/* and clear so_session so the hero renders for the anon path; assert
  hero visible + correct rate/example/CTA, badge hugs its text (not full-width — the old
  `.db-badge` flex stretched it; `.hero-main{align-items:flex-start}` fixes it), 0 JS errors, no
  mobile overflow at 390px. THEN vision-review desktop + mobile (selector counts miss
  see-through/stretch issues). Deploy from a clean worktree (concurrent-agent rule above).
GENERAL: when asked to put a customer-facing PRICE on the site, trace it to the Stripe/billing
config module (the one that mints the live price), confirm SaaS-fee vs energy-value, and
compute any "≈ $X/mo for a typical Y" example with real arithmetic before it goes live.

### Demo scare-stat → opportunity framing (Jun 2026, commit 423ecd1)
Ford: "fix that demo scare." The most prominent number on the demo dashboard was a big ALARM-RED
"$48,555/mo AT STAKE" (command-center.js `.fc-risk`), which on a DEMO reads as fear-mongering
("this tool exists to make me anxious"), not value. Fix = REFRAME, don't remove (the number IS
the value prop): copy "at stake" → "recoverable" + a sub-label "by fixing the flagged inverters";
color `--bad` (red) → `--gold2` (the value/money accent). This is honest for REAL owners too —
"recoverable" is the motivating, accurate frame on a live fleet, not just the demo. GENERAL: a
loss/threat KPI that's the product's whole value should be framed as UPSIDE TO CAPTURE (recoverable/
opportunity), not a red bleeding-money alarm — especially in a demo a prospect is sizing up.

### Status tint that double-codes live output → false-alarm on a literal 0 (Jun 2026, commit b66640e)
Ford spotted healthy "All good" inverter cards glowing ORANGE in an evening screenshot. Root cause:
the card tint (`data-tone`) and the output bar both came from live %-of-max
(`current_power_w ÷ nameplate`), and SolarEdge reports `current_power_w: 0` (a LITERAL zero, NOT
null) in the evening when panels aren't generating — so `0/maxW = 0%` → `pctTone(0)` = "bad" →
orange, on a perfectly healthy unit. The existing "idle" guard only caught `null`, so the literal 0
slipped through. WORSE: the 1-second live ticker (`startLiveTicker`) recomputed `pctTone(pct)` from
the seeded `data-curw` and re-applied it to BOTH bar and card every second, so even a render-time
fix would get re-painted orange.
Fix pattern — ONE health-aware source of truth for the tone:
- `outputState(inv, statusCls)` returns `{reporting, pct, tone}` and is used by BOTH `outputBar()`
  and the card-tint (`obTone`). Two rules baked in: (1) a near-zero live reading
  (`curW <= max(25W, 1% of rated)`) = idle/`reporting:false` → tone "idle" (calm, dim, "not
  producing right now"), NEVER orange — kills the nightly false-orange. (2) The orange/red output
  tint fires ONLY when the inverter's HEALTH status is already flagged
  (`tone = statusCls === "ok" ? "ok" : pctTone(pct)`) — a healthy inverter dipping under a cloud
  stays green; real underperformance is caught by the peer-based health status (its own border +
  alert line).
- Apply the SAME logic in the live ticker: read the card's status class
  (`card.classList.contains("bad"|"warn")`) and gate `tone = statusCls==="ok" ? "ok" : pctTone(pct)`
  so the 1s re-paint can't re-introduce the orange.
- Verify against the SHIPPED function (extract `outputState`/`pctTone` with regex, run in node — no
  jsdom needed, it's pure): evening 0W healthy → idle/no-orange; daytime 8% fault → bad/orange;
  healthy daytime 50% dip → ok/green; null power → idle.
GENERAL LESSON: when a single visual signal (a color tint) DOUBLE-CODES two different things — here
"live output level" AND "health status" — a low value on one axis false-alarms even when the other
axis is fine. Decide which axis OWNS the alarm (health status), make the other axis (instantaneous
output) only ADJUST within a non-alarm range or read as a neutral "idle" state, and treat a literal
0 from a live feed as "not producing right now," not "0% = failing." Also: any metric with a
separate live-TICKER re-render path must apply the exact same gating, or the ticker silently undoes
the render-time fix.

### Connect-FIRST funnel — see value before any sign-up (Jun 2026, commit 39eaa98)
Ford: "kill that name and email step ... vendor should go straight to seeing their arrays." Audit
finding: name+email+password were asked BEFORE the prospect picked a vendor or saw any value. The
reorder (not just a field delete): Connect step = JUST the brand picker (removed name/email/password
inputs + their validation gates in `syncConnect` AND the 3 extension-login fns that gated on them);
discover/portal-login fire on CREDENTIALS ALONE. Email moved to the ARRAYS screen — collected AFTER
the owner sees their discovered arrays ("Your email is your login · free 14-day trial, no card"),
with the trial CTA gated on a valid email (`syncClaim`) and the input auto-focused after the reveal.
THE HARD CONSTRAINT you design AROUND (don't fight it): the account still NEEDS an email to persist —
without it they connect, see arrays, close the tab, and nothing's saved. So email can't disappear;
the win is moving it AFTER the value moment. Name died entirely → derive `full_name` from the email
local-part (`ford.genereaux@x → "Ford Genereaux"`) to satisfy the backend's `full_name` min_length=2
(StartRequest in api/onboarding.py); editable later in Master Account. GENERAL friction-reduction
rule for a signup funnel: show value FIRST, ask for identity LAST, and when a required field truly
can't be removed (account identity), RELOCATE it past the value moment rather than gating the whole
flow on it up front. Verify the reordered flow end-to-end headless (mock the `/preview` endpoint to
return demo arrays): connect shows only vendors → discover enables on key alone (no email) → arrays
render → email+CTA gate correctly → derived name works → 0 JS errors.

### Brand unification — product name vs shared-engine name (Jun 2026, commit a95bc17)
Audit found 4 names for one thing (Array Operator / EnergyAgent / NEPOOL Operator /
solar-operator-sync) — a trust wobble, worst at the moment a prospect installs the extension and
sees a DIFFERENT name than the site. Ford's call (and it's architecturally CORRECT): product =
Array Operator everywhere; the EXTENSION stays "EnergyAgent" because it is genuinely SHARED — its
own manifest description says "for NEPOOL/Array Operator", so it powers two products and CANNOT be
"Array Operator" without being wrong for the NEPOOL side. Clean hierarchy: EnergyAgent = company/
engine/helper, Array Operator = product. Fix pattern:
- The real trust leak is INCONSISTENCY, not the EnergyAgent name itself. Renaming everything to
  Array Operator while the extension stays EnergyAgent only works if you BRIDGE the names where
  they meet: onboarding connect copy "It's EnergyAgent, the helper that powers Array Operator" so
  the Chrome-store name is EXPECTED, not a surprise. The "by EnergyAgent" subtitle on each page is
  a FEATURE (signals a real company) — keep it.
- DISTINGUISH product-name slips from legit references before deleting. Most "NEPOOL Operator"
  hits on the AO site were CODE/CSS COMMENTS (invisible — leave them). Of the customer-VISIBLE
  ones: (a) the onboarding "powers NEPOOL Operator" line was pure sister-product leakage → bridge
  to Array Operator; (b) the REC cross-sell "hand you to a <b>NEPOOL Operator</b>" was using NEPOOL
  as a grid-MARKET term but capitalized like the product → reworded to "a REC broker" (keep the
  cross-sell, de-confuse the noun); (c) login.html "That's a NEPOOL Operator account — taking you
  there…" is CORRECT and necessary (accurately names the OTHER product to redirect a misdirected
  user) → keep; (d) the "EnergyAgent helper" references in the connect/auto-refresh UI correctly
  name the extension → keep.
- Verify: grep customer-visible files for the old product name EXCLUDING comments/CSS, confirm
  every page <title> + topbar reads the product name, render onboarding headless to assert the
  bridge line shows and the old leakage is gone, 0 JS errors. The extension stays UNTOUCHED.
GENERAL LESSON: when one Chrome extension / shared backend powers multiple products, it needs a
neutral PARENT/company name, and the product sites must BRIDGE that name ("X — the helper that
powers <Product>") rather than hide it. Don't blanket-rename; separate (1) product-name slips to
fix, (2) shared-engine names to keep + bridge, (3) cross-product references that are correct
(redirect copy) or are generic market terms mis-capitalized to read like a product.

## Liquid-fill inverter card visualization (Ford idea, Jun 2026 — SHIPPED LIVE)

Each inverter card has a bubbling green "liquid energy" fill that rises behind the content to
show production at a glance. STATUS: BUILT + DEPLOYED to arrayoperator.com (commit d04de2e) —
the earlier "mockup phase / write-a-spec-don't-build" framing below is SUPERSEDED; it's live.
The reusable design recipe + the real pitfall:
- **The winning structure:** a `.liquid` layer (`position:absolute;bottom:0;width:100%;z-index:1`,
  `transition:height` so it rises smoothly on data updates) rises the FULL card height behind a
  `.plate` (`z-index:3`, opaque `rgba(11,16,23,.8)+backdrop-filter:blur`) that holds ALL text +
  the sparkline. The plate is what GUARANTEES text/graph never wash out — never let liquid alpha-
  blend over glyphs. Card must be `position:relative;overflow:hidden` so liquid clips to the radius.
- **Fill = capacity factor** = `current_output_w / max_output_w` — the SAME number behind the
  "OUTPUT NOW %", so the liquid REPLACES the thin horizontal progress bar (same data, more glance).
  It does NOT mean "vs expected-for-this-hour" (that needs a model) — confirm the metaphor with Ford.
- **THE FAILURE MODE (caught by vision-review, would've shipped otherwise):** when the frosted
  plate has to hold the WHOLE card's content (name + sparkline + % + of-max + status pill + vendor
  tag), the plate covers nearly the entire card, so the liquid only shows as a bright green RIM
  around the plate edge — and the fill LEVEL (the whole point, the at-a-glance gauge) becomes
  invisible (55% / 34% / 53% cards all looked the same). A liquid+sparkline hybrid fights for the
  same card space and the liquid loses. The three honest forks if this bites: (1) transparent-tint
  plate (re-opens wash-out risk — protect just the sparkline+numbers with small solid backings),
  (2) split the card (sparkline top, liquid tank bottom or a side beaker — cleanest, most legible),
  (3) flood-fill the area UNDER the sparkline curve (the graph becomes the tank — prettiest, finicky).
- **Always carried (same as the %-of-max bar):** keep the numeric % + status pill — color is NOT the
  only signal (~8% of men can't separate green/amber); treat a literal 0/near-zero live reading as
  IDLE (calm), never a 0%-of-max alarm (the zero-vs-null trap above); fault paints amber + freezes
  bubbles. Perf at 40+ inverters: use pure CSS keyframes (GPU-composited, share the compositor
  clock — NOT a per-card rAF), cap bubbles ≤6, `content-visibility:auto` to freeze offscreen cards,
  and `aria-hidden` the decorative liquid. Drop `backdrop-filter:blur` first if it janks.
- A standalone reference mockup lives at `solar-operator/sketches/liquid-cards/`
  (HYBRID-liquid-plus-sparkline.html + INTEGRATION-SPEC.md) — disposable, not shipped.

### When the target UI isn't committed yet + another agent owns the frontend → write a SPEC, don't build
Collision-avoidance move distinct from the "isolate your push" notes (which assume you DO build).
When asked to implement a UI change but (a) the target component isn't committed and (b) another
agent owns that surface: `git log`/search to PROVE the target's absence first, then deliver a
drop-in integration SPEC (framework-agnostic component + CSS + the data field needed + keep/replace
notes + pitfalls) over a colliding build — don't materialize a second component that must merge
against work you can't see. (This session it later flipped to BUILD once the owning agent handed off
the spec + confirmed they weren't in `sandbox.js` — see the SHIPPED decision tree above.)

## The loop

1. **Write a tight task brief to a file**, launch Claude Code in tmux, poll the JSON result:
   ```
   cd /root/array-operator
   tmux new-session -d -s <tag> "claude -p \"$(cat /tmp/task.md)\" \
     --permission-mode acceptEdits --model opus --output-format json \
     > /tmp/result.json 2>/tmp/err.log; echo DONE_$? >> /tmp/err.log"
   ```
   Brief MUST state: NO npm/build, dark sun-mirror tokens only, SAME-ORIGIN relative `/v1/*`
   + `Bearer so_session`, do-not-commit, do-not-touch `/root/solar-operator`, do-not-invent
   endpoints (list the allowed ones), and a self-verify + "report which files changed" demand.
   Poll with a background watcher on `[ -s /tmp/result.json ]` + notify_on_complete.

2. **Hermes verifies — never trust the agent's self-report:**
   - `node --check public/sandbox.js public/app.js` (agent can't run it; it always asks you to).
   - `git log --oneline -1` to confirm the agent did NOT auto-commit (behavior is inconsistent;
     it sometimes commits + bundles dirty WIP under a wrong title). `git status --porcelain`
     to confirm only the expected files changed.
   - **`--max-turns` truncation is the #1 way a big multi-file restructure looks half-broken
     (proven Jun 2026).** A large sandbox.js + index.html + styles.css + layout-removal job blew
     past `--max-turns 80` → JSON `subtype:"error_max_turns"`, exit 1. When that happens the work
     is usually MOSTLY done but committed in a confusing state — DON'T assume it's lost. Confirm
     completeness by GREPPING for the feature's own markers in the file (e.g. `sb-array-details`,
     `ao_array_expanded`, `wireInvToggle`, `origin_links`) rather than trusting `git diff` —
     because (next bullet) the agent may have already committed the big file, so `git diff` shows
     nothing for it. Budget turns generously for multi-file restructures: ~80+ and still it can
     truncate; a single render() rewrite + 3 file edits + a deletion is realistically 80–110 turns.
   - **Mislabeled bundled commit + concurrent-agent fast-forward (the real trap this loop hit).**
     If `git diff` shows the PRIMARY file unchanged but other touched files dirty, the agent
     COMMITTED that file. Check `git log --oneline -4` + `git show 8895915:public/sandbox.js |
     grep -c <your-marker>` to see if your work is inside a commit titled as something ELSE (it
     bundled dirty GMP-meter WIP and titled the whole commit "feat(gmp-meter)" while burying the
     restructure inside it). WORSE: when Ford runs a PARALLEL agent on the SAME repo, `main` /
     `origin/main` can get **fast-forwarded out from under you mid-run** — verify with `git log
     --oneline -1 main` vs the base you branched from, and look for tell-tale branches like
     "merge: consume the other agent sandbox work". Result is an INCONSISTENT half-state on
     origin/main (e.g. new render present but old Layout tabs still loaded). Per the SKILL.md
     multi-agent rule: STOP before any push/deploy, keep your half as a CLEAN LOCAL commit on your
     own branch (don't push), and report the exact split (what's on origin/main vs local-only) so
     Ford decides how to reconcile. Never race a push against the other agent.
   - **Live-proxy playwright QA** (see scripts/ao_live_qa_harness.js): a tiny static server for
     `public/` that PROXIES `/v1/*` → `https://arrayoperator.com` so the page fetches REAL
     fleet data; inject the session via `localStorage.setItem('so_session', <token>)`. Assert
     structure (tab default, column/inverter counts, badges, modal opens, localStorage
     persistence keys set), screenshot fullPage, vision-review the PNG.
     NOTE (Jun 2026): `scripts/ao_live_qa_harness.js` was NOT present on a fresh box — write a
     self-contained harness into the array-operator repo dir (so `require('playwright')` resolves
     from `node_modules`, like the codebase note says), run it, then move it OUT of the repo to
     /tmp before committing so it isn't part of the feature commit. Anonymous (no token) is a
     fine first pass: demo data exercises the origin-links FALLBACK path (PORTAL_URL base links),
     which is exactly what's live until the backend deploys. chromium is cached at
     `/root/.cache/ms-playwright`; `node_modules/playwright` already present in the repo.

3. **Deploy:** `git add <files> && git commit` (Hermes writes the commit, crediting the agent
   in the body), `git push origin main`, then
   `netlify deploy --prod --dir public --site 966cb1f5-944e-41fd-855b-10053edc5d18`
   (MUST use the UUID, not the slug — slug 404s on `deploy`). Confirm the new bundle is live:
   `curl -s https://arrayoperator.com/sandbox.js | grep -c <new-feature-token>`.

4. **Cold-browser E2E against PROD** (not the local proxy) with a fresh token in a brand-new
   browser context (no localStorage) — this is the exact path Bruce hits. Assert the deployed
   bundle renders + token auto-scrubs from URL + 0 JS errors.

5. **Refresh the family launcher** (mint a fresh `_sign_session(str(tenant.id))`, rewrite the
   desktop HTML launcher — see the "Dad/family live-test launcher" note in SKILL.md).

## Pitfalls that cost real time (all confirmed, all avoidable)

- **QA-harness MIME bug (looks like a SITE bug, isn't):** when the static server handles `/`,
  compute the Content-Type from `/index.html`, NOT the bare `/` path — `path.extname("/")` is
  empty → serves HTML as `text/plain` → playwright shows raw HTML source and every selector
  returns 0. If a QA run suddenly reports empty columns when a prior run was fine, suspect the
  harness, not the site.

- **Synthetic drag can't prove a visual move.** HTML5 `DragEvent` + jsdom `DataTransfer`
  reliably fires the handlers but often won't visually reorder/move DOM nodes. Assert the
  PERSISTENCE side-effect instead (e.g. `localStorage.ao_array_order` / `ao_inverter_layout`
  written) + that a Reset control exists. Tell Ford the real-mouse drag is his to confirm.

- **Sticky-nav full-page screenshot artifact (looks like duplicated nav / white band, isn't).**
  `position:sticky` nav + `background-attachment:fixed` body render the nav at its stuck
  position AND leave the html element's default white showing in a fullPage capture. Two
  defenses: (a) set `html{background:#0a0e14}` so overscroll/fullPage never flash white (this
  is a REAL fix — also helps mobile overscroll); (b) when a fullPage shot shows nav mid-page,
  re-shoot VIEWPORT-only and count `panel.querySelectorAll('nav,.brand,footer')` — 0 means the
  DOM is clean and it was just the artifact.

- **fitView floats SHORT/collapsed cards tiny in the middle of a tall canvas (sandbox.js).**
  The pan/zoom `fitView()` (~line 1270) was tuned for the old TALL 3-tier columns; when a
  restructure makes columns SHORT (e.g. collapsible inverter combs, array-as-card), it
  over-shrinks (`Math.min(vw/cw, vh/ch)` goes tiny) AND vertically centers them, so cards look
  broken floating mid-canvas. FIX: cap the zoom tighter (`Math.min(1.15, …)` not `2.2`) and
  TOP-ANCHOR when scaled content is shorter than the viewport: `const y = scaledH < vh-32 ? 56
  : Math.max(8,(vh-scaledH)/2)` (the 56 also clears the legend overlap top-right). Vision-review
  catches this instantly — the structural QA STATS pass while the screenshot looks wrong, so
  ALWAYS vision-review the PNG, never trust selector counts alone for a layout change.

## Diagnosing "why isn't the dashboard showing X" — query live data FIRST, never guess

When Ford asks "why don't most of dad's inverters show <metric>" / "is it broken or a backend
issue?", DON'T speculate — answer with his real fleet data, then explain root cause. Proven
methodology (Jun 2026, the empty-production-bar investigation):

1. **Run build_fleet_tree against his tenant via railway ssh** and tally the metric across all
   inverters, BROKEN OUT BY VENDOR. TENANT = ten_544fd6541eb8405b. His fleet = 62 inverters:
   Fronius 31, SMA 14, SolarEdge 13, Chint 4. Count: have-the-value vs None, by vendor. The
   per-vendor split almost always IS the answer.
2. **Distinguish the two data delivery models — this is the crux:**
   - SolarEdge = LIVE API, polled every dashboard load (`_live_power_w` / `_telemetry_for_site`).
     Its live metrics are always current; nothing persisted.
   - Fronius / SMA / Chint = EXTENSION CAPTURE only (the `{fronius,chint,sma}` `_CAPTURE_VENDORS`
     set, `/v1/array-owners/inverter-capture`). These update ONLY when Ford manually re-runs the
     extension — their `last_power_w`/`last_power_at` are a frozen SNAPSHOT, not a feed. So a
     metric can be "missing" simply because the last capture didn't include it / was stale.
3. **Prove the inverters are actually WORKING (not broken) by reading stored InverterDaily** —
   if they have real daytime kWh (SMA #4 made 124 kWh today, Chint 252/260 kWh), they're healthy
   and the gap is purely a capture/transport issue. ALWAYS make this distinction for Ford: "your
   inverters are fine, it's a data-coverage gap" vs "they're down."
4. **Check freshness, not just presence:** `last_seen_at` (capture recency) vs `last_power_at`
   (did power specifically land). Jun 2026 finding: ALL 49 extension inverters had fresh energy
   (last_seen 3-4h) but ZERO `last_power_at` — capture ran but power never persisted.
5. **The `|| null` collapse pitfall (extension content scripts):** sunnyportal_content.js does
   `current_power_w: liveW || null` — a legitimate 0 / low reading (cloud, brief idle) becomes
   null, so the backend (which only stores `last_power_w` when `site.current_power_w is not None`,
   array_owners.py ~1709) saves nothing and the bar VANISHES instead of showing 0%. The backend
   ingest + CaptureSite.current_power_w schema are already wired end-to-end; the break is on the
   capture side (null power, silently-swallowed live endpoint, or stale snapshot).
6. **A bar/gauge needs BOTH numerator AND denominator:** the %-of-max bar is current_power_w ÷
   nameplate_kw. Jun 2026: 7 SolarEdge + 4 Chint were ALSO missing nameplate_kw, so even a live
   reading couldn't compute a %. Check both fields when a gauge is blank.
7. **GROUND the fix on a daytime HAR per vendor before building (Ford's rule):** the live-power
   endpoints drift (Fronius `/ActualData/GetActualValues`->`TotalPower`; SMA
   `.../overview/<plant>/devices`->`pvPower`; Chint `currentPower`). Ask Ford for a logged-in,
   MID-DAY HAR of each so you can confirm the field is non-null when the sun's up — don't rebuild
   on a guessed/off-peak payload (that's how the silent-null shipped originally).
8. **TRACE THE WHOLE PIPELINE BEFORE PATCHING — the bug is often the LAST INCH (the card render),
   not capture or backend (proven Jun 2026, the Chint "not producing right now" saga).** When
   "cards appear but no live data streams," walk capture → broadcast → ingest → persist → fleet-tree
   → render and find WHERE the value dies, rather than assuming the front of the pipe. This session
   the data was correct ALL THE WAY to `/fleet-tree` (returned `current_power_w=51000`), and the
   card STILL showed "not producing right now" — because `sandbox.js outputState()` required
   `nameplate_kw` to render ANY output (`meaningful = curW != null && maxW != null && …`). **Chint
   reports no nameplate**, so `maxW=null` → it refused to show a real 51 kW reading. The render
   CONFLATED "I can't compute % of max" with "it's not producing." FIX: "producing" needs only a
   real live reading — with nameplate show "% of max", WITHOUT it show absolute "X.X kW · producing
   now" + a full calm bar; never claim "not producing" just because the rated max is unknown. (Also
   audited a backend schema gap in the same hunt — `CaptureInverter` had no `current_power_w` field
   so Pydantic silently DROPPED the per-inverter watts the Chint extension shipped; added the field
   + made ingest PREFER the inverter's own reading over the site-allocation split. Both fixes
   needed; the render one was the visible symptom.)
   GENERAL LESSON: a `%-of-max` / ratio gauge has TWO inputs (live value + rated ceiling). A missing
   DENOMINATOR (nameplate) must degrade to showing the raw numerator, not to "nothing/idle" — losing
   the rated max should never hide a real reading. Whenever a vendor lacks nameplate (Chint), make
   every derived display fall back to the absolute value.

### Diagnostic discipline for a capture bug — turn logging ON, probe prod, NEVER iterate blind (Jun 2026)
The Chint live-power bug took several screenshots to pin ONLY because the extension's diagnostic
flags were OFF (`chint_inject.js DBG_VERBOSE=false`, `chint_content.js CHINT_DEBUG=false`) — the
console showed "loaded + hooks installed" but NOTHING about whether data was observed or what watts
emitted, so we were blind. Per the SKILL.md memory rule (VEC cost 4 blind builds), the move when a
capture symptom is ambiguous:
- **Ship a DIAGNOSABLE build first, don't guess-patch.** Flip the vendor's debug flags ON and add a
  DECISIVE log line at the emit point that dumps each inverter's `serial / power_w / today_kwh /
  status` — so the owner's ONE console screenshot answers "is the real data leaving the extension?"
  definitively. Bump the manifest version so the version number itself confirms the reload took.
- **Then probe PROD directly to find where the value dies** — `railway ssh` running a script that
  reads `Inverter.last_power_w/last_power_at` for the vendor's serials AND runs `build_fleet_tree`
  against the tenant to see what the API actually returns. If the DB + API have the value, the bug
  is purely front-end render (the last-inch case above). Base64-encode the probe script
  (`echo <b64> | base64 -d | python`) — inline `python -c` with f-strings/parens trips the shell
  quoting through `railway ssh`.
- **Watch for DUPLICATE TEST TENANTS:** repeated "Log in with <vendor>" attempts each create a NEW
  array_operator tenant (onboarding mints one per signup), so the prod probe shows the same inverter
  serials under many `ten_*` ids — and the dashboard session may be pointed at an OLD one (pre-fix,
  `last_power_w=None`) while the newest capture wrote correct values elsewhere. When verifying a
  capture fix, confirm WHICH tenant the live session is on, and offer a scoped cleanup of the dup
  test tenants (carve out the live-demo tenant per the SKILL.md bulk-delete rule).

## Instant-graph HISTORY BACKFILL on connect — every vendor, vendor-agnostic backend (Jun 2026)

The per-inverter/array production graphs need ≥2 days of stored data to draw (`arrayGraph`/
`invSpark` return "" / "history building" below that). For extension-capture vendors that only
ship TODAY's kWh, a freshly-connected owner stares at an empty graph for days until the 03:30
snapshot job accumulates history. Fix = pull the few days of history the portal ALREADY exposes
AT CAPTURE TIME so the graph fills the instant they connect. The architecture is intentionally
vendor-agnostic so "make it work for EVERY vendor we support" is just "each content script emits
a `daily[]`":
- **Backend path (built once, shared):** `CaptureSite.daily: list[CaptureDaily]` (`{date, kwh}`)
  in `array_owners.py`; the ingest loop persists each day as a `DailyGeneration` row (idempotent,
  MAX-wins per `(array, day)` so a re-capture never lowers a day and today's row is preserved).
  `inverter_fleet.build_fleet_tree` then surfaces a column-level `daily` via `_array_daily(db,
  array_id, 14)`, and the front-end `arrayGraph(sortedInvs, col.daily)` falls back to that
  array-level series when per-inverter series are sparse. (`CaptureDaily` must be defined BEFORE
  `CaptureSite` in the file — don't reference the later `UtilityMeterDaily`, whose field is
  `generated_kwh` not `kwh`.) Thread `daily` through fleet-store `adaptTree` + `toColumns` or it
  gets dropped between the API and the card.
- **Per-vendor history source (all grounded on existing endpoints — no new HAR needed):**
  - **Chint:** the `site/retrieve` response ALREADY carries `weekETrend[]` (`[{name:"20260610",
    value:"996.2"}, …]`, ~7 days site daily kWh) — zero extra fetch, just parse `name`→ISO date +
    `value`→kWh in `chint_content.js dailyFromTrend`.
  - **Fronius:** reuse the SAME proven `/Chart/GetAnalysisChart` endpoint per day for the last 7
    days (only the date query varies), integrate each device's "Total Power" curve, SUM to one
    site daily kWh (`captureSiteHistory` in `solarweb_content.js`).
  - **SMA:** `POST /measurements/search` with channel `Measurement.Metering.TotWhOut.Pv` at
    `resolution:"OneDay", aggregate:"Dif"` → Wh/day, ÷1000 → kWh (`fetchSiteHistory` in
    `sunnyportal_content.js`; the `postJson` helper already exists).
  - **SolarEdge:** ALREADY has native per-inverter history (live API → `InverterDaily` +
    `_persist_daily_series` + the 03:30 snapshot job) — nothing to add.
- **HONESTY RULE (critical):** backfill at the SITE/ARRAY level only. Chint/Fronius/SMA expose no
  PER-INVERTER history, so NEVER split the array history across inverters — that fabricates fake
  per-inverter trends and fools the peer-comparison engine. So the ARRAY graph fills immediately;
  per-inverter sparklines stay honestly today-only until days accumulate.
- Each history pull is BEST-EFFORT (try/except → []), so a failed/unexpected-shape history fetch
  just leaves the graph to build up naturally; it never blocks the live capture.
- Verify: a `{provider, sites:[{daily:[…]}]}` capture persists each day as `DailyGeneration`
  (incl. a literal 0-output day), re-capture with a higher value max-wins with no dup row, and the
  fleet-tree column `daily` carries the dates. CAVEAT to flag: Chint `weekETrend` is live-verified;
  Fronius/SMA history reuses grounded endpoints but the history-variant RESPONSE SHAPE wasn't
  re-HAR'd — written defensively so worst case is an empty graph, not a wrong one. Leave the
  `*_DEBUG` flags reachable so a `site history backfill: <id> N day(s)` line confirms N>0 live.
GENERAL: when a freshly-connected data source shows an empty time-series, check whether the portal
exposes a short HISTORY window (most do — weekly trend arrays, date-ranged chart calls, OneDay
aggregates) and backfill it on connect through ONE vendor-agnostic ingest field, rather than making
the owner wait for a daily snapshot job. Build the shared persist/render path once; each vendor
only has to emit the series.

## Server-side daylight flag for a night/"Sleeping" UI state — real solar elevation, not a fixed hour (Jun 2026)

The liquid card's calm "Sleeping" night state (and any "is it night?" UI gate) MUST distinguish
"zero output because the sun is down" from "zero output because of a fault" — gate on SUN POSITION
AND zero output, NEVER zero-output alone, or a noon fault that zeroes every inverter gets mislabeled
"asleep" and hides a real outage. Computed ONCE server-side in `inverter_fleet.py` (so 40+ cards
don't each recompute) and exposed as `is_daylight` on every fleet-tree column + in `summary`.
- **No lat/long is stored anywhere** (no Array model column, no adapter supplies one — the
  front-end `col.lat` reads are for the SYNTHETIC demo fleet only). So a precise per-array sunrise
  is impossible today.
- **DON'T use the spec's fixed-hour fallback (`h<5||h>=21`)** — badly wrong seasonally (VT sunrise
  swings ~5:05am Jun → ~7:25am Dec; a fixed cutoff calls a winter 6am "day"). Instead compute REAL
  solar elevation via the dependency-free NOAA algorithm (`_solar_elevation_deg` + `_is_daylight`)
  at a central-Vermont regional default (`_VT_LAT=44.26, _VT_LON=-72.58`), sun "up" above ~-2°
  elevation (a panel still trickles near the horizon). The fns accept per-array `lat`/`lon` so the
  instant a vendor capture supplies coordinates (Chint's site response carries lat/long), pass them
  through for exact-per-site with zero further change.
- Front-end gate: `state = !col.is_daylight && currentW <= 1 ? "sleep" : faultFlag ? "fault" : …`.
  Default `is_daylight` to TRUE on any error/missing (`c.is_daylight !== false`) — never let a
  sun-calc hiccup hide a real card behind a "sleeping" mask.
- Verify seasonally (it's the whole point): Jun local-noon (16:00 UTC) = day, Jun local-midnight
  (04:00 UTC) = night, Dec ~6am EST (11:00 UTC) = NIGHT (the case the fixed-hour rule gets wrong),
  Dec ~noon EST = day. Pure unit test on `_is_daylight(when=…)` + assert the flag is a bool on the
  fleet-tree column + summary.
GENERAL: for any "is it night / should this read as resting" UI gate, compute it SERVER-SIDE from
real solar geometry (NOAA elevation is dependency-free, accurate to a fraction of a degree), not a
hardcoded clock hour, and gate the calm state on (night AND idle), never idle alone.

## Honesty rule for client-side "customization" features

When a feature lets the owner rearrange things (drag inverters between arrays, reorder
columns), it is a VISUAL/organizational layout saved to `localStorage` — it does NOT re-wire
the real hardware→vendor→site mapping on the backend (no reassignment endpoint exists, and a
panel physically belongs to its SolarEdge site). Always: (1) persist to localStorage with an
orphan-safe apply (unknown/new items fall back to their backend array, never lost) + a Reset
control; (2) put a one-line footer note ("Arrangement is saved to this browser"); (3) tell
Ford plainly it's a view, not a backend reassignment, and offer the larger backend-writeback
build only if he actually wants it. He trust-checks — never imply more than it does.

## Observed agent economics (opus, multi-file static-site features)

3-tier sandbox backend+frontend ≈ $3.43 / 38 turns / 8 min. Three-tab restructure ≈
$2.21 / 22 turns. Per-inverter drag + vendor badges ≈ $2.50 / 32 turns. Array-as-main-card
restructure (render() rewrite + collapsible inverters + origin links + Layout-view removal,
4 files) ≈ $6.14 but it TRUNCATED at 80 turns (error_max_turns) — give big restructures 100+
turns. A small additive BACKEND change (origin-link deep links + 1 test) ≈ $0.66 / 16 turns.
Budget ~$2–3.50 and ~6–10 min per multi-file UI feature on opus; double both for a full
render() restructure.
