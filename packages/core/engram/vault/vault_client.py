"""
engram.vault.vault_client — Envelope encryption engine for the secrets vault.

Design
------
Every secret is protected by a two-layer envelope:

  plaintext
      └─ encrypted with a random 256-bit DEK (AES-256-GCM)   → value_enc
  DEK
      └─ encrypted with the KEK (Key Encryption Key)          → dek_enc

Only the ciphertexts are persisted.  The DEK is ephemeral — it exists in
memory only during encrypt/decrypt and is never written anywhere in
plaintext form.

KEK providers
-------------
  local           KEK is derived from ENGRAM_VAULT_KEY env var (or config
                  fallback_key). Dev/single-user mode.

  azure_keyvault  DEK is wrapped by an RSA key in Azure Key Vault using
                  RSA-OAEP-256.  The KEK never leaves Azure.
                  Requires: pip install azure-keyvault-keys azure-identity

  aws_kms         AWS KMS GenerateDataKey (AES_256) provides an encrypted DEK.
                  Requires: pip install boto3

Key rotation
------------
When the KEK is rotated, only dek_enc must be re-encrypted — value_enc is
unchanged.  Run:  engram vault rotate-kek  (CLI, not yet implemented).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.config import VaultConfig

logger = logging.getLogger(__name__)

# AES-256-GCM nonce length (bytes)
_NONCE_LEN = 12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _b64dec(s: str) -> bytes:
    # urlsafe_b64decode requires correct padding
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _aes_gcm_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Return nonce (12 B) + ciphertext. Key must be 32 bytes."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce + ct


def _aes_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    """Inverse of _aes_gcm_encrypt."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, None)


# ---------------------------------------------------------------------------
# VaultClient
# ---------------------------------------------------------------------------

class VaultClient:
    """Encrypt and decrypt secrets using envelope encryption."""

    def __init__(self, config: "VaultConfig") -> None:
        self._config = config
        self._local_kek: bytes | None = None  # cached after first derivation

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def encrypt(self, plaintext: str) -> tuple[str, str]:
        """Encrypt a plaintext string.

        Returns
        -------
        (value_enc_b64, dek_enc_b64)
            Both values are URL-safe base64 strings suitable for storing in
            ArcadeDB.
        """
        provider = self._config.kms.provider.lower()
        if provider == "azure_keyvault":
            return await self._encrypt_azure(plaintext)
        if provider == "aws_kms":
            return await self._encrypt_aws(plaintext)
        return self._encrypt_local(plaintext)

    async def decrypt(self, value_enc_b64: str, dek_enc_b64: str) -> str:
        """Decrypt a (value_enc, dek_enc) pair returned by encrypt().

        Returns the original plaintext string.
        """
        provider = self._config.kms.provider.lower()
        if provider == "azure_keyvault":
            return await self._decrypt_azure(value_enc_b64, dek_enc_b64)
        if provider == "aws_kms":
            return await self._decrypt_aws(value_enc_b64, dek_enc_b64)
        return self._decrypt_local(value_enc_b64, dek_enc_b64)

    # ------------------------------------------------------------------
    # Local mode (ENGRAM_VAULT_KEY env var / fallback_key)
    # ------------------------------------------------------------------

    def _get_local_kek(self) -> bytes:
        if self._local_kek is not None:
            return self._local_kek

        key_str = self._config.fallback_key or os.environ.get("ENGRAM_VAULT_KEY", "")
        if not key_str:
            raise RuntimeError(
                "Vault is enabled but ENGRAM_VAULT_KEY is not set.  "
                "Generate a key with:  python -c \"import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"  "
                "and set it as the ENGRAM_VAULT_KEY environment variable."
            )

        # Try direct base64 decode (32-byte raw key)
        try:
            raw = _b64dec(key_str)
            if len(raw) == 32:
                self._local_kek = raw
                return self._local_kek
        except Exception:
            pass

        # Treat as passphrase — derive 32-byte key with HKDF-SHA256
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # type: ignore
        from cryptography.hazmat.primitives import hashes  # type: ignore
        self._local_kek = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"engram-vault-v1",
            info=b"kek",
        ).derive(key_str.encode())
        return self._local_kek

    def _encrypt_local(self, plaintext: str) -> tuple[str, str]:
        kek = self._get_local_kek()
        dek = os.urandom(32)
        value_blob = _aes_gcm_encrypt(dek, plaintext.encode())
        dek_blob = _aes_gcm_encrypt(kek, dek)
        return _b64enc(value_blob), _b64enc(dek_blob)

    def _decrypt_local(self, value_enc_b64: str, dek_enc_b64: str) -> str:
        kek = self._get_local_kek()
        dek = _aes_gcm_decrypt(kek, _b64dec(dek_enc_b64))
        plaintext_bytes = _aes_gcm_decrypt(dek, _b64dec(value_enc_b64))
        return plaintext_bytes.decode()

    # ------------------------------------------------------------------
    # Azure Key Vault mode
    # ------------------------------------------------------------------

    async def _encrypt_azure(self, plaintext: str) -> tuple[str, str]:
        """Wrap DEK with Azure Key Vault RSA-OAEP-256."""
        try:
            from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm  # type: ignore
            from azure.identity import DefaultAzureCredential  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Azure Key Vault support requires: "
                "pip install azure-keyvault-keys azure-identity"
            ) from exc

        dek = os.urandom(32)
        value_blob = _aes_gcm_encrypt(dek, plaintext.encode())

        credential = DefaultAzureCredential()
        crypto_client = CryptographyClient(self._config.kms.key_url, credential)
        wrap_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: crypto_client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek),
        )
        return _b64enc(value_blob), _b64enc(wrap_result.encrypted_key)

    async def _decrypt_azure(self, value_enc_b64: str, dek_enc_b64: str) -> str:
        try:
            from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm  # type: ignore
            from azure.identity import DefaultAzureCredential  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Azure Key Vault support requires: "
                "pip install azure-keyvault-keys azure-identity"
            ) from exc

        credential = DefaultAzureCredential()
        crypto_client = CryptographyClient(self._config.kms.key_url, credential)
        unwrap_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: crypto_client.unwrap_key(
                KeyWrapAlgorithm.rsa_oaep_256, _b64dec(dek_enc_b64)
            ),
        )
        dek = unwrap_result.key
        return _aes_gcm_decrypt(dek, _b64dec(value_enc_b64)).decode()

    # ------------------------------------------------------------------
    # AWS KMS mode
    # ------------------------------------------------------------------

    async def _encrypt_aws(self, plaintext: str) -> tuple[str, str]:
        """Use AWS KMS GenerateDataKey (AES_256)."""
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "AWS KMS support requires: pip install boto3"
            ) from exc

        kms = boto3.client("kms")
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: kms.generate_data_key(
                KeyId=self._config.kms.key_url,
                KeySpec="AES_256",
            ),
        )
        dek: bytes = response["Plaintext"]
        dek_enc: bytes = response["CiphertextBlob"]
        value_blob = _aes_gcm_encrypt(dek, plaintext.encode())
        return _b64enc(value_blob), _b64enc(dek_enc)

    async def _decrypt_aws(self, value_enc_b64: str, dek_enc_b64: str) -> str:
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "AWS KMS support requires: pip install boto3"
            ) from exc

        kms = boto3.client("kms")
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: kms.decrypt(CiphertextBlob=_b64dec(dek_enc_b64)),
        )
        dek: bytes = response["Plaintext"]
        return _aes_gcm_decrypt(dek, _b64dec(value_enc_b64)).decode()


