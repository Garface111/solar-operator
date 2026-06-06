# REPRO: Second add-login lands on previous customer's dashboard

## Bug description

When an operator uses "Add Client → GMP" twice in a row (e.g. to add two
separate GMP customers), the second click opens a new tab but lands on the
PREVIOUS customer's GMP dashboard instead of a fresh login screen.  The
extension then re-scrapes that customer and the CaptureListener warns
"Looks like you re-captured a client you already have."

The operator has to click "Add Client → GMP" a *third* time to get a clean
login screen.

---

## Root cause

The pre-fix `pick()` in `AddClientByLoginModal.tsx` did:

```
1. window.open(PORTAL_URL, "_blank", "noopener,noreferrer")  ← fires immediately
2. window.postMessage(SO_WIPE_COOKIES)                        ← fire-and-forget
```

Both happen in the same JS turn, but the timeline diverges immediately after:

| Wall-clock | New tab | Extension |
|---|---|---|
| T+0 ms | Navigation to PORTAL_URL starts; SYN packet sent with session cookies | postMessage in flight |
| T+3 ms | DNS resolved | so_bridge.js delivers to background.js service worker |
| T+15 ms | TLS handshake | background.js `chrome.cookies.getAll()` queued |
| T+50 ms | HTTP request arrives at GMP WITH session cookies | background.js starts removing cookies |
| T+80 ms | GMP returns dashboard HTML for existing session | Cookie removal completes |

The portal has already authenticated the operator's existing session before
the cookie wipe finishes. Result: second tab shows the previous customer's
dashboard.

---

## Observed timing (from console logs)

With the fix's diagnostic logs enabled, the pre-fix sequence looks like:

```
[SO 1717000000000] add-login: open about:blank for gmp      ← NEW TAB OPENS (pre-fix: opens PORTAL_URL here)
[SO 1717000000002] add-login: wipe-start for greenmountainpower.com
[SO 1717000000048] wipe-done domain=greenmountainpower.com wiped=7 +46ms  ← background.js
[SO 1717000000049] add-login: wipe-done (+49ms), navigating tab
[SO 1717000000049] add-login: tab.location.href set → https://greenmountainpower.com/account/
```

Pre-fix, the tab navigation started at T+0 and the wipe finished at T+46ms.
The portal's HTTPS request is typically in-flight within 5–20ms of tab open
on a fast connection — well before the 46ms wipe completes.

Post-fix, the tab navigates only at T+49ms (after wipe-done), so the portal
request carries zero session cookies.

---

## Steps to reproduce the bug (pre-fix)

1. Have an existing GMP customer already captured (at least one client in
   the operator's dashboard).
2. Open the Solar Operator dashboard. The extension should be installed and
   paired.
3. Click **+ Add client** → **Green Mountain Power**.
4. In the new tab, sign in as Customer A (or confirm you're already on
   Customer A's dashboard).
5. Come back to the dashboard. Do NOT close the GMP tab.
6. Click **+ Add client** → **Green Mountain Power** again (for Customer B).

**Expected:** New tab shows GMP login screen.  
**Actual (pre-fix):** New tab shows Customer A's GMP dashboard.  The
  extension immediately scrapes Customer A again. The CaptureListener shows
  "Looks like you re-captured a client you already have."

---

## Steps to verify the fix (post-fix)

1. Open Chrome DevTools on the Solar Operator dashboard tab (Console panel).
2. Repeat steps 1–6 above.
3. In the Console, confirm you see these log lines in order:

   ```
   [SO <t0>] add-login: open about:blank for gmp
   [SO <t1>] add-login: wipe-start for greenmountainpower.com
   [SO <t2>] wipe-done domain=greenmountainpower.com wiped=N +Xms  (in background.js service worker console)
   [SO <t3>] add-login: wipe-done (+Xms), navigating tab
   [SO <t4>] add-login: tab.location.href set → https://greenmountainpower.com/account/
   ```

   Crucially: `tab.location.href set` must appear AFTER `wipe-done`.

4. Confirm the new tab opens to the GMP **login** screen (not an
   authenticated dashboard).
5. Sign in as Customer B. Confirm the CaptureListener toasts
   "**Customer B added** — they're on your dashboard." (not a re-capture
   warning).
6. Repeat for a third add-login. Should also land on the login screen.

---

## Automated test coverage added

- `web/app/src/__tests__/wipeCookiesAndWait.test.ts` — unit tests for the
  ack-based wipe helper (resolves on ack, resolves on timeout, ignores
  wrong reqIds, no double-resolve).
- `web/app/src/__tests__/addLoginTwice.test.ts` — integration simulation:
  two consecutive pick() calls, each verifying navigation only happens after
  its own wipe ack; also verifies the fallback timeout path.

Run: `cd web/app && npm test`
