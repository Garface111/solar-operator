#!/usr/bin/env python3
"""One-time migration: encrypt existing vendor credentials at rest.

Wraps PLAINTEXT secrets at rest — vendor API keys
(`InverterConnection.config`, `Array.solaredge_api_key`), utility portal
JWTs (`UtilitySession.api_token` / `refresh_token` / `raw_payload`), and
Cloud Capture passwords (`PortalCredential.secret_enc` / session state) —
in the Fernet `SOENC1:` envelope keyed on SO_CONFIG_KEY. Idempotent and self-classifying: a row already enveloped is
skipped, so re-running is a no-op, and a mixed table (some rows migrated, some
not) converges.

SAFE:
  * DRY-RUN by default — classifies every row and prints what WOULD change,
    writes NOTHING. Add --apply to write.
  * Reversible: --decrypt unwraps every row back to plaintext (run this BEFORE
    removing SO_CONFIG_KEY, or you lock the creds out — losing the key is the
    one irreversible move).
  * Operates on the raw TEXT column, so it neither depends on nor mutates the
    ORM's decrypt path. Requires the Postgres `config` column to already be
    TEXT — run `python -m api.migrate` first (it widens json -> text).

MODES:
  (default)      DRY-RUN encrypt  — report plaintext vs already-encrypted counts
  --apply        ENCRYPT          — wrap plaintext rows (needs SO_CONFIG_KEY)
  --decrypt      DRY-RUN decrypt  — report what would be unwrapped
  --decrypt --apply  DECRYPT      — unwrap to plaintext (rollback; needs the key)
  --verify       LIVE VERIFY      — decrypt each connection and hit the vendor
                                    API (read-only) to PROVE real SolarEdge /
                                    Fronius captures still work post-encryption

Run (read-only):   python -m scripts.encrypt_vendor_credentials
Encrypt for real:  SO_CONFIG_KEY=... python -m scripts.encrypt_vendor_credentials --apply
Live-verify:       SO_CONFIG_KEY=... python -m scripts.encrypt_vendor_credentials --verify
Rollback:          SO_CONFIG_KEY=... python -m scripts.encrypt_vendor_credentials --decrypt --apply

Full runbook: docs/knowledge/encrypting-vendor-credentials-at-rest.md
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from api import crypto

# (table, id column, secret column) pairs holding plaintext-at-rest vendor creds.
_TARGETS = [
    ("inverter_connections", "id", "config"),
    ("arrays", "id", "solaredge_api_key"),
    # Utility portal JWTs / refresh tokens (highest residual dump risk).
    ("utility_sessions", "id", "api_token"),
    ("utility_sessions", "id", "refresh_token"),
    ("utility_sessions", "id", "raw_payload"),
    # Cloud Capture portal passwords + playwright storage_state.
    # Table is singular in models.py (__tablename__ = "portal_credential").
    ("portal_credential", "id", "secret_enc"),
    ("portal_credential", "id", "session_state_enc"),
]


def _select_sql(engine, table: str, col: str) -> str:
    """SELECT that guarantees the secret column comes back as a string, even if
    a Postgres `config` column is still native json (cast to text)."""
    cast = "::text" if engine.dialect.name == "postgresql" else ""
    return f"SELECT {table}.id AS rid, {col}{cast} AS val FROM {table} WHERE {col} IS NOT NULL"


# mode → (acts on encrypted rows?, verb, transform(raw)->new, "already" label)
_MODES = {
    # encrypt: wrap plaintext rows under the active key.
    "encrypt": (False, "wrap",   lambda v: crypto.encrypt_str(v), "encrypted"),
    # decrypt: unwrap ciphertext back to plaintext (rollback before key removal).
    "decrypt": (True,  "unwrap", lambda v: crypto.decrypt_str(v), "plaintext"),
    # rotate: re-wrap ciphertext under the CURRENT primary key, so an old key in
    # SO_CONFIG_KEY can be retired (decrypt tries all keys, encrypt uses the 1st).
    "rotate":  (True,  "rewrap", lambda v: crypto.encrypt_str(crypto.decrypt_str(v)), "n/a"),
}


def process(engine, *, mode: str = "encrypt", apply: bool = False, out=print) -> dict:
    """Encrypt / decrypt / rotate every vendor-cred row at the raw TEXT level.

    Deliberately ORM-free: SQLAlchemy suppresses the UPDATE when a re-assigned
    dict compares equal to the stored one, so a load-and-resave would NOT
    re-encrypt. Reading/writing the raw column sidesteps that entirely.

    Returns a report dict: per-target counts of changed / skipped / total.
    Pure on dry-run (apply=False). Raises on a Postgres column still typed json.
    """
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if not crypto.encryption_enabled():
        raise SystemExit(
            f"{crypto.ENV_KEY} is not set — nothing to do. Set it to a Fernet "
            f"key first (see the module docstring) so rows can be {mode}ed."
        )
    on_encrypted, verb, transform, already_label = _MODES[mode]

    out(f"MODE: {mode.upper()} · {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}\n")
    report: dict[str, dict] = {}

    with engine.begin() as conn:
        for table, idcol, col in _TARGETS:
            # Skip tables that do not exist yet (older DBs / partial test schemas).
            try:
                rows = conn.execute(text(_select_sql(engine, table, col))).fetchall()
            except Exception as exc:
                msg = str(exc).lower()
                if "no such table" in msg or "does not exist" in msg or "undefinedtable" in msg:
                    out(f"  ↷ {table}.{col}: table missing — skipped\n")
                    report[f"{table}.{col}"] = {"changed": 0, "skipped": 0, "total": 0, "missing": True}
                    continue
                raise
            changed = skipped = 0
            for rid, val in rows:
                if val is None:
                    continue
                if not isinstance(val, str):
                    raise SystemExit(
                        f"{table}.{col} row {rid} came back as {type(val).__name__}, "
                        f"not str — the Postgres column is still json. Run "
                        f"`python -m api.migrate` to widen it to TEXT first."
                    )
                # encrypt acts on plaintext; decrypt/rotate act on ciphertext.
                if crypto.is_encrypted(val) != on_encrypted:
                    skipped += 1
                    continue
                new_val = transform(val)
                changed += 1
                preview = (val[:18] + "…") if len(val) > 19 else val
                out(f"  {verb}  {table}.{col} id={rid}  [{preview}]")
                if apply:
                    conn.execute(
                        text(f"UPDATE {table} SET {col} = :v WHERE {idcol} = :i"),
                        {"v": new_val, "i": rid},
                    )
            report[f"{table}.{col}"] = {"changed": changed, "skipped": skipped, "total": len(rows)}
            tail = (f"{skipped} already {already_label}" if mode != "rotate"
                    else f"{skipped} plaintext (skipped)")
            out(f"  → {table}.{col}: {changed} to {verb}, {tail}, {len(rows)} total\n")
    return report


def verify_live(engine, *, out=print) -> dict:
    """Decrypt every InverterConnection and hit its vendor API (read-only) to
    prove real captures still decrypt + work post-encryption. This is the LIVE
    verification step — it makes outbound calls to SolarEdge/Fronius/SMA/etc."""
    from sqlalchemy.orm import Session
    from api import inverters
    from api.models import InverterConnection

    if not crypto.encryption_enabled():
        out(f"WARNING: {crypto.ENV_KEY} is not set — verifying PLAINTEXT reads "
            f"(this still proves the read path, but nothing is encrypted).\n")

    ok = fail = 0
    results = []
    with Session(engine) as db:
        conns = db.query(InverterConnection).all()
        out(f"Verifying {len(conns)} inverter connection(s)…\n")
        for c in conns:
            cfg = c.config or {}  # <-- exercises the ORM decrypt path
            has_key = bool(cfg.get("api_key") or cfg.get("access_token") or cfg.get("refresh_token"))
            label = f"array={c.array_id} vendor={c.vendor}"
            try:
                live = inverters.fetch_live(c.vendor, cfg)
                ok += 1
                out(f"  OK   {label}  creds_present={has_key}  live={'yes' if live else 'none'}")
                results.append((c.array_id, c.vendor, True, None))
            except Exception as exc:  # noqa: BLE001 — report, don't abort the sweep
                fail += 1
                out(f"  FAIL {label}  creds_present={has_key}  err={type(exc).__name__}: {exc}")
                results.append((c.array_id, c.vendor, False, str(exc)))
    out(f"\nLive verify: {ok} ok, {fail} failed, {len(results)} total.")
    return {"ok": ok, "fail": fail, "results": results}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Encrypt/decrypt vendor credentials at rest.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--decrypt", action="store_true", help="unwrap ciphertext back to plaintext (rollback)")
    mode.add_argument("--rotate", action="store_true",
                      help="re-wrap ciphertext under the current primary key (to retire an old key)")
    ap.add_argument("--verify", action="store_true",
                    help="decrypt every connection and hit its vendor API (read-only live check)")
    args = ap.parse_args(argv)

    # Prefer a fresh engine from the current env so a public DATABASE_URL override
    # (local ops against prod) is honoured — api.db may have been imported earlier
    # with the private railway.internal host.
    from sqlalchemy import create_engine
    import os
    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("SOLAR_DB_URL") or "").strip()
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    if not db_url:
        from api.db import engine  # noqa: F401 — local sqlite / default
    else:
        engine = create_engine(db_url, future=True, pool_pre_ping=True)

    if args.verify:
        rep = verify_live(engine)
        return 1 if rep["fail"] else 0

    mode = "decrypt" if args.decrypt else "rotate" if args.rotate else "encrypt"
    process(engine, mode=mode, apply=args.apply)
    if not args.apply:
        print("\nDRY-RUN complete — no writes. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
