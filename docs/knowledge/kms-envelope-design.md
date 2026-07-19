# T1-3 design: AWS KMS envelope encryption (not built yet)

**Status:** design only — needs Ford $1–3/mo approval before provisioning.

## Goal
Remove `SO_CONFIG_KEY` from process env entirely. Root key never leaves KMS HSM.
Per-credential data keys with encryption context bound to `tenant_id`.

## Shape
1. On **encrypt** (web collect path):
   - `kms:GenerateDataKey` → plaintext DEK + ciphertext blob
   - Fernet(DEK).encrypt(password)
   - Store `SOENC2:<b64(wrapped_dek)>:<b64(ciphertext)>` + optional `tenant_id` AAD
2. On **decrypt** (harvester only):
   - `kms:Decrypt` wrapped DEK with encryption context `{tenant_id, purpose: portal_vault}`
   - Fernet decrypt payload
3. **web** IAM: `kms:GenerateDataKey` only (encrypt capability)
4. **harvester** IAM: `kms:Decrypt` only
5. Migrate: script re-wraps SOENC1 → SOENC2 offline; dual-read during rollout

## Cost
- ~$1/mo customer-managed key + negligible API calls at our volume
- Requires AWS account + IAM roles on Railway (or Workload Identity)

## Why not now
Split-key (`SO_VAULT_DECRYPT=0` on web) already removes public-API decrypt blast radius.
KMS is the next step once offline escrow + TCP lock-down are done.
