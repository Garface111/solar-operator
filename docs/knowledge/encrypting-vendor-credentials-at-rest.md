# Encrypting vendor credentials at rest (`SO_CONFIG_KEY`)

**What it protects:** the vendor API keys / OAuth tokens we store for each array —
`InverterConnection.config` (SolarEdge / Fronius / SMA / Chint / Locus) and the
legacy `Array.solaredge_api_key` column. Historically these sat in **plain text**,
so a DB dump leaked *working* credentials. With a key set, a dump yields Fernet
ciphertext instead.

**How it works (one paragraph):** `api/crypto.py` defines two transparent
SQLAlchemy `TypeDecorator`s — `EncryptedJSON` (for `config`) and `EncryptedStr`
(for `solaredge_api_key`). They encrypt on the way to the DB and decrypt on the
way back, so **no call site changes** — every reader still does
`conn.config["api_key"]`. Each stored value is self-describing: ciphertext carries
the `SOENC1:` prefix, plaintext does not, so the table can hold a mix during
migration. The key lives in the `SO_CONFIG_KEY` env var.

> ⚠️ **The one irreversible move:** losing `SO_CONFIG_KEY` after rows are encrypted
> locks every vendor connection out permanently. Treat the key like the database
> password. **Do not set or rotate the prod key without Ford's explicit go.**

---

## The safety property that makes this deployable

When `SO_CONFIG_KEY` is **unset**, the decorators are a **pure pass-through**:
values are stored exactly as before (plaintext JSON / plaintext string) and a
one-time warning is logged. So **merging and deploying this PR changes nothing**
on its own. Encryption only begins when a key is deliberately provisioned. And it
is reversible right up until you delete the key (see Rollback).

---

## Rollout runbook (do these in order)

### 0. Generate a key (keep it secret, store it in the secret manager)
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 1. Deploy the code with **no key set**
Merge + deploy. `SO_CONFIG_KEY` stays unset → pure pass-through, plaintext, a
warning in the logs. Nothing else changes. Verify the app is healthy and captures
still run. *(This is the only step that touches prod without a key — and it's a
no-op by design.)*

### 2. Widen the Postgres column type (no key yet)
On Postgres `config` is still a native `json` column, which would reject a
non-JSON Fernet token. The migration widens it to `text`:
```bash
python -m api.migrate          # idempotent; Postgres-only ALTER, no-op on SQLite
# look for: "~ inverter_connections.config JSON -> TEXT (encryption-at-rest)"
```
This is safe with no key set (plaintext JSON is valid text) and is a prerequisite
for encryption. `Array.solaredge_api_key` is already `text`, so no change there.

### 3. Provision the key on a **non-prod / staging** env and dry-run
Set `SO_CONFIG_KEY=<key>` on staging, then:
```bash
python -m scripts.encrypt_vendor_credentials            # DRY-RUN — reports, writes nothing
```
Confirm the plaintext/encrypted counts look right.

### 4. Encrypt the rows (staging), then live-verify
```bash
SO_CONFIG_KEY=<key> python -m scripts.encrypt_vendor_credentials --apply
SO_CONFIG_KEY=<key> python -m scripts.encrypt_vendor_credentials --verify
```
`--verify` decrypts every connection and hits the real vendor API (read-only),
printing `OK array=… vendor=solaredge` / `vendor=fronius` per connection. **This
is the proof that a real SolarEdge and Fronius capture still decrypt and work.**
Green here = the scheme is sound end-to-end.

### 5. Prod (⚠️ Ford's explicit go required — money/irreversible)
Only after staging is green:
1. Set `SO_CONFIG_KEY` in the prod secret manager.
2. `--apply` to encrypt prod rows.
3. `--verify` against prod and confirm every connection is `OK`.
4. Watch the next scheduled capture cycle and confirm fresh data lands.

---

## Rollback

Encryption is reversible **until the key is deleted**. To undo:
```bash
SO_CONFIG_KEY=<key> python -m scripts.encrypt_vendor_credentials --decrypt --apply
```
This unwraps every row back to plaintext. **Run it before removing
`SO_CONFIG_KEY`** — once the key is gone, encrypted rows are unrecoverable and a
read raises loudly (`Found encrypted vendor credentials but SO_CONFIG_KEY is not
set`). The column type stays `text`; that's harmless and needs no revert.

---

## Key rotation (retiring an old key)

`SO_CONFIG_KEY` accepts a comma-separated list. The **first** key encrypts; **all**
keys are tried for decryption (`MultiFernet`). To rotate:
```bash
# 1. Prepend the new key, keep the old for decryption. Reads keep working.
SO_CONFIG_KEY="<NEW>,<OLD>"
# 2. Re-wrap every encrypted row under the new primary key:
SO_CONFIG_KEY="<NEW>,<OLD>" python -m scripts.encrypt_vendor_credentials --rotate --apply
# 3. Verify, then drop the old key:
SO_CONFIG_KEY="<NEW>" python -m scripts.encrypt_vendor_credentials --verify
```

---

## Files

| File | Role |
|------|------|
| `api/crypto.py` | `EncryptedJSON` / `EncryptedStr` decorators, key loading, `SOENC1:` envelope |
| `api/models.py` | `InverterConnection.config` → `EncryptedJSON`; `Array.solaredge_api_key` → `EncryptedStr` |
| `api/migrate.py` | idempotent Postgres `config` `json` → `text` widening (no key needed) |
| `scripts/encrypt_vendor_credentials.py` | one-time encrypt / decrypt / rotate / live-verify (dry-run default) |
| `tests/test_config_encryption.py` | pass-through, at-rest, mixed-mode, rotation, fail-loud, script, live-verify |

## Threat model (what this does and does not cover)
- **Covers:** a stolen DB dump / read replica / backup — secrets are ciphertext,
  useless without the key (which lives in the env / secret manager, not the DB).
- **Does not cover:** an attacker with app-process memory or the env var (they have
  the key by definition). This is encryption *at rest*, not *in use*. Pair it with
  the usual secret-manager hygiene for `SO_CONFIG_KEY`.
