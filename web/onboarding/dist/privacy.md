# Privacy Policy — Solar Operator

*Last updated: June 4, 2026*

## Quick summary

- **We never sell your data.** Your information is used only to run the reporting service.
- **We only read your utility billing data (Green Mountain Power and Vermont Electric Co-op)** — nothing else from your browser.
- **You can delete everything** by emailing admin@solaroperator.org. We purge your data within 24 hours.
- **Your login session expires automatically** (~21 days) and is replaced each time you log in to your utility portal with the extension active.
- **Our only email provider is Resend.com**, which delivers your reports and sign-in links. No other third party sees your data.

---

Solar Operator ("we," "the extension") is a Chrome extension that helps
community solar operators automate their utility-data reporting. This policy
describes what we collect, why, and how we protect it.

## What we collect

When you visit your utility portal (greenmountainpower.com or
vermontelectric.smarthub.coop) while signed in, the extension reads the
following from your browser's session for that site and sends it to Solar
Operator's servers:

- Your utility account name, username, and email address
- The sign-in session your browser holds with your utility portal. We use
  this to read your bill data the same way you would by clicking around
  the site yourself — we never see your password.
- A list of utility accounts (meters) attached to your login, including
  account number, nickname, and a link to the current bill
- The activation code you paste into the extension's settings page

The extension does **not** collect:

- Your utility password or any other password
- Your browsing history outside your connected utility portals
- Any data from other websites, tabs, or apps
- Payment information

## How we use it

We use your sign-in session to fetch your bill PDFs and billing history
from your utility portals on a regular schedule. The production figures we
extract are written into the quarterly report spreadsheet, which is then
emailed to the address you specified.

## How long we keep it

- **Sign-in session:** kept only while it is valid. Utility portal sessions
  expire after about 21 days; the extension captures a fresh session each
  time you log in.
- **Account information and billing data:** kept for as long as your Solar
  Operator account is active, so that your historical reports stay available.
- **Activation code:** kept until you disconnect or close your account.

## What we never do

- We never sell, rent, or share your data with third parties.
- We never use your data for advertising or any purpose other than
  running the reporting service.
- We never read or send data from any website other than your connected
  utility portals (greenmountainpower.com and vermontelectric.smarthub.coop).

## Deleting your data

You can delete all of your data at any time by emailing
**admin@solaroperator.org** from the address on your account. We will
remove your sessions, account data, and billing data from our servers
within 24 hours and confirm by email.

You can also click **Disconnect** in the extension's settings to remove
the local copy of your session immediately. The extension will capture a
new session next time you log in to your utility portal, unless you
uninstall it first.

## Security

- All data travels over an encrypted connection (HTTPS).
- Our servers store your data in an encrypted database, accessible only
  to your account.
- We follow standard practices for keeping credentials rotated and systems
  monitored.

## Chrome permissions explained

The extension asks for a small set of browser permissions. Here is what
each one does in plain English:

| Permission | What it's for |
|---|---|
| Store data locally | To remember your activation code and the most recent captured session on your device, so you don't have to re-enter them. |
| Run a background check (alarms) | To check every 12 hours whether your utility session is close to expiring, so we can remind you before it does. |
| Show desktop notifications | To display a reminder when your session will expire within 3 days, so you can log into your utility portal and refresh it. |
| Access greenmountainpower.com | So the extension can read your Green Mountain Power sign-in data after you log in. |
| Access vermontelectric.smarthub.coop | So the extension can read your Vermont Electric Co-op sign-in data after you log in. |
| Access api.greenmountainpower.com | Reserved for future use. The extension does not currently make any requests to this address. |
| Access api.solaroperator.org | So the extension can send the captured data to your Solar Operator account. |

## Email delivery

We use Resend.com as our email delivery provider to send your reports and
sign-in links. Resend receives only the information needed to deliver
each email (recipient address and message content).

## Changes to this policy

If we update this policy, we will email registered Solar Operator customers
and post the new version at solaroperator.org/privacy.

## Contact

Email **admin@solaroperator.org** with any privacy questions or to
request deletion of your data.
