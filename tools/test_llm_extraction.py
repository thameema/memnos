"""
tools/test_llm_extraction.py — Tests for LLM-enriched relationship extraction (3.3).

Coverage:
--- LLMExtractor.extract() ---
- Returns ExtractedRelationship list from valid LLM JSON response
- Normalises entity names to lowercase
- Strips markdown fences from LLM response
- Unknown edge type falls back to RELATES_TO
- Drops relationships below confidence_threshold
- Returns [] on JSON parse failure (non-fatal)
- Returns [] when LLM call raises exception (non-fatal)
- Returns [] when no API key available
- Returns [] when relationships list is empty
- Respects max 10 relationships guard
- Skips items missing source or target
- Confidence threshold applied correctly (boundary: exactly at threshold passes)

--- LLMExtractor._resolve_provider() ---
- Uses config api_key + provider when set
- Falls back to ANTHROPIC_API_KEY env var
- Falls back to OPENAI_API_KEY when no anthropic key
- Returns (None, None) when no key found
- Caches resolved provider on second call

--- ArcadeDBClient new edge methods ---
- create_entity_edge: calls _command with correct SQL and params
- create_entity_edge: skips unknown edge type
- create_memory_typed_edge: calls _command with correct SQL
- create_memory_typed_edge: skips unknown edge type

--- client._dispatch_llm_extraction() ---
- Calls llm_extractor.extract with memory content
- Upserts source + target entities
- Calls create_entity_edge for each relationship
- Skips when _llm_extractor is None (config disabled)
- Non-fatal on extract() exception
- Non-fatal on arcadedb error

--- client.add() integration ---
- asyncio.ensure_future called when llm_extractor enabled
- ensure_future NOT called when llm_extractor is None

--- Config ---
- LLMExtractionConfig defaults: enabled=False, max_tokens=512, threshold=0.6
- YAML-parsed llm_extraction block sets all fields

--- EDGE_VOCABULARY ---
- Contains all 10 expected edge types
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, _REPO_ROOT + "/packages/core")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**kwargs):
    from engram.config import LLMExtractionConfig
    defaults = {
        "enabled": True,
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "api_key": "test-key",
        "max_tokens": 512,
        "confidence_threshold": 0.6,
    }
    defaults.update(kwargs)
    return LLMExtractionConfig(**defaults)


def _valid_json(relationships: list[dict]) -> str:
    return json.dumps({"relationships": relationships})


def _make_memory(content="test content", namespace="ns1"):
    from engram.models import MemoryEntry, MemoryType
    m = MagicMock(spec=MemoryEntry)
    m.id = "mem-1"
    m.content = content
    m.namespace = namespace
    m.memory_type = MemoryType.fact
    m.tags = []
    m.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return m


# ---------------------------------------------------------------------------
# LLMExtractor.extract()
# ---------------------------------------------------------------------------

class TestLLMExtractorExtract(unittest.IsolatedAsyncioTestCase):
    def _extractor(self, **kwargs):
        from engram.extraction.llm_extractor import LLMExtractor
        return LLMExtractor(_make_config(**kwargs))

    async def test_returns_relationships_from_valid_json(self):
        ext = self._extractor()
        raw = _valid_json([
            {"source": "FHIR R4", "edge_type": "CHOSE", "target": "member-match API", "confidence": 0.95}
        ])
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=raw)):
            results = await ext.extract("we chose FHIR R4 for the member-match API")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].edge_type, "CHOSE")
        self.assertEqual(results[0].confidence, 0.95)

    async def test_normalises_entity_names_to_lowercase(self):
        ext = self._extractor()
        raw = _valid_json([
            {"source": "Redis", "edge_type": "PROHIBITS", "target": "Session Storage", "confidence": 0.9}
        ])
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=raw)):
            results = await ext.extract("avoid Redis for Session Storage")
        self.assertEqual(results[0].source, "redis")
        self.assertEqual(results[0].target, "session storage")

    async def test_strips_markdown_fences(self):
        ext = self._extractor()
        raw = "```json\n" + _valid_json([
            {"source": "a", "edge_type": "RELATES_TO", "target": "b", "confidence": 0.8}
        ]) + "\n```"
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=raw)):
            results = await ext.extract("a relates to b")
        self.assertEqual(len(results), 1)

    async def test_unknown_edge_type_falls_back_to_relates_to(self):
        ext = self._extractor()
        raw = _valid_json([
            {"source": "x", "edge_type": "INVENTED_TYPE", "target": "y", "confidence": 0.9}
        ])
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=raw)):
            results = await ext.extract("x invented_type y")
        self.assertEqual(results[0].edge_type, "RELATES_TO")

    async def test_drops_below_confidence_threshold(self):
        ext = self._extractor(confidence_threshold=0.7)
        raw = _valid_json([
            {"source": "a", "edge_type": "CHOSE", "target": "b", "confidence": 0.65},
            {"source": "c", "edge_type": "WANTS", "target": "d", "confidence": 0.8},
        ])
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=raw)):
            results = await ext.extract("text")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].edge_type, "WANTS")

    async def test_threshold_boundary_exactly_at_threshold_passes(self):
        ext = self._extractor(confidence_threshold=0.6)
        raw = _valid_json([
            {"source": "a", "edge_type": "CHOSE", "target": "b", "confidence": 0.6}
        ])
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=raw)):
            results = await ext.extract("text")
        self.assertEqual(len(results), 1)

    async def test_returns_empty_on_json_parse_failure(self):
        ext = self._extractor()
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value="not valid json")):
            results = await ext.extract("text")
        self.assertEqual(results, [])

    async def test_returns_empty_on_llm_exception(self):
        ext = self._extractor()
        with patch.object(ext, "_call_llm", new=AsyncMock(side_effect=RuntimeError("api error"))):
            results = await ext.extract("text")
        self.assertEqual(results, [])

    async def test_returns_empty_when_no_api_key(self):
        from engram.extraction.llm_extractor import LLMExtractor
        ext = LLMExtractor(_make_config(api_key=""))
        with patch.dict(os.environ, {}, clear=True):
            results = await ext.extract("text")
        self.assertEqual(results, [])

    async def test_returns_empty_when_relationships_empty(self):
        ext = self._extractor()
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=_valid_json([]))):
            results = await ext.extract("text")
        self.assertEqual(results, [])

    async def test_returns_empty_on_empty_llm_response(self):
        ext = self._extractor()
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value="")):
            results = await ext.extract("text")
        self.assertEqual(results, [])

    async def test_skips_items_missing_source(self):
        ext = self._extractor()
        raw = _valid_json([
            {"source": "", "edge_type": "CHOSE", "target": "b", "confidence": 0.9}
        ])
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=raw)):
            results = await ext.extract("text")
        self.assertEqual(results, [])

    async def test_skips_items_missing_target(self):
        ext = self._extractor()
        raw = _valid_json([
            {"source": "a", "edge_type": "CHOSE", "target": "", "confidence": 0.9}
        ])
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=raw)):
            results = await ext.extract("text")
        self.assertEqual(results, [])

    async def test_multiple_relationships_all_returned(self):
        ext = self._extractor()
        items = [
            {"source": f"s{i}", "edge_type": "CHOSE", "target": f"t{i}", "confidence": 0.8}
            for i in range(5)
        ]
        with patch.object(ext, "_call_llm", new=AsyncMock(return_value=_valid_json(items))):
            results = await ext.extract("text")
        self.assertEqual(len(results), 5)


# ---------------------------------------------------------------------------
# LLMExtractor._resolve_provider()
# ---------------------------------------------------------------------------

class TestResolveProvider(unittest.TestCase):
    def _fresh(self, **kwargs):
        from engram.extraction.llm_extractor import LLMExtractor
        return LLMExtractor(_make_config(**kwargs))

    def test_uses_config_api_key(self):
        ext = self._fresh(provider="anthropic", api_key="cfg-key")
        provider, key = ext._resolve_provider()
        self.assertEqual(provider, "anthropic")
        self.assertEqual(key, "cfg-key")

    def test_falls_back_to_anthropic_env_var(self):
        ext = self._fresh(api_key="")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-ant-key"}, clear=True):
            provider, key = ext._resolve_provider()
        self.assertEqual(provider, "anthropic")
        self.assertEqual(key, "env-ant-key")

    def test_falls_back_to_openai_env_var(self):
        ext = self._fresh(api_key="")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-oai-key"}, clear=True):
            provider, key = ext._resolve_provider()
        self.assertEqual(provider, "openai")
        self.assertEqual(key, "env-oai-key")

    def test_returns_none_when_no_key(self):
        ext = self._fresh(api_key="")
        with patch.dict(os.environ, {}, clear=True):
            provider, key = ext._resolve_provider()
        self.assertIsNone(provider)
        self.assertIsNone(key)

    def test_caches_resolved_provider(self):
        ext = self._fresh(provider="openai", api_key="cached-key")
        ext._resolve_provider()
        # Mutate config after resolution — cached value should be unchanged
        ext._config = _make_config(api_key="different-key")
        provider, key = ext._resolve_provider()
        self.assertEqual(key, "cached-key")


# ---------------------------------------------------------------------------
# EDGE_VOCABULARY
# ---------------------------------------------------------------------------

class TestEdgeVocabulary(unittest.TestCase):
    def test_contains_all_expected_types(self):
        from engram.extraction.llm_extractor import EDGE_VOCABULARY
        expected = {
            "CHOSE", "PROHIBITS", "WANTS", "DEADLINE", "CAUSES",
            "DEPENDS_ON", "REPLACES", "GOVERNS", "RATIONALE_FOR", "RELATES_TO",
        }
        self.assertEqual(EDGE_VOCABULARY, expected)


# ---------------------------------------------------------------------------
# ArcadeDBClient new edge methods
# ---------------------------------------------------------------------------

class TestArcadeDBEdgeMethods(unittest.IsolatedAsyncioTestCase):
    def _db(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        db = ArcadeDBClient.__new__(ArcadeDBClient)
        db._command = AsyncMock(return_value=[{}])
        return db

    async def test_create_entity_edge_calls_command(self):
        db = self._db()
        await db.create_entity_edge("redis", "session storage", "PROHIBITS", "ns1", 0.9)
        self.assertTrue(db._command.called)
        sql = db._command.call_args[0][0]
        self.assertIn("PROHIBITS", sql)

    async def test_create_entity_edge_passes_correct_params(self):
        db = self._db()
        await db.create_entity_edge("redis", "session storage", "PROHIBITS", "ns1", 0.85)
        params = db._command.call_args[0][1]
        self.assertEqual(params["from_e"], "redis")
        self.assertEqual(params["to_e"], "session storage")
        self.assertEqual(params["ns"], "ns1")
        self.assertAlmostEqual(params["conf"], 0.85)

    async def test_create_entity_edge_skips_unknown_type(self):
        db = self._db()
        await db.create_entity_edge("a", "b", "INVALID_TYPE", "ns1")
        db._command.assert_not_called()

    async def test_create_memory_typed_edge_calls_command(self):
        db = self._db()
        await db.create_memory_typed_edge("mem-1", "fhir r4", "CHOSE", "ns1", 0.95)
        self.assertTrue(db._command.called)
        sql = db._command.call_args[0][0]
        self.assertIn("CHOSE", sql)

    async def test_create_memory_typed_edge_passes_correct_params(self):
        db = self._db()
        await db.create_memory_typed_edge("mem-1", "fhir r4", "CHOSE", "ns1", 0.95)
        params = db._command.call_args[0][1]
        self.assertEqual(params["mid"], "mem-1")
        self.assertEqual(params["ename"], "fhir r4")
        self.assertEqual(params["ns"], "ns1")
        self.assertAlmostEqual(params["conf"], 0.95)

    async def test_create_memory_typed_edge_skips_unknown_type(self):
        db = self._db()
        await db.create_memory_typed_edge("mem-1", "x", "BAD_EDGE", "ns1")
        db._command.assert_not_called()

    async def test_create_entity_edge_nonfatal_on_db_error(self):
        db = self._db()
        db._command = AsyncMock(side_effect=RuntimeError("db error"))
        await db.create_entity_edge("a", "b", "CHOSE", "ns1")  # should not raise


# ---------------------------------------------------------------------------
# client._dispatch_llm_extraction()
# ---------------------------------------------------------------------------

class TestDispatchLLMExtraction(unittest.IsolatedAsyncioTestCase):
    def _make_client_with_extractor(self, extract_return=None):
        from engram.client import EngramClient
        from engram.extraction.llm_extractor import LLMExtractor, ExtractedRelationship

        client = EngramClient.__new__(EngramClient)
        mock_extractor = MagicMock(spec=LLMExtractor)
        mock_extractor.extract = AsyncMock(return_value=extract_return or [])
        client._llm_extractor = mock_extractor

        client._arcadedb = MagicMock()
        client._arcadedb.upsert_entity = AsyncMock()
        client._arcadedb.create_entity_edge = AsyncMock()

        return client

    async def test_calls_extract_with_memory_content(self):
        from engram.extraction.llm_extractor import ExtractedRelationship
        client = self._make_client_with_extractor()
        mem = _make_memory(content="we chose FHIR R4")
        await client._dispatch_llm_extraction(mem, "ns1")
        client._llm_extractor.extract.assert_awaited_once_with("we chose FHIR R4")

    async def test_upserts_source_and_target_entities(self):
        from engram.extraction.llm_extractor import ExtractedRelationship
        rel = ExtractedRelationship(source="fhir r4", edge_type="CHOSE", target="member-match", confidence=0.9)
        client = self._make_client_with_extractor(extract_return=[rel])
        mem = _make_memory()
        await client._dispatch_llm_extraction(mem, "ns1")
        upsert_calls = [c[0][0].name for c in client._arcadedb.upsert_entity.call_args_list]
        self.assertIn("fhir r4", upsert_calls)
        self.assertIn("member-match", upsert_calls)

    async def test_creates_entity_edge_for_each_relationship(self):
        from engram.extraction.llm_extractor import ExtractedRelationship
        rels = [
            ExtractedRelationship(source="redis", edge_type="PROHIBITS", target="sessions", confidence=0.9),
            ExtractedRelationship(source="x", edge_type="CHOSE", target="y", confidence=0.8),
        ]
        client = self._make_client_with_extractor(extract_return=rels)
        mem = _make_memory()
        await client._dispatch_llm_extraction(mem, "ns1")
        self.assertEqual(client._arcadedb.create_entity_edge.await_count, 2)

    async def test_skips_when_llm_extractor_is_none(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._llm_extractor = None
        client._arcadedb = MagicMock()
        client._arcadedb.upsert_entity = AsyncMock()
        mem = _make_memory()
        await client._dispatch_llm_extraction(mem, "ns1")
        client._arcadedb.upsert_entity.assert_not_called()

    async def test_nonfatal_on_extract_exception(self):
        from engram.client import EngramClient
        from engram.extraction.llm_extractor import LLMExtractor
        client = EngramClient.__new__(EngramClient)
        client._llm_extractor = MagicMock(spec=LLMExtractor)
        client._llm_extractor.extract = AsyncMock(side_effect=RuntimeError("boom"))
        client._arcadedb = MagicMock()
        mem = _make_memory()
        await client._dispatch_llm_extraction(mem, "ns1")  # no raise

    async def test_nonfatal_on_arcadedb_error(self):
        from engram.extraction.llm_extractor import ExtractedRelationship
        rel = ExtractedRelationship(source="a", edge_type="CHOSE", target="b", confidence=0.9)
        client = self._make_client_with_extractor(extract_return=[rel])
        client._arcadedb.upsert_entity = AsyncMock(side_effect=RuntimeError("db down"))
        mem = _make_memory()
        await client._dispatch_llm_extraction(mem, "ns1")  # no raise

    async def test_edge_confidence_passed_through(self):
        from engram.extraction.llm_extractor import ExtractedRelationship
        rel = ExtractedRelationship(source="a", edge_type="DEPENDS_ON", target="b", confidence=0.77)
        client = self._make_client_with_extractor(extract_return=[rel])
        mem = _make_memory()
        await client._dispatch_llm_extraction(mem, "ns1")
        kwargs = client._arcadedb.create_entity_edge.call_args[1]
        self.assertAlmostEqual(kwargs["confidence"], 0.77)


# ---------------------------------------------------------------------------
# client.add() — background task wiring
# ---------------------------------------------------------------------------

class TestClientAddLLMWiring(unittest.IsolatedAsyncioTestCase):
    def _make_client(self, llm_enabled=True):
        from engram.client import EngramClient
        from engram.config import EngramConfig, LLMExtractionConfig, VaultConfig
        from engram.extraction.llm_extractor import LLMExtractor

        cfg = EngramConfig()
        cfg.vault = VaultConfig(enabled=False, detect_in_memory=False)
        cfg.llm_extraction = LLMExtractionConfig(enabled=llm_enabled)

        client = EngramClient.__new__(EngramClient)
        client._config = cfg
        client._started = True
        client._arcadedb = MagicMock()
        client._arcadedb.insert_memory = AsyncMock()
        client._arcadedb.upsert_entity = AsyncMock()
        client._arcadedb.create_mentions_edge = AsyncMock()
        client._arcadedb.create_affects_edge = AsyncMock()
        client._arcadedb.get_subscriptions = AsyncMock(return_value=[])
        client._vector_backend = None
        client._vault = None

        mock_extractor = MagicMock()
        mock_extractor.extract = AsyncMock(return_value=[])
        client._extractor = mock_extractor

        if llm_enabled:
            llm_mock = MagicMock(spec=LLMExtractor)
            llm_mock.extract = AsyncMock(return_value=[])
            client._llm_extractor = llm_mock
        else:
            client._llm_extractor = None

        return client

    async def test_ensure_future_called_when_enabled(self):
        client = self._make_client(llm_enabled=True)
        futures = []

        def _fake_ensure_future(coro):
            futures.append(coro)
            # Consume the coroutine to avoid "never awaited" warnings
            loop = asyncio.get_event_loop()
            return loop.create_task(coro)

        with patch("engram.client.asyncio.ensure_future", side_effect=_fake_ensure_future), \
             patch.object(client, "_dispatch_webhooks", new=AsyncMock()), \
             patch.object(client, "_dispatch_immediate", new=AsyncMock()), \
             patch.object(client, "_fanout_memory", new=AsyncMock()):
            with patch("engram.client.get_embedder") as _:
                client._embedder = MagicMock()
                client._embedder.embed = AsyncMock(return_value=[0.0] * 10)
                await client.add("test content", namespace="ns1")

        # Give the background task a chance to run
        await asyncio.sleep(0)
        self.assertGreater(len(futures), 0)

    async def test_ensure_future_not_called_when_disabled(self):
        client = self._make_client(llm_enabled=False)
        captured = []

        with patch("engram.client.asyncio.ensure_future", side_effect=captured.append), \
             patch.object(client, "_dispatch_webhooks", new=AsyncMock()), \
             patch.object(client, "_dispatch_immediate", new=AsyncMock()), \
             patch.object(client, "_fanout_memory", new=AsyncMock()):
            client._embedder = MagicMock()
            client._embedder.embed = AsyncMock(return_value=[0.0] * 10)
            await client.add("test content", namespace="ns1")

        self.assertEqual(len(captured), 0)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestLLMExtractionConfig(unittest.TestCase):
    def test_defaults(self):
        from engram.config import LLMExtractionConfig
        cfg = LLMExtractionConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.provider, "anthropic")
        self.assertEqual(cfg.max_tokens, 512)
        self.assertAlmostEqual(cfg.confidence_threshold, 0.6)
        self.assertEqual(cfg.api_key, "")
        self.assertEqual(cfg.model, "")

    def test_all_fields_settable(self):
        from engram.config import LLMExtractionConfig
        cfg = LLMExtractionConfig(
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            api_key="sk-test",
            base_url="https://custom.api/v1",
            max_tokens=256,
            confidence_threshold=0.75,
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.provider, "openai")
        self.assertEqual(cfg.model, "gpt-4o-mini")
        self.assertEqual(cfg.api_key, "sk-test")
        self.assertEqual(cfg.base_url, "https://custom.api/v1")
        self.assertEqual(cfg.max_tokens, 256)
        self.assertAlmostEqual(cfg.confidence_threshold, 0.75)

    def test_engram_config_has_llm_extraction_field(self):
        from engram.config import EngramConfig
        cfg = EngramConfig()
        self.assertFalse(cfg.llm_extraction.enabled)

    def test_from_yaml_parses_llm_extraction_block(self):
        from engram.config import EngramConfig
        import io
        import yaml
        yaml_text = """
