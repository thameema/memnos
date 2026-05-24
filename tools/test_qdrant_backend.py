"""
tools/test_qdrant_backend.py — Unit tests for Qdrant vector backend.

qdrant-client is mocked at the sys.modules level so these tests run
without a Qdrant installation. The fake classes in _QDRANT_STUBS replicate
the minimal API surface the backend uses.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Stub qdrant_client before any backend imports touch it
# ---------------------------------------------------------------------------

class _PointStruct:
    def __init__(self, id, vector, payload):
        self.id = id
        self.vector = vector
        self.payload = payload

class _VectorParams:
    def __init__(self, size, distance):
        self.size = size
        self.distance = distance

class _Distance:
    COSINE = "Cosine"

class _Filter:
    def __init__(self, must=None, should=None):
        self.must = must or []

class _FieldCondition:
    def __init__(self, key, match):
        self.key = key
        self.match = match

class _MatchValue:
    def __init__(self, value):
        self.value = value

class _PointIdsList:
    def __init__(self, points):
        self.points = points

class _SetPayload:
    pass

_mock_models = MagicMock()
_mock_models.PointStruct = _PointStruct
_mock_models.VectorParams = _VectorParams
_mock_models.Distance = _Distance
_mock_models.Filter = _Filter
_mock_models.FieldCondition = _FieldCondition
_mock_models.MatchValue = _MatchValue
_mock_models.PointIdsList = _PointIdsList
_mock_models.SetPayload = _SetPayload

_mock_qdrant = MagicMock()
_mock_qdrant.models = _mock_models

sys.modules.setdefault("qdrant_client", _mock_qdrant)
sys.modules.setdefault("qdrant_client.models", _mock_models)

# ---------------------------------------------------------------------------
# Now import the modules under test
# ---------------------------------------------------------------------------

sys.path.insert(0, "/Users/thameema/git/engram/packages/core")
sys.path.insert(0, "/Users/thameema/git/engram/packages/api")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestCreateVectorBackend(unittest.TestCase):
    def test_no_env_returns_none(self):
        env = {k: v for k, v in os.environ.items() if k != "ENGRAM_VECTOR_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            from engram.storage.vector_backend import create_vector_backend
            result = create_vector_backend(1536)
            self.assertIsNone(result)

    def test_arcadedb_explicit_returns_none(self):
        with patch.dict(os.environ, {"ENGRAM_VECTOR_BACKEND": "arcadedb"}):
            from engram.storage.vector_backend import create_vector_backend
            result = create_vector_backend(384)
            self.assertIsNone(result)

    def test_unknown_backend_returns_none(self):
        with patch.dict(os.environ, {"ENGRAM_VECTOR_BACKEND": "pinecone"}):
            from engram.storage.vector_backend import create_vector_backend
            result = create_vector_backend(384)
            self.assertIsNone(result)

    def test_qdrant_backend_returned(self):
        with patch.dict(os.environ, {
            "ENGRAM_VECTOR_BACKEND": "qdrant",
            "ENGRAM_QDRANT_URL": "http://custom:6333",
            "ENGRAM_QDRANT_COLLECTION": "test_col",
        }):
            from engram.storage.vector_backend import create_vector_backend
            result = create_vector_backend(768)
            from engram.storage.qdrant_backend import QdrantVectorBackend
            self.assertIsInstance(result, QdrantVectorBackend)

    def test_qdrant_url_from_env(self):
        with patch.dict(os.environ, {
            "ENGRAM_VECTOR_BACKEND": "qdrant",
            "ENGRAM_QDRANT_URL": "http://custom-qdrant:6333",
            "ENGRAM_QDRANT_COLLECTION": "my_col",
        }):
            from engram.storage.vector_backend import create_vector_backend
            from engram.storage.qdrant_backend import QdrantVectorBackend
            result = create_vector_backend(512)
            self.assertIsInstance(result, QdrantVectorBackend)
            self.assertEqual(result._url, "http://custom-qdrant:6333")
            self.assertEqual(result._collection, "my_col")


# ---------------------------------------------------------------------------
# QdrantVectorBackend — using stubbed qdrant_client
# ---------------------------------------------------------------------------

def _make_mock_async_qdrant_client():
    client = MagicMock()
    coll_list = MagicMock()
    coll_list.collections = []
    client.get_collections = AsyncMock(return_value=coll_list)
    client.create_collection = AsyncMock()
    client.upsert = AsyncMock()
    client.search = AsyncMock(return_value=[])
    client.delete = AsyncMock()
    client.set_payload = AsyncMock()
    client.close = AsyncMock()
    return client


def _make_backend(vector_dim=384):
    from engram.storage.qdrant_backend import QdrantVectorBackend
    return QdrantVectorBackend(
        url="http://localhost:6333",
        collection="test_memories",
        vector_dim=vector_dim,
    )


class TestQdrantVectorBackend(unittest.IsolatedAsyncioTestCase):

    async def test_upsert_calls_qdrant_upsert(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client

        await backend.upsert("abc-123", [0.1] * 384, "test:ns", "fact")
        mock_client.upsert.assert_awaited_once()
        call_kwargs = mock_client.upsert.call_args.kwargs
        self.assertEqual(call_kwargs["collection_name"], "test_memories")
        points = call_kwargs["points"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].id, "abc-123")
        self.assertEqual(points[0].payload["namespace"], "test:ns")
        self.assertEqual(points[0].payload["memory_type"], "fact")
        self.assertFalse(points[0].payload["superseded"])

    async def test_search_passes_namespace_filter(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client

        hit = MagicMock()
        hit.id = "mem-001"
        hit.score = 0.95
        mock_client.search = AsyncMock(return_value=[hit])

        results = await backend.search([0.2] * 384, "test:ns", top_k=5)
        self.assertEqual(results, [("mem-001", 0.95)])
        call_kwargs = mock_client.search.call_args.kwargs
        self.assertEqual(call_kwargs["collection_name"], "test_memories")
        self.assertEqual(call_kwargs["limit"], 5)
        filt = call_kwargs["query_filter"]
        must_keys = [c.key for c in filt.must]
        self.assertIn("namespace", must_keys)

    async def test_search_excludes_superseded_by_default(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client

        await backend.search([0.1] * 384, "test:ns", top_k=5, include_superseded=False)
        filt = mock_client.search.call_args.kwargs["query_filter"]
        must_keys = [c.key for c in filt.must]
        self.assertIn("superseded", must_keys)

    async def test_search_includes_superseded_when_requested(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client

        await backend.search([0.1] * 384, "test:ns", top_k=5, include_superseded=True)
        filt = mock_client.search.call_args.kwargs["query_filter"]
        must_keys = [c.key for c in filt.must]
        self.assertNotIn("superseded", must_keys)

    async def test_search_returns_id_score_pairs(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client

        h1 = MagicMock(); h1.id = "m1"; h1.score = 0.9
        h2 = MagicMock(); h2.id = "m2"; h2.score = 0.7
        mock_client.search = AsyncMock(return_value=[h1, h2])

        results = await backend.search([0.1] * 384, "ns", top_k=2)
        self.assertEqual(results, [("m1", 0.9), ("m2", 0.7)])

    async def test_delete_calls_qdrant_delete(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client

        await backend.delete("mem-to-delete")
        mock_client.delete.assert_awaited_once()
        call_kwargs = mock_client.delete.call_args.kwargs
        self.assertEqual(call_kwargs["collection_name"], "test_memories")
        self.assertIn("mem-to-delete", call_kwargs["points_selector"].points)

    async def test_mark_superseded_sets_payload(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client

        await backend.mark_superseded("mem-old")
        mock_client.set_payload.assert_awaited_once()
        call_kwargs = mock_client.set_payload.call_args.kwargs
        self.assertTrue(call_kwargs["payload"]["superseded"])
        self.assertIn("mem-old", call_kwargs["points"])

    async def test_close_clears_client_reference(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client

        await backend.close()
        mock_client.close.assert_awaited_once()
        self.assertIsNone(backend._client)

    async def test_close_when_no_client_is_noop(self):
        backend = _make_backend()
        self.assertIsNone(backend._client)
        await backend.close()  # should not raise

    async def test_collection_created_if_missing(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend(vector_dim=512)
        # collections list is empty → should create
        _mock_qdrant.AsyncQdrantClient = MagicMock(return_value=mock_client)

        await backend._ensure_client()
        mock_client.create_collection.assert_awaited_once()
        create_kwargs = mock_client.create_collection.call_args.kwargs
        self.assertEqual(create_kwargs["collection_name"], "test_memories")
        self.assertEqual(create_kwargs["vectors_config"].size, 512)

    async def test_collection_not_recreated_if_exists(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()

        existing = MagicMock()
        existing.name = "test_memories"
        coll_list = MagicMock()
        coll_list.collections = [existing]
        mock_client.get_collections = AsyncMock(return_value=coll_list)
        _mock_qdrant.AsyncQdrantClient = MagicMock(return_value=mock_client)

        await backend._ensure_client()
        mock_client.create_collection.assert_not_awaited()

    async def test_second_ensure_client_reuses_connection(self):
        mock_client = _make_mock_async_qdrant_client()
        backend = _make_backend()
        backend._client = mock_client
        # Second call should not create another client
        result = await backend._ensure_client()
        self.assertIs(result, mock_client)

    async def test_missing_qdrant_client_raises_import_error(self):
        from engram.storage.qdrant_backend import QdrantVectorBackend
        backend = QdrantVectorBackend(url="http://x", collection="c", vector_dim=4)
        # Temporarily hide qdrant_client
        saved = sys.modules.pop("qdrant_client", None)
        sys.modules["qdrant_client"] = None  # type: ignore[assignment]
        try:
            with self.assertRaises((ImportError, Exception)):
                await backend._ensure_client()
        finally:
            if saved is not None:
                sys.modules["qdrant_client"] = saved
            else:
                sys.modules.pop("qdrant_client", None)
            sys.modules["qdrant_client"] = _mock_qdrant


# ---------------------------------------------------------------------------
# EngramClient._vector_search routing
# ---------------------------------------------------------------------------

class TestEngramClientVectorSearchRouting(unittest.IsolatedAsyncioTestCase):
    def _make_client(self):
        from engram.models import MemoryEntry, MemoryType, SearchResult
        from engram.client import EngramClient

        cfg = MagicMock()
        cfg.embeddings = MagicMock()
        cfg.arcadedb = MagicMock()
        cfg.vault = MagicMock()
        cfg.vault.enabled = False

        mem = MemoryEntry(
            id="mem-001",
            content="test memory",
            namespace="test:ns",
            memory_type=MemoryType.fact,
        )

        with patch("engram.client.get_embedder"), \
             patch("engram.client.ArcadeDBClient"), \
             patch("engram.client.get_extractor"), \
             patch("engram.client.get_vault_client"), \
             patch("engram.client.create_vector_backend", return_value=None):
            client = EngramClient(cfg)

        client._embedder = MagicMock()
        client._embedder.embed = AsyncMock(return_value=[0.1] * 384)
        client._embedder.vector_size = 384
        client._arcadedb = MagicMock()
        client._arcadedb.vector_search = AsyncMock(return_value=[
            SearchResult(memory=mem, score=0.9, source="vector", is_current=True, recency_score=1.0)
        ])
        client._arcadedb.get_memory = AsyncMock(return_value=mem)
        client._started = True
        return client, mem

    async def test_no_vector_backend_uses_arcadedb(self):
        client, _ = self._make_client()
        client._vector_backend = None
        results = await client._vector_search([0.1] * 384, "test:ns", 5, False, "query")
        client._arcadedb.vector_search.assert_awaited_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "vector")

    async def test_qdrant_backend_used_when_set(self):
        client, mem = self._make_client()
        mock_backend = MagicMock()
        mock_backend.search = AsyncMock(return_value=[("mem-001", 0.88)])
        client._vector_backend = mock_backend

        results = await client._vector_search([0.1] * 384, "test:ns", 5, False, "query")
        mock_backend.search.assert_awaited_once_with(
            embedding=[0.1] * 384,
            namespace="test:ns",
            top_k=5,
            include_superseded=False,
        )
        client._arcadedb.get_memory.assert_awaited_once_with("mem-001", "test:ns")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "qdrant")
        self.assertAlmostEqual(results[0].score, 0.88)

    async def test_qdrant_skips_missing_memories(self):
        client, _ = self._make_client()
        mock_backend = MagicMock()
        mock_backend.search = AsyncMock(return_value=[("nonexistent", 0.75)])
        client._arcadedb.get_memory = AsyncMock(return_value=None)
        client._vector_backend = mock_backend

        results = await client._vector_search([0.1] * 384, "test:ns", 5, False, "q")
        self.assertEqual(results, [])

    async def test_qdrant_upsert_called_on_add(self):
        client, _ = self._make_client()
        mock_backend = MagicMock()
        mock_backend.upsert = AsyncMock()
        client._vector_backend = mock_backend

        client._arcadedb.insert_memory = AsyncMock(return_value=None)
        client._arcadedb.get_fanout_subscribers = AsyncMock(return_value=[])
        client._extractor = MagicMock()
        client._extractor.extract = AsyncMock(return_value=[])

        await client.add("test content", namespace="test:ns")
        mock_backend.upsert.assert_awaited_once()
        call_args = mock_backend.upsert.call_args
        # upsert(memory_id, embedding, namespace, memory_type=...)
        positional = call_args.args
        self.assertEqual(positional[2], "test:ns")   # namespace is 3rd positional arg
        self.assertEqual(call_args.kwargs.get("memory_type"), "fact")

    async def test_upsert_failure_is_non_fatal(self):
        client, _ = self._make_client()
        mock_backend = MagicMock()
        mock_backend.upsert = AsyncMock(side_effect=RuntimeError("qdrant down"))
        client._vector_backend = mock_backend
        client._arcadedb.insert_memory = AsyncMock(return_value=None)
        client._arcadedb.get_fanout_subscribers = AsyncMock(return_value=[])
        client._extractor = MagicMock()
        client._extractor.extract = AsyncMock(return_value=[])

        # Should not raise even though upsert failed
        result = await client.add("test content", namespace="test:ns")
        self.assertIsNotNone(result)

    async def test_stop_closes_vector_backend(self):
        client, _ = self._make_client()
        mock_backend = MagicMock()
        mock_backend.close = AsyncMock()
        client._vector_backend = mock_backend
        client._arcadedb.close = AsyncMock()
        client._started = True

        await client.stop()
        mock_backend.close.assert_awaited_once()
        self.assertIsNone(client._vector_backend)


if __name__ == "__main__":
    unittest.main(verbosity=2)
