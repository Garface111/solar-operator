# EnergyAgent Sync — Chrome Extension

A Manifest V3 Chrome extension that captures the user's utility-portal session
(Green Mountain Power and any NISC SmartHub utility) and forwards it to the
EnergyAgent API so quarterly investor reports can be drafted automatically.

## Design

![EnergyAgent icon](icons/icon128.png)

Cream background (`#FAF8F5`), emerald-700 action button, a single wood-300 gold
hairline under the header strip, zinc body text. The popup shows connection
status, last capture timestamp, today's capture count, and a one-click link to
the EnergyAgent dashboard.

## What it does

When the user logs into greenmountainpower.com, the content script reads the
`gmp-vue` entry from `localStorage`, extracts the API token + account map (all
arrays under the login, with direct bill-PDF URLs), and POSTs it to the
EnergyAgent backend. The user never sees DevTools, never copies JSON.

While the API isn't live yet, the extension runs in **local-only mode** — it
captures the payload to `chrome.storage.local` and shows it in the popup. Set a
tenant key in Options to enable upstream sync.

## Install (developer mode)

1. Open `chrome://extensions/`
2. Toggle "Developer mode" on (top right)
3. Click "Load unpacked"
4. Select the `extension/` directory
5. Pin the icon to the toolbar
6. Visit https://greenmountainpower.com/ and log in normally
7. Click the extension icon — you should see "Captured locally" with the
   account count and token expiry

## Files

```
extension/
├── manifest.json          # MV3 manifest
├── background.js          # Service worker — receives captures, POSTs to API,
│                          # schedules token-expiry notifications
├── content.js             # Runs on greenmountainpower.com; reads localStorage
├── popup/
│   ├── popup.html         # Toolbar popup (status + last sync)
│   └── popup.js
├── options/
│   ├── options.html       # Settings: API endpoint + tenant key
│   └── options.js
└── icons/                 # 16/48/128 px solar icons
```

## Configuration

Two settings live in `chrome.storage.local`:

- `api_endpoint` — defaults to `https://web-production-49c83.up.railway.app/v1/sync`
- `tenant_key`   — issued per customer from the EnergyAgent admin dashboard

Without a tenant key the extension is capture-only (no network requests
besides what the user already makes to greenmountainpower.com). With a tenant
key, every fresh capture POSTs to the endpoint with `Authorization: Bearer …`.

## API contract (what the backend receives)

```json
{
  "provider": "gmp",
  "capturedAt": "2026-05-29T20:14:33.512Z",
  "pageUrl": "https://greenmountainpower.com/account/billing/",
  "user": {
    "accountId": "6ciDwNMr7HusvwpibhRDXK",
    "username": "GMCSolar",
    "email": "solar@gmcommunitysolar.com",
    "fullName": "Bruce Genereaux"
  },
  "auth": {
    "apiToken": "eyJzdHQi…",
    "apiTokenExpires": "2026-06-18T16:19:34.641Z",
    "refreshToken": "LtR35…"
  },
  "accounts": [
    {
      "accountNumber": "2778764040",
      "nickname": "Tannery Brook",
      "customerNumber": "3035654512",
      "currentBillUrl": "https://document.utilitec.net/GDCNDN/Wwn8ABPT3B/GMP/…",
      "currentBillUrlBinary": "https://document.utilitec.net/GDCNDNB/…",
      "serviceAddress": { "street1": "1035 SCOTT HWY SOLAR", "city": "Groton", … },
      "solarNetMeter": true,
      "groupNetMetered": true,
      "isPrimary": true
    },
    …
  ]
}
```

The backend's job: persist this, then use the `currentBillUrl` values to pull
each PDF (same flow as `pull_bills.py` from the
`green-mountain-solar-quarterly` skill), parse with `extract_bills.py`, write
to the tenant's master spreadsheet, draft the report.

## Security notes

- The captured JWT grants full access to the user's GMP account. Treat the
  endpoint as a high-value target: TLS only, log access, rotate tenant keys.
- The extension stores the payload in `chrome.storage.local`, which is sandboxed
  per-extension but readable by anyone with local OS access. Acceptable for
  dev mode; for production, consider clearing local copies after successful
  upstream sync.
