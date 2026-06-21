# Notification-email link integrity & the notify.py email family (Jun 2026)

Operator/customer-facing emails live in `solar-operator/api/notify.py`. They are
sent via `_send_via_resend(...)` and skinned by `render_email_skin` /
`render_email_skin_text`. Each has an HTML body, a text body, and a `cta`
{label,url} button. This is where a customer who "clicked the link and it didn't
work" is debugging.

## 1. "The login link in the email doesn't work" — verify the URL, don't trust the string
A customer (Bruce) forwarded the auto "reconnect your GMP account" email saying
the GMP login link at the bottom didn't work. ROOT CAUSE: `send_gmp_reauth_needed_email`
hardcoded `gmp_url = "https://mypower.greenmountainpower.com/"` — a subdomain that
DOES NOT RESOLVE. Both the green CTA button AND the inline link used that one
`gmp_url`, so both were dead.

DIAGNOSE FAST (do this before editing anything):
- curl every candidate URL and read the status:
  `for u in URL1 URL2 …; do echo "$(curl -s -o /dev/null -w '%{http_code}' -L --max-time 20 "$u")  $u"; done`
  `000` = DNS/connection failure (the URL is bogus); `200` = live.
- Find the CORRECT URL the rest of the codebase already uses rather than guessing
  a new one. The extension + other emails use `https://greenmountainpower.com/account/login/`
  (verified 200). RULE: a notification's link should point where the EXTENSION
  watches for the capture (so the user's login actually refreshes the session),
  which is the same URL `content.js` runs on — not an invented vanity subdomain.
- FIX = one-line change to `gmp_url`; both the button and inline link inherit it.
  No DB change, no migrate — `git push origin HEAD:main` auto-deploys (Railway).
- LESSON / bug class: any hardcoded external URL in an outbound email can silently
  rot (vendor retires a subdomain, marketing renames a path). When a customer says
  "the link didn't work," the first move is curl the literal URL from the code, not
  re-explain the flow. Grep `notify.py` for other hardcoded vendor URLs and curl
  them when auditing.

## 2. The "reconnect GMP" email is correct in INTENT — the dead link was the only bug
`send_gmp_reauth_needed_email` fires when we can't auto-refresh a tenant's GMP
session (revoked, e.g. password change). The remedy IS for the owner to log into
GMP once so the extension captures a fresh session. So the email should be sent —
it was only the link that was broken. When fixing a broken-link complaint, don't
suppress the notification; fix the link and (if asked) RE-SEND the corrected one.

## 3. Re-sending a corrected email by hand — call the real function, set the key
To prove a fixed email actually goes out (not just unit-pass), call the real
sender in-process with the Resend key exported, and confirm it returns True +
`_send_via_resend._last_error is None`:
- Local env has NO `RESEND_API_KEY` by default → the function logs and returns
  False ("RESEND_API_KEY not set — logging email instead of sending"). That's a
  no-op, NOT a failure. Export the key from the stored secret first:
  `RESEND_API_KEY` from `~/.hermes/secrets/resend_full_key` (read it INSIDE a
  python snippet or write key+cmd to a .sh file — the masker mangles inline
  `$(cat …)`; see ao-deploy ref §2).
- Then:
  ```python
  from api.notify import send_gmp_reauth_needed_email, _send_via_resend
  ok = send_gmp_reauth_needed_email(to='bruce.genereaux@gmail.com', name='Bruce Genereaux')
  print('SENT_OK' if ok else 'SEND_FAILED', getattr(_send_via_resend,'_last_error',None))
  ```
- The FROM resolves via `branding.from_address(product)` (default product
  "nepool" → `NEPOOL Operator <hello@nepooloperator.com>`). NEVER hardcode a From;
  branding domain status drifts (see resend-email skill). Note the original email
  Ford showed came from `admin@nepooloperator.com` while branding resolves
  `hello@nepooloperator.com` — both are the same verified domain so delivery is
  fine, but flag the cosmetic mismatch if asked to standardize the sender address.

## 4. Email-family conventions worth keeping (when adding a new notify.py email)
- Build BOTH `render_email_skin` (html) and `render_email_skin_text` (text);
  pass the SAME `cta={label,url}` to both so the button + plaintext link agree.
- Send through `_send_via_resend(to, subject, html, text, product=…)` — it sets
  `from` via branding and always sets a `reply_to` to the monitored support inbox
  so brand-domain From never strands a reply.
- Prove a NEW email feature by rendering the real builder + sending ONE real email
  to Ford and confirming `last_event: delivered` (resend-email skill curl path),
  not just unit tests (mirrors morning-fleet-digest §9 of capture/billing ref).
