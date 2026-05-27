"""
test_api_features.py — Tests for Phase 2 features:
  1. RuntimeKeyStore  (create / verify / list / revoke / hard-delete)
  2. check_namespace_access  (read-only enforcement, prefix wildcards)
  3. _validate_key  (YAML key lookup + runtime key store fallback)
  4. require_admin_access  (wildcard-only gate)
  5. Knowledge Q&A endpoint  (POST /api/v1/knowledge/ask)
  6. Knowledge search endpoint  (GET /api/v1/knowledge/search)
  7. Admin key endpoints  (GET / POST / DELETE /api/v1/admin/keys)

All tests are fully self-contained — no ArcadeDB or live Anthropic call needed.
The knowledge router tests mock the engram client and the Anthropic async client.

Usage
-----
cd /path/to/engram
.venv/bin/python -m pytest tools/test_api_features.py -v
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
for pkg in ["core", "api", "mcp-server", "learning", "orchestrator"]:
    p = REPO_ROOT / "packages" / pkg
    if p.exists():
        sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(namespaces, read_only=False):
    e = SimpleNamespace()
    e.namespaces = namespaces
    e.read_only = read_only
    e.user_id = "testuser"
    e.vault_namespaces = []
    return e


def _make_config(keys: list[dict]):
    entries = []
    for k in keys:
        e = SimpleNamespace()
        e.key = k["key"]
        e.user_id = k.get("user_id", "user")
        e.namespaces = k.get("namespaces", ["*"])
        e.read_only = k.get("read_only", False)
        e.vault_namespaces = []
        entries.append(e)
    cfg = SimpleNamespace()
    cfg.auth = SimpleNamespace()
    cfg.auth.api_keys = entries
    return cfg


def _make_search_result(content, namespace="test:ns", score=0.9):
    """Build a fake search result object matching what EngramClient.search returns."""
    mem = SimpleNamespace()
    mem.id = "mem-id-1"
    mem.content = content
    mem.namespace = namespace
    mem.created_at = "2026-01-01T00:00:00+00:00"
    mem.tags = ["test"]
    result = SimpleNamespace()
    result.memory = mem
    result.score = score
    return result


# ===========================================================================
# 1. RuntimeKeyStore
# ===========================================================================

class TestRuntimeKeyStore:

    @pytest.fixture
    def store(self, tmp_path):
        import asyncio
        from engram_api.key_store import RuntimeKeyStore
        db = tmp_path / "keys.db"
        s = RuntimeKeyStore(db_path=db)
        asyncio.run(s.init())
        return s

    async def test_create_returns_plaintext_key(self, store):
        result = await store.create(user_id="alice", namespaces=["personal:alice"])
        assert result["key"]
        assert result["key_prefix"] == result["key"][:8]
        assert result["user_id"] == "alice"
        assert result["namespaces"] == ["personal:alice"]
        assert result["read_only"] is False

    async def test_verify_valid_key(self, store):
        result = await store.create(user_id="bob", namespaces=["org:acme:*"], read_only=True)
        entry = await store.verify(result["key"])
        assert entry is not None
        assert entry.user_id == "bob"
        assert entry.namespaces == ["org:acme:*"]
        assert entry.read_only is True

    async def test_verify_wrong_key_returns_none(self, store):
        await store.create(user_id="carol", namespaces=["*"])
        assert await store.verify("notarealkey") is None

    async def test_verify_revoked_key_returns_none(self, store):
        result = await store.create(user_id="dave", namespaces=["*"])
        await store.revoke(result["id"])
        assert await store.verify(result["key"]) is None

    async def test_revoke_nonexistent_returns_false(self, store):
        assert await store.revoke("00000000-0000-0000-0000-000000000000") is False

    async def test_list_keys_returns_all(self, store):
        await store.create(user_id="u1", namespaces=["a"])
        await store.create(user_id="u2", namespaces=["b"])
        keys = await store.list_keys()
        assert len(keys) == 2
        assert {k["user_id"] for k in keys} == {"u1", "u2"}

    async def test_list_keys_excludes_hash_and_plaintext(self, store):
        await store.create(user_id="u1", namespaces=["*"])
        for row in await store.list_keys():
            assert "key_hash" not in row
            assert "key" not in row

    async def test_hard_delete_removes_row(self, store):
        result = await store.create(user_id="eve", namespaces=["*"])
        assert await store.delete(result["id"]) is True
        assert all(k["id"] != result["id"] for k in await store.list_keys())

    async def test_hard_delete_nonexistent_returns_false(self, store):
        assert await store.delete("no-such-id") is False

    async def test_read_only_flag_round_trips(self, store):
        r = await store.create(user_id="x", namespaces=["ns"], read_only=True)
        entry = await store.verify(r["key"])
        assert entry.read_only is True

    async def test_multiple_creates_produce_unique_keys(self, store):
        r1 = await store.create(user_id="u", namespaces=["*"])
        r2 = await store.create(user_id="u", namespaces=["*"])
        assert r1["key"] != r2["key"]
        assert r1["id"] != r2["id"]


# ===========================================================================
# 2. check_namespace_access
# ===========================================================================

class TestCheckNamespaceAccess:

    async def test_wildcard_allows_any_namespace(self):
        from engram_api.auth import check_namespace_access
        await check_namespace_access(_make_entry(["*"]), "anything:goes:here")

    async def test_exact_match_allowed(self):
        from engram_api.auth import check_namespace_access
        entry = _make_entry(["personal:alice", "team:eng"])
        await check_namespace_access(entry, "personal:alice")
        await check_namespace_access(entry, "team:eng")

    async def test_exact_mismatch_raises_403(self):
        from fastapi import HTTPException
        from engram_api.auth import check_namespace_access
        with pytest.raises(HTTPException) as exc:
            await check_namespace_access(_make_entry(["personal:alice"]), "personal:bob")
        assert exc.value.status_code == 403

    async def test_prefix_wildcard_allows_subnamespace(self):
        from engram_api.auth import check_namespace_access
        entry = _make_entry(["org:acme:*"])
        await check_namespace_access(entry, "org:acme:engineering")
        await check_namespace_access(entry, "org:acme:qa")

    async def test_prefix_wildcard_blocks_sibling_prefix(self):
        from fastapi import HTTPException
        from engram_api.auth import check_namespace_access
        with pytest.raises(HTTPException) as exc:
            await check_namespace_access(_make_entry(["org:acme:*"]), "org:other:engineering")
        assert exc.value.status_code == 403

    async def test_read_only_blocks_write(self):
        from fastapi import HTTPException
        from engram_api.auth import check_namespace_access
        with pytest.raises(HTTPException) as exc:
            await check_namespace_access(_make_entry(["*"], read_only=True), "any:ns", operation="write")
        assert exc.value.status_code == 403
        assert "read-only" in exc.value.detail.lower()

    async def test_read_only_allows_read(self):
        from engram_api.auth import check_namespace_access
        await check_namespace_access(_make_entry(["*"], read_only=True), "any:ns", operation="read")

    async def test_read_write_key_allows_write(self):
        from engram_api.auth import check_namespace_access
        await check_namespace_access(_make_entry(["*"], read_only=False), "any:ns", operation="write")

    async def test_empty_namespaces_blocks_all(self):
        from fastapi import HTTPException
        from engram_api.auth import check_namespace_access
        with pytest.raises(HTTPException):
            await check_namespace_access(_make_entry([]), "personal:me")


# ===========================================================================
# 3. _validate_key — YAML lookup + runtime store fallback
# ===========================================================================

class TestValidateKey:

    async def test_yaml_key_found(self):
        from engram_api.auth import _validate_key
        cfg = _make_config([{"key": "secret-yaml-key", "user_id": "alice"}])
        entry = await _validate_key("Bearer secret-yaml-key", cfg)
        assert entry.user_id == "alice"

    async def test_yaml_key_wrong_raises_401(self):
        from fastapi import HTTPException
        from engram_api.auth import _validate_key
        cfg = _make_config([{"key": "correct-key"}])
        with pytest.raises(HTTPException) as exc:
            await _validate_key("Bearer wrong-key", cfg)
        assert exc.value.status_code == 401

    async def test_missing_authorization_raises_401(self):
        from fastapi import HTTPException
        from engram_api.auth import _validate_key
        with pytest.raises(HTTPException) as exc:
            await _validate_key(None, _make_config([]))
        assert exc.value.status_code == 401

    async def test_malformed_bearer_raises_401(self):
        from fastapi import HTTPException
        from engram_api.auth import _validate_key
        with pytest.raises(HTTPException) as exc:
            await _validate_key("Token abc", _make_config([]))
        assert exc.value.status_code == 401

    async def test_runtime_store_fallback_hit(self):
        from engram_api.auth import _validate_key
        cfg = _make_config([])  # no YAML keys

        fake_entry = SimpleNamespace(user_id="runtime-user", namespaces=["*"], read_only=False)
        store = AsyncMock()
        store.verify = AsyncMock(return_value=fake_entry)

        entry = await _validate_key("Bearer some-runtime-key", cfg, key_store=store)
        assert entry.user_id == "runtime-user"
        store.verify.assert_awaited_once_with("some-runtime-key")

    async def test_runtime_store_miss_raises_401(self):
        from fastapi import HTTPException
        from engram_api.auth import _validate_key
        cfg = _make_config([])

        store = AsyncMock()
        store.verify = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc:
            await _validate_key("Bearer bad-key", cfg, key_store=store)
        assert exc.value.status_code == 401


# ===========================================================================
# 4. require_admin_access
# ===========================================================================

class TestRequireAdminAccess:

    async def test_wildcard_key_passes(self):
        from engram_api.auth import require_admin_access
        entry = _make_entry(["*"])
        result = await require_admin_access(entry)
        assert result is entry

    async def test_scoped_key_raises_403(self):
        from fastapi import HTTPException
        from engram_api.auth import require_admin_access
        with pytest.raises(HTTPException) as exc:
            await require_admin_access(_make_entry(["personal:me", "team:eng"]))
        assert exc.value.status_code == 403

    async def test_empty_namespaces_raises_403(self):
        from fastapi import HTTPException
        from engram_api.auth import require_admin_access
        with pytest.raises(HTTPException) as exc:
            await require_admin_access(_make_entry([]))
        assert exc.value.status_code == 403


# ===========================================================================
# 5 & 6. Knowledge endpoints
# ===========================================================================

def _build_knowledge_app(search_results=None):
    """
    Build a minimal FastAPI app with the knowledge router mounted.
    EngramClient.search is mocked; Anthropic must be patched per-test.
    """
    from fastapi import FastAPI

    from engram_api.routers import knowledge as knowledge_router

    app = FastAPI()
    app.state.config = _make_config([
        {"key": "test-key", "user_id": "admin", "namespaces": ["*"]}
    ])
    app.state.key_store = None

    default_results = [_make_search_result("JWT tokens expire after 24 hours", "team:engineering", 0.92)]
    fake_client = MagicMock()
    fake_client.search = AsyncMock(return_value=default_results if search_results is None else search_results)
    app.state.client = fake_client

    app.include_router(knowledge_router.router, prefix="/api/v1")
    return app


class TestKnowledgeEndpoints:

    _AUTH = {"Authorization": "Bearer test-key"}

    def _client(self, search_results=None):
        from fastapi.testclient import TestClient
        return TestClient(_build_knowledge_app(search_results))

    def _mock_anthropic(self, answer="The auth service uses JWT.", error=None):
        """
        Inject a mock 'anthropic' module into sys.modules so the local
        ``import anthropic`` inside knowledge_ask() gets our fake.
        """
        import sys

        mock_response = MagicMock()
        mock_response.content = [SimpleNamespace(text=answer)]
        mock_response.usage = SimpleNamespace(input_tokens=100, output_tokens=50)

        mock_aclient = AsyncMock()
        if error:
            mock_aclient.messages.create = AsyncMock(side_effect=error)
        else:
            mock_aclient.messages.create = AsyncMock(return_value=mock_response)

        mock_module = MagicMock()
        mock_module.AsyncAnthropic.return_value = mock_aclient
        mock_module.APIError = Exception

        patcher = patch.dict(sys.modules, {"anthropic": mock_module})
        patcher.start()
        return patcher, mock_module

    def test_ask_returns_answer_and_sources(self):
        client = self._client()
        patcher, _ = self._mock_anthropic("Auth uses JWT with 24h expiry.")
        try:
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
                resp = client.post(
                    "/api/v1/knowledge/ask",
                    json={"question": "How does auth work?", "namespace": "team:engineering"},
                    headers=self._AUTH,
                )
        finally:
            patcher.stop()

        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "Auth uses JWT with 24h expiry."
        assert len(data["sources"]) == 1
        assert data["sources"][0]["namespace"] == "team:engineering"
        assert data["tokens_used"] == 150

    def test_ask_without_anthropic_key_returns_503(self):
        import os
        client = self._client()
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            resp = client.post(
                "/api/v1/knowledge/ask",
                json={"question": "Q?", "namespace": "all"},
                headers=self._AUTH,
            )
            assert resp.status_code == 503
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_ask_requires_auth_header(self):
        client = self._client()
        resp = client.post(
            "/api/v1/knowledge/ask",
            json={"question": "Q?", "namespace": "all"},
        )
        assert resp.status_code == 401

    def test_ask_empty_search_results_sources_is_empty_list(self):
        client = self._client(search_results=[])
        patcher, _ = self._mock_anthropic("No relevant memories found.")
        try:
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
                resp = client.post(
                    "/api/v1/knowledge/ask",
                    json={"question": "Q?", "namespace": "all"},
                    headers=self._AUTH,
                )
        finally:
            patcher.stop()

        assert resp.status_code == 200
        assert resp.json()["sources"] == []

    def test_ask_response_includes_model_and_namespace(self):
        client = self._client()
        patcher, _ = self._mock_anthropic("answer")
        try:
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
                resp = client.post(
                    "/api/v1/knowledge/ask",
                    json={"question": "Q?", "namespace": "team:eng", "model": "claude-haiku-4-5-20251001"},
                    headers=self._AUTH,
                )
        finally:
            patcher.stop()

        data = resp.json()
        assert data["namespace"] == "team:eng"
        assert data["model_used"] == "claude-haiku-4-5-20251001"

    def test_knowledge_search_returns_list(self):
        client = self._client()
        resp = client.get(
            "/api/v1/knowledge/search?q=JWT&ns=team%3Aengineering&top_k=3",
            headers=self._AUTH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["content"] == "JWT tokens expire after 24 hours"

    def test_knowledge_search_requires_auth(self):
        client = self._client()
        resp = client.get("/api/v1/knowledge/search?q=x&ns=all")
        assert resp.status_code == 401

    def test_ask_anthropic_error_returns_502(self):
        client = self._client()
        patcher, _ = self._mock_anthropic(error=Exception("rate limited"))
        try:
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
                resp = client.post(
                    "/api/v1/knowledge/ask",
                    json={"question": "Q?", "namespace": "all"},
                    headers=self._AUTH,
                )
        finally:
            patcher.stop()

        assert resp.status_code == 502


# ===========================================================================
# 7. Admin key management endpoints
# ===========================================================================

def _build_admin_app():
    import asyncio
    from engram_api.key_store import RuntimeKeyStore
    from engram_api.routers import admin as admin_router
    from fastapi import FastAPI

    app = FastAPI()
    app.state.config = _make_config([
        {"key": "admin-key", "user_id": "admin", "namespaces": ["*"]}
    ])

    tmp = tempfile.mktemp(suffix=".db")
    store = RuntimeKeyStore(db_path=tmp)
    asyncio.run(store.init())
    app.state.key_store = store

    app.include_router(admin_router.router, prefix="/api/v1")
    return app, store


class TestAdminKeyEndpoints:

    _AUTH = {"Authorization": "Bearer admin-key"}

    def _setup(self):
        from fastapi.testclient import TestClient
        app, store = _build_admin_app()
        return TestClient(app), store, app

    def test_list_keys_empty_initially(self):
        client, _, _ = self._setup()
        resp = client.get("/api/v1/admin/keys", headers=self._AUTH)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_key_returns_201_with_plaintext(self):
        client, _, _ = self._setup()
        resp = client.post(
            "/api/v1/admin/keys",
            json={"user_id": "webapp", "namespaces": ["team:docs"], "read_only": True},
            headers=self._AUTH,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["key"] is not None
        assert len(data["key"]) > 20
        assert data["user_id"] == "webapp"
        assert data["read_only"] is True
        assert data["namespaces"] == ["team:docs"]

    def test_created_key_appears_in_list(self):
        client, _, _ = self._setup()
        client.post(
            "/api/v1/admin/keys",
            json={"user_id": "carol", "namespaces": ["*"]},
            headers=self._AUTH,
        )
        keys = client.get("/api/v1/admin/keys", headers=self._AUTH).json()
        assert any(k["user_id"] == "carol" for k in keys)

    def test_listed_keys_do_not_expose_plaintext(self):
        client, _, _ = self._setup()
        client.post(
            "/api/v1/admin/keys",
            json={"user_id": "u", "namespaces": ["*"]},
            headers=self._AUTH,
        )
        for k in client.get("/api/v1/admin/keys", headers=self._AUTH).json():
            assert "key_hash" not in k
            # key field exists but must be None in list response
            assert k.get("key") is None

    def test_revoke_key_returns_204(self):
        client, _, _ = self._setup()
        created = client.post(
            "/api/v1/admin/keys",
            json={"user_id": "temp", "namespaces": ["*"]},
            headers=self._AUTH,
        ).json()
        resp = client.delete(f"/api/v1/admin/keys/{created['id']}", headers=self._AUTH)
        assert resp.status_code == 204

    def test_revoke_nonexistent_returns_404(self):
        client, _, _ = self._setup()
        resp = client.delete(
            "/api/v1/admin/keys/00000000-0000-0000-0000-000000000000",
            headers=self._AUTH,
        )
        assert resp.status_code == 404

    def test_non_admin_key_cannot_list(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engram_api.routers import admin as admin_router
        import asyncio
        from engram_api.key_store import RuntimeKeyStore

        app = FastAPI()
        app.state.config = _make_config([
            {"key": "admin-key", "user_id": "admin", "namespaces": ["*"]},
            {"key": "scoped-key", "user_id": "bob", "namespaces": ["personal:bob"]},
        ])
        tmp = tempfile.mktemp(suffix=".db")
        store = RuntimeKeyStore(db_path=tmp)
        asyncio.run(store.init())
        app.state.key_store = store
        app.include_router(admin_router.router, prefix="/api/v1")

        client = TestClient(app)
        resp = client.get(
            "/api/v1/admin/keys",
            headers={"Authorization": "Bearer scoped-key"},
        )
        assert resp.status_code == 403

    def test_create_without_auth_returns_401(self):
        client, _, _ = self._setup()
        resp = client.post(
            "/api/v1/admin/keys",
            json={"user_id": "x", "namespaces": ["*"]},
        )
        assert resp.status_code == 401
