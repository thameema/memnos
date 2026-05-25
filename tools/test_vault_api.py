"""
tools/test_vault_api.py — Tests for the vault REST endpoints and auth helper.

Covers:
- check_vault_access: wildcard key grants vault_admin everywhere
- check_vault_access: exact namespace match with correct level
- check_vault_access: prefix wildcard (org:acme:* covers org:acme:eng)
- check_vault_access: insufficient access level raises 403
- check_vault_access: namespace not in vault_namespaces raises 403
- check_vault_access: vault_admin level satisfies vault_write and vault_read
- POST /vault/secrets: stores secret, returns id/key_name/namespace
- POST /vault/secrets: all fields forwarded to client.secret_set
- POST /vault/secrets: 500 on unexpected exception
- GET /vault/secrets: returns list from client.secret_list
- GET /vault/secrets: accessed_by set to user_id
- GET /vault/secrets: 500 on exception
- GET /vault/secrets/{key_name}: returns decrypted value
- GET /vault/secrets/{key_name}: 404 on KeyError
- GET /vault/secrets/{key_name}: 500 on unexpected exception
- PUT /vault/secrets/{key_name}/rotate: rotates with new_value
- PUT /vault/secrets/{key_name}/rotate: 500 on exception
- DELETE /vault/secrets/{key_name}: 200 with superseded=True when found
- DELETE /vault/secrets/{key_name}: 404 when secret not found
- DELETE /vault/secrets/{key_name}: 500 on unexpected exception
- GET /vault/audit: returns list from client.secret_audit
- GET /vault/audit: limit param passed through
- GET /vault/audit: 500 on exception
"""
from __future__ import annotations

import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, _REPO_ROOT + "/packages/api")
sys.path.insert(0, _REPO_ROOT + "/packages/core")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_key_entry(namespaces=None, vault_namespaces=None):
    entry = MagicMock()
    entry.user_id = "user-1"
    entry.namespaces = namespaces or []
    entry.vault_namespaces = vault_namespaces or []
    return entry


def _make_vault_ns_entry(namespace, access="vault_read"):
    e = MagicMock()
    e.namespace = namespace
    e.access = access
    return e


def _make_client():
    c = MagicMock()
    c.secret_set = AsyncMock(return_value={"id": "sec-1", "key_name": "MY_KEY", "namespace": "ns1"})
    c.secret_list = AsyncMock(return_value=[{"key_name": "MY_KEY", "namespace": "ns1"}])
    c.secret_get = AsyncMock(return_value="s3cr3t")
    c.secret_rotate = AsyncMock(return_value={"id": "sec-2", "key_name": "MY_KEY", "namespace": "ns1"})
    c.secret_audit = AsyncMock(return_value=[{"action": "set", "secret_name": "MY_KEY"}])
    c._arcadedb = AsyncMock()
    return c


# ---------------------------------------------------------------------------
# check_vault_access
# ---------------------------------------------------------------------------

