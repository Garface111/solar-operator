"""
Encryption-at-rest for vendor credentials (keyed on SO_CONFIG_KEY).

Vendor API keys and OAuth tokens (SolarEdge / Fronius / SMA / Chint / Locus)
live in ``InverterConnection.config`` and the legacy ``Array.solaredge_api_key``
column. Historically these were stored in PLAIN TEXT, so a database dump leaked
*working* vendor credentials. This module adds transparent Fernet encryption at
the SQLAlchemy column layer so a DB compromise yields ciphertext, not creds.

Design goals, in priority order:

  1. SAFE, REVERSIBLE ROLLOUT. When ``SO_CONFIG_KEY`` is unset the decorators are
     a pure pass-through: values are stored exactly as before (plaintext JSON /
     plaintext string) and a one-time warning is logged. So *deploying this code
     changes nothing* until a key is deliberately provisioned — and removing the
     key (after decrypting rows) restores the old posture.

  2. TRANSPARENT to every caller. Readers do ``conn.config["api_key"]`` and
     writers do ``conn.config = {...}``. The ``TypeDecorator`` encrypts on the
     way to the DB and decrypts on the way back, so not one call site changes.

  3. MIXED-MODE TOLERANT. During migration the table holds BOTH plaintext and
     encrypted rows. Every stored value is self-describing: ciphertext carries
     the ``SOENC1:`` envelope prefix, plaintext does not. A reader can always
     tell which path to take, so an un-migrated row still decodes correctly
     after the key is switched on — no flag day, no big-bang migration.

  4. ROTATABLE. ``SO_CONFIG_KEY`` may hold several comma-separated Fernet keys.
     The FIRST is the active encryption key; ALL are tried for decryption
     (``MultiFernet``), so you rotate by prepending a new key and re-running the
     migration script, then drop the old key once nothing decrypts with it.

Generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Rollout / rollback runbook:
    docs/knowledge/encrypting-vendor-credentials-at-rest.md
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

log = logging.getLogger("solar.crypto")

ENV_KEY = "SO_CONFIG_KEY"

# Envelope prefix marks a value as ciphertext. Plaintext never collides with it:
# JSON starts with {/[/"/digit, a SolarEdge key is hex, a Fernet token is
# urlsafe-base64 (starts with 'gAAAAA'). Versioned so a future scheme can coexist.
_PREFIX = "SOENC1:"

# Memoize the parsed MultiFernet by the raw env string so tests and a rotated
# deploy pick up env changes without a module reload. Keyed on the exact value
# of SO_CONFIG_KEY, so flipping the env invalidates the cache for free.
_cache: dict[str, MultiFernet] = {}
_warned = False


def _warn_plaintext() -> None:
    """Log once that creds are being stored/read in plaintext (no key set)."""
    global _warned
    if not _warned:
        log.warning(
            "%s is not set — vendor credentials are stored in PLAINTEXT "
            "(encryption-at-rest disabled). This is a safe pass-through; set %s "
            "to encrypt. See docs/knowledge/encrypting-vendor-credentials-at-rest.md",
            ENV_KEY, ENV_KEY,
        )
        _warned = True


def _fernet() -> Optional[MultiFernet]:
    """Return a ``MultiFernet`` built from ``SO_CONFIG_KEY``, or ``None`` if unset.

    Read fresh from the environment (memoized per raw value) so a rotated deploy
    and the test-suite pick up changes correctly. A malformed key fails LOUD
    rather than silently degrading to plaintext — a half-configured key is a
    bug we want surfaced, not swallowed.
    """
    raw = (os.environ.get(ENV_KEY) or "").strip()
    if not raw:
        return None
    cached = _cache.get(raw)
    if cached is not None:
        return cached
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    try:
        mf = MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except Exception as exc:  # malformed key — never silently fall back to plaintext
        raise RuntimeError(
            f"{ENV_KEY} is set but invalid ({exc}). Each comma-separated value "
            f"must be a urlsafe-base64 32-byte Fernet key. Generate one with: "
            f'python -c "from cryptography.fernet import Fernet; '
            f'print(Fernet.generate_key().decode())"'
        ) from exc
    _cache[raw] = mf
    return mf


def encryption_enabled() -> bool:
    """True when a key is configured (encryption active), False = pass-through."""
    return _fernet() is not None


def vault_decrypt_enabled() -> bool:
    """Whether this process may decrypt Cloud Capture vault secrets.

    Split-key posture (T1-1): public ``web`` encrypts on collect but must not hold
    decrypt capability for portal passwords. Set ``SO_VAULT_DECRYPT=0`` on web;
    only ``cloud-capture-harvester`` (and offline rotation tooling) sets it on
    (default ON when unset, for backward-compatible single-process deploys).
    Vendor API keys / utility JWTs still use :func:`decrypt_str` (shared key).
    """
    raw = (os.environ.get("SO_VAULT_DECRYPT") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_encrypted(value: Any) -> bool:
    """True if ``value`` is a stored ciphertext envelope (vs plaintext)."""
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt_str(plaintext: str) -> str:
    """Encrypt a plaintext string to the ``SOENC1:`` envelope, or pass through
    unchanged when no key is set (logging a one-time warning)."""
    f = _fernet()
    if f is None:
        _warn_plaintext()
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_str(value: str) -> str:
    """Decrypt a ``SOENC1:`` envelope back to plaintext. A non-enveloped value is
    returned unchanged (it predates encryption / is a pass-through write).

    Raises ``RuntimeError`` if the value IS encrypted but no key is configured —
    returning ``None``/garbage there would silently break a live capture, so we
    surface the misconfiguration instead. A genuinely wrong key raises
    ``cryptography.fernet.InvalidToken`` (let it propagate; data is unreadable
    and the caller must know).
    """
    if not is_encrypted(value):
        return value
    f = _fernet()
    if f is None:
        raise RuntimeError(
            f"Found encrypted vendor credentials but {ENV_KEY} is not set. "
            f"Restore the key to read them, or run the decrypt migration "
            f"(scripts/encrypt_vendor_credentials.py --decrypt) before removing it."
        )
    plain = f.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    # Detective control: volume + context without the secret itself.
    try:
        log.info("crypto_decrypt kind=shared envelope=SOENC1")
    except Exception:
        pass
    return plain


def decrypt_vault_str(value: str) -> str:
    """Decrypt a Cloud Capture vault secret — refused when SO_VAULT_DECRYPT=0."""
    if not is_encrypted(value):
        return value
    if not vault_decrypt_enabled():
        raise RuntimeError(
            "Vault decrypt is disabled in this process (SO_VAULT_DECRYPT=0). "
            "Only the harvester role may unwrap portal passwords."
        )
    plain = decrypt_str(value)
    try:
        log.info("crypto_decrypt kind=vault envelope=SOENC1")
    except Exception:
        pass
    return plain


class EncryptedJSON(TypeDecorator):
    """A JSON dict column, encrypted at rest. Drop-in for ``JSON``.

    Stored form is TEXT: plaintext JSON when no key is set, or ``SOENC1:``+token
    when ``SO_CONFIG_KEY`` is set. The Python side is always a ``dict`` (or
    ``None``) — every existing ``conn.config["api_key"]`` reader is unaffected.

    NB: like the plain ``JSON`` type this replaces, in-place mutation
    (``conn.config["x"] = y``) is NOT tracked — callers already reassign the
    whole dict (``conn.config = {...}``), which is what triggers a re-encrypt.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # Deterministic dump (sorted keys) keeps re-encryption idempotent and the
        # ciphertext stable across runs of the migration script.
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
        return encrypt_str(payload)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        # Transition tolerance: a Postgres ``json`` column not yet ALTERed to
        # TEXT may be pre-parsed to a dict/list by the driver. Pass it straight
        # through — this only happens in plaintext pass-through mode (ciphertext
        # is never written until the column is TEXT; see api/migrate.py).
        if isinstance(value, (dict, list)):
            return value
        return json.loads(decrypt_str(value))


class EncryptedStr(TypeDecorator):
    """A short secret string (e.g. a vendor API key), encrypted at rest.

    impl=Text, same stored form as :class:`EncryptedJSON` minus the JSON layer.
    Used for the legacy ``Array.solaredge_api_key`` column. SQL-level
    ``IS NOT NULL`` filters still work (ciphertext is non-null when set).
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_str(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        return decrypt_str(value)


class EncryptedVaultStr(TypeDecorator):
    """Portal password column — encrypt everywhere; decrypt only when allowed.

    Used for ``PortalCredential.secret_enc``. Public web can collect (encrypt)
    without being able to unwrap the fleet vault if ``SO_VAULT_DECRYPT=0``.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_str(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        return decrypt_vault_str(value)


class EncryptedVaultJSON(TypeDecorator):
    """Playwright session_state — same split-key posture as EncryptedVaultStr."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
        return encrypt_str(payload)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(decrypt_vault_str(value))