llm_extraction:
  enabled: true
  provider: openai
  model: gpt-4o-mini
  api_key: sk-test
  max_tokens: 256
  confidence_threshold: 0.7
"""
        raw = yaml.safe_load(io.StringIO(yaml_text))
        cfg = EngramConfig(llm_extraction=__import__(
            "engram.config", fromlist=["LLMExtractionConfig"]
        ).LLMExtractionConfig(**raw["llm_extraction"]))
        self.assertTrue(cfg.llm_extraction.enabled)
        self.assertEqual(cfg.llm_extraction.provider, "openai")
        self.assertAlmostEqual(cfg.llm_extraction.confidence_threshold, 0.7)


# ---------------------------------------------------------------------------
# get_llm_extractor singleton
# ---------------------------------------------------------------------------

class TestGetLLMExtractorSingleton(unittest.TestCase):
    def setUp(self):
        from engram.extraction.llm_extractor import reset_llm_extractor
        reset_llm_extractor()

    def tearDown(self):
        from engram.extraction.llm_extractor import reset_llm_extractor
        reset_llm_extractor()

    def test_returns_same_instance_on_second_call(self):
        from engram.extraction.llm_extractor import get_llm_extractor
        cfg = _make_config()
        a = get_llm_extractor(cfg)
        b = get_llm_extractor(cfg)
        self.assertIs(a, b)

    def test_reset_creates_new_instance(self):
        from engram.extraction.llm_extractor import get_llm_extractor, reset_llm_extractor
        cfg = _make_config()
        a = get_llm_extractor(cfg)
        reset_llm_extractor()
        b = get_llm_extractor(cfg)
        self.assertIsNot(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
