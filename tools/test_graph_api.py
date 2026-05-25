"""
tools/test_graph_api.py — Tests for graph and viz REST endpoints.

Covers:
- graph_query: returns results, None→[], datetime→ISO, 500 on exception
- graph_query: params forwarded to client.query_graph
- get_entity: returns entity + relations dict
- get_entity: 404 when entity not found, 500 on exception
- get_entity: relations serialised with expected fields
- get_entity: works when related returns empty list / no relations attr
- add_fact: fields forwarded to client.add_fact
- add_fact: fact object serialised to dict; dict passthrough; 500 on exception
- _build_date_histogram: counts within 30 days, ignores older entries
- _build_date_histogram: handles missing/None created_at, ISO string dates
- graph_stats: all expected keys present, namespace_distribution sorted desc
- graph_stats: graceful fallback when client.stats raises
- graph_stats: recent_activity built from search results
- graph_visualize: data from client.visualize, limit passed through
- graph_visualize: empty fallback when client.visualize raises
"""
from __future__ import annotations

import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, _REPO_ROOT + "/packages/api")
sys.path.insert(0, _REPO_ROOT + "/packages/core")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _key_entry(namespaces=None):
    e = MagicMock()
    e.namespaces = namespaces or ["*"]
    e.read_only = False
    return e


def _fact(fid="fact-1", subject="Alice", predicate="uses", obj="Python",
          namespace="ns1", source_memory_id=None):
    f = MagicMock()
    f.id = fid
    f.subject = subject
    f.predicate = predicate
    f.object = obj
    f.namespace = namespace
    f.valid_from = datetime(2025, 1, 1, tzinfo=timezone.utc)
    f.valid_until = None
    f.source_memory_id = source_memory_id
    return f


def _entity(name="Alice", etype="PERSON", namespace="ns1"):
    e = MagicMock()
    e.id = "ent-1"
    e.name = name
    e.entity_type = etype
    e.namespace = namespace
    e.attributes = {}
    e.created_at = None
    e.valid_until = None
    return e


def _relation():
    r = MagicMock()
    r.id = "rel-1"
    r.source_entity_id = "ent-1"
    r.target_entity_id = "ent-2"
    r.relation_type = "KNOWS"
    r.namespace = "ns1"
    r.weight = 1.0
    r.created_at = None
    r.valid_until = None
    r.attributes = {}
    return r


def _graph(relations=None):
    g = MagicMock()
    g.relations = relations or []
    return g


def _search_result(created_at):
    m = MagicMock()
    m.created_at = created_at
    r = MagicMock()
    r.memory = m
    return r


# ---------------------------------------------------------------------------
# graph_query
# ---------------------------------------------------------------------------

