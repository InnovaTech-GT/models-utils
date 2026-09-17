"""
Envelope encryption for device credentials (Cycle 5 Phase 1, canon C1).

Plan: docs/isp-platform/23-network-config-implementation-plan.md §2.1.

Scheme (AES-256-GCM envelope):
- Each credential row gets a fresh random 32-byte DEK (data encryption key).
- The secret plaintext is encrypted with the DEK (12-byte random nonce
  prefixed to the ciphertext).
- The DEK is wrapped (encrypted) with the active KEK (key encryption key),
  also nonce-prefixed. Only the wrapped DEK is stored — the KEK never touches
  the database.
- AAD (associated data) binds a ciphertext to its row: `f"{company_id}:{credential_id}"`.
  A ciphertext copied onto another row or tenant fails authentication.

Keys live in environment variables (Railway has no Vault/KMS — this refines
doc 21 §3.8's "KMS-held key"):
- `CREDENTIALS_KEKS`         JSON map `{"<kid>": "<base64 32-byte key>"}`
- `CREDENTIALS_ACTIVE_KEK_ID` the kid new writes wrap with

The module imports cleanly without those vars set; a clear error is raised only
when encrypt/decrypt is actually called and no usable KEK is configured. This
keeps the shared library importable in contexts (auth-erp, cron-erp, tests)
that never touch device credentials.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Dict, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Env var names (canon C18).
ENV_KEKS = "CREDENTIALS_KEKS"
ENV_ACTIVE_KEK_ID = "CREDENTIALS_ACTIVE_KEK_ID"

_NONCE_BYTES = 12
_DEK_BYTES = 32
_KEK_BYTES = 32


class CredentialCryptoError(Exception):
    """Base class for all crypto errors (misconfiguration or auth failure)."""


class CredentialKeyUnavailable(CredentialCryptoError):
    """The KEK referenced by a row (or the configured active KEK) is not
    present in CREDENTIALS_KEKS. The worker fails the step with a
    non-retryable error and never logs the payload."""


def _load_keks() -> Dict[str, bytes]:
    """Parse CREDENTIALS_KEKS into {kid: 32-byte key}. Raises a clear error if
    unset/malformed. Read on every call (no module-level cache) so a rotated
    env is picked up on the next operation without a process restart."""
    raw = os.getenv(ENV_KEKS)
    if not raw:
        raise CredentialKeyUnavailable(
            f"{ENV_KEKS} is not set — device-credential encryption is unavailable. "
            f"Set it to a JSON map of {{\"<kid>\": \"<base64 32-byte key>\"}}."
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialCryptoError(f"{ENV_KEKS} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise CredentialCryptoError(f"{ENV_KEKS} must be a non-empty JSON object")

    keks: Dict[str, bytes] = {}
    for kid, b64 in parsed.items():
        try:
            key = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise CredentialCryptoError(
                f"{ENV_KEKS}['{kid}'] is not valid base64: {exc}"
            ) from exc
        if len(key) != _KEK_BYTES:
            raise CredentialCryptoError(
                f"{ENV_KEKS}['{kid}'] must decode to {_KEK_BYTES} bytes (got {len(key)})"
            )
        keks[kid] = key
    return keks


def _active_kek_id() -> str:
    kid = os.getenv(ENV_ACTIVE_KEK_ID)
    if not kid:
        raise CredentialKeyUnavailable(
            f"{ENV_ACTIVE_KEK_ID} is not set — cannot choose a key to wrap new DEKs with."
        )
    return kid


def _resolve_kek(kid: str) -> bytes:
    keks = _load_keks()
    if kid not in keks:
        raise CredentialKeyUnavailable(
            f"KEK id '{kid}' is not present in {ENV_KEKS} "
            f"(available: {sorted(keks)})"
        )
    return keks[kid]


def _aad(company_id, credential_id) -> bytes:
    """canon C1: AAD binds a ciphertext to its (company, credential) row."""
    return f"{company_id}:{credential_id}".encode("utf-8")


def encrypt_secret(plaintext: str, company_id, credential_id) -> Tuple[bytes, bytes, str]:
    """Encrypt `plaintext` under a fresh per-row DEK, wrap the DEK with the
    active KEK, and return `(secret_ciphertext, dek_wrapped, kek_id)`:

    - `secret_ciphertext`: nonce(12) + AES-256-GCM(DEK, plaintext, aad)
    - `dek_wrapped`:       nonce(12) + AES-256-GCM(KEK, DEK, aad)
    - `kek_id`:            the active KEK id (stored so decrypt resolves the key)

    Raises CredentialKeyUnavailable if no KEK is configured.
    """
    kek_id = _active_kek_id()
    kek = _resolve_kek(kek_id)
    aad = _aad(company_id, credential_id)

    dek = os.urandom(_DEK_BYTES)
    secret_nonce = os.urandom(_NONCE_BYTES)
    secret_ciphertext = secret_nonce + AESGCM(dek).encrypt(
        secret_nonce, plaintext.encode("utf-8"), aad
    )

    wrap_nonce = os.urandom(_NONCE_BYTES)
    dek_wrapped = wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, aad)

    return secret_ciphertext, dek_wrapped, kek_id


def decrypt_secret(secret_ciphertext: bytes, dek_wrapped: bytes, kek_id: str,
                   company_id, credential_id) -> str:
    """Reverse of `encrypt_secret`. Resolves the KEK by `kek_id`, unwraps the
    DEK, and decrypts the payload. Raises CredentialKeyUnavailable if `kek_id`
    is absent from CREDENTIALS_KEKS; raises CredentialCryptoError on any GCM
    authentication failure (wrong key, tampered ciphertext, or a mismatched
    company/credential AAD)."""
    kek = _resolve_kek(kek_id)
    aad = _aad(company_id, credential_id)

    try:
        wrap_nonce, wrapped = dek_wrapped[:_NONCE_BYTES], dek_wrapped[_NONCE_BYTES:]
        dek = AESGCM(kek).decrypt(wrap_nonce, wrapped, aad)

        secret_nonce, body = secret_ciphertext[:_NONCE_BYTES], secret_ciphertext[_NONCE_BYTES:]
        plaintext = AESGCM(dek).decrypt(secret_nonce, body, aad)
    except CredentialCryptoError:
        raise
    except Exception as exc:  # cryptography raises InvalidTag et al.
        raise CredentialCryptoError(
            "device-credential decryption failed (wrong key, tampered ciphertext, "
            "or company/credential mismatch)"
        ) from exc

    return plaintext.decode("utf-8")


def fingerprint(plaintext: str) -> str:
    """Display-safe fingerprint of a secret (canon C19): the last 4 hex chars
    of a SHA-256 over the plaintext. Never reversible to the secret; lets the
    UI confirm which value is stored without exposing it."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()[-4:]
