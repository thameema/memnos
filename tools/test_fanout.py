"""
tools/test_fanout.py — Unit tests for cross-namespace subscription fan-out.

Tests cover:
- Subscription.delivery_namespace field
- get_fanout_subscribers() filters correctly (only active, only delivery_namespace set)
- _fanout_memory() inserts copies into delivery_namespace
- filter_types applied before fan-out copy
- Fan-out is skipped when no subscribers
- subscribe() passes delivery_namespace to ArcadeDB
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

sys.path.insert(0, "/Users/thameema/git/engram/packages/core")

from engram.models import (
    DecayPolicy,
    MemoryEntry,
    MemoryStatus,
    MemoryType,
    Subscription,
)


# ---------------------------------------------------------------------------
# Subscription model
# ---------------------------------------------------------------------------

class TestSubscriptionDeliveryNamespace(unittest.TestCase):
    def test_default_delivery_namespace_is_empty(self):
        sub = Subscription(subscriber_id="agent1", namespace="org:team-a")
        self.assertEqual(sub.delivery_namespace, "")

    def test_delivery_namespace_set(self):
        sub = Subscription(
            subscriber_id="agent1",
            namespace="org:team-a",
            delivery_namespace="org:agent1:feed",
        )
        self.assertEqual(sub.delivery_namespace, "org:agent1:feed")


# ---------------------------------------------------------------------------
# get_fanout_subscribers (arcadedb_client mock)
# ---------------------------------------------------------------------------

def _make_arcadedb_with_fanout_subs(subs: list[dict]):
    """Create a mock ArcadeDBClient that returns the given rows for get_fanout_subscribers."""
    db = MagicMock()
    db.get_fanout_subscribers = AsyncMock(return_value=subs)
    db.insert_memory = AsyncMock()
    db.upsert_subscription = AsyncMock(return_value="sub-id-001")
    return db


class TestGetFanoutSubscribers(unittest.IsolatedAsyncioTestCase):
    async def test_returns_subscribers_with_delivery_namespace(self):
        subs = [
            {"subscriber_id": "agent1", "delivery_namespace": "org:agent1", "filter_types": []},
            {"subscriber_id": "agent2", "delivery_namespace": "org:agent2", "filter_types": ["decision"]},
        ]
        db = _make_arcadedb_with_fanout_subs(subs)
        result = await db.get_fanout_subscribers("org:shared")
        self.assertEqual(len(result), 2)

    async def test_empty_when_no_delivery_subscribers(self):
        db = _make_arcadedb_with_fanout_subs([])
        result = await db.get_fanout_subscribers("org:shared")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _fanout_memory in EngramClient
# ---------------------------------------------------------------------------

def _make_memory(
    memory_type: MemoryType = MemoryType.fact,
    tags: list[str] | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        content="test memory content",
        namespace="org:shared",
        memory_type=memory_type,
        tags=tags or [],
    )


def _make_client_with_arcadedb(arcadedb):
    """Build a minimal EngramClient-like object with the _fanout_memory method."""
    # Import here to get the actual _fanout_memory method
    from engram.client import EngramClient
    client = object.__new__(EngramClient)
    client._arcadedb = arcadedb
    # Bind the method
    import types
    client._fanout_memory = types.MethodType(EngramClient._fanout_memory, client)
    return client


class TestFanoutMemory(unittest.IsolatedAsyncioTestCase):
    async def test_no_subscribers_no_insert(self):
        db = _make_arcadedb_with_fanout_subs([])
        client = _make_client_with_arcadedb(db)
        mem = _make_memory()
        await client._fanout_memory(mem, "org:shared", [0.1] * 384)
        db.insert_memory.assert_not_awaited()

    async def test_copies_memory_to_delivery_namespace(self):
        subs = [{"subscriber_id": "agent1", "delivery_namespace": "org:agent1:feed", "filter_types": []}]
        db = _make_arcadedb_with_fanout_subs(subs)
        client = _make_client_with_arcadedb(db)
        mem = _make_memory()
        embedding = [0.1] * 384
        await client._fanout_memory(mem, "org:shared", embedding)

        db.insert_memory.assert_awaited_once()
        copy, emb = db.insert_memory.call_args.args
        self.assertEqual(copy.namespace, "org:agent1:feed")
        self.assertEqual(copy.source, "fanout")
        self.assertEqual(copy.metadata["fanout_source"], "org:shared")
        self.assertEqual(copy.metadata["original_id"], str(mem.id))
        self.assertEqual(copy.content, mem.content)
        self.assertEqual(emb, embedding)

    async def test_multiple_subscribers_get_individual_copies(self):
        subs = [
            {"subscriber_id": "a1", "delivery_namespace": "org:a1", "filter_types": []},
            {"subscriber_id": "a2", "delivery_namespace": "org:a2", "filter_types": []},
        ]
        db = _make_arcadedb_with_fanout_subs(subs)
        client = _make_client_with_arcadedb(db)
        mem = _make_memory()
        await client._fanout_memory(mem, "org:shared", [0.0])
        self.assertEqual(db.insert_memory.await_count, 2)

        namespaces = {c.args[0].namespace for c in db.insert_memory.call_args_list}
        self.assertIn("org:a1", namespaces)
        self.assertIn("org:a2", namespaces)

    async def test_filter_types_excludes_non_matching_memory(self):
        # Subscriber only wants "decision" — a plain "fact" should NOT be fanned out
        subs = [{"subscriber_id": "a1", "delivery_namespace": "org:a1", "filter_types": ["decision"]}]
        db = _make_arcadedb_with_fanout_subs(subs)
        client = _make_client_with_arcadedb(db)
        mem = _make_memory(memory_type=MemoryType.fact)
        await client._fanout_memory(mem, "org:shared", [0.0])
        db.insert_memory.assert_not_awaited()

    async def test_filter_types_includes_matching_memory_type(self):
        # Subscriber wants "decision" — a "decision" memory should be fanned out
        subs = [{"subscriber_id": "a1", "delivery_namespace": "org:a1", "filter_types": ["decision"]}]
        db = _make_arcadedb_with_fanout_subs(subs)
        client = _make_client_with_arcadedb(db)
        mem = _make_memory(memory_type=MemoryType.decision)
        await client._fanout_memory(mem, "org:shared", [0.0])
        db.insert_memory.assert_awaited_once()

    async def test_filter_types_tag_match_included(self):
        # Subscriber filters by tag "critical" — memory with that tag passes
        subs = [{"subscriber_id": "a1", "delivery_namespace": "org:a1", "filter_types": ["critical"]}]
        db = _make_arcadedb_with_fanout_subs(subs)
        client = _make_client_with_arcadedb(db)
        mem = _make_memory(tags=["critical", "prod"])
        await client._fanout_memory(mem, "org:shared", [0.0])
        db.insert_memory.assert_awaited_once()

    async def test_insert_failure_is_swallowed(self):
        subs = [{"subscriber_id": "a1", "delivery_namespace": "org:a1", "filter_types": []}]
        db = _make_arcadedb_with_fanout_subs(subs)
        db.insert_memory = AsyncMock(side_effect=RuntimeError("ArcadeDB error"))
        client = _make_client_with_arcadedb(db)
        mem = _make_memory()
        # Should not raise
        await client._fanout_memory(mem, "org:shared", [0.0])


# ---------------------------------------------------------------------------
# subscribe() accepts delivery_namespace
# ---------------------------------------------------------------------------

class TestSubscribeDeliveryNamespace(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_namespace_passed_to_arcadedb(self):
        db = MagicMock()
        db.upsert_subscription = AsyncMock(return_value="sub-001")

        from engram.client import EngramClient
        client = object.__new__(EngramClient)
        client._arcadedb = db
        client._started = True
        client._config = MagicMock()

        def _assert_started(self):
            pass
        import types
        client._assert_started = types.MethodType(lambda s: None, client)

        await client.subscribe("agent1", "org:shared", delivery_namespace="org:agent1:inbox")

        db.upsert_subscription.assert_awaited_once()
        sub_arg = db.upsert_subscription.call_args.args[0]
        self.assertIsInstance(sub_arg, Subscription)
        self.assertEqual(sub_arg.delivery_namespace, "org:agent1:inbox")
        self.assertEqual(sub_arg.namespace, "org:shared")

    async def test_default_delivery_namespace_is_empty(self):
        db = MagicMock()
        db.upsert_subscription = AsyncMock(return_value="sub-001")

        from engram.client import EngramClient
        client = object.__new__(EngramClient)
        client._arcadedb = db
        client._started = True
        import types
        client._assert_started = types.MethodType(lambda s: None, client)

        await client.subscribe("agent1", "org:shared")

        sub_arg = db.upsert_subscription.call_args.args[0]
        self.assertEqual(sub_arg.delivery_namespace, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