class TestGraphQuery(unittest.IsolatedAsyncioTestCase):
    async def test_returns_list_from_client(self):
        from engram_api.routers.graph import graph_query
        from engram_api.schemas import GraphQueryRequest
        client = MagicMock()
        client.query_graph = AsyncMock(return_value=[{"id": "1", "name": "Alice"}])
        req = GraphQueryRequest(cypher="SELECT FROM Entity", namespace="ns1")
        result = await graph_query(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Alice")

    async def test_none_result_returns_empty_list(self):
        from engram_api.routers.graph import graph_query
        from engram_api.schemas import GraphQueryRequest
        client = MagicMock()
        client.query_graph = AsyncMock(return_value=None)
        req = GraphQueryRequest(cypher="SELECT FROM Entity", namespace="ns1")
        result = await graph_query(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertEqual(result, [])

    async def test_datetime_in_results_converted_to_iso(self):
        from engram_api.routers.graph import graph_query
        from engram_api.schemas import GraphQueryRequest
        dt = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        client = MagicMock()
        client.query_graph = AsyncMock(return_value=[{"created_at": dt, "name": "Alice"}])
        req = GraphQueryRequest(cypher="SELECT FROM Entity", namespace="ns1")
        result = await graph_query(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertIsInstance(result[0]["created_at"], str)
        self.assertIn("2025-06-01", result[0]["created_at"])

    async def test_params_forwarded_to_client(self):
        from engram_api.routers.graph import graph_query
        from engram_api.schemas import GraphQueryRequest
        client = MagicMock()
        client.query_graph = AsyncMock(return_value=[])
        req = GraphQueryRequest(cypher="SELECT FROM Entity WHERE name = :n", namespace="ns1", params={"n": "Alice"})
        await graph_query(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        client.query_graph.assert_awaited_once_with(
            "SELECT FROM Entity WHERE name = :n", "ns1", {"n": "Alice"}
        )

    async def test_500_on_exception(self):
        from engram_api.routers.graph import graph_query
        from engram_api.schemas import GraphQueryRequest
        from fastapi import HTTPException
        client = MagicMock()
        client.query_graph = AsyncMock(side_effect=RuntimeError("db error"))
        req = GraphQueryRequest(cypher="BAD SQL", namespace="ns1")
        with self.assertRaises(HTTPException) as ctx:
            await graph_query(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertEqual(ctx.exception.status_code, 500)

    async def test_empty_list_returned_as_is(self):
        from engram_api.routers.graph import graph_query
        from engram_api.schemas import GraphQueryRequest
        client = MagicMock()
        client.query_graph = AsyncMock(return_value=[])
        req = GraphQueryRequest(cypher="SELECT FROM Entity LIMIT 0", namespace="ns1")
        result = await graph_query(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# get_entity
# ---------------------------------------------------------------------------

class TestGetEntity(unittest.IsolatedAsyncioTestCase):
    async def test_returns_entity_and_relations(self):
        from engram_api.routers.graph import get_entity
        entity = _entity("Alice")
        graph = _graph([_relation()])
        client = MagicMock()
        client.get_entity = AsyncMock(return_value=entity)
        client.get_related = AsyncMock(return_value=graph)
        result = await get_entity(name="Alice", ns="ns1", depth=2, user_id="u1",
                                   key_entry=_key_entry(), client=client)
        self.assertIn("entity", result)
        self.assertIn("relations", result)
        self.assertEqual(result["entity"]["name"], "Alice")
        self.assertEqual(len(result["relations"]), 1)

    async def test_404_when_entity_not_found(self):
        from engram_api.routers.graph import get_entity
        from fastapi import HTTPException
        client = MagicMock()
        client.get_entity = AsyncMock(return_value=None)
        client.get_related = AsyncMock(return_value=_graph())
        with self.assertRaises(HTTPException) as ctx:
            await get_entity(name="Ghost", ns="ns1", depth=2, user_id="u1",
                              key_entry=_key_entry(), client=client)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("Ghost", ctx.exception.detail)

    async def test_500_on_exception(self):
        from engram_api.routers.graph import get_entity
        from fastapi import HTTPException
        client = MagicMock()
        client.get_entity = AsyncMock(side_effect=RuntimeError("db crash"))
        client.get_related = AsyncMock(return_value=_graph())
        with self.assertRaises(HTTPException) as ctx:
            await get_entity(name="Alice", ns="ns1", depth=2, user_id="u1",
                              key_entry=_key_entry(), client=client)
        self.assertEqual(ctx.exception.status_code, 500)

    async def test_relation_fields_serialised(self):
        from engram_api.routers.graph import get_entity
        rel = _relation()
        rel.relation_type = "DEPENDS_ON"
        rel.weight = 0.9
        client = MagicMock()
        client.get_entity = AsyncMock(return_value=_entity())
        client.get_related = AsyncMock(return_value=_graph([rel]))
        result = await get_entity(name="Alice", ns="ns1", depth=2, user_id="u1",
                                   key_entry=_key_entry(), client=client)
        r = result["relations"][0]
        self.assertEqual(r["relation_type"], "DEPENDS_ON")
        self.assertAlmostEqual(r["weight"], 0.9)

    async def test_empty_relations_when_graph_has_none(self):
        from engram_api.routers.graph import get_entity
        client = MagicMock()
        client.get_entity = AsyncMock(return_value=_entity())
        client.get_related = AsyncMock(return_value=_graph([]))
        result = await get_entity(name="Alice", ns="ns1", depth=2, user_id="u1",
                                   key_entry=_key_entry(), client=client)
        self.assertEqual(result["relations"], [])

    async def test_related_returns_list_directly(self):
        from engram_api.routers.graph import get_entity
        client = MagicMock()
        client.get_entity = AsyncMock(return_value=_entity())
        client.get_related = AsyncMock(return_value=[_relation()])   # list, not Graph obj
        result = await get_entity(name="Alice", ns="ns1", depth=2, user_id="u1",
                                   key_entry=_key_entry(), client=client)
        self.assertEqual(len(result["relations"]), 1)

    async def test_depth_passed_to_client(self):
        from engram_api.routers.graph import get_entity
        client = MagicMock()
        client.get_entity = AsyncMock(return_value=_entity())
        client.get_related = AsyncMock(return_value=_graph())
        await get_entity(name="Alice", ns="ns1", depth=4, user_id="u1",
                          key_entry=_key_entry(), client=client)
        client.get_related.assert_awaited_once_with("Alice", "ns1", 4)


# ---------------------------------------------------------------------------
# add_fact
# ---------------------------------------------------------------------------

class TestAddFact(unittest.IsolatedAsyncioTestCase):
    async def test_fields_forwarded_to_client(self):
        from engram_api.routers.graph import add_fact
        from engram_api.schemas import FactRequest
        client = MagicMock()
        client.add_fact = AsyncMock(return_value=_fact())
        req = FactRequest(subject="Alice", predicate="uses", object="Python", namespace="ns1")
        await add_fact(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        client.add_fact.assert_awaited_once_with(
            subject="Alice", predicate="uses", object="Python", namespace="ns1"
        )

    async def test_fact_object_serialised(self):
        from engram_api.routers.graph import add_fact
        from engram_api.schemas import FactRequest
        client = MagicMock()
        client.add_fact = AsyncMock(return_value=_fact("f-1", "Alice", "uses", "Python"))
        req = FactRequest(subject="Alice", predicate="uses", object="Python", namespace="ns1")
        result = await add_fact(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertEqual(result["subject"], "Alice")
        self.assertEqual(result["predicate"], "uses")
        self.assertEqual(result["object"], "Python")
        self.assertEqual(result["id"], "f-1")

    async def test_dict_return_passed_through(self):
        from engram_api.routers.graph import add_fact
        from engram_api.schemas import FactRequest
        client = MagicMock()
        client.add_fact = AsyncMock(return_value={"id": "f-2", "subject": "Bob", "predicate": "knows", "object": "Alice", "namespace": "ns1"})
        req = FactRequest(subject="Bob", predicate="knows", object="Alice", namespace="ns1")
        result = await add_fact(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertEqual(result["id"], "f-2")
        self.assertEqual(result["subject"], "Bob")

    async def test_500_on_exception(self):
        from engram_api.routers.graph import add_fact
        from engram_api.schemas import FactRequest
        from fastapi import HTTPException
        client = MagicMock()
        client.add_fact = AsyncMock(side_effect=RuntimeError("insert failed"))
        req = FactRequest(subject="A", predicate="b", object="C", namespace="ns1")
        with self.assertRaises(HTTPException) as ctx:
            await add_fact(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertEqual(ctx.exception.status_code, 500)

    async def test_datetime_valid_from_converted_to_iso(self):
        from engram_api.routers.graph import add_fact
        from engram_api.schemas import FactRequest
        f = _fact()
        f.valid_from = datetime(2025, 3, 15, tzinfo=timezone.utc)
        client = MagicMock()
        client.add_fact = AsyncMock(return_value=f)
        req = FactRequest(subject="A", predicate="b", object="C", namespace="ns1")
        result = await add_fact(req=req, user_id="u1", key_entry=_key_entry(), client=client)
        self.assertIsInstance(result["valid_from"], str)
        self.assertIn("2025-03-15", result["valid_from"])


# ---------------------------------------------------------------------------
# _build_date_histogram
# ---------------------------------------------------------------------------

class TestBuildDateHistogram(unittest.TestCase):
    def _result_with_date(self, d):
        m = MagicMock()
        if isinstance(d, str):
            m.created_at = d
        elif d is None:
            m.created_at = None
        else:
            m.created_at = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        r = MagicMock()
        r.memory = m
        return r

    def test_counts_within_30_days(self):
        from engram_api.routers.viz import _build_date_histogram
        today = datetime.now(timezone.utc).date()
        results = [self._result_with_date(today) for _ in range(3)]
        hist = _build_date_histogram(results)
        total = sum(e["count"] for e in hist)
        self.assertEqual(total, 3)

    def test_ignores_entries_older_than_30_days(self):
        from engram_api.routers.viz import _build_date_histogram
        old_date = datetime.now(timezone.utc).date() - timedelta(days=31)
        results = [self._result_with_date(old_date)]
        hist = _build_date_histogram(results)
        self.assertEqual(sum(e["count"] for e in hist), 0)

    def test_handles_none_created_at(self):
        from engram_api.routers.viz import _build_date_histogram
        results = [self._result_with_date(None)]
        hist = _build_date_histogram(results)  # should not raise
        self.assertEqual(sum(e["count"] for e in hist), 0)

    def test_handles_iso_string_date(self):
        from engram_api.routers.viz import _build_date_histogram
        today = datetime.now(timezone.utc).date()
        results = [self._result_with_date(f"{today.isoformat()}T00:00:00")]
        hist = _build_date_histogram(results)
        self.assertEqual(sum(e["count"] for e in hist), 1)

    def test_multiple_dates_sorted(self):
        from engram_api.routers.viz import _build_date_histogram
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        results = [self._result_with_date(today), self._result_with_date(yesterday)]
        hist = _build_date_histogram(results)
        dates = [e["date"] for e in hist]
        self.assertEqual(dates, sorted(dates))

    def test_empty_input_returns_empty_list(self):
        from engram_api.routers.viz import _build_date_histogram
        self.assertEqual(_build_date_histogram([]), [])

    def test_result_with_no_memory_attr_skipped(self):
        from engram_api.routers.viz import _build_date_histogram
        r = MagicMock()
        r.memory = None
        hist = _build_date_histogram([r])
        self.assertEqual(sum(e["count"] for e in hist), 0)


# ---------------------------------------------------------------------------
# graph_stats
# ---------------------------------------------------------------------------

class TestGraphStats(unittest.IsolatedAsyncioTestCase):
    def _make_client(self, memories=5, edges=10, ns_dist=None, search_results=None, tag_rows=None):
        c = MagicMock()
        c.stats = AsyncMock(return_value={
            "memories": memories,
            "edges": edges,
            "namespace_distribution": ns_dist or {"ns1": 3, "ns2": 2},
        })
        c.search = AsyncMock(return_value=search_results or [])
        c.query_graph = AsyncMock(return_value=tag_rows or [])
        return c

    async def test_all_expected_keys_present(self):
        from engram_api.routers.viz import graph_stats
        client = self._make_client()
        result = await graph_stats(namespace="all", user_id="u1", _key_entry=_key_entry(), client=client)
        for key in ("node_count", "edge_count", "memory_count", "namespace_distribution", "top_tags", "recent_activity"):
            self.assertIn(key, result)

    async def test_memory_and_edge_counts(self):
        from engram_api.routers.viz import graph_stats
        client = self._make_client(memories=7, edges=14)
        result = await graph_stats(namespace="all", user_id="u1", _key_entry=_key_entry(), client=client)
        self.assertEqual(result["node_count"], 7)
        self.assertEqual(result["edge_count"], 14)
        self.assertEqual(result["memory_count"], 7)

    async def test_namespace_distribution_sorted_descending(self):
        from engram_api.routers.viz import graph_stats
        client = self._make_client(ns_dist={"ns1": 2, "ns2": 10, "ns3": 5})
        result = await graph_stats(namespace="all", user_id="u1", _key_entry=_key_entry(), client=client)
        counts = [e["count"] for e in result["namespace_distribution"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    async def test_graceful_fallback_when_stats_raises(self):
        from engram_api.routers.viz import graph_stats
        client = MagicMock()
        client.stats = AsyncMock(side_effect=RuntimeError("arcadedb down"))
        client.search = AsyncMock(return_value=[])
        client.query_graph = AsyncMock(return_value=[])
        result = await graph_stats(namespace="all", user_id="u1", _key_entry=_key_entry(), client=client)
        self.assertEqual(result["node_count"], 0)
        self.assertEqual(result["edge_count"], 0)

    async def test_recent_activity_built_from_search(self):
        from engram_api.routers.viz import graph_stats
        today = datetime.now(timezone.utc).date()
        sr = _search_result(datetime(today.year, today.month, today.day, tzinfo=timezone.utc))
        client = self._make_client(search_results=[sr])
        result = await graph_stats(namespace="all", user_id="u1", _key_entry=_key_entry(), client=client)
        total = sum(e["count"] for e in result["recent_activity"])
        self.assertEqual(total, 1)

    async def test_top_tags_extracted(self):
        from engram_api.routers.viz import graph_stats
        tag_rows = [{"tags": ["incident", "critical"], "cnt": 5}]
        client = self._make_client(tag_rows=tag_rows)
        result = await graph_stats(namespace="all", user_id="u1", _key_entry=_key_entry(), client=client)
        tag_names = [t["tag"] for t in result["top_tags"]]
        self.assertIn("incident", tag_names)
        self.assertIn("critical", tag_names)

    async def test_empty_search_returns_empty_recent_activity(self):
        from engram_api.routers.viz import graph_stats
        client = self._make_client(search_results=[])
        result = await graph_stats(namespace="all", user_id="u1", _key_entry=_key_entry(), client=client)
        self.assertEqual(result["recent_activity"], [])


# ---------------------------------------------------------------------------
# graph_visualize
# ---------------------------------------------------------------------------

class TestGraphVisualize(unittest.IsolatedAsyncioTestCase):
    async def test_returns_data_from_client(self):
        from engram_api.routers.viz import graph_visualize
        data = {"nodes": [{"id": "n1"}], "edges": [], "truncated": False}
        client = MagicMock()
        client.visualize = AsyncMock(return_value=data)
        result = await graph_visualize(namespace="ns1", limit=150, user_id="u1",
                                        _key_entry=_key_entry(), client=client)
        self.assertEqual(len(result["nodes"]), 1)
        self.assertFalse(result["truncated"])

    async def test_limit_passed_to_client(self):
        from engram_api.routers.viz import graph_visualize
        client = MagicMock()
        client.visualize = AsyncMock(return_value={"nodes": [], "edges": [], "truncated": False})
        await graph_visualize(namespace="ns1", limit=42, user_id="u1",
                               _key_entry=_key_entry(), client=client)
        client.visualize.assert_awaited_once_with(namespace="ns1", limit=42)

    async def test_empty_fallback_on_exception(self):
        from engram_api.routers.viz import graph_visualize
        client = MagicMock()
        client.visualize = AsyncMock(side_effect=RuntimeError("arcadedb unavailable"))
        result = await graph_visualize(namespace="ns1", limit=150, user_id="u1",
                                        _key_entry=_key_entry(), client=client)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])
        self.assertFalse(result["truncated"])

    async def test_namespace_passed_to_client(self):
        from engram_api.routers.viz import graph_visualize
        client = MagicMock()
        client.visualize = AsyncMock(return_value={"nodes": [], "edges": [], "truncated": False})
        await graph_visualize(namespace="org:acme", limit=100, user_id="u1",
                               _key_entry=_key_entry(), client=client)
        client.visualize.assert_awaited_once_with(namespace="org:acme", limit=100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
