# Privacy Policy — EnergyAgent

*Last updated: June 21, 2026*

## Quick summary

- **We never sell your data.** It's used only to run your EnergyAgent reporting and monitoring service.
- **We only read your own utility and solar-inverter data** — nothing else from your browser.
- **We never receive your portal passwords.** Optional auto-login credentials are encrypted and stored **only on your device**.
- **After a capture we clear the portal session cookies** the extension used.
- **You can delete everything** by emailing admin@solaroperator.org — purged within 24 hours.

---

EnergyAgent ("we," "the extension") is a Chrome extension that helps solar-array
owners and community-solar operators bring their utility and inverter data into
one dashboard automatically. It powers Array Operator (arrayoperator.com) and
NEPOOL Operator (nepooloperator.com). This policy describes what we collect, why,
and how we protect it.

## What we collect

When you sign in to one of your own supported portals and click **Connect**, the
extension reads the following from your browser's session for that site and sends
it to your EnergyAgent account:

- **From your utility portal** (Green Mountain Power, or any NISC SmartHub
  utility): your utility account name, username, email, the list of meters on
  your login (account number, nickname, current-bill link), and your billing /
  generation history.
- **From your solar inverter monitoring portal** (SolarEdge, Fronius / Solar.web,
  SMA / Sunny Portal / ennexOS, or Chint / CPS Monitor): your site list and
  per-inverter production data (serial, model, current power, energy today, daily
  history) — the same numbers shown on the portal's own dashboard.
- The activation code you paste into the extension's settings.

To read these, the extension uses **the temporary sign-in session your browser
already holds** with each portal — the same way you would by clicking around the
site yourself.

The extension does **not** collect:

- Your portal passwords. (If you turn on optional auto-login, your username and
  password are encrypted and stored **only on your device** — never sent to our
  servers. See "On-device auto-login" below.)
- Your browsing history outside your connected utility and inverter portals.
- Any data from other websites, tabs, or apps.
- Payment information.

## On-device auto-login (optional)

To keep your live production numbers fresh between visits, you can optionally
save your portal login in the extension. When you do:

- The username and password are **encrypted on your device** and stored in the
  browser's local extension storage.
- They are used **only** to sign in to that portal in your own browser to refresh
  your data.
- They are **never transmitted to EnergyAgent's servers** or anywhere else.
- You can remove them at any time from the extension popup, or switch the feature
  off per-portal.

## How we use it

We use your portal session to fetch your bill PDFs, billing history, and inverter
production on a regular schedule. The figures we extract populate your EnergyAgent
dashboard and the reports we generate for you.

## Cookies

The extension requests the `cookies` permission for one purpose only: **after a
capture, it clears the session cookies for the portal it just read**, so it
doesn't leave a lingering signed-in session in your browser. It never reads the
contents of your cookies and never transmits them.

## How long we keep it

- **Portal sessions:** kept only while valid. Utility/inverter sessions expire on
  their own; the extension captures a fresh session next time you sign in.
- **Account and energy data:** kept while your EnergyAgent account is active, so
  your historical reports stay available.
- **On-device credentials (if you opt in):** kept on your device until you remove
  them or uninstall the extension.

## What we never do

- We never sell, rent, or share your data with third parties (other than our
  email-delivery provider, Resend.com, which receives only what's needed to send
  your reports and sign-in links).
- We never use your data for advertising, creditworthiness, lending, or any
  purpose other than running your reporting and monitoring service.
- We never read or send data from any website other than the utility and inverter
  portals you connect, and your own EnergyAgent account.

## Chrome permissions explained

| Permission | What it's for |
|---|---|
| Store data locally (`storage`) | Remember your activation code, connection state, and — if you opt in — your encrypted on-device auto-login, so you don't re-enter them. |
| Background checks (`alarms`) | Periodically check whether a portal session is near expiry, and schedule re-captures so your live data stays current. |
| Notifications | Remind you when a session is about to expire or a re-capture is needed. |
| Clear cookies (`cookies`) | Clear the portal session cookies the extension used, after a capture. Contents are never read or sent. |
| Inject capture script (`scripting`) | Read your data from a portal tab when you explicitly click Connect for that vendor. |
| Access your utility portals (greenmountainpower.com, *.smarthub.coop) | Read your own utility billing/generation data after you sign in. |
| Access your inverter portals (solaredge.com, solarweb.com, sunnyportal.com, sma.energy, chintpower.com, chintpowersystems.com) | Read your own solar production data from whichever portal your inverter brand uses. |
| Access EnergyAgent (nepooloperator.com, arrayoperator.com, solaroperator.org) | Send the captured data to your own EnergyAgent account and coordinate the in-page Connect handoff. |

## Security

- All data travels over an encrypted connection (HTTPS).
- Our servers store your data in an encrypted database accessible only to your
  account.
- On-device credentials are encrypted before storage.

## Deleting your data

Email **admin@solaroperator.org** from the address on your account; we remove your
sessions, account data, and energy data within 24 hours and confirm by email. You
can also remove the local session and any saved credentials instantly from the
extension popup.

## Changes to this policy

If we update this policy we will post the new version at the same URL and, for
material changes, email registered customers.

## Contact

Email **admin@solaroperator.org** with any privacy questions or deletion requests.