class TestCheckVaultAccess(unittest.IsolatedAsyncioTestCase):
    async def test_wildcard_key_passes_as_vault_admin(self):
        from engram_api.auth import check_vault_access
        entry = _make_key_entry(namespaces=["*"])
        await check_vault_access(entry, "org:acme:private", required="vault_admin")  # no raise

    async def test_wildcard_key_passes_all_levels(self):
        from engram_api.auth import check_vault_access
        entry = _make_key_entry(namespaces=["*"])
        for level in ("vault_read", "vault_write", "vault_admin"):
            await check_vault_access(entry, "any:namespace", required=level)

    async def test_exact_namespace_match_with_sufficient_level(self):
        from engram_api.auth import check_vault_access
        entry = _make_key_entry(vault_namespaces=[
            _make_vault_ns_entry("org:acme", "vault_write")
        ])
        await check_vault_access(entry, "org:acme", required="vault_read")
        await check_vault_access(entry, "org:acme", required="vault_write")

    async def test_exact_namespace_insufficient_level_raises_403(self):
        from engram_api.auth import check_vault_access
        from fastapi import HTTPException
        entry = _make_key_entry(vault_namespaces=[
            _make_vault_ns_entry("org:acme", "vault_read")
        ])
        with self.assertRaises(HTTPException) as ctx:
            await check_vault_access(entry, "org:acme", required="vault_write")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_prefix_wildcard_covers_child_namespace(self):
        from engram_api.auth import check_vault_access
        entry = _make_key_entry(vault_namespaces=[
            _make_vault_ns_entry("org:acme:*", "vault_admin")
        ])
        await check_vault_access(entry, "org:acme:eng", required="vault_write")

    async def test_prefix_wildcard_does_not_cover_unrelated_namespace(self):
        from engram_api.auth import check_vault_access
        from fastapi import HTTPException
        entry = _make_key_entry(vault_namespaces=[
            _make_vault_ns_entry("org:acme:*", "vault_admin")
        ])
        with self.assertRaises(HTTPException) as ctx:
            await check_vault_access(entry, "org:other", required="vault_read")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_namespace_not_listed_raises_403(self):
        from engram_api.auth import check_vault_access
        from fastapi import HTTPException
        entry = _make_key_entry(vault_namespaces=[])
        with self.assertRaises(HTTPException) as ctx:
            await check_vault_access(entry, "org:acme", required="vault_read")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_vault_admin_satisfies_vault_write_and_read(self):
        from engram_api.auth import check_vault_access
        entry = _make_key_entry(vault_namespaces=[
            _make_vault_ns_entry("ns1", "vault_admin")
        ])
        await check_vault_access(entry, "ns1", required="vault_read")
        await check_vault_access(entry, "ns1", required="vault_write")
        await check_vault_access(entry, "ns1", required="vault_admin")

    async def test_universal_ns_wildcard_in_vault_namespaces(self):
        from engram_api.auth import check_vault_access
        entry = _make_key_entry(vault_namespaces=[
            _make_vault_ns_entry("*", "vault_admin")
        ])
        await check_vault_access(entry, "org:anything:deep", required="vault_admin")


# ---------------------------------------------------------------------------
# POST /vault/secrets (set_secret)
# ---------------------------------------------------------------------------

class TestSetSecret(unittest.IsolatedAsyncioTestCase):
    async def test_returns_201_response(self):
        from engram_api.routers.vault import set_secret, SecretSetRequest
        req = SecretSetRequest(key_name="MY_KEY", value="s3cr3t", namespace="ns1")
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        result = await set_secret(req=req, user_id="user-1", key_entry=key_entry, client=client)
        self.assertEqual(result["id"], "sec-1")
        self.assertEqual(result["key_name"], "MY_KEY")

    async def test_fields_forwarded_to_client(self):
        from engram_api.routers.vault import set_secret, SecretSetRequest
        req = SecretSetRequest(
            key_name="DB_PASS", value="hunter2", namespace="ns1",
            secret_type="password", note="prod db", tags=["db"],
        )
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        await set_secret(req=req, user_id="alice", key_entry=key_entry, client=client)
        client.secret_set.assert_awaited_once()
        kw = client.secret_set.call_args.kwargs
        self.assertEqual(kw["key_name"], "DB_PASS")
        self.assertEqual(kw["value"], "hunter2")
        self.assertEqual(kw["secret_type"], "password")
        self.assertEqual(kw["note"], "prod db")
        self.assertEqual(kw["tags"], ["db"])
        self.assertEqual(kw["created_by"], "alice")

    async def test_500_on_unexpected_exception(self):
        from engram_api.routers.vault import set_secret, SecretSetRequest
        from fastapi import HTTPException
        req = SecretSetRequest(key_name="K", value="v", namespace="ns1")
        client = _make_client()
        client.secret_set = AsyncMock(side_effect=RuntimeError("storage failure"))
        key_entry = _make_key_entry(namespaces=["*"])

        with self.assertRaises(HTTPException) as ctx:
            await set_secret(req=req, user_id="u1", key_entry=key_entry, client=client)
        self.assertEqual(ctx.exception.status_code, 500)


# ---------------------------------------------------------------------------
# GET /vault/secrets (list_secrets)
# ---------------------------------------------------------------------------

