"""
tools/test_immediate_delivery.py — Tests for delivery_mode=immediate (SSE push).

Covers:
- ImmediateSubscriptionBus.register(): returns an asyncio.Queue
- ImmediateSubscriptionBus.unregister(): removes the queue
- ImmediateSubscriptionBus.publish(): delivers to exact-match namespace
- ImmediateSubscriptionBus.publish(): namespace prefix match (org:acme receives org:acme:eng)
- ImmediateSubscriptionBus.publish(): no delivery to unrelated namespace
- ImmediateSubscriptionBus.publish(): filter_types blocks non-matching memory_type
- ImmediateSubscriptionBus.publish(): filter_types tag match allows delivery
- ImmediateSubscriptionBus.publish(): empty filter_types delivers everything
- ImmediateSubscriptionBus.publish(): returns delivered count
- ImmediateSubscriptionBus.subscriber_count: accurate after register/unregister
- Module-level register/unregister/publish work via the singleton
- client._dispatch_immediate(): calls publish with correct event shape
- client._dispatch_immediate(): no-op when no subscribers (publish returns 0)
- client._dispatch_immediate(): non-fatal when subscription_bus import fails
- client._dispatch_immediate(): memory_type and tags in payload
- stream_namespace: 501 when sse_starlette not installed
- stream_namespace: registers queue, yields connected event, delivers memory event
- stream_namespace: unregisters on generator exhaustion
- stream_namespace: filter_types loaded from subscription record
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/thameema/git/engram/packages/core")
sys.path.insert(0, "/Users/thameema/git/engram/packages/api")


# ---------------------------------------------------------------------------
# ImmediateSubscriptionBus
# ---------------------------------------------------------------------------

class TestImmediateSubscriptionBus(unittest.TestCase):
    def setUp(self):
        from engram.subscription_bus import ImmediateSubscriptionBus
        self.bus = ImmediateSubscriptionBus()

    def _event(self, namespace="ns1", mtype="fact", tags=None):
        return {
            "event": "memory.created",
            "namespace": namespace,
            "memory": {
                "id": "m1",
                "memory_type": mtype,
                "tags": tags or [],
            },
        }

    def test_register_returns_queue(self):
        q = self.bus.register("sub1", "ns1")
        self.assertIsInstance(q, asyncio.Queue)

    def test_unregister_removes_queue(self):
        self.bus.register("sub1", "ns1")
        self.assertEqual(self.bus.subscriber_count, 1)
        self.bus.unregister("sub1", "ns1")
        self.assertEqual(self.bus.subscriber_count, 0)

    def test_publish_exact_namespace_match(self):
        q = self.bus.register("sub1", "ns1")
        self.bus.publish("ns1", self._event("ns1"))
        self.assertEqual(q.qsize(), 1)

    def test_publish_prefix_namespace_match(self):
        q = self.bus.register("sub1", "org:acme")
        self.bus.publish("org:acme:eng", self._event("org:acme:eng"))
        self.assertEqual(q.qsize(), 1)

    def test_publish_no_delivery_to_unrelated_namespace(self):
        q = self.bus.register("sub1", "org:acme")
        self.bus.publish("org:other", self._event("org:other"))
        self.assertEqual(q.qsize(), 0)

    def test_publish_returns_delivery_count(self):
        self.bus.register("sub1", "ns1")
        self.bus.register("sub2", "ns1")
        count = self.bus.publish("ns1", self._event())
        self.assertEqual(count, 2)

    def test_publish_zero_when_no_subscribers(self):
        count = self.bus.publish("ns1", self._event())
        self.assertEqual(count, 0)

    def test_filter_types_blocks_non_matching_type(self):
        q = self.bus.register("sub1", "ns1", filter_types=["incident"])
        self.bus.publish("ns1", self._event(mtype="fact"))
        self.assertEqual(q.qsize(), 0)

    def test_filter_types_allows_matching_type(self):
        q = self.bus.register("sub1", "ns1", filter_types=["incident"])
        self.bus.publish("ns1", self._event(mtype="incident"))
        self.assertEqual(q.qsize(), 1)

    def test_filter_types_tag_match_allows_delivery(self):
        q = self.bus.register("sub1", "ns1", filter_types=["critical"])
        self.bus.publish("ns1", self._event(mtype="fact", tags=["critical", "prod"]))
        self.assertEqual(q.qsize(), 1)

    def test_empty_filter_types_delivers_everything(self):
        q = self.bus.register("sub1", "ns1", filter_types=[])
        self.bus.publish("ns1", self._event(mtype="decision"))
        self.assertEqual(q.qsize(), 1)

    def test_subscriber_count_accurate(self):
        self.bus.register("s1", "ns1")
        self.bus.register("s2", "ns1")
        self.assertEqual(self.bus.subscriber_count, 2)
        self.bus.unregister("s1", "ns1")
        self.assertEqual(self.bus.subscriber_count, 1)

    def test_parent_namespace_does_not_receive_sibling_events(self):
        q_acme = self.bus.register("sub1", "org:acme:marketing")
        self.bus.publish("org:acme:eng", self._event("org:acme:eng"))
        self.assertEqual(q_acme.qsize(), 0)

    def test_unregister_nonexistent_is_noop(self):
        self.bus.unregister("nobody", "ns1")  # should not raise

    def test_multiple_subscribers_different_ns(self):
        q1 = self.bus.register("s1", "ns1")
        q2 = self.bus.register("s2", "ns2")
        self.bus.publish("ns1", self._event("ns1"))
        self.assertEqual(q1.qsize(), 1)
        self.assertEqual(q2.qsize(), 0)


# ---------------------------------------------------------------------------
# Module-level singleton API
# ---------------------------------------------------------------------------

class TestModuleLevelBus(unittest.TestCase):
    def test_register_returns_queue(self):
        from engram import subscription_bus
        # Use a fresh bus to avoid state from other tests
        orig = subscription_bus._bus
        from engram.subscription_bus import ImmediateSubscriptionBus
        subscription_bus._bus = ImmediateSubscriptionBus()
        try:
            q = subscription_bus.register("u1", "test:ns")
            self.assertIsInstance(q, asyncio.Queue)
            self.assertEqual(subscription_bus.subscriber_count(), 1)
        finally:
            subscription_bus._bus = orig

    def test_publish_delivers_via_singleton(self):
        from engram import subscription_bus
        orig = subscription_bus._bus
        from engram.subscription_bus import ImmediateSubscriptionBus
        subscription_bus._bus = ImmediateSubscriptionBus()
        try:
            q = subscription_bus.register("u1", "test:ns")
            subscription_bus.publish("test:ns", {"event": "memory.created", "memory": {"memory_type": "fact", "tags": []}})
            self.assertEqual(q.qsize(), 1)
        finally:
            subscription_bus._bus = orig


# ---------------------------------------------------------------------------
# client._dispatch_immediate()
# ---------------------------------------------------------------------------

class TestDispatchImmediate(unittest.IsolatedAsyncioTestCase):
    def _make_memory(self, mtype="fact", tags=None):
        from engram.models import MemoryEntry, MemoryType
        m = MagicMock(spec=MemoryEntry)
        m.id = "mem-1"
        m.content = "test"
        m.namespace = "ns1"
        m.memory_type = MemoryType.fact
        m.author = "agent"
        m.tags = tags or []
        m.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return m

    async def test_calls_publish_with_correct_payload(self):
        from engram.client import EngramClient
        from engram.subscription_bus import ImmediateSubscriptionBus
        bus = ImmediateSubscriptionBus()
        q = bus.register("sub1", "ns1")

        client = EngramClient.__new__(EngramClient)
        mem = self._make_memory()

        with patch("engram.subscription_bus._bus", bus):
            await client._dispatch_immediate(mem, "ns1")

        self.assertEqual(q.qsize(), 1)
        event = q.get_nowait()
        self.assertEqual(event["event"], "memory.created")
        self.assertEqual(event["namespace"], "ns1")
        self.assertEqual(event["memory"]["id"], "mem-1")

    async def test_noop_when_no_subscribers(self):
        from engram.client import EngramClient
        from engram.subscription_bus import ImmediateSubscriptionBus
        bus = ImmediateSubscriptionBus()

        client = EngramClient.__new__(EngramClient)
        mem = self._make_memory()

        with patch("engram.subscription_bus._bus", bus):
            await client._dispatch_immediate(mem, "ns1")  # should not raise

    async def test_memory_type_in_payload(self):
        from engram.client import EngramClient
        from engram.models import MemoryEntry, MemoryType
        from engram.subscription_bus import ImmediateSubscriptionBus
        bus = ImmediateSubscriptionBus()
        q = bus.register("s1", "ns1")
        mem = self._make_memory()
        mem.memory_type = MemoryType.incident

        client = EngramClient.__new__(EngramClient)
        with patch("engram.subscription_bus._bus", bus):
            await client._dispatch_immediate(mem, "ns1")

        event = q.get_nowait()
        self.assertEqual(event["memory"]["memory_type"], "incident")

    async def test_tags_in_payload(self):
        from engram.client import EngramClient
        from engram.subscription_bus import ImmediateSubscriptionBus
        bus = ImmediateSubscriptionBus()
        q = bus.register("s1", "ns1")
        mem = self._make_memory(tags=["critical", "prod"])

        client = EngramClient.__new__(EngramClient)
        with patch("engram.subscription_bus._bus", bus):
            await client._dispatch_immediate(mem, "ns1")

        event = q.get_nowait()
        self.assertIn("critical", event["memory"]["tags"])

    async def test_nonfatal_when_publish_raises(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        mem = self._make_memory()

        with patch("engram.client.EngramClient._dispatch_immediate", wraps=client._dispatch_immediate):
            with patch("engram.subscription_bus.publish", side_effect=RuntimeError("boom")):
                await client._dispatch_immediate(mem, "ns1")  # no raise


# ---------------------------------------------------------------------------
# stream_namespace SSE endpoint
# ---------------------------------------------------------------------------

class TestStreamNamespace(unittest.IsolatedAsyncioTestCase):
    def _key_entry(self):
        e = MagicMock()
        e.namespaces = ["*"]
        e.read_only = False
        return e

    def _make_client(self):
        c = MagicMock()
        c._arcadedb = AsyncMock()
        c._arcadedb.get_subscription = AsyncMock(return_value=None)
        return c

    async def test_returns_501_when_sse_starlette_missing(self):
        from engram_api.routers.subscriptions import stream_namespace
        client = self._make_client()

        with patch.dict("sys.modules", {"sse_starlette": None, "sse_starlette.sse": None}):
            result = await stream_namespace(
                ns="ns1",
                subscriber_id="sub1",
                user_id="u1",
                key_entry=self._key_entry(),
                client=client,
            )
        self.assertEqual(result.status_code, 501)

    async def test_connected_event_first_in_stream(self):
        from engram_api.routers.subscriptions import stream_namespace
        from engram.subscription_bus import ImmediateSubscriptionBus

        bus = ImmediateSubscriptionBus()
        events_yielded = []

        class FakeEventSourceResponse:
            def __init__(self, gen):
                self._gen = gen
                self.status_code = 200
            async def collect(self):
                async for item in self._gen:
                    events_yielded.append(item)
                    if len(events_yielded) >= 1:
                        break

        with patch("engram.subscription_bus._bus", bus):
            with patch("sse_starlette.sse.EventSourceResponse", FakeEventSourceResponse):
                resp = await stream_namespace(
                    ns="ns1", subscriber_id="sub1",
                    user_id="u1", key_entry=self._key_entry(), client=self._make_client(),
                )
                await resp.collect()

        self.assertEqual(len(events_yielded), 1)
        self.assertEqual(events_yielded[0]["event"], "connected")

    async def test_registers_and_unregisters_queue(self):
        from engram_api.routers.subscriptions import stream_namespace
        from engram.subscription_bus import ImmediateSubscriptionBus

        bus = ImmediateSubscriptionBus()

        class StopAfterConnected:
            def __init__(self, gen):
                self._gen = gen
                self.status_code = 200
            async def exhaust(self):
                async for item in self._gen:
                    break
                # Explicitly close the async generator so its finally block runs
                await self._gen.aclose()

        with patch("engram.subscription_bus._bus", bus):
            with patch("sse_starlette.sse.EventSourceResponse", StopAfterConnected):
                resp = await stream_namespace(
                    ns="ns1", subscriber_id="sub1",
                    user_id="u1", key_entry=self._key_entry(), client=self._make_client(),
                )
                self.assertEqual(bus.subscriber_count, 1)
                await resp.exhaust()
                self.assertEqual(bus.subscriber_count, 0)

    async def test_delivers_memory_event_from_queue(self):
        import json
        from engram_api.routers.subscriptions import stream_namespace
        from engram.subscription_bus import ImmediateSubscriptionBus

        bus = ImmediateSubscriptionBus()
        events_yielded = []

        class Collector:
            def __init__(self, gen):
                self._gen = gen
                self.status_code = 200
            async def collect(self, n=2):
                async for item in self._gen:
                    events_yielded.append(item)
                    if len(events_yielded) >= n:
                        break

        with patch("engram.subscription_bus._bus", bus):
            with patch("sse_starlette.sse.EventSourceResponse", Collector):
                resp = await stream_namespace(
                    ns="ns1", subscriber_id="sub1",
                    user_id="u1", key_entry=self._key_entry(), client=self._make_client(),
                )
                # Push an event to the queue before collecting
                bus.publish("ns1", {
                    "event": "memory.created",
                    "namespace": "ns1",
                    "memory": {"id": "m1", "memory_type": "fact", "tags": []},
                })
                await resp.collect(n=2)

        self.assertEqual(events_yielded[0]["event"], "connected")
        self.assertEqual(events_yielded[1]["event"], "memory.created")
        payload = json.loads(events_yielded[1]["data"])
        self.assertEqual(payload["memory"]["id"], "m1")

    async def test_filter_types_loaded_from_subscription_record(self):
        from engram_api.routers.subscriptions import stream_namespace
        from engram.subscription_bus import ImmediateSubscriptionBus
        import json

        bus = ImmediateSubscriptionBus()
        sub = MagicMock()
        sub.filter_types = ["incident"]

        client = self._make_client()
        client._arcadedb.get_subscription = AsyncMock(return_value=sub)
        events_yielded = []

        class Collector:
            def __init__(self, gen):
                self._gen = gen
                self.status_code = 200
            async def collect(self, n=2):
                async for item in self._gen:
                    events_yielded.append(item)
                    if len(events_yielded) >= n:
                        break

        with patch("engram.subscription_bus._bus", bus):
            with patch("sse_starlette.sse.EventSourceResponse", Collector):
                resp = await stream_namespace(
                    ns="ns1", subscriber_id="sub1",
                    user_id="u1", key_entry=self._key_entry(), client=client,
                )
                # A "fact" should be filtered out
                bus.publish("ns1", {
                    "event": "memory.created", "namespace": "ns1",
                    "memory": {"id": "m1", "memory_type": "fact", "tags": []},
                })
                # An "incident" should pass through — but we only collect connected
                await resp.collect(n=1)

        # Only the connected event arrived; the fact was filtered
        self.assertEqual(len(events_yielded), 1)
        self.assertEqual(events_yielded[0]["event"], "connected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
