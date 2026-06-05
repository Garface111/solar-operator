# Privacy Policy — Solar Operator Sync

*Last updated: June 4, 2026*

Solar Operator Sync ("we," "the extension") is a Chrome extension that helps
community solar operators automate their utility-data reporting. This policy
describes what we collect, why, and how we protect it.

## What we collect

When you visit greenmountainpower.com or smarthub.coop while signed in,
the extension reads the following from your browser's local storage for
those sites and sends it to the Solar Operator backend
(`https://api.solaroperator.org`):

- Your utility account identifier, username, email, and full name (as
  shown on your GMP or VEC profile)
- The temporary session token (JWT) your browser uses to authenticate to
  the utility portal
- A list of utility accounts (meters) attached to your login, including
  account number, customer number, nickname, and the URL of the current
  bill PDF

The extension does **not** collect:

- Your utility portal password or any other password
- Browsing history outside greenmountainpower.com and smarthub.coop
- Any data from other websites, tabs, or apps
- Payment information
- Personally identifiable information beyond what the utility already
  shows on your account profile

## How we use it

We use your captured session token to fetch your bill PDFs and billing
history from GMP's public API on a recurring schedule. The kWh production
figures we extract are written into the reporting spreadsheet you provided
to us during onboarding. That spreadsheet is then emailed to the address
you specified.

## How long we keep it

- **Session token:** retained only while valid (GMP tokens expire after
  approximately 21 days). Replaced on each fresh capture.
- **Account metadata + billing data:** retained for as long as your Solar
  Operator account is active, so that historical reports remain available.

## What we never do

- We never sell, rent, or share your data with third parties.
- We never use your data for advertising, profiling, or any purpose other
  than operating the reporting service.
- We never read or transmit anything from websites other than the utility
  portals you've connected (greenmountainpower.com and smarthub.coop).

## Deletion

You can delete all of your data at any time by emailing
**support@solaroperator.org** from the address associated with your
account. We will purge your captured sessions, account metadata, and
billing data from our servers within 24 hours and confirm by email.

You may also click **Disconnect** in the extension's settings to remove
your local copy of the captured session immediately; visiting GMP again
will re-capture unless you uninstall the extension entirely.

## Security

- All data is transmitted over HTTPS.
- Our backend stores your data in an encrypted database, accessible only
  to your tenant account.
- We follow standard industry practices for credential rotation and
  vulnerability monitoring.

## Permissions explained

The extension requests these Chrome permissions:

| Permission     | Why                                                    |
|----------------|--------------------------------------------------------|
| `storage`      | To remember your paired account identity and the latest captured utility session locally on your device. |
| `alarms`       | To run a periodic check (every 12 hours) for whether your GMP token is close to expiring, so we can remind you to refresh it. |
| `notifications`| To display the expiry reminder as a desktop notification when your token is within 3 days of expiring. |
| `cookies`      | To clear existing utility portal session cookies when you open the portal for a new client, ensuring a clean sign-in. We never read cookie values. |
| `host_permissions` for `greenmountainpower.com` | So the content script can read GMP's local storage after you sign in. |
| `host_permissions` for `api.greenmountainpower.com` | Future use only — currently the extension makes no API calls to this host. |
| `host_permissions` for `smarthub.coop` | So the content script can read Vermont Electric Co-op session data the same way it does for GMP. |
| `host_permissions` for `solaroperator.org` | So the bridge script can auto-pair your account when you open the Solar Operator dashboard, and so captured data can be delivered to our backend. |
| `host_permissions` for `web-production-49c83.up.railway.app` | Fallback backend URL used during initial deployment; accepts the same requests as api.solaroperator.org. |

## Third-party services

Solar Operator uses the following third-party services:

- **Resend** (resend.com) — we use Resend to send you the monthly
  reporting spreadsheet and account notifications by email. Your email
  address is transmitted to Resend solely to deliver messages to you.
- **Stripe** (stripe.com) — we use Stripe to process subscription
  payments. Your payment information is entered directly on Stripe's
  hosted checkout page; we never receive or store your card details.

## Changes to this policy

If we change this policy, we will email registered Solar Operator
customers and post the updated version at
[https://solaroperator.org/privacy](https://solaroperator.org/privacy).

## Contact

Email **support@solaroperator.org** for any privacy questions or to
exercise your data-deletion rights.
