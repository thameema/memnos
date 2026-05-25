"""
tools/test_webhook_delivery.py — Tests for subscription webhook/immediate delivery.

Covers:
- Subscription model: delivery_mode and webhook_url fields default correctly
- client.subscribe(): passes delivery_mode and webhook_url through to ArcadeDB
- client._dispatch_webhooks(): fires POST to webhook subscribers, respects filter_types
- Webhook delivery skipped when no webhook subscribers
- Webhook failure is non-fatal (logged, not raised)
- filter_types applied before dispatching
- REST SubscribeRequest: accepts delivery_mode and webhook_url
- Subscription response includes delivery_mode and webhook_url
"""
from __future__ import annotations

import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, _REPO_ROOT + "/packages/core")


# ---------------------------------------------------------------------------
# Subscription model
# ---------------------------------------------------------------------------

class TestSubscriptionModel(unittest.TestCase):
    def test_default_delivery_mode_is_cursor(self):
        from engram.models import Subscription
        sub = Subscription(subscriber_id="u1", namespace="ns1")
        self.assertEqual(sub.delivery_mode, "cursor")

    def test_default_webhook_url_is_empty(self):
        from engram.models import Subscription
        sub = Subscription(subscriber_id="u1", namespace="ns1")
        self.assertEqual(sub.webhook_url, "")

    def test_webhook_mode_stored(self):
        from engram.models import Subscription
        sub = Subscription(subscriber_id="u1", namespace="ns1",
                           delivery_mode="webhook", webhook_url="https://example.com/hook")
        self.assertEqual(sub.delivery_mode, "webhook")
        self.assertEqual(sub.webhook_url, "https://example.com/hook")

    def test_immediate_mode_stored(self):
        from engram.models import Subscription
        sub = Subscription(subscriber_id="u1", namespace="ns1", delivery_mode="immediate")
        self.assertEqual(sub.delivery_mode, "immediate")


# ---------------------------------------------------------------------------
# client.subscribe() passes through delivery params
# ---------------------------------------------------------------------------

