# Privacy Policy — Solar Operator Sync

*Last updated: June 1, 2026*

Solar Operator Sync ("we," "the extension") is a Chrome extension that helps
community solar operators automate their utility-data reporting. This policy
describes what we collect, why, and how we protect it.

## What we collect

When you visit greenmountainpower.com while signed in, the extension reads
the following from your browser's local storage for that site and sends it
to the Solar Operator backend (`https://api.solaroperator.com`):

- Your Green Mountain Power account identifier, username, email, and full
  name (as shown on your GMP profile)
- The temporary sign-in session your browser holds with Green Mountain
  Power. We use this to read your bill data in the same way you would by
  clicking around the GMP site yourself.
- A list of utility accounts (meters) attached to your GMP login,
  including account number, customer number, nickname, and the URL of the
  current bill PDF
- The "activation code" you paste into the extension's options page

The extension does **not** collect:

- Your Green Mountain Power password or any other password
- Browsing history outside greenmountainpower.com
- Any data from other websites, tabs, or apps
- Payment information
- Personally identifiable information beyond what GMP already shows on your
  account profile

## How we use it

We use your captured sign-in session to fetch your bill PDFs and billing
history from Green Mountain Power on a recurring schedule. The kWh production
figures we extract are written into the reporting spreadsheet you provided
to us during onboarding. That spreadsheet is then emailed to the address
you specified.

## How long we keep it

- **Sign-in session:** retained only while valid (Green Mountain Power
  sessions expire after approximately 21 days). Replaced on each fresh
  capture.
- **Account metadata + billing data:** retained for as long as your Solar
  Operator account is active, so that historical reports remain available.
- **Activation code:** retained until you disconnect or close your account.

## What we never do

- We never sell, rent, or share your data with third parties.
- We never use your data for advertising, profiling, or any purpose other
  than operating the reporting service.
- We never read or transmit anything from websites other than
  greenmountainpower.com.

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
| `storage`      | To remember your activation code and the latest captured session locally on your device. |
| `alarms`       | To run a periodic check (every 12 hours) for whether your GMP token is close to expiring, so we can remind you to refresh it. |
| `notifications`| To display the expiry reminder as a desktop notification when your token is within 3 days of expiring. |
| `host_permissions` for `greenmountainpower.com` | So the extension can read your GMP sign-in data after you log in. |
| `host_permissions` for `api.greenmountainpower.com` | Reserved for future use — the extension currently makes no requests to this host. |
| `host_permissions` for `api.solaroperator.com` | So we can deliver captured data to your Solar Operator workspace. |

## Changes to this policy

We use Resend.com as our third-party email delivery provider to send
reports and sign-in links on your behalf.

If we change this policy, we will email registered Solar Operator
customers and post the updated version at
[https://solaroperator.org/privacy](https://solaroperator.org/privacy).

## Contact

Email **support@solaroperator.org** for any privacy questions or to
exercise your data-deletion rights.
