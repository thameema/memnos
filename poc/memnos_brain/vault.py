"""Encrypted secret vault — AES-256-GCM at rest, value-refs at use-time.

memnos can now hold secrets (LLM/provider keys, integration creds) WITHOUT them leaking:
- Plaintext is encrypted with a master key (env MEMNOS_SECRET_KEY) before it touches the
  DB; only ciphertext + nonce are stored.
- Config/memory reference a secret as `secret://NAME`; the plaintext is resolved only at
  the moment of use, never written to logs, config files, or recall context.

Master key: `MEMNOS_SECRET_KEY` = base64 of 32 random bytes (generate with
`memnos_admin.py secret-keygen`, store in .env). If absent, the vault is LOCKED — set/get
raise VaultLocked (we never fall back to an ephemeral key — that would make stored secrets
undecryptable after restart). Redaction (memnos_brain.redact) needs no key and stays on.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REF_PREFIX = "secret://"


class VaultLocked(RuntimeError):
    """MEMNOS_SECRET_KEY is not set — the vault cannot encrypt/decrypt."""


def _key() -> bytes:
    raw = os.environ.get("MEMNOS_SECRET_KEY", "").strip()
    if not raw:
        raise VaultLocked("MEMNOS_SECRET_KEY not set — run `memnos_admin.py secret-keygen`")
    try:
        k = base64.b64decode(raw)
    except Exception as e:
        raise VaultLocked(f"MEMNOS_SECRET_KEY is not valid base64: {e}")
    if len(k) != 32:
        raise VaultLocked("MEMNOS_SECRET_KEY must decode to 32 bytes (AES-256)")
    return k


class Vault:
    """Stateless over a pooled connection (like Control)."""

    @staticmethod
    def available() -> bool:
        try:
            _key(); return True
        except VaultLocked:
            return False

    @staticmethod
    def keygen() -> str:
        return base64.b64encode(os.urandom(32)).decode()

    @staticmethod
    def set(conn, name, plaintext, description=None):
        aes = AESGCM(_key())
        nonce = os.urandom(12)
        ct = aes.encrypt(nonce, plaintext.encode(), name.encode())   # name as AAD
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.secrets(name,nonce,ciphertext,description) "
                      "VALUES(%s,%s,%s,%s) ON CONFLICT (name) DO UPDATE SET "
                      "nonce=EXCLUDED.nonce, ciphertext=EXCLUDED.ciphertext, "
                      "description=COALESCE(EXCLUDED.description, memnos_control.secrets.description), "
                      "updated_at=now()", (name, nonce, ct, description))

    @staticmethod
    def get(conn, name):
        with conn.cursor() as c:
            c.execute("SELECT nonce, ciphertext FROM memnos_control.secrets WHERE name=%s", (name,))
            r = c.fetchone()
        if not r:
            return None
        aes = AESGCM(_key())
        return aes.decrypt(bytes(r["nonce"]), bytes(r["ciphertext"]), name.encode()).decode()

    @staticmethod
    def list(conn):
        """Metadata only — plaintext is NEVER returned by list."""
        with conn.cursor() as c:
            c.execute("SELECT name, description, created_at, updated_at FROM memnos_control.secrets "
                      "ORDER BY name")
            return c.fetchall()

    @staticmethod
    def delete(conn, name):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.secrets WHERE name=%s", (name,))

    @staticmethod
    def resolve(conn, value):
        """`secret://NAME` -> plaintext (at use-time). Any other value is returned as-is."""
        if isinstance(value, str) and value.startswith(REF_PREFIX):
            return Vault.get(conn, value[len(REF_PREFIX):])
        return value
