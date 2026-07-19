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

## Cloud Capture vault hardening (2026-07-18/19)

Shipped on main (`4a56fa5d`, `7f200a9c`, follow-ups):

| Item | Status |
|------|--------|
| T0-1 churn: `_tenant_allowed` requires `Tenant.active`; vault teardown on cancel/stripe-delete/succession | Done (code) |
| T0-1 residual: inactive tenant still held 1 enabled vault row | **Purged 2026-07-19** (test tenant `ten_47d849…`, 2793 harvest_run + 1 credential) |
| T0-2 desk auth binds `tenant_id` allowlist (not editable email) | Done |
| T0-3 email change confirmation link | Done |
| T0-4 due-scan never selects `secret_enc` | Done |
| T0-5 rotate `ADMIN_API_KEY` (leaked in agent transcript) | **Done 2026-07-19** (web+worker; old key 403s) |
| T0-6 `POST /v1/account/delete` + script vault omit fix | Done |
| T1-1 `SO_VAULT_DECRYPT=0` on web/worker; `=1` on harvester only | Done (env) |
| T1-4 `RUN_SCHEDULER` defaults off | Done |
| T2-3 Sentry locals scrub / include_local_variables=False | Done |
| T2-4 remove decrypt-apply HTTP mode | Done |
| T2-11 Sovereign ops flags fail-closed in code **and** prod env | **Prod set to 0 on 2026-07-19** |
| cloud-capture `/status` load_only (no vault decrypt on web) | Done (`7f200a9c`) |
| Decrypt audit log + volume anomaly warn | Done (contextvars + threshold) |

**T0-0 live counts (2026-07-19 post-purge):**  
`portal_credential` total=24, all enabled, **0** on inactive tenants, all have `secret_enc`.

## Still open

- **T1-2** Postgres TCP proxy still public (`zephyr.proxy.rlwy.net:54704`). Ciphertext mitigates dump risk; closing needs a replacement ops path (Railway private networking / working `railway ssh`). Do not close until agents can run read-only probes another way.
- **T1-3** AWS KMS envelope encryption (~$1–3/mo) — money gate; ask Ford.
- **T2-1** customer-visible “we signed into your portal N times” surface — detective log exists; product UI not built.
- **T2-7** key escrow offline copy + dry-run prod rotation (never exercised against live vault).
- Hash `tenant_key` at rest (needs extension re-auth UX)
- HttpOnly cookie sessions / shorter TTL
- CSP nonces (drop `unsafe-inline`)
- Redis rate limits
- Stripe pk/sk live/test alignment (ops config)
- MFA on operator accounts (Tier 3 / money)

## Prod encrypt status (2026-07-11)

Bulk encrypt APPLY completed against prod Postgres via public URL + SO_CONFIG_KEY:

| target | result |
|--------|--------|
| inverter_connections.config | 72/72 already encrypted |
| arrays.solaredge_api_key | 68/68 already encrypted |
| utility_sessions.api_token | 42 encrypted (0 plain left) |
| utility_sessions.refresh_token | 20 encrypted |
| utility_sessions.raw_payload | 41 encrypted |
| portal_credential.secret_enc | 5/5 already encrypted |
| portal_credential.session_state_enc | 5/5 already encrypted |

Note: table is `portal_credential` (singular). Encrypt script fixed to match.
Also: `main()` builds a fresh engine from `DATABASE_URL` so local ops against
`DATABASE_PUBLIC_URL` actually write (api.db.engine was still on railway.internal).
