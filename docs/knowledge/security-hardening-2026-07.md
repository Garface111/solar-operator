# Security hardening (Jul 2026)

What shipped after the architecture security audit. Pair with
`encrypting-vendor-credentials-at-rest.md`.

## Changes

1. **Utility session tokens encrypted at rest**  
   `UtilitySession.api_token`, `refresh_token`, and `raw_payload` use
   `EncryptedStr` / `EncryptedJSON` (same `SO_CONFIG_KEY` Fernet envelope as
   vendor keys). A DB dump no longer yields live GMP/SmartHub JWTs in the clear
   once rows are migrated.

2. **Encryption migration targets expanded**  
   `scripts/encrypt_vendor_credentials.py` also covers:
   - `utility_sessions.{api_token,refresh_token,raw_payload}`
   - `portal_credentials.{secret_enc,session_state_enc}`  
   Missing tables are skipped (older DBs / partial test schemas).

3. **Public API docs dark in production**  
   On Railway (or `SO_DISABLE_API_DOCS=1`), FastAPI serves
   `docs_url=redoc_url=openapi_url=None`. Local dev still has Swagger.

4. **Admin tenant list no longer bulk-leaks `tenant_key`**  
   `GET /admin/tenants` returns `tenant_key_last4` only. Full key only on
   create / regen.

5. **Session epoch (logout-all on password change)**  
   `Tenant.session_epoch` is embedded in session tokens as `se`. Password
   change and activation-key regen bump the epoch; older sessions get 401.
   `set-password` returns a fresh `session_token` so the acting client stays in.

6. **Cloud Capture password-save rate limit**  
   20 saves / hour / tenant.

7. **Sentry scrub list expanded**  
   `api_token`, admin/seed/maint headers, `secret_enc`, etc.

8. **`/health` flags**  
   `encryption_at_rest` (bool: key set) and `api_docs_public`.

## Prod rollout (order)

```bash
# 1. Deploy this code (pass-through until key already set — key IS set on prod)
git push origin main   # Railway auto-deploys

# 2. Migrate (session_epoch column + JSON→TEXT for raw_payload / session_state)
railway ssh --service web "cd /app && python -m api.migrate"

# 3. Encrypt any remaining plaintext rows (dry-run first)
railway ssh --service web "cd /app && python -m scripts.encrypt_vendor_credentials"
railway ssh --service web "cd /app && python -m scripts.encrypt_vendor_credentials --apply"

# 4. Confirm
curl -s https://web-production-49c83.up.railway.app/health | jq .
# encryption_at_rest:true, api_docs_public:false
curl -sI https://web-production-49c83.up.railway.app/docs | head -1   # expect 404
```

## Still open (not in this PR)

- Hash `tenant_key` at rest (needs extension re-auth UX)
- HttpOnly cookie sessions / shorter TTL
- CSP nonces (drop `unsafe-inline`)
- Redis rate limits
- Stripe pk/sk live/test alignment (ops config)
- Rotate secrets that may have appeared in agent tool logs
