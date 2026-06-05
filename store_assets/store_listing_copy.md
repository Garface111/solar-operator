# Chrome Web Store Listing — Solar Operator Sync

Copy these fields verbatim into the Web Store Developer Dashboard.

---

## Name (max 45 chars)

`Solar Operator Sync`

## Short description (max 132 chars)

`Automatically updates your community solar reporting spreadsheet every month with fresh Green Mountain Power data.`

## Detailed description

```
Solar Operator Sync is the easiest way for community-solar operators in
Vermont to keep their quarterly and monthly reporting current.

You produce solar. We do the paperwork.

HOW IT WORKS

1. Install the extension.
2. Open your Solar Operator dashboard — the extension pairs to your account automatically.
3. Sign into greenmountainpower.com once.

That's it. Every month, around the 20th, we email you your reporting
spreadsheet — in your exact format — fully populated with the previous
month's kWh production for every one of your arrays.

WHAT WE DO

• Pull your latest GMP bills automatically, on a recurring schedule
• Extract kWh production for each meter
• Write the numbers into the cells of your existing reporting spreadsheet
• Email the updated workbook to you (and any team members you specify)
• Alert you immediately if anything ever breaks

WHAT YOU DO

Visit greenmountainpower.com once every 2-3 weeks. GMP's session token
refreshes when you visit — just opening the homepage while signed in is
enough. We'll remind you if you forget.

WHAT WE NEVER DO

• We never see your GMP password
• We never share your data with third parties
• We never read anything outside the utility portals you've connected (greenmountainpower.com and smarthub.coop)

PRICING

$250 one-time setup. $15/array/month after that. Most operators recoup
the setup cost in the first quarter from time saved on quarterly reporting.
Details at solaroperator.org.

PRIVACY

Full privacy policy: https://solaroperator.org/privacy

You can delete your data at any time by emailing
support@solaroperator.org.

QUESTIONS

support@solaroperator.org — we reply same business day.
```

## Category

`Productivity` (primary) — Workflow & Planning

## Language

`English (United States)`

---

## Permission justifications (Web Store will ask for these)

### `storage`

"We use chrome.storage.local to remember the user's paired account identity
and the most recent captured utility session payload (account list + JWT) so
the extension knows who to sync to and doesn't re-send identical data."

### `alarms`

"We schedule a recurring alarm (every 12 hours) to check whether the
user's GMP session token is within 3 days of expiring. This lets us notify
them in time to refresh."

### `cookies`

"When an operator adds a new client from the Solar Operator dashboard,
the extension clears the existing greenmountainpower.com or smarthub.coop
session cookies so the portal opens to a clean sign-in page for that
client's credentials. We never read cookie values — the permission is used
only to delete them on explicit user-initiated portal-open actions."

### `notifications`

"When the user's GMP token is about to expire, we display a one-time
desktop notification reminding them to visit greenmountainpower.com to
refresh. Without this, their data sync would silently stop."

### Host permission: `https://*.greenmountainpower.com/*`

"The content script reads the user's authenticated GMP session payload
from localStorage on greenmountainpower.com pages and forwards it to our
backend so we can fetch their bills. This is the core functionality of
the extension."

### Host permission: `https://api.greenmountainpower.com/*`

"Reserved for future use. The extension currently makes no API calls to
this host; all GMP API calls happen server-side from our backend using
the captured token."

### Host permission: `https://vermontelectric.smarthub.coop/*` and `https://*.smarthub.coop/*`

"A content script reads authenticated session data from Vermont Electric
Co-op's SmartHub portal the same way it does for GMP — to capture billing
data for operators whose accounts are on VEC rather than GMP."

### Host permission: `https://solaroperator.org/*` and `https://*.solaroperator.org/*`

"A lightweight bridge script runs on the Solar Operator dashboard to
enable automatic account pairing when the operator signs in. The
background service worker also POSTs captured utility session payloads to
api.solaroperator.org so the user's spreadsheet can be updated."

### Host permission: `https://api.solaroperator.org/*`

"The background service worker POSTs captured session payloads to our
backend at this host so the user's account can be synced and their
spreadsheet updated."

### Host permission: `https://web-production-49c83.up.railway.app/*`

"This is the Railway-hosted backend URL used during initial deployment.
It accepts the same requests as api.solaroperator.org and is retained as
a fallback endpoint."

### Remote code: **NO**

"The extension contains no remote-code execution. All JavaScript is
shipped within the extension package."

---

## Single Purpose statement (required)

"Solar Operator Sync has one purpose: to capture an authenticated Green
Mountain Power session from a logged-in user's browser and forward it to
the Solar Operator backend so that the user's community-solar reporting
spreadsheet can be automatically updated each month."

---

## Data Use disclosures (form fields in dev console)

Check the boxes:

- [x] **Authentication information** — yes, we collect a JWT session
      token from greenmountainpower.com
- [x] **Personally identifiable information** — yes, account holder name
      and email (as already shown on GMP profile)
- [x] **Web history** — NO (we only see greenmountainpower.com pages
      while the user is on them, and we don't track navigation)
- [ ] Financial / payment info — NO
- [ ] Health info — NO
- [ ] Personal communications — NO
- [ ] Location — NO
- [ ] User activity — NO
- [ ] Website content — NO

Certifications (must all be true):

- [x] I do not sell or transfer user data to third parties, apart from
      the approved use cases
- [x] I do not use or transfer user data for purposes that are unrelated
      to my item's single purpose
- [x] I do not use or transfer user data to determine creditworthiness or
      for lending purposes
