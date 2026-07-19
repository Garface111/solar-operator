# SO_CONFIG_KEY offline escrow checklist

Generated: 2026-07-19T20:33:32.308141+00:00

## Fingerprints (not the key)

- segment[0] sha256_16=8d4364b94cf911ba len=44

## Offline storage (do by hand)

- [ ] Copy current SO_CONFIG_KEY from Railway (web) into a password manager entry
      named `array-operator-SO_CONFIG_KEY` — never into git / chat / Desktop plain text
- [ ] Second copy: printed sealed envelope / hardware token / second vault
- [ ] Record the sha256_16 fingerprints above next to the escrow so you can
      confirm you restored the *right* key without pasting the key into a chat
- [ ] Test restore: decrypt one known SOENC1 blob in a throwaway shell, then wipe

## Rotation dry-run (no apply)

```bash
export SO_CONFIG_KEY=...   # from escrow, not from chat
export DATABASE_PUBLIC_URL=...  # or run inside Railway private net
python -m scripts.vault_key_escrow_and_rotate --fingerprint
python -m scripts.vault_key_escrow_and_rotate --verify-decrypt
```

## Live rotation (apply)

```bash
python -m scripts.vault_key_escrow_and_rotate --mint-rotation
# then follow the printed MultiFernet prepend recipe
```

Losing SO_CONFIG_KEY with no escrow = permanent loss of every portal password
and vendor key in the vault. There is no backdoor.
