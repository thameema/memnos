"""
tests/test_community_detection.py — Feature 3.4: Community Detection test suite.

Run with:
    python -m pytest tools/test_community_detection.py -v --no-header -p no:flask
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/thameema/git/engram/packages/core")
sys.path.insert(0, "/Users/thameema/git/engram/packages/api")
sys.path.insert(0, "/Users/thameema/git/engram/packages/mcp-server")

# ---------------------------------------------------------------------------
# TestCommunityResult
# ---------------------------------------------------------------------------

class TestCommunityResult(unittest.TestCase):
    """Tests for the CommunityResult dataclass."""

    def setUp(self):
        from engram.community.detector import CommunityResult
        self.CommunityResult = CommunityResult

    def test_fields_accessible(self):
        r = self.CommunityResult(
            community_id="abc123",
            label="python / fhir",
            namespace="org:acme",
            member_names=["python", "fhir", "kafka"],
            member_count=3,
        )
        self.assertEqual(r.community_id, "abc123")
        self.assertEqual(r.label, "python / fhir")
        self.assertEqual(r.namespace, "org:acme")
        self.assertEqual(r.member_names, ["python", "fhir", "kafka"])
        self.assertEqual(r.member_count, 3)

    def test_member_count_matches_len(self):
        members = ["alpha", "beta", "gamma", "delta"]
        r = self.CommunityResult(
            community_id="xyz",
            label="alpha / beta",
            namespace="ns",
            member_names=members,
            member_count=len(members),
        )
        self.assertEqual(r.member_count, len(r.member_names))

    def test_label_is_string(self):
        r = self.CommunityResult(
            community_id="id1",
            label="entity-a / entity-b",
            namespace="org",
            member_names=["entity-a", "entity-b"],
            member_count=2,
        )
        self.assertIsInstance(r.label, str)


# ---------------------------------------------------------------------------
# TestGetEntityCooccurrences
# ---------------------------------------------------------------------------

class TestGetEntityCooccurrences(unittest.IsolatedAsyncioTestCase):
    """Tests for ArcadeDBClient.get_entity_cooccurrences."""

    def _make_client(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        client = ArcadeDBClient.__new__(ArcadeDBClient)
        client._query = AsyncMock()
        client._command = AsyncMock()
        return client

    async def test_returns_pairs_from_shared_memory(self):
        client = self._make_client()
        # Two entities co-appear in memory m1
        client._query.return_value = [
            {"memory_id": "m1", "entity_name": "python"},
            {"memory_id": "m1", "entity_name": "fhir"},
        ]
        from engram.storage.arcadedb_client import ArcadeDBClient
        pairs = await ArcadeDBClient.get_entity_cooccurrences(client, "org:acme")
        self.assertEqual(len(pairs), 1)
        pair_set = {frozenset(p) for p in pairs}
        self.assertIn(frozenset({"python", "fhir"}), pair_set)

    async def test_no_pairs_when_entities_dont_share_memories(self):
        client = self._make_client()
        # Entities in different memories — no co-occurrence
        client._query.return_value = [
            {"memory_id": "m1", "entity_name": "python"},
            {"memory_id": "m2", "entity_name": "fhir"},
        ]
        from engram.storage.arcadedb_client import ArcadeDBClient
        pairs = await ArcadeDBClient.get_entity_cooccurrences(client, "org:acme")
        self.assertEqual(pairs, [])

    async def test_deduplicates_entity_names_within_same_memory(self):
        client = self._make_client()
        # Same entity name appears twice for same memory — should not create self-pair
        client._query.return_value = [
            {"memory_id": "m1", "entity_name": "python"},
            {"memory_id": "m1", "entity_name": "python"},
            {"memory_id": "m1", "entity_name": "fhir"},
        ]
        from engram.storage.arcadedb_client import ArcadeDBClient
        pairs = await ArcadeDBClient.get_entity_cooccurrences(client, "org:acme")
        # After dedup: {python, fhir} → 1 pair
        self.assertEqual(len(pairs), 1)
        self.assertNotIn(("python", "python"), pairs)

    async def test_wildcard_namespace_uses_no_filter(self):
        client = self._make_client()
        client._query.return_value = []
        from engram.storage.arcadedb_client import ArcadeDBClient
        await ArcadeDBClient.get_entity_cooccurrences(client, "*")
        # The no-filter query should NOT include :ns parameter
        call_args = client._query.call_args
        sql = call_args[0][0]
        self.assertNotIn(":ns", sql)


# ---------------------------------------------------------------------------
# TestUpsertCommunity
# ---------------------------------------------------------------------------

class TestUpsertCommunity(unittest.IsolatedAsyncioTestCase):
    """Tests for ArcadeDBClient.upsert_community."""

    def _make_client(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        client = ArcadeDBClient.__new__(ArcadeDBClient)
        client._query = AsyncMock()
        client._command = AsyncMock()
        return client

    def _make_community(self, cid="abc123"):
        from engram.models import Community
        return Community(
            id=cid,
            label="alpha / beta",
            namespace="org:acme",
            member_names=["alpha", "beta"],
            member_count=2,
            detected_at=datetime.now(timezone.utc),
        )

    async def test_inserts_when_not_found(self):
        client = self._make_client()
        # UPDATE returns count=0 → triggers INSERT
        client._command.return_value = [{"count": 0}]
        from engram.storage.arcadedb_client import ArcadeDBClient
        community = self._make_community()
        cid = await ArcadeDBClient.upsert_community(client, community)
        self.assertEqual(cid, community.id)
        # _command called twice: UPDATE then INSERT
        self.assertEqual(client._command.call_count, 2)
        insert_sql = client._command.call_args_list[1][0][0]
        self.assertIn("INSERT INTO Community", insert_sql)

    async def test_updates_when_found(self):
        client = self._make_client()
        # UPDATE returns count=1 → skip INSERT
        client._command.return_value = [{"count": 1}]
        from engram.storage.arcadedb_client import ArcadeDBClient
        community = self._make_community()
        cid = await ArcadeDBClient.upsert_community(client, community)
        self.assertEqual(cid, community.id)
        # Only 1 call (the UPDATE)
        self.assertEqual(client._command.call_count, 1)
        update_sql = client._command.call_args_list[0][0][0]
        self.assertIn("UPDATE Community", update_sql)

    async def test_returns_community_id(self):
        client = self._make_client()
        client._command.return_value = [{"count": 1}]
        from engram.storage.arcadedb_client import ArcadeDBClient
        community = self._make_community("my-unique-id")
        result = await ArcadeDBClient.upsert_community(client, community)
        self.assertEqual(result, "my-unique-id")


# ---------------------------------------------------------------------------
# TestCreateBelongsToEdge
# ---------------------------------------------------------------------------

class TestCreateBelongsToEdge(unittest.IsolatedAsyncioTestCase):
    """Tests for ArcadeDBClient.create_belongs_to_edge."""

    def _make_client(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        client = ArcadeDBClient.__new__(ArcadeDBClient)
        client._command = AsyncMock()
        return client

    async def test_calls_command_with_correct_sql(self):
        client = self._make_client()
        from engram.storage.arcadedb_client import ArcadeDBClient
        await ArcadeDBClient.create_belongs_to_edge(client, "python", "cid123", "org:acme")
        client._command.assert_called_once()
        sql = client._command.call_args[0][0]
        self.assertIn("BELONGS_TO", sql)
        params = client._command.call_args[0][1]
        self.assertEqual(params["ename"], "python")
        self.assertEqual(params["cid"], "cid123")
        self.assertEqual(params["ns"], "org:acme")

    async def test_non_fatal_on_db_error(self):
        client = self._make_client()
        client._command.side_effect = Exception("ArcadeDB connection refused")
        from engram.storage.arcadedb_client import ArcadeDBClient
        # Should NOT raise
        try:
            await ArcadeDBClient.create_belongs_to_edge(client, "alpha", "cid", "ns")
        except Exception as exc:
            self.fail(f"create_belongs_to_edge raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# TestListCommunities
# ---------------------------------------------------------------------------

class TestListCommunities(unittest.IsolatedAsyncioTestCase):
    """Tests for ArcadeDBClient.list_communities."""

    def _make_client(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        client = ArcadeDBClient.__new__(ArcadeDBClient)
        client._query = AsyncMock()
        return client

    async def test_returns_list_of_dicts_with_required_fields(self):
        client = self._make_client()
        client._query.return_value = [
            {
                "id": "abc",
                "label": "python / fhir",
                "namespace": "org:acme",
                "member_names": ["python", "fhir"],
                "member_count": 2,
                "detected_at": "2026-01-01T00:00:00",
            }
        ]
        from engram.storage.arcadedb_client import ArcadeDBClient
        result = await ArcadeDBClient.list_communities(client, "org:acme")
        self.assertEqual(len(result), 1)
        c = result[0]
        for field in ("id", "label", "namespace", "member_names", "member_count", "detected_at"):
            self.assertIn(field, c)

    async def test_wildcard_namespace_no_filter(self):
        client = self._make_client()
        client._query.return_value = []
        from engram.storage.arcadedb_client import ArcadeDBClient
        await ArcadeDBClient.list_communities(client, "*")
        sql = client._query.call_args[0][0]
        self.assertNotIn(":ns", sql)
        # Wildcard query should have LIMIT 100
        self.assertIn("LIMIT 100", sql)

    async def test_empty_result_returns_empty_list(self):
        client = self._make_client()
        client._query.return_value = []
        from engram.storage.arcadedb_client import ArcadeDBClient
        result = await ArcadeDBClient.list_communities(client, "org:acme")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# TestGetEntityCommunity
# ---------------------------------------------------------------------------

class TestGetEntityCommunity(unittest.IsolatedAsyncioTestCase):
    """Tests for ArcadeDBClient.get_entity_community."""

    def _make_client(self):
        from engram.storage.arcadedb_client import ArcadeDBClient
        client = ArcadeDBClient.__new__(ArcadeDBClient)
        client._query = AsyncMock()
        return client

    async def test_returns_community_dict_when_found(self):
        client = self._make_client()
        client._query.return_value = [
            {
                "id": "cid123",
                "label": "python / fhir",
                "member_names": ["python", "fhir"],
                "member_count": 2,
            }
        ]
        from engram.storage.arcadedb_client import ArcadeDBClient
        result = await ArcadeDBClient.get_entity_community(client, "python", "org:acme")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "cid123")
        self.assertEqual(result["label"], "python / fhir")

    async def test_returns_none_when_entity_has_no_community(self):
        client = self._make_client()
        client._query.return_value = []
        from engram.storage.arcadedb_client import ArcadeDBClient
        result = await ArcadeDBClient.get_entity_community(client, "unknown-entity", "org:acme")
        self.assertIsNone(result)

    async def test_lowercases_entity_name(self):
        client = self._make_client()
        client._query.return_value = []
        from engram.storage.arcadedb_client import ArcadeDBClient
        await ArcadeDBClient.get_entity_community(client, "Python", "org:acme")
        params = client._query.call_args[0][1]
        self.assertEqual(params["ename"], "python")


# ---------------------------------------------------------------------------
# TestDetectCommunities
# ---------------------------------------------------------------------------

class TestDetectCommunities(unittest.IsolatedAsyncioTestCase):
    """Tests for detect_communities() in engram.community.detector."""

    def _make_db_client(self, pairs=None, upsert_raises=False):
        client = MagicMock()
        client.get_entity_cooccurrences = AsyncMock(return_value=pairs or [])
        client.upsert_community = AsyncMock(side_effect=Exception("DB error") if upsert_raises else None)
        client.create_belongs_to_edge = AsyncMock()
        return client

    async def test_returns_community_result_list_from_valid_cooccurrences(self):
        pairs = [
            ("alpha", "beta"),
            ("alpha", "gamma"),
            ("beta", "gamma"),
            ("delta", "epsilon"),
            ("delta", "zeta"),
            ("epsilon", "zeta"),
        ]
        client = self._make_db_client(pairs=pairs)
        from engram.community.detector import detect_communities, CommunityResult
        results = await detect_communities(client, "org:acme", persist=False)
        self.assertIsInstance(results, list)
        for r in results:
            self.assertIsInstance(r, CommunityResult)

    async def test_filters_by_min_size(self):
        # Small community of 1 pair → 2 members = community of size 2, which satisfies min_size=2
        # But if min_size=3 it should be filtered
        pairs = [("alpha", "beta")]
        client = self._make_db_client(pairs=pairs)
        from engram.community.detector import detect_communities
        results = await detect_communities(client, "org:acme", min_size=3, persist=False)
        # alpha+beta community has only 2 members — below min_size=3
        self.assertEqual(results, [])

    async def test_stable_id_is_deterministic(self):
        import hashlib
        members = ["alpha", "beta", "gamma"]
        expected_id = hashlib.sha256(":".join(sorted(members)).encode()).hexdigest()[:16]
        pairs = [
            ("alpha", "beta"),
            ("beta", "gamma"),
            ("alpha", "gamma"),
        ]
        client = self._make_db_client(pairs=pairs)
        from engram.community.detector import detect_communities
        results1 = await detect_communities(client, "org:acme", persist=False)
        client2 = self._make_db_client(pairs=pairs)
        results2 = await detect_communities(client2, "org:acme", persist=False)
        self.assertTrue(len(results1) > 0)
        self.assertEqual(
            {r.community_id for r in results1},
            {r.community_id for r in results2},
        )

    async def test_label_uses_top3_most_connected_entities(self):
        # Build a graph where alpha has degree 3, beta 2, gamma 1, delta 1
        pairs = [
            ("alpha", "beta"),
            ("alpha", "gamma"),
            ("alpha", "delta"),
            ("beta", "gamma"),
        ]
        client = self._make_db_client(pairs=pairs)
        from engram.community.detector import detect_communities
        results = await detect_communities(client, "org:acme", persist=False)
        self.assertTrue(len(results) > 0)
        label = results[0].label
        # alpha should appear first as it has highest degree
        self.assertTrue(label.startswith("alpha"), f"Expected label to start with 'alpha', got: {label!r}")

    async def test_calls_upsert_community_when_persist_true(self):
        pairs = [
            ("alpha", "beta"),
            ("alpha", "gamma"),
            ("beta", "gamma"),
        ]
        client = self._make_db_client(pairs=pairs)
        from engram.community.detector import detect_communities
        results = await detect_communities(client, "org:acme", persist=True)
        if results:
            client.upsert_community.assert_called()

    async def test_does_not_call_upsert_community_when_persist_false(self):
        pairs = [
            ("alpha", "beta"),
            ("alpha", "gamma"),
            ("beta", "gamma"),
        ]
        client = self._make_db_client(pairs=pairs)
        from engram.community.detector import detect_communities
        await detect_communities(client, "org:acme", persist=False)
        client.upsert_community.assert_not_called()

    async def test_returns_empty_gracefully_when_networkx_not_installed(self):
        client = self._make_db_client(pairs=[("a", "b"), ("a", "c")])
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "networkx":
                raise ImportError("No module named 'networkx'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            # Re-import detect_communities so the lazy import runs inside the patch
            import importlib
            import engram.community.detector as det_module
            importlib.reload(det_module)
            results = await det_module.detect_communities(client, "org:acme", persist=False)
            self.assertEqual(results, [])
        # Restore by reloading after patch removed
        importlib.reload(det_module)

    async def test_non_fatal_when_db_persist_fails_still_returns_results(self):
        pairs = [
            ("alpha", "beta"),
            ("alpha", "gamma"),
            ("beta", "gamma"),
        ]
        # upsert_community raises
        client = self._make_db_client(pairs=pairs, upsert_raises=True)
        from engram.community.detector import detect_communities
        results = await detect_communities(client, "org:acme", persist=True)
        # Results should still be returned despite DB errors
        self.assertIsInstance(results, list)
        # At least tried to upsert
        client.upsert_community.assert_called()


# ---------------------------------------------------------------------------
# TestCommunityArcadeDBSchema
# ---------------------------------------------------------------------------

class TestCommunityArcadeDBSchema(unittest.TestCase):
    """Tests that schema initialisation commands include Community types."""

    def _get_schema_commands(self):
        """Extract the schema_cmds list from _init_schema source."""
        from engram.storage.arcadedb_client import ArcadeDBClient
        import inspect
        src = inspect.getsource(ArcadeDBClient._init_schema)
        return src

    def test_community_vertex_type_commands_present(self):
        src = self._get_schema_commands()
        self.assertIn("CREATE VERTEX TYPE Community IF NOT EXISTS", src)
        self.assertIn("CREATE PROPERTY Community.id IF NOT EXISTS STRING", src)
        self.assertIn("CREATE PROPERTY Community.label IF NOT EXISTS STRING", src)
        self.assertIn("CREATE PROPERTY Community.namespace IF NOT EXISTS STRING", src)
        self.assertIn("CREATE PROPERTY Community.member_names IF NOT EXISTS LIST", src)
        self.assertIn("CREATE PROPERTY Community.member_count IF NOT EXISTS INTEGER", src)
        self.assertIn("CREATE PROPERTY Community.detected_at IF NOT EXISTS DATETIME", src)

    def test_belongs_to_edge_type_command_present(self):
        src = self._get_schema_commands()
        self.assertIn("CREATE EDGE TYPE BELONGS_TO IF NOT EXISTS", src)


# ---------------------------------------------------------------------------
# TestCommunityMCPTool
# ---------------------------------------------------------------------------

class TestCommunityMCPTool(unittest.TestCase):
    """Tests for the community_search MCP tool definition and handler."""

    def test_community_search_tool_present_in_server_tools_list(self):
        from engram_mcp.server import TOOLS
        tool_names = [t.name for t in TOOLS]
        self.assertIn("community_search", tool_names)

    def test_community_search_tool_has_correct_schema(self):
        from engram_mcp.server import TOOLS
        tool = next(t for t in TOOLS if t.name == "community_search")
        props = tool.inputSchema["properties"]
        self.assertIn("entity", props)
        self.assertIn("namespace", props)
        self.assertIn("entity", tool.inputSchema["required"])
        self.assertIn("namespace", tool.inputSchema["required"])


class TestCommunityMCPHandler(unittest.IsolatedAsyncioTestCase):
    """Tests for community_search dispatch handler."""

    def _make_client(self, community=None):
        client = MagicMock()
        client._arcadedb = MagicMock()
        client._arcadedb.get_entity_community = AsyncMock(return_value=community)
        return client

    async def test_handler_returns_community_label_and_members_when_found(self):
        community = {
            "id": "cid1",
            "label": "python / fhir",
            "member_names": ["python", "fhir", "kafka"],
            "member_count": 3,
        }
        client = self._make_client(community=community)
        from engram_mcp.server import _dispatch
        result = await _dispatch(
            "community_search",
            {"entity": "Python", "namespace": "org:acme"},
            client,
            None,
        )
        from mcp.types import TextContent
        self.assertTrue(len(result) > 0)
        text = result[0].text
        self.assertIn("python / fhir", text)
        self.assertIn("python", text)

    async def test_handler_returns_no_community_message_when_get_entity_community_returns_none(self):
        client = self._make_client(community=None)
        from engram_mcp.server import _dispatch
        result = await _dispatch(
            "community_search",
            {"entity": "unknown-entity", "namespace": "org:acme"},
            client,
            None,
        )
        from mcp.types import TextContent
        text = result[0].text
        self.assertIn("No community found", text)
        self.assertIn("engram-community detect", text)


# ---------------------------------------------------------------------------
# TestCommunityAPIEndpoint
# ---------------------------------------------------------------------------

class TestCommunityAPIEndpoint(unittest.IsolatedAsyncioTestCase):
    """Tests for GET /knowledge/communities FastAPI endpoint."""

    async def test_get_communities_calls_list_communities_with_correct_namespace(self):
        from engram_api.routers.knowledge import get_communities

        mock_client = MagicMock()
        mock_client._arcadedb = MagicMock()
        mock_client._arcadedb.list_communities = AsyncMock(return_value=[])

        mock_key_entry = MagicMock()

        with patch("engram_api.routers.knowledge.check_namespace_access", new=AsyncMock()) as mock_check:
            result = await get_communities(
                ns="org:acme",
                user_id="test-user",
                key_entry=mock_key_entry,
                client=mock_client,
            )
        mock_client._arcadedb.list_communities.assert_called_once_with("org:acme")

    async def test_returns_dict_with_communities_count_namespace_keys(self):
        from engram_api.routers.knowledge import get_communities

        fake_communities = [
            {"id": "c1", "label": "python / fhir", "namespace": "org:acme",
             "member_names": ["python", "fhir"], "member_count": 2, "detected_at": ""},
        ]
        mock_client = MagicMock()
        mock_client._arcadedb = MagicMock()
        mock_client._arcadedb.list_communities = AsyncMock(return_value=fake_communities)
        mock_key_entry = MagicMock()

        with patch("engram_api.routers.knowledge.check_namespace_access", new=AsyncMock()):
            result = await get_communities(
                ns="org:acme",
                user_id="test-user",
                key_entry=mock_key_entry,
                client=mock_client,
            )
        self.assertIn("communities", result)
        self.assertIn("count", result)
        self.assertIn("namespace", result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["namespace"], "org:acme")

    async def test_check_namespace_access_called(self):
        from engram_api.routers.knowledge import get_communities

        mock_client = MagicMock()
        mock_client._arcadedb = MagicMock()
        mock_client._arcadedb.list_communities = AsyncMock(return_value=[])
        mock_key_entry = MagicMock()

        with patch(
            "engram_api.routers.knowledge.check_namespace_access",
            new_callable=lambda: lambda: AsyncMock(),
        ) as mock_check_factory:
            mock_check = AsyncMock()
            with patch("engram_api.routers.knowledge.check_namespace_access", mock_check):
                await get_communities(
                    ns="org:acme",
                    user_id="test-user",
                    key_entry=mock_key_entry,
                    client=mock_client,
                )
            mock_check.assert_called_once_with(mock_key_entry, "org:acme")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