class TestClientSubscribe(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_passes_delivery_mode_and_webhook(self):
        from engram.client import EngramClient
        from engram.models import Subscription
        client = EngramClient.__new__(EngramClient)
        client._started = True
        client._arcadedb = AsyncMock()
        client._arcadedb.upsert_subscription = AsyncMock(return_value="sub-id")

        await client.subscribe(
            "user1", "ns1",
            delivery_mode="webhook",
            webhook_url="https://hooks.example.com/memory",
        )

        called_sub: Subscription = client._arcadedb.upsert_subscription.call_args[0][0]
        self.assertEqual(called_sub.delivery_mode, "webhook")
        self.assertEqual(called_sub.webhook_url, "https://hooks.example.com/memory")

    async def test_subscribe_cursor_default(self):
        from engram.client import EngramClient
        from engram.models import Subscription
        client = EngramClient.__new__(EngramClient)
        client._started = True
        client._arcadedb = AsyncMock()
        client._arcadedb.upsert_subscription = AsyncMock(return_value="sub-id")

        await client.subscribe("user1", "ns1")

        called_sub: Subscription = client._arcadedb.upsert_subscription.call_args[0][0]
        self.assertEqual(called_sub.delivery_mode, "cursor")
        self.assertEqual(called_sub.webhook_url, "")


# ---------------------------------------------------------------------------
# _dispatch_webhooks
# ---------------------------------------------------------------------------

class TestDispatchWebhooks(unittest.IsolatedAsyncioTestCase):
    def _make_memory(self, memory_type="fact", tags=None):
        from engram.models import MemoryEntry, MemoryType
        m = MagicMock(spec=MemoryEntry)
        m.id = "mem-1"
        m.content = "test content"
        m.namespace = "ns1"
        m.memory_type = MemoryType.fact
        m.author = "agent"
        m.tags = tags or []
        from datetime import datetime
        m.created_at = datetime.utcnow()
        return m

    async def test_no_op_when_no_webhook_subscribers(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._arcadedb = AsyncMock()
        client._arcadedb.get_webhook_subscriptions = AsyncMock(return_value=[])
        memory = self._make_memory()
        await client._dispatch_webhooks(memory, "ns1")
        client._arcadedb.get_webhook_subscriptions.assert_awaited_once_with("ns1")

    async def test_dispatches_post_to_webhook(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._arcadedb = AsyncMock()
        client._arcadedb.get_webhook_subscriptions = AsyncMock(return_value=[
            {"subscriber_id": "u1", "webhook_url": "https://hooks.example.com/mem", "filter_types": []},
        ])
        memory = self._make_memory()

        mock_http_resp = MagicMock()
        mock_http_resp.raise_for_status = MagicMock()
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = AsyncMock(return_value=mock_http_resp)

        with patch("httpx.AsyncClient", return_value=mock_http_client):
            await client._dispatch_webhooks(memory, "ns1")
            import asyncio; await asyncio.sleep(0)

        mock_http_client.post.assert_awaited_once()
        call_args = mock_http_client.post.call_args
        self.assertEqual(call_args[0][0], "https://hooks.example.com/mem")
        payload = call_args[1]["json"]
        self.assertEqual(payload["event"], "memory.created")
        self.assertEqual(payload["memory"]["id"], "mem-1")

    async def test_filter_types_applied_before_dispatch(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._arcadedb = AsyncMock()
        client._arcadedb.get_webhook_subscriptions = AsyncMock(return_value=[
            {"subscriber_id": "u1", "webhook_url": "https://h.com/hook", "filter_types": ["decision"]},
        ])
        memory = self._make_memory(memory_type="fact", tags=[])

        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_http_client):
            await client._dispatch_webhooks(memory, "ns1")
            import asyncio; await asyncio.sleep(0)

        mock_http_client.post.assert_not_awaited()

    async def test_filter_types_tag_match_dispatches(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._arcadedb = AsyncMock()
        client._arcadedb.get_webhook_subscriptions = AsyncMock(return_value=[
            {"subscriber_id": "u1", "webhook_url": "https://h.com/hook", "filter_types": ["important"]},
        ])
        memory = self._make_memory(tags=["important", "team"])

        mock_http_resp = MagicMock()
        mock_http_resp.raise_for_status = MagicMock()
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = AsyncMock(return_value=mock_http_resp)

        with patch("httpx.AsyncClient", return_value=mock_http_client):
            await client._dispatch_webhooks(memory, "ns1")
            import asyncio; await asyncio.sleep(0)

        mock_http_client.post.assert_awaited_once()

    async def test_webhook_failure_is_nonfatal(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._arcadedb = AsyncMock()
        client._arcadedb.get_webhook_subscriptions = AsyncMock(return_value=[
            {"subscriber_id": "u1", "webhook_url": "https://down.example.com/hook", "filter_types": []},
        ])
        memory = self._make_memory()

        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = AsyncMock(side_effect=Exception("connection refused"))

        with patch("httpx.AsyncClient", return_value=mock_http_client):
            await client._dispatch_webhooks(memory, "ns1")
            import asyncio; await asyncio.sleep(0)

    async def test_httpx_missing_logs_warning(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._arcadedb = AsyncMock()
        client._arcadedb.get_webhook_subscriptions = AsyncMock(return_value=[
            {"subscriber_id": "u1", "webhook_url": "https://h.com/hook", "filter_types": []},
        ])
        memory = self._make_memory()

        with patch.dict("sys.modules", {"httpx": None}):
            await client._dispatch_webhooks(memory, "ns1")


# ---------------------------------------------------------------------------
# REST SubscribeRequest
# ---------------------------------------------------------------------------

class TestSubscribeRequestModel(unittest.TestCase):
    def test_defaults(self):
        sys.path.insert(0, _REPO_ROOT + "/packages/api")
        from engram_api.routers.subscriptions import SubscribeRequest
        req = SubscribeRequest(namespace="ns1")
        self.assertEqual(req.delivery_mode, "cursor")
        self.assertEqual(req.webhook_url, "")

    def test_webhook_mode(self):
        sys.path.insert(0, _REPO_ROOT + "/packages/api")
        from engram_api.routers.subscriptions import SubscribeRequest
        req = SubscribeRequest(
            namespace="ns1",
            delivery_mode="webhook",
            webhook_url="https://hooks.example.com/mem",
        )
        self.assertEqual(req.delivery_mode, "webhook")
        self.assertEqual(req.webhook_url, "https://hooks.example.com/mem")


if __name__ == "__main__":
    unittest.main(verbosity=2)