- The token deduper (`tokenHash`) prevents resending an unchanged token, so
  every page-load doesn't hammer the API.

## Known limitations

- The extension only runs while the GMP tab is open. If the user never visits
  greenmountainpower.com, the backend gets no fresh tokens.
- JWT lifetime is ~21 days; the user must log in at least that often. The
  background alarm pops a Chrome notification 3 days before expiry.
- Cloudflare Turnstile sits in front of the login form — the extension cannot
  bypass it. It only activates once the user is already past Turnstile, which
  is fine because we're not trying to automate the login itself.

## Vault security posture (encryption-at-rest honesty)

`vault.js` stores portal passwords AES-256-GCM encrypted in
`chrome.storage.local` — but the AES key is generated per-install and persisted
in the SAME store (`so_vault_key` beside `so_vault_creds`). **That makes the
at-rest layer obfuscation, not real encryption**: an attacker who can read the
extension's storage from disk gets both the key and the ciphertext.

This is a deliberate, documented MV3 limitation, not an oversight:

- Chrome extensions have **no OS-keychain access** (no DPAPI / macOS Keychain /
  libsecret surface), so there is nowhere non-colocated to root a key.
- Every derivation input available to us (extension id, install-time salt)
  lives on the same disk with the same readability — indirection, not defense.
- A non-extractable `CryptoKey` persisted in IndexedDB would prevent JS-context
  key export and split the stores, but Chrome still writes the key material into
  the profile directory (a disk attacker still wins), and extension-SW IndexedDB
  can be **evicted under storage pressure** — an evicted key silently destroys
  every saved login and with it the flagship "password once, never sign in
  again" feature. We judged that marginal gain not worth that real risk.
- A user-supplied master password would be real encryption but defeats
  hands-off auto-login entirely.

What the AES layer genuinely provides: no plaintext passwords in storage dumps,
logs, or exports, and a leak of the credential blob alone (without the key
record) is useless. What it cannot provide: protection against full local
profile access — an attacker in that position already owns the live portal
session cookies anyway. Credentials still never leave the machine; nothing is
ever sent to EnergyAgent servers.

## host_permissions rationale (why the wildcards stay)

Reviewed 2026-07-02 (security lane). The manifest's host patterns look broad
but each wildcard is load-bearing — narrowing them breaks captures or
auto-login:

- `https://*.smarthub.coop/*` — SmartHub is a multi-tenant platform hosting
  ~500 co-op subdomains (see `smarthub_registry.js`, generated), and the
  extension supports **discovered** co-ops (`sh_<subdomain>` codes) that are not
  in the registry yet. Enumerating hosts would cap coverage at the registry
  snapshot and require a Web Store re-review for every new co-op.
- `https://*.fronius.com/*`, `https://*.sma.energy/*` — a lapsed dashboard
  session redirects to the vendor's SSO IdP on a *different subdomain*
  (`login.fronius.com`, `auth.fronius.com`, `login.sma.energy`); auto-login
  must `executeScript` there, and the IdP host has changed before. The
  `_LOGIN_HOSTS` matcher deliberately accepts any host on these vendor-owned
  apex domains (dot-anchored against lookalikes).
- `https://*.solarweb.com/*`, `https://*.sunnyportal.com/*`,
  `https://*.chintpower.com/*`, `https://*.chintpowersystems.com/*`,
  `https://*.greenmountainpower.com/*` — vendor/utility-owned single-purpose
  domains whose dashboards call sibling API subdomains (e.g.
  `uiapi.sunnyportal.com`, `api.greenmountainpower.com`); the wildcard is
  within a domain the vendor owns, not across third parties.
- App origins (`*.nepooloperator.com`, `*.arrayoperator.com`,
  `*.solaroperator.org`, Railway) — the bridge content script matches wildcard
  subdomains but is **inert off the real app origins** (`so_bridge.js`
  `ALLOWED_ORIGINS` origin lock), so a rogue subdomain cannot drive the
  protocol even though the manifest matches it.

Cookie-wipe blast radius is bounded separately: `SO_WIPE_COOKIES` has its own
dot-anchored domain allowlist (greenmountainpower.com / smarthub.coop only) and
since v1.9.109 requires an in-popup confirmation when requested by a page.
