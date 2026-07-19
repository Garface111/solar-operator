#!/usr/bin/env python3
"""Vault key escrow + MultiFernet rotation dry-run (T2-7).

NEVER prints SO_CONFIG_KEY or any decrypted secret. Safe defaults:

  # Key fingerprints only (local env must have SO_CONFIG_KEY)
  python -m scripts.vault_key_escrow_and_rotate --fingerprint

  # Prove every portal_credential.secret_enc decrypts with the current key
  # (uses DATABASE_PUBLIC_URL or DATABASE_URL; no writes)
  python -m scripts.vault_key_escrow_and_rotate --verify-decrypt

  # Generate a NEW Fernet key and print the MultiFernet prepend recipe
  # (does NOT apply; does NOT print the existing key)
  python -m scripts.vault_key_escrow_and_rotate --mint-rotation

  # Write an offline escrow checklist to a path (no secrets in the file)
  python -m scripts.vault_key_escrow_and_rotate --write-escrow-checklist /path/to/ESCROW.md

Apply rotation (separate, deliberate steps — not automated here):
  1. Mint new key: --mint-rotation
  2. Store NEW and OLD offline (password manager / paper in safe)
  3. Set SO_CONFIG_KEY=\"<NEW>,<OLD>\" on web, worker, harvester (same order)
  4. Redeploy; --verify-decrypt must still pass
  5. scripts/encrypt_vendor_credentials.py --rotate --apply  (re-wrap with NEW)
  6. --verify-decrypt again
  7. Drop OLD from SO_CONFIG_KEY only after step 6 is clean
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def cmd_fingerprint() -> int:
    raw = (os.environ.get("SO_CONFIG_KEY") or "").strip()
    if not raw:
        print("SO_CONFIG_KEY unset")
        return 1
    segs = [s.strip() for s in raw.split(",") if s.strip()]
    print(f"segments={len(segs)}")
    for i, s in enumerate(segs):
        print(f"  [{i}] len={len(s)} sha256_16={_fp(s)}")
    print(f"combined_sha256_16={_fp(raw)}")
    return 0


def cmd_verify_decrypt() -> int:
    raw = (os.environ.get("SO_CONFIG_KEY") or "").strip()
    if not raw:
        print("SO_CONFIG_KEY unset — cannot verify")
        return 1
    url = (
        os.environ.get("DATABASE_PUBLIC_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if not url:
        print("DATABASE_PUBLIC_URL / DATABASE_URL unset")
        return 1

    from cryptography.fernet import Fernet, MultiFernet, InvalidToken
    from sqlalchemy import create_engine, text

    segs = [s.strip() for s in raw.split(",") if s.strip()]
    mf = MultiFernet([Fernet(s.encode("ascii")) for s in segs])

    eng = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 20})
    ok = fail = plain = empty = 0
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT id, tenant_id, provider, secret_enc FROM portal_credential"
        )).all()
        for rid, tid, prov, secret in rows:
            if not secret:
                empty += 1
                continue
            if not str(secret).startswith("SOENC1:"):
                plain += 1
                continue
            token = str(secret)[len("SOENC1:"):].encode("ascii")
            try:
                _ = mf.decrypt(token)
                ok += 1
            except InvalidToken:
                fail += 1
                print(f"FAIL id={rid} tenant={tid} provider={prov}")
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"ERR id={rid} type={type(e).__name__}")
    print(
        f"verify_decrypt ok={ok} fail={fail} plaintext_residual={plain} empty={empty} "
        f"total={ok + fail + plain + empty}"
    )
    return 0 if fail == 0 else 2


def cmd_mint_rotation() -> int:
    from cryptography.fernet import Fernet

    new = Fernet.generate_key().decode("ascii")
    print("NEW_KEY_GENERATED")
    print(f"new_key_sha256_16={_fp(new)}")
    print(f"new_key_len={len(new)}")
    # Print the new key ONCE so the operator can escrow it offline.
    # The OLD key is never printed by this tool.
    print("--- BEGIN NEW FERNET KEY (store offline, then put in SO_CONFIG_KEY as FIRST segment) ---")
    print(new)
    print("--- END NEW FERNET KEY ---")
    print()
    print("Apply recipe (manual):")
    print("  1. Escrow NEW + existing SO_CONFIG_KEY offline (password manager / safe).")
    print("  2. railway variables --set 'SO_CONFIG_KEY=<NEW>,<OLD>' on web, worker, harvester")
    print("  3. Redeploy; run --verify-decrypt")
    print("  4. python -m scripts.encrypt_vendor_credentials --rotate --apply")
    print("  5. --verify-decrypt again; only then drop OLD from SO_CONFIG_KEY")
    return 0


def cmd_write_checklist(path: str) -> int:
    raw = (os.environ.get("SO_CONFIG_KEY") or "").strip()
    fps = []
    if raw:
        segs = [seg.strip() for seg in raw.split(",") if seg.strip()]
        for i, seg in enumerate(segs):
            fps.append(f"- segment[{i}] sha256_16={_fp(seg)} len={len(seg)}")
    body = f"""# SO_CONFIG_KEY offline escrow checklist

Generated: {datetime.now(timezone.utc).isoformat()}

## Fingerprints (not the key)

{chr(10).join(fps) if fps else "- (SO_CONFIG_KEY not in this environment when checklist was written)"}

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
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fingerprint", action="store_true")
    p.add_argument("--verify-decrypt", action="store_true")
    p.add_argument("--mint-rotation", action="store_true")
    p.add_argument("--write-escrow-checklist", metavar="PATH")
    args = p.parse_args(argv)
    if args.fingerprint:
        return cmd_fingerprint()
    if args.verify_decrypt:
        return cmd_verify_decrypt()
    if args.mint_rotation:
        return cmd_mint_rotation()
    if args.write_escrow_checklist:
        return cmd_write_checklist(args.write_escrow_checklist)
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