class TestListSecrets(unittest.IsolatedAsyncioTestCase):
    async def test_returns_list(self):
        from engram_api.routers.vault import list_secrets
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        result = await list_secrets(namespace="ns1", user_id="u1", key_entry=key_entry, client=client)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["key_name"], "MY_KEY")

    async def test_accessed_by_set_to_user_id(self):
        from engram_api.routers.vault import list_secrets
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        await list_secrets(namespace="ns1", user_id="bob", key_entry=key_entry, client=client)
        client.secret_list.assert_awaited_once_with(namespace="ns1", accessed_by="bob")

    async def test_500_on_exception(self):
        from engram_api.routers.vault import list_secrets
        from fastapi import HTTPException
        client = _make_client()
        client.secret_list = AsyncMock(side_effect=RuntimeError("db error"))
        key_entry = _make_key_entry(namespaces=["*"])

        with self.assertRaises(HTTPException) as ctx:
            await list_secrets(namespace="ns1", user_id="u1", key_entry=key_entry, client=client)
        self.assertEqual(ctx.exception.status_code, 500)


# ---------------------------------------------------------------------------
# GET /vault/secrets/{key_name} (get_secret)
# ---------------------------------------------------------------------------

class TestGetSecret(unittest.IsolatedAsyncioTestCase):
    async def test_returns_decrypted_value(self):
        from engram_api.routers.vault import get_secret
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        result = await get_secret(
            key_name="MY_KEY", namespace="ns1",
            user_id="u1", key_entry=key_entry, client=client,
        )
        self.assertEqual(result["key_name"], "MY_KEY")
        self.assertEqual(result["namespace"], "ns1")
        self.assertEqual(result["value"], "s3cr3t")

    async def test_accessed_by_passed_to_client(self):
        from engram_api.routers.vault import get_secret
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        await get_secret(
            key_name="K", namespace="ns1",
            user_id="carol", key_entry=key_entry, client=client,
        )
        client.secret_get.assert_awaited_once_with(
            key_name="K", namespace="ns1", accessed_by="carol"
        )

    async def test_404_on_key_error(self):
        from engram_api.routers.vault import get_secret
        from fastapi import HTTPException
        client = _make_client()
        client.secret_get = AsyncMock(side_effect=KeyError("MY_KEY"))
        key_entry = _make_key_entry(namespaces=["*"])

        with self.assertRaises(HTTPException) as ctx:
            await get_secret(
                key_name="MY_KEY", namespace="ns1",
                user_id="u1", key_entry=key_entry, client=client,
            )
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_500_on_unexpected_exception(self):
        from engram_api.routers.vault import get_secret
        from fastapi import HTTPException
        client = _make_client()
        client.secret_get = AsyncMock(side_effect=RuntimeError("vault failure"))
        key_entry = _make_key_entry(namespaces=["*"])

        with self.assertRaises(HTTPException) as ctx:
            await get_secret(
                key_name="K", namespace="ns1",
                user_id="u1", key_entry=key_entry, client=client,
            )
        self.assertEqual(ctx.exception.status_code, 500)


# ---------------------------------------------------------------------------
# PUT /vault/secrets/{key_name}/rotate (rotate_secret)
# ---------------------------------------------------------------------------

class TestRotateSecret(unittest.IsolatedAsyncioTestCase):
    async def test_rotates_with_new_value(self):
        from engram_api.routers.vault import rotate_secret, SecretRotateRequest
        req = SecretRotateRequest(new_value="new_s3cr3t")
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        result = await rotate_secret(
            key_name="MY_KEY", req=req, namespace="ns1",
            user_id="u1", key_entry=key_entry, client=client,
        )
        client.secret_rotate.assert_awaited_once_with(
            key_name="MY_KEY", new_value="new_s3cr3t",
            namespace="ns1", accessed_by="u1",
        )
        self.assertEqual(result["id"], "sec-2")

    async def test_500_on_exception(self):
        from engram_api.routers.vault import rotate_secret, SecretRotateRequest
        from fastapi import HTTPException
        req = SecretRotateRequest(new_value="v")
        client = _make_client()
        client.secret_rotate = AsyncMock(side_effect=RuntimeError("rotation failed"))
        key_entry = _make_key_entry(namespaces=["*"])

        with self.assertRaises(HTTPException) as ctx:
            await rotate_secret(
                key_name="K", req=req, namespace="ns1",
                user_id="u1", key_entry=key_entry, client=client,
            )
        self.assertEqual(ctx.exception.status_code, 500)