# ---------------------------------------------------------------------------
# KEK rotation (local mode only)
# ---------------------------------------------------------------------------

class RotationResult:
    """Summary returned by rotate_kek_local."""

    def __init__(self) -> None:
        self.rotated: int = 0
        self.skipped: int = 0
        self.failed: list[tuple[str, str]] = []  # [(secret_id, error_message)]

    @property
    def total(self) -> int:
        return self.rotated + self.skipped + len(self.failed)


async def rotate_kek_local(
    old_kek_str: str,
    new_kek_str: str,
    arcadedb_client: "Any",
    namespace: str,
    dry_run: bool = False,
) -> RotationResult:
    """Re-encrypt every secret's DEK under a new KEK (local mode only).

    Re-encrypts ``dek_enc`` for each secret in *namespace* using *new_kek_str*
    without touching ``value_enc``.  The plaintext value is never re-derived.

    Parameters
    ----------
    old_kek_str:
        Current ENGRAM_VAULT_KEY (raw key or passphrase).
    new_kek_str:
        Replacement ENGRAM_VAULT_KEY.
    arcadedb_client:
        An open ArcadeDBClient (caller must have called ``start()``).
    namespace:
        The namespace scope.  Pass ``"*"`` or ``""`` for all namespaces.
    dry_run:
        If True, validate decryption with old KEK but skip DB writes.
    """
    from typing import Any  # noqa: F401 — imported for type hint resolution

    result = RotationResult()

    # Derive both KEKs
    def _derive(key_str: str) -> bytes:
        try:
            raw = _b64dec(key_str)
            if len(raw) == 32:
                return raw
        except Exception:
            pass
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # type: ignore
        from cryptography.hazmat.primitives import hashes  # type: ignore
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"engram-vault-v1",
            info=b"kek",
        ).derive(key_str.encode())

    old_kek = _derive(old_kek_str)
    new_kek = _derive(new_kek_str)

    secrets = await arcadedb_client.list_secrets_with_ciphertext(namespace)

    for secret in secrets:
        try:
            # Decrypt DEK with old KEK
            raw_dek = _aes_gcm_decrypt(old_kek, _b64dec(secret.dek_enc))
        except Exception as exc:
            result.failed.append((secret.id, f"decrypt dek_enc: {exc}"))
            continue

        # Re-encrypt DEK with new KEK
        new_dek_blob = _aes_gcm_encrypt(new_kek, raw_dek)
        new_dek_enc = _b64enc(new_dek_blob)

        if dry_run:
            result.skipped += 1
            continue

        try:
            await arcadedb_client.update_dek_enc(secret.id, new_dek_enc, secret.namespace)
            result.rotated += 1
            logger.debug("rotate-kek: rotated %s / %s", secret.id, secret.namespace)
        except Exception as exc:
            result.failed.append((secret.id, f"update dek_enc: {exc}"))

    return result


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------

_vault_client: VaultClient | None = None


def get_vault_client(config: "VaultConfig") -> VaultClient:
    """Return or create the module-level VaultClient singleton."""
    global _vault_client
    if _vault_client is None:
        _vault_client = VaultClient(config)
    return _vault_client
