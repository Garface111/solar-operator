# EnergyAgent Bridge Protocol (v1.3.0)

The `so_bridge.js` content script is the only bidirectional channel
between the EnergyAgent SPA (solaroperator.org) and the extension's
service worker. The SPA never calls `chrome.*` directly; it uses
`window.postMessage` and the bridge forwards to/from
`chrome.runtime.sendMessage` + broadcasts.

All messages are JSON objects with a `type` field. The bridge ignores
any message whose source is not the same `window` (XSS hardening).

────────────────────────────────────────────────────────────────────────
PAGE → BRIDGE (the SPA posts these)
────────────────────────────────────────────────────────────────────────

1.  `SO_OPEN_PORTAL` — open a utility portal in a background tab.
    Already shipped in v1.2.0. Unchanged.
    `{ type, url: string, reqId: string }`
    → ack: `SO_OPEN_PORTAL_ACK { reqId, ok, error? }`

2.  `SO_PAIR` — hand the extension a tenant_key + API endpoint.
    The extension persists them and immediately replies with current
    state so the SPA can show a "paired ✓" badge without polling.
    `{ type, tenantKey: string, endpoint?: string, reqId: string }`
    → ack: `SO_PAIR_ACK { reqId, ok, version, lastSyncAt?, error? }`

3.  `SO_STATUS_REQUEST` — read current extension state.
    Used on SPA mount so we don't depend on broadcast timing.
    `{ type, reqId: string }`
    → ack: `SO_STATUS_ACK { reqId, ok, version, tenantKeySet, lastSyncAt?, lastPayload?, loginState? }`
    where `loginState` mirrors the most recent SO_LOGIN_STATE broadcast.

────────────────────────────────────────────────────────────────────────
BRIDGE → PAGE (broadcasts; the SPA listens)
────────────────────────────────────────────────────────────────────────

A.  `SO_EXTENSION_PRESENT` — fired once on every solaroperator.org page
    load by the bridge. Lets the SPA detect the extension synchronously.
    `{ type, version: string }`

B.  `SO_LOGIN_STATE` — fired by content.js / vec_content.js when they
    detect the user is on a utility portal login screen, signed in, or
    transitioned from one to the other. The bridge forwards every
    occurrence to all solaroperator.org tabs.
    `{ type, provider: "gmp"|"vec", state: "login_required"|"signed_in"|"unknown", url: string, at: string }`

C.  `SO_CAPTURE_LANDED` — fired by background.js right after a
    successful POST /v1/sync. The SPA uses this to auto-advance the
    wizard without polling.
    `{ type, ok: boolean, provider: "gmp"|"vec", accountCount: number,
       at: string, error?: string }`

────────────────────────────────────────────────────────────────────────
EXTENSION INTERNALS
────────────────────────────────────────────────────────────────────────

- background.js owns chrome.storage.local. The bridge forwards
  SO_PAIR / SO_STATUS_REQUEST via chrome.runtime.sendMessage to background.
- background.js broadcasts SO_CAPTURE_LANDED + SO_LOGIN_STATE updates to
  every active solaroperator.org tab via chrome.tabs.query +
  chrome.tabs.sendMessage; so_bridge.js listens on chrome.runtime.onMessage
  and rebroadcasts to its page via window.postMessage.
- content.js (GMP) and vec_content.js (VEC) detect login state by
  inspecting the DOM after a short settle delay (login form vs.
  account widgets) and call chrome.runtime.sendMessage with
  type=LOGIN_STATE_DETECTED — background.js debounces and rebroadcasts.

────────────────────────────────────────────────────────────────────────
INTENT-GATED OPERATIONS (v1.9.109 — security hardening)
────────────────────────────────────────────────────────────────────────

Two page-reachable operations are destructive or credential-bearing and
NO LONGER execute directly off a page postMessage. The page can only
POST AN INTENT; the operation runs when the owner clicks the confirm
card in the extension popup (a user gesture inside extension UI that a
web page — including an XSS'd legit origin — cannot fake). The toolbar
icon shows an octarine badge with the number of approvals waiting.

1.  `SO_VAULT { op:"set", vendor, username, password, reqId }`
    → ack: `SO_VAULT_ACK { reqId, op:"set", ok:false, pending:true,
                           error:"confirm-in-extension" }`
    The background stashes the request AES-encrypted at rest
    (`so_vault_pending`, password never plaintext) and the popup shows
    "Save your <vendor> sign-in?" with the requesting origin + username.
    Confirm = commit to the vault + the usual arming side effects
    (fail-counter reset, live-mode arm). Dismiss = dropped.
    `op:"status" | "clear" | "optout"` are unchanged (status carries no
    secrets; clear/optout are non-credential-bearing).

2.  `SO_WIPE_COOKIES { domain, reqId }`
    → ack: `SO_WIPE_COOKIES_ACK { reqId, ok:false, pending:true,
                                  error:"confirm-in-extension" }`
    Domain must still pass the dot-anchored allowlist
    (greenmountainpower.com / smarthub.coop). The popup shows "Reset
    your <domain> session?"; confirm = wipe cookies + flag
    portal_cleaner's storage wipe + RELOAD any open tab on that domain
    so an already-open portal lands signed-out.

SPA handling: check `pending` on both acks and point the owner at the
toolbar icon ("click the EnergyAgent icon and approve") instead of
assuming the operation completed. The extension POPUP's own requests
(chrome.runtime senders with no tab) still execute immediately.

Popup-internal messages (never relayed by the bridge, rejected from any
non-extension-UI sender): `SO_PENDING_LIST`, `SO_PENDING_CONFIRM
{ kind:"vault"|"wipe", vendor?|domain? }`, `SO_PENDING_DISMISS { … }`.