# ---------------------------------------------------------------------------
# DELETE /vault/secrets/{key_name} (delete_secret)
# ---------------------------------------------------------------------------

class TestDeleteSecret(unittest.IsolatedAsyncioTestCase):
    async def test_supersedes_existing_secret(self):
        from engram_api.routers.vault import delete_secret
        client = _make_client()
        existing = MagicMock()
        existing.id = "sec-1"
        client._arcadedb.get_secret = AsyncMock(return_value=existing)
        client._arcadedb.supersede_secret = AsyncMock(return_value=True)
        key_entry = _make_key_entry(namespaces=["*"])

        result = await delete_secret(
            key_name="MY_KEY", namespace="ns1",
            user_id="u1", key_entry=key_entry, client=client,
        )
        client._arcadedb.supersede_secret.assert_awaited_once_with("sec-1", "ns1")
        self.assertTrue(result["superseded"])
        self.assertEqual(result["key_name"], "MY_KEY")

    async def test_404_when_secret_not_found(self):
        from engram_api.routers.vault import delete_secret
        from fastapi import HTTPException
        client = _make_client()
        client._arcadedb.get_secret = AsyncMock(return_value=None)
        key_entry = _make_key_entry(namespaces=["*"])

        with self.assertRaises(HTTPException) as ctx:
            await delete_secret(
                key_name="MISSING", namespace="ns1",
                user_id="u1", key_entry=key_entry, client=client,
            )
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("MISSING", ctx.exception.detail)

    async def test_500_on_unexpected_exception(self):
        from engram_api.routers.vault import delete_secret
        from fastapi import HTTPException
        client = _make_client()
        client._arcadedb.get_secret = AsyncMock(side_effect=RuntimeError("db crash"))
        key_entry = _make_key_entry(namespaces=["*"])

        with self.assertRaises(HTTPException) as ctx:
            await delete_secret(
                key_name="K", namespace="ns1",
                user_id="u1", key_entry=key_entry, client=client,
            )
        self.assertEqual(ctx.exception.status_code, 500)

    async def test_namespace_returned_in_response(self):
        from engram_api.routers.vault import delete_secret
        client = _make_client()
        existing = MagicMock(); existing.id = "sec-1"
        client._arcadedb.get_secret = AsyncMock(return_value=existing)
        client._arcadedb.supersede_secret = AsyncMock(return_value=True)
        key_entry = _make_key_entry(namespaces=["*"])

        result = await delete_secret(
            key_name="K", namespace="my:ns",
            user_id="u1", key_entry=key_entry, client=client,
        )
        self.assertEqual(result["namespace"], "my:ns")


# ---------------------------------------------------------------------------
# GET /vault/audit (get_audit_log)
# ---------------------------------------------------------------------------

class TestGetAuditLog(unittest.IsolatedAsyncioTestCase):
    async def test_returns_audit_entries(self):
        from engram_api.routers.vault import get_audit_log
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        result = await get_audit_log(
            namespace="ns1", limit=100,
            user_id="u1", key_entry=key_entry, client=client,
        )
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["action"], "set")

    async def test_limit_passed_to_client(self):
        from engram_api.routers.vault import get_audit_log
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        await get_audit_log(
            namespace="ns1", limit=42,
            user_id="u1", key_entry=key_entry, client=client,
        )
        client.secret_audit.assert_awaited_once_with(namespace="ns1", limit=42)

    async def test_500_on_exception(self):
        from engram_api.routers.vault import get_audit_log
        from fastapi import HTTPException
        client = _make_client()
        client.secret_audit = AsyncMock(side_effect=RuntimeError("audit db down"))
        key_entry = _make_key_entry(namespaces=["*"])

        with self.assertRaises(HTTPException) as ctx:
            await get_audit_log(
                namespace="ns1", limit=100,
                user_id="u1", key_entry=key_entry, client=client,
            )
        self.assertEqual(ctx.exception.status_code, 500)

    async def test_namespace_passed_to_client(self):
        from engram_api.routers.vault import get_audit_log
        client = _make_client()
        key_entry = _make_key_entry(namespaces=["*"])

        await get_audit_log(
            namespace="org:acme:private", limit=50,
            user_id="u1", key_entry=key_entry, client=client,
        )
        client.secret_audit.assert_awaited_once_with(namespace="org:acme:private", limit=50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
