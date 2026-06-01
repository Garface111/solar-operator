# Solar Operator Sync — Chrome Extension

A Manifest V3 Chrome extension that captures the user's utility-portal session
(currently Green Mountain Power) and forwards it to the Solar Operator API so
quarterly investor reports can be drafted automatically.

## What it does

When the user logs into greenmountainpower.com, the content script reads the
`gmp-vue` entry from `localStorage`, extracts the API token + account map (all
arrays under the login, with direct bill-PDF URLs), and POSTs it to the
Solar Operator backend. The user never sees DevTools, never copies JSON.

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

- `api_endpoint` — defaults to `https://api.solar-operator.com/v1/sync`
- `tenant_key`   — issued per customer from the Solar Operator admin dashboard

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

## Next utilities to add

Each adapter is `manifest.json` host_permissions + a `content.js` block. The
shape of `accounts[]` is provider-agnostic, so the backend stays the same.

Targets (ranked by addressable market):
1. National Grid (NY + MA) — same MyAccount pattern
2. Eversource (CT + MA + NH)
3. PSE&G (NJ)
4. BGE (MD)

For each, the discovery work mirrors what we did for GMP: log in, watch the
network tab for the post-login API call that returns account list, find where
the SPA stores the bearer.

## Known limitations

- The extension only runs while the GMP tab is open. If the user never visits
  greenmountainpower.com, the backend gets no fresh tokens.
- JWT lifetime is ~21 days; the user must log in at least that often. The
  background alarm pops a Chrome notification 3 days before expiry.
- Cloudflare Turnstile sits in front of the login form — the extension cannot
  bypass it. It only activates once the user is already past Turnstile, which
  is fine because we're not trying to automate the login itself.
