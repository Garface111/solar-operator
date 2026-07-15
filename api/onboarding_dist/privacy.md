# Privacy Policy — NEPOOL Operator

*Last updated: July 15, 2026*

## Quick summary

- **We never sell your data.** Your information is used only to run the reporting service.
- **We only read your utility billing data** from the portals you connect.
- **You can delete everything** by emailing admin@solaroperator.org. We purge your data within 24 hours.
- **Two capture paths (your choice):**
  - **Cloud Capture:** portal passwords you opt in to store with us are **encrypted on our servers** so we can sign in and refresh bills around the clock. Remove any login anytime.
  - **On-device (browser extension):** passwords stay **encrypted on your computer** and never reach our servers; capture runs while a signed-in browser session is available.
- **Our only email provider is Resend.com**, which delivers your reports and sign-in links. No other third party sees your data.

---

NEPOOL Operator ("we," "the service") helps community solar operators automate utility-data reporting into NEPOOL-GIS workbooks. This policy describes what we collect, why, and how we protect it.

## What we collect

### Account data

When you create an account we collect your name, email, company name (if provided), and the password you choose for NEPOOL Operator (stored as a one-way hash — we never store it in plain text).

### Utility billing data

Depending on the capture path you choose:

**Cloud Capture (opt-in).** If you save a utility portal username and password in Cloud Capture, we store that password **encrypted at rest** on our servers and use a headless browser on our infrastructure to sign into the utility portal on a schedule, read billing history the same way you would in a browser, and write production figures into your quarterly reports. We never return the stored password to the dashboard or any API response. You can remove a login at any time, which hard-deletes the encrypted credential.

**On-device extension (optional).** If you use the EnergyAgent browser extension instead, it reads your utility session and bill data while you are signed into the utility portal in Chrome. In that mode we never see your utility password — only session material and billing fields needed for reports.

In either path we may collect:

- Utility account name, username, and email address associated with the portal
- A list of utility accounts (meters), including account numbers and nicknames
- Billing history and production (kWh) figures needed for NEPOOL reports

We do **not** collect:

- Browsing history outside connected utility portals
- Data from other websites, tabs, or apps
- Payment card numbers (handled by Stripe; we never see full card data)

## How we use it

We use connected portal access to fetch bill PDFs and billing history so we can build quarterly NEPOOL-format reports and email them to the addresses you specify. Cloud Capture also keeps your bill data fresh without requiring a browser tab.

## How long we keep it

- **Cloud Capture credentials:** until you remove them or close your account.
- **On-device extension sessions:** kept only while valid; utility sessions typically expire on the portal’s own schedule.
- **Account information and billing data:** for as long as your NEPOOL Operator account is active, so historical reports stay available.

## What we never do

- We never sell, rent, or share your data with third parties for advertising.
- We never use your data for any purpose other than running the reporting service.
- We never read data from websites other than the utility portals you connect.

## Deleting your data

Email **admin@solaroperator.org** from the address on your account. We remove sessions, account data, and billing data within 24 hours and confirm by email.

In Cloud Capture, remove a login from Master account → Cloud Capture to delete that credential immediately. In extension mode, use Disconnect in the extension settings to clear the local vault.

## Security

- All data travels over HTTPS.
- Cloud Capture passwords are encrypted at rest with a server-side key; they are decrypted only inside the harvester process to perform a sign-in.
- Extension vault passwords use client-side encryption and never leave your device.
- Payment processing is handled by Stripe.

## Contact

Questions: **admin@solaroperator.org**
