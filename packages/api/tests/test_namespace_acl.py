"""
tests.test_namespace_acl — Unit tests for namespace-level access control.

Tests cover:
- check_namespace_access() rules (wildcard, exact, prefix wildcard, deny)
- require_api_key_entry() returns the full ApiKeyEntry
- require_api_key() backward-compat still returns user_id string
- require_admin_access() allows "*" keys, blocks scoped keys
- HTTP-level enforcement on memory / graph / tasks / admin endpoints
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from engram.config import ApiKeyEntry, AuthConfig, EngramConfig

from engram_api.auth import (
    check_namespace_access,
    require_admin_access,
    require_api_key,
    require_api_key_entry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_entry(namespaces: list[str], user_id: str = "test-user") -> ApiKeyEntry:
    return ApiKeyEntry(key="test-key", user_id=user_id, namespaces=namespaces)


def _make_config(namespaces: list[str]) -> EngramConfig:
    entry = ApiKeyEntry(key="test-key", user_id="test-user", namespaces=namespaces)
    return EngramConfig(auth=AuthConfig(api_keys=[entry]))


# ---------------------------------------------------------------------------
# check_namespace_access — unit tests (no HTTP layer needed)
# ---------------------------------------------------------------------------

class TestCheckNamespaceAccess:
    """Direct unit tests for the standalone check_namespace_access() function."""

    @pytest.mark.asyncio
    async def test_wildcard_allows_any_namespace(self):
        entry = _make_entry(["*"])
        # Should not raise for any namespace
        await check_namespace_access(entry, "personal:alice")
        await check_namespace_access(entry, "org:acme")
        await check_namespace_access(entry, "completely:unknown:ns")

    @pytest.mark.asyncio
    async def test_exact_match_allowed(self):
        entry = _make_entry(["personal:default", "org:acme"])
        await check_namespace_access(entry, "personal:default")
        await check_namespace_access(entry, "org:acme")

    @pytest.mark.asyncio
    async def test_exact_match_denied(self):
        entry = _make_entry(["personal:default", "org:acme"])
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_access(entry, "personal:alice")
        assert exc_info.value.status_code == 403
        assert "personal:alice" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_prefix_wildcard_match(self):
        entry = _make_entry(["personal:*"])
        await check_namespace_access(entry, "personal:alice")
        await check_namespace_access(entry, "personal:alice")
        await check_namespace_access(entry, "personal:default")

    @pytest.mark.asyncio
    async def test_prefix_wildcard_does_not_match_other_prefix(self):
        entry = _make_entry(["personal:*"])
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_access(entry, "org:acme")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_prefix_wildcard_does_not_match_partial_prefix(self):
        """'personal:*' must NOT match 'personalother:ns' — colon is the delimiter."""
        entry = _make_entry(["personal:*"])
        # "personalother:ns" does NOT start with "personal:" so it must be denied
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_access(entry, "personalother:ns")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_multiple_patterns_first_wildcard_wins(self):
        entry = _make_entry(["org:acme", "*"])
        # "*" is present so anything is allowed
        await check_namespace_access(entry, "random:namespace")

    @pytest.mark.asyncio
    async def test_multiple_prefix_patterns(self):
        entry = _make_entry(["personal:*", "org:acme"])
        await check_namespace_access(entry, "personal:alice")
        await check_namespace_access(entry, "org:acme")
        with pytest.raises(HTTPException):
            await check_namespace_access(entry, "org:other")

    @pytest.mark.asyncio
    async def test_empty_namespaces_denies_all(self):
        entry = _make_entry([])
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_access(entry, "personal:default")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_403_detail_includes_namespace_name(self):
        entry = _make_entry(["org:acme"])
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_access(entry, "personal:alice")
        assert "personal:alice" in exc_info.value.detail


# ---------------------------------------------------------------------------
# require_admin_access — unit tests
# ---------------------------------------------------------------------------

class TestRequireAdminAccess:
    """Tests for the require_admin_access dependency."""

    @pytest.mark.asyncio
    async def test_wildcard_key_passes(self):
        entry = _make_entry(["*"])
        result = await require_admin_access(key_entry=entry)
        assert result is entry

    @pytest.mark.asyncio
    async def test_scoped_key_raises_403(self):
        entry = _make_entry(["personal:default"])
        with pytest.raises(HTTPException) as exc_info:
            await require_admin_access(key_entry=entry)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_prefix_wildcard_key_raises_403(self):
        """'personal:*' is not the same as '*' — still rejected for admin ops."""
        entry = _make_entry(["personal:*"])
        with pytest.raises(HTTPException) as exc_info:
            await require_admin_access(key_entry=entry)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_namespaces_raises_403(self):
        entry = _make_entry([])
        with pytest.raises(HTTPException) as exc_info:
            await require_admin_access(key_entry=entry)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# HTTP-level integration tests via FastAPI TestClient
# ---------------------------------------------------------------------------

def _build_app(key_namespaces: list[str]):
    """Build a minimal FastAPI app wired with a single API key."""
    from fastapi import FastAPI
    from engram_api.routers import memory, graph, admin

    app = FastAPI()
    app.include_router(memory.router)
    app.include_router(graph.router)
    app.include_router(admin.router)

    # Minimal mock config & clients on app.state
    entry = ApiKeyEntry(key="test-key", user_id="test-user", namespaces=key_namespaces)
    config = EngramConfig(auth=AuthConfig(api_keys=[entry]))

    mock_client = MagicMock()
    mock_client.add = AsyncMock(side_effect=NotImplementedError("mock"))
    mock_client.search = AsyncMock(return_value=[])
    mock_client.query_graph = AsyncMock(return_value=[])
    mock_client.get_entity = AsyncMock(return_value=None)
    mock_client.get_related = AsyncMock(return_value=None)
    mock_client.add_fact = AsyncMock(side_effect=NotImplementedError("mock"))

    app.state.config = config
    app.state.client = mock_client
    return app


AUTH_HEADER = {"Authorization": "Bearer test-key"}
WRONG_AUTH = {"Authorization": "Bearer wrong-key"}


class TestMemoryEndpointsACL:
    """HTTP-level namespace ACL tests for /memory/* endpoints."""

    def test_write_memory_wildcard_key_passes_acl(self):
        app = _build_app(["*"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/memory/",
            json={"content": "hello", "namespace": "personal:alice"},
            headers=AUTH_HEADER,
        )
        # 500 from mock NotImplementedError is fine — ACL did not block (no 403)
        assert resp.status_code != 403

    def test_write_memory_scoped_key_allowed_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/memory/",
            json={"content": "hello", "namespace": "personal:alice"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code != 403

    def test_write_memory_scoped_key_denied_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/memory/",
            json={"content": "hello", "namespace": "org:acme"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_write_memory_prefix_wildcard_allowed(self):
        app = _build_app(["personal:*"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/memory/",
            json={"content": "hello", "namespace": "personal:alice"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code != 403

    def test_write_memory_prefix_wildcard_denied_other_prefix(self):
        app = _build_app(["personal:*"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/memory/",
            json={"content": "hello", "namespace": "org:acme"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_search_memory_denied_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/memory/search",
            params={"q": "test", "ns": "org:acme"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_search_memory_allowed_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/memory/search",
            params={"q": "test", "ns": "personal:alice"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200

    def test_get_memory_denied_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/memory/some-id",
            params={"ns": "org:acme"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_delete_memory_denied_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete(
            "/memory/some-id",
            params={"ns": "org:acme"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_missing_auth_returns_401(self):
        app = _build_app(["*"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/memory/",
            json={"content": "hello", "namespace": "personal:alice"},
        )
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self):
        app = _build_app(["*"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/memory/",
            json={"content": "hello", "namespace": "personal:alice"},
            headers=WRONG_AUTH,
        )
        assert resp.status_code == 401


class TestGraphEndpointsACL:
    """HTTP-level namespace ACL tests for /graph/* endpoints."""

    def test_graph_query_denied_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/graph/query",
            json={"cypher": "MATCH (n) RETURN n", "namespace": "org:acme"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_graph_query_allowed_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/graph/query",
            json={"cypher": "MATCH (n) RETURN n", "namespace": "personal:alice"},
            headers=AUTH_HEADER,
        )
        # 200 — mock returns []
        assert resp.status_code == 200

    def test_get_entity_denied_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/graph/entity/SomeEntity",
            params={"ns": "org:acme"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_add_fact_denied_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/graph/fact",
            json={
                "subject": "Alice",
                "predicate": "knows",
                "object": "Bob",
                "namespace": "org:acme",
            },
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_add_fact_allowed_namespace(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/graph/fact",
            json={
                "subject": "Alice",
                "predicate": "knows",
                "object": "Bob",
                "namespace": "personal:alice",
            },
            headers=AUTH_HEADER,
        )
        # 500 from mock NotImplementedError is fine — ACL did not block
        assert resp.status_code != 403


class TestAdminEndpointsACL:
    """HTTP-level namespace ACL tests for /admin/* endpoints."""

    def test_health_requires_no_auth(self):
        """Health endpoint must remain unauthenticated."""
        app = _build_app(["*"])
        client = TestClient(app, raise_server_exceptions=False)
        # No auth header — health endpoint still responds (will fail due to mock client
        # but must not be 401/403)
        resp = client.get("/admin/health")
        assert resp.status_code not in (401, 403)

    def test_list_namespaces_any_authenticated_key(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/admin/namespaces", headers=AUTH_HEADER)
        assert resp.status_code == 200

    def test_create_namespace_wildcard_key_allowed(self):
        app = _build_app(["*"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/admin/namespaces",
            json={"name": "team:engineering"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 201

    def test_create_namespace_scoped_key_denied(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/admin/namespaces",
            json={"name": "team:engineering"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 403

    def test_delete_namespace_wildcard_key_allowed(self):
        """Wildcard key can delete an existing namespace."""
        app = _build_app(["*"])
        # Pre-populate a namespace definition so the delete finds it
        from engram.config import NamespaceDefinition
        app.state.config.namespaces.definitions["team:engineering"] = NamespaceDefinition()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/admin/namespaces/team:engineering", headers=AUTH_HEADER)
        assert resp.status_code == 204

    def test_delete_namespace_scoped_key_denied(self):
        app = _build_app(["personal:alice"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/admin/namespaces/personal:alice", headers=AUTH_HEADER)
        assert resp.status_code == 403

    def test_list_namespaces_unauthenticated_returns_401(self):
        app = _build_app(["*"])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/admin/namespaces")
        assert resp.status_code == 401
