"""
tools/test_kek_rotation.py — Tests for engram vault rotate-kek.

Covers:
- ArcadeDBClient.list_secrets_with_ciphertext(): returns Secret objects with ciphertext
- ArcadeDBClient.update_dek_enc(): updates only dek_enc
- rotate_kek_local(): rotates all secrets, result.rotated count correct
- rotate_kek_local(): skips secrets that fail old-KEK decryption (result.failed)
- rotate_kek_local(): dry_run=True skips DB writes, result.skipped count correct
- rotate_kek_local(): empty namespace returns empty result
- rotate_kek_local(): DB update failure captured in result.failed
- rotate_kek_local(): re-encrypted DEK decrypts correctly with new KEK
- rotate_kek_local(): old KEK passphrase path (HKDF derivation)
- rotate_kek_local(): new KEK passphrase path
- RotationResult: total property sums all categories
- CLI _build_parser(): rotate-kek subcommand present
- CLI _build_parser(): --dry-run flag parsed correctly
- CLI _build_parser(): --namespace default from env
- CLI _run_rotate(): returns 1 when old_key missing
- CLI _run_rotate(): returns 1 when new_key missing
- CLI _run_rotate(): returns 1 when old == new
- CLI _run_rotate(): calls rotate_kek_local with correct args
- CLI _run_rotate(): returns 2 on partial failure
- CLI _run_rotate(): returns 0 on success
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, _REPO_ROOT + "/packages/core")

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_kek(passphrase: str = "test-kek-passphrase") -> bytes:
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"engram-vault-v1",
        info=b"kek",
    ).derive(passphrase.encode())


def _b64enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _b64dec(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _make_secret(secret_id: str = "s1", key_name: str = "k1",
                 namespace: str = "ns1", value_enc: str = "ve",
                 dek_enc: str = "de") -> "Secret":
    from engram.models import Secret
    return Secret(
        id=secret_id,
        key_name=key_name,
        namespace=namespace,
        value_enc=value_enc,
        dek_enc=dek_enc,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _encrypt_dek(kek: bytes, dek: bytes) -> str:
    """Encrypt DEK with KEK using same scheme as vault_client."""
    import os as _os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = _os.urandom(12)
    ct = AESGCM(kek).encrypt(nonce, dek, None)
    return _b64enc(nonce + ct)


def _decrypt_dek(kek: bytes, dek_enc_b64: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    blob = _b64dec(dek_enc_b64)
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(kek).decrypt(nonce, ct, None)


# ---------------------------------------------------------------------------
# ArcadeDB new methods
# ---------------------------------------------------------------------------

class TestListSecretsWithCiphertext(unittest.IsolatedAsyncioTestCase):
    async def test_returns_secret_objects(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        db = ArcadeDBClient.__new__(ArcadeDBClient)
        row = {
            "id": "s1", "key_name": "k1", "note": "", "secret_type": "api_key",
            "namespace": "ns1", "value_enc": "ve1", "dek_enc": "de1",
            "created_at": None, "superseded_at": None,
            "created_by": "user", "tags": [],
        }
        db._query = AsyncMock(return_value=[row])
        results = await db.list_secrets_with_ciphertext("ns1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "s1")
        self.assertEqual(results[0].value_enc, "ve1")
        self.assertEqual(results[0].dek_enc, "de1")

    async def test_includes_value_enc_and_dek_enc(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        db = ArcadeDBClient.__new__(ArcadeDBClient)
        row = {
            "id": "s2", "key_name": "k2", "note": "", "secret_type": "token",
            "namespace": "ns1", "value_enc": "VALUEENC", "dek_enc": "DEKENC",
            "created_at": None, "superseded_at": None, "created_by": "u", "tags": [],
        }
        db._query = AsyncMock(return_value=[row])
        results = await db.list_secrets_with_ciphertext("ns1")
        self.assertEqual(results[0].value_enc, "VALUEENC")
        self.assertEqual(results[0].dek_enc, "DEKENC")

    async def test_wildcard_namespace_uses_no_filter(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        db = ArcadeDBClient.__new__(ArcadeDBClient)
        db._query = AsyncMock(return_value=[])
        await db.list_secrets_with_ciphertext("*")
        call_args = db._query.call_args[0][0]
        self.assertNotIn(":ns", call_args)

    async def test_returns_empty_when_no_secrets(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        db = ArcadeDBClient.__new__(ArcadeDBClient)
        db._query = AsyncMock(return_value=[])
        results = await db.list_secrets_with_ciphertext("ns1")
        self.assertEqual(results, [])


class TestUpdateDekEnc(unittest.IsolatedAsyncioTestCase):
    async def test_returns_true_on_success(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        db = ArcadeDBClient.__new__(ArcadeDBClient)
        db._command = AsyncMock(return_value=[{"updated": 1}])
        result = await db.update_dek_enc("s1", "newdekenc", "ns1")
        self.assertTrue(result)

    async def test_returns_false_when_no_rows(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        db = ArcadeDBClient.__new__(ArcadeDBClient)
        db._command = AsyncMock(return_value=[])
        result = await db.update_dek_enc("missing", "newdekenc", "ns1")
        self.assertFalse(result)

    async def test_passes_correct_params(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        db = ArcadeDBClient.__new__(ArcadeDBClient)
        db._command = AsyncMock(return_value=[{}])
        await db.update_dek_enc("sid123", "newenc", "myns")
        params = db._command.call_args[0][1]
        self.assertEqual(params["id"], "sid123")
        self.assertEqual(params["dek_enc"], "newenc")
        self.assertEqual(params["ns"], "myns")


# ---------------------------------------------------------------------------
# rotate_kek_local
# ---------------------------------------------------------------------------

class TestRotateKekLocal(unittest.IsolatedAsyncioTestCase):
    def _make_db(self, secrets=None):
        db = MagicMock()
        db.list_secrets_with_ciphertext = AsyncMock(return_value=secrets or [])
        db.update_dek_enc = AsyncMock(return_value=True)
        return db

    async def test_rotates_all_secrets(self):
        from engram.vault.vault_client import rotate_kek_local
        old_kek = _make_kek("old")
        dek = os.urandom(32)
        dek_enc = _encrypt_dek(old_kek, dek)
        secret = _make_secret(dek_enc=dek_enc)

        db = self._make_db([secret])
        result = await rotate_kek_local("old", "new", db, "ns1")
        self.assertEqual(result.rotated, 1)
        self.assertEqual(result.failed, [])
        db.update_dek_enc.assert_awaited_once()

    async def test_failed_decrypt_goes_to_failed(self):
        from engram.vault.vault_client import rotate_kek_local
        # dek_enc is garbage — cannot be decrypted with correct old KEK
        secret = _make_secret(dek_enc=_b64enc(b"not-valid-ciphertext"))
        db = self._make_db([secret])
        result = await rotate_kek_local("old", "new", db, "ns1")
        self.assertEqual(result.rotated, 0)
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0][0], "s1")

    async def test_dry_run_skips_db_writes(self):
        from engram.vault.vault_client import rotate_kek_local
        old_kek = _make_kek("old")
        dek = os.urandom(32)
        dek_enc = _encrypt_dek(old_kek, dek)
        secret = _make_secret(dek_enc=dek_enc)

        db = self._make_db([secret])
        result = await rotate_kek_local("old", "new", db, "ns1", dry_run=True)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.rotated, 0)
        db.update_dek_enc.assert_not_awaited()

    async def test_empty_secrets_returns_zero(self):
        from engram.vault.vault_client import rotate_kek_local
        db = self._make_db([])
        result = await rotate_kek_local("old", "new", db, "ns1")
        self.assertEqual(result.rotated, 0)
        self.assertEqual(result.total, 0)

    async def test_db_update_failure_captured(self):
        from engram.vault.vault_client import rotate_kek_local
        old_kek = _make_kek("old")
        dek = os.urandom(32)
        dek_enc = _encrypt_dek(old_kek, dek)
        secret = _make_secret(dek_enc=dek_enc)

        db = self._make_db([secret])
        db.update_dek_enc = AsyncMock(side_effect=RuntimeError("db error"))
        result = await rotate_kek_local("old", "new", db, "ns1")
        self.assertEqual(result.rotated, 0)
        self.assertEqual(len(result.failed), 1)
        self.assertIn("db error", result.failed[0][1])

    async def test_new_dek_enc_decryptable_with_new_kek(self):
        """The re-encrypted DEK must decrypt correctly under the new KEK."""
        from engram.vault.vault_client import rotate_kek_local
        old_kek = _make_kek("old-passphrase")
        new_kek = _make_kek("new-passphrase")
        dek = os.urandom(32)
        dek_enc = _encrypt_dek(old_kek, dek)
        secret = _make_secret(dek_enc=dek_enc)

        captured = {}

        async def _capture_update(sid, new_enc, ns):
            captured["new_dek_enc"] = new_enc
            return True

        db = self._make_db([secret])
        db.update_dek_enc = _capture_update
        await rotate_kek_local("old-passphrase", "new-passphrase", db, "ns1")

        recovered_dek = _decrypt_dek(new_kek, captured["new_dek_enc"])
        self.assertEqual(recovered_dek, dek)

    async def test_raw_b64_key_accepted(self):
        """A 32-byte base64 key string is used directly without HKDF."""
        from engram.vault.vault_client import rotate_kek_local
        old_raw = os.urandom(32)
        new_raw = os.urandom(32)
        old_key_str = _b64enc(old_raw)
        new_key_str = _b64enc(new_raw)
        dek = os.urandom(32)
        dek_enc = _encrypt_dek(old_raw, dek)
        secret = _make_secret(dek_enc=dek_enc)

        captured = {}

        async def _capture_update(sid, new_enc, ns):
            captured["new_dek_enc"] = new_enc
            return True

        db = self._make_db([secret])
        db.update_dek_enc = _capture_update
        result = await rotate_kek_local(old_key_str, new_key_str, db, "ns1")
        self.assertEqual(result.rotated, 1)
        recovered_dek = _decrypt_dek(new_raw, captured["new_dek_enc"])
        self.assertEqual(recovered_dek, dek)

    async def test_multiple_secrets_all_rotated(self):
        from engram.vault.vault_client import rotate_kek_local
        old_kek = _make_kek("old")
        secrets = []
        for i in range(3):
            dek = os.urandom(32)
            dek_enc = _encrypt_dek(old_kek, dek)
            secrets.append(_make_secret(secret_id=f"s{i}", key_name=f"k{i}", dek_enc=dek_enc))

        db = self._make_db(secrets)
        result = await rotate_kek_local("old", "new", db, "ns1")
        self.assertEqual(result.rotated, 3)
        self.assertEqual(db.update_dek_enc.await_count, 3)

    async def test_partial_failure_continues(self):
        """One bad secret should not prevent the others from being rotated."""
        from engram.vault.vault_client import rotate_kek_local
        old_kek = _make_kek("old")
        good_dek = os.urandom(32)
        good_dek_enc = _encrypt_dek(old_kek, good_dek)
        bad_secret = _make_secret(secret_id="bad", key_name="bad", dek_enc=_b64enc(b"garbage"))
        good_secret = _make_secret(secret_id="good", key_name="good", dek_enc=good_dek_enc)

        db = self._make_db([bad_secret, good_secret])
        result = await rotate_kek_local("old", "new", db, "ns1")
        self.assertEqual(result.rotated, 1)
        self.assertEqual(len(result.failed), 1)


# ---------------------------------------------------------------------------
# RotationResult
# ---------------------------------------------------------------------------

class TestRotationResult(unittest.TestCase):
    def test_total_sums_all_categories(self):
        from engram.vault.vault_client import RotationResult
        r = RotationResult()
        r.rotated = 5
        r.skipped = 2
        r.failed = [("s1", "err"), ("s2", "err")]
        self.assertEqual(r.total, 9)

    def test_initial_values(self):
        from engram.vault.vault_client import RotationResult
        r = RotationResult()
        self.assertEqual(r.rotated, 0)
        self.assertEqual(r.skipped, 0)
        self.assertEqual(r.failed, [])
        self.assertEqual(r.total, 0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestVaultCLIParser(unittest.TestCase):
    def _parser(self):
        from engram.cli.vault_cli import _build_parser
        return _build_parser()

    def test_rotate_kek_subcommand_exists(self):
        p = self._parser()
        args = p.parse_args(["rotate-kek", "--namespace", "ns1",
                              "--old-key", "old", "--new-key", "new"])
        self.assertEqual(args.command, "rotate-kek")

    def test_dry_run_flag_defaults_false(self):
        p = self._parser()
        args = p.parse_args(["rotate-kek", "--namespace", "ns1",
                              "--old-key", "o", "--new-key", "n"])
        self.assertFalse(args.dry_run)

    def test_dry_run_flag_true_when_passed(self):
        p = self._parser()
        args = p.parse_args(["rotate-kek", "--namespace", "ns1",
                              "--old-key", "o", "--new-key", "n", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_namespace_short_flag(self):
        p = self._parser()
        args = p.parse_args(["rotate-kek", "-n", "myns",
                              "--old-key", "o", "--new-key", "n"])
        self.assertEqual(args.namespace, "myns")

    def test_old_key_defaults_empty(self):
        p = self._parser()
        args = p.parse_args(["rotate-kek", "--namespace", "ns1", "--new-key", "n"])
        self.assertEqual(args.old_key, "")

    def test_new_key_defaults_empty(self):
        p = self._parser()
        args = p.parse_args(["rotate-kek", "--namespace", "ns1", "--old-key", "o"])
        self.assertEqual(args.new_key, "")


class TestVaultCLIRun(unittest.IsolatedAsyncioTestCase):
    def _args(self, **kwargs):
        import argparse
        defaults = {
            "command": "rotate-kek",
            "namespace": "ns1",
            "old_key": "old",
            "new_key": "new",
            "dry_run": False,
        }
        defaults.update(kwargs)
        ns = argparse.Namespace(**defaults)
        return ns

    async def test_returns_1_when_old_key_missing(self):
        from engram.cli.vault_cli import _run_rotate
        args = self._args(old_key="", new_key="new")
        with patch.dict(os.environ, {}, clear=True):
            rc = await _run_rotate(args)
        self.assertEqual(rc, 1)

    async def test_returns_1_when_new_key_missing(self):
        from engram.cli.vault_cli import _run_rotate
        args = self._args(old_key="old", new_key="")
        with patch.dict(os.environ, {}, clear=True):
            rc = await _run_rotate(args)
        self.assertEqual(rc, 1)

    async def test_returns_1_when_keys_identical(self):
        from engram.cli.vault_cli import _run_rotate
        args = self._args(old_key="same", new_key="same")
        rc = await _run_rotate(args)
        self.assertEqual(rc, 1)

    async def test_returns_0_on_success(self):
        from engram.cli.vault_cli import _run_rotate
        from engram.vault.vault_client import RotationResult

        ok_result = RotationResult()
        ok_result.rotated = 3

        with patch("engram.config.EngramConfig.from_yaml", return_value=MagicMock()), \
             patch("engram.storage.arcadedb_client.ArcadeDBClient") as MockDB, \
             patch("engram.vault.vault_client.rotate_kek_local", new=AsyncMock(return_value=ok_result)):
            mock_db_instance = AsyncMock()
            MockDB.return_value = mock_db_instance

            args = self._args()
            rc = await _run_rotate(args)
        self.assertEqual(rc, 0)

    async def test_returns_2_on_partial_failure(self):
        from engram.cli.vault_cli import _run_rotate
        from engram.vault.vault_client import RotationResult

        partial = RotationResult()
        partial.rotated = 2
        partial.failed = [("s1", "decrypt error")]

        with patch("engram.config.EngramConfig.from_yaml", return_value=MagicMock()), \
             patch("engram.storage.arcadedb_client.ArcadeDBClient") as MockDB, \
             patch("engram.vault.vault_client.rotate_kek_local", new=AsyncMock(return_value=partial)):
            mock_db_instance = AsyncMock()
            MockDB.return_value = mock_db_instance

            args = self._args()
            rc = await _run_rotate(args)
        self.assertEqual(rc, 2)

    async def test_rotate_called_with_dry_run(self):
        from engram.cli.vault_cli import _run_rotate
        from engram.vault.vault_client import RotationResult

        mock_rotate = AsyncMock(return_value=RotationResult())

        with patch("engram.config.EngramConfig.from_yaml", return_value=MagicMock()), \
             patch("engram.storage.arcadedb_client.ArcadeDBClient") as MockDB, \
             patch("engram.vault.vault_client.rotate_kek_local", mock_rotate):
            mock_db_instance = AsyncMock()
            MockDB.return_value = mock_db_instance

            args = self._args(dry_run=True)
            await _run_rotate(args)

        call_kwargs = mock_rotate.call_args[1]
        self.assertTrue(call_kwargs.get("dry_run", False))

    async def test_env_var_fallback_for_old_key(self):
        from engram.cli.vault_cli import _run_rotate
        from engram.vault.vault_client import RotationResult

        mock_rotate = AsyncMock(return_value=RotationResult())

        with patch.dict(os.environ, {"ENGRAM_VAULT_KEY": "env-old-key"}), \
             patch("engram.config.EngramConfig.from_yaml", return_value=MagicMock()), \
             patch("engram.storage.arcadedb_client.ArcadeDBClient") as MockDB, \
             patch("engram.vault.vault_client.rotate_kek_local", mock_rotate):
            mock_db_instance = AsyncMock()
            MockDB.return_value = mock_db_instance

            args = self._args(old_key="", new_key="new-key")
            rc = await _run_rotate(args)

        # Should not return 1 (missing old key) — env var was used
        self.assertNotEqual(rc, 1)
        first_positional = mock_rotate.call_args[1]["old_kek_str"]
        self.assertEqual(first_positional, "env-old-key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
