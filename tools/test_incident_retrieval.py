"""
tools/test_incident_retrieval.py — Tests for automatic past-incident retrieval.

Covers:
- EngramClient.get_past_incidents(): returns (MemoryEntry, score) pairs
- get_past_incidents(): empty when no similar found
- get_past_incidents(): respects top_k and threshold passthrough
- IncidentWebhookResponse: past_incidents enriched with full content
- receive_incident: past_incidents populated when similar incidents exist
- receive_incident: past_incidents empty when no similar incidents
- PastIncidentSummary: default resolution is empty string
- MCP incident_context tool: returns formatted past incident list
- MCP incident_context tool: returns "No similar" message when none found
- MCP incident_context tool: top_k and threshold passed to client
"""
from __future__ import annotations

import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, _REPO_ROOT + "/packages/core")
sys.path.insert(0, _REPO_ROOT + "/packages/api")
sys.path.insert(0, _REPO_ROOT + "/packages/mcp-server")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_memory(mem_id="mem-1", content="Redis OOM at 3am", severity="CRITICAL"):
    from engram.models import MemoryEntry, MemoryType
    m = MagicMock(spec=MemoryEntry)
    m.id = mem_id
    m.content = content
    m.namespace = "ns1"
    m.memory_type = MemoryType.incident
    m.author = "oncall"
    m.tags = ["incident", severity.lower()]
    m.created_at = datetime(2025, 1, 1, 3, 0, 0)
    m.metadata = {"severity": severity}
    m.affects = []
    return m


# ---------------------------------------------------------------------------
# EngramClient.get_past_incidents()
# ---------------------------------------------------------------------------

class TestGetPastIncidents(unittest.IsolatedAsyncioTestCase):
    async def _make_client(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._started = True
        client._arcadedb = AsyncMock()
        client._embedder = AsyncMock()
        client._embedder.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        return client

    async def test_returns_empty_when_no_similar(self):
        client = await self._make_client()
        client._arcadedb.find_similar_incidents = AsyncMock(return_value=[])
        result = await client.get_past_incidents("disk full on node-1", "ns1")
        self.assertEqual(result, [])
        client._arcadedb.find_similar_incidents.assert_awaited_once()

    async def test_returns_incidents_with_scores(self):
        client = await self._make_client()
        mem = _make_memory("past-1", "Redis OOM on prod")
        client._arcadedb.find_similar_incidents = AsyncMock(
            return_value=[("past-1", 0.91)]
        )
        client._arcadedb.get_memory = AsyncMock(return_value=mem)

        result = await client.get_past_incidents("Redis out of memory", "ns1")
        self.assertEqual(len(result), 1)
        self.assertIs(result[0][0], mem)
        self.assertAlmostEqual(result[0][1], 0.91)

    async def test_skips_memory_when_get_memory_returns_none(self):
        client = await self._make_client()
        client._arcadedb.find_similar_incidents = AsyncMock(
            return_value=[("gone-id", 0.88)]
        )
        client._arcadedb.get_memory = AsyncMock(return_value=None)
        result = await client.get_past_incidents("crash", "ns1")
        self.assertEqual(result, [])

    async def test_multiple_incidents_returned(self):
        client = await self._make_client()
        mem1 = _make_memory("past-1", "Redis OOM")
        mem2 = _make_memory("past-2", "Redis timeout")
        client._arcadedb.find_similar_incidents = AsyncMock(
            return_value=[("past-1", 0.93), ("past-2", 0.80)]
        )
        client._arcadedb.get_memory = AsyncMock(side_effect=[mem1, mem2])

        result = await client.get_past_incidents("Redis error", "ns1")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0].content, "Redis OOM")
        self.assertEqual(result[1][0].content, "Redis timeout")

    async def test_top_k_passed_to_arcadedb(self):
        client = await self._make_client()
        client._arcadedb.find_similar_incidents = AsyncMock(return_value=[])
        await client.get_past_incidents("crash", "ns1", top_k=3)
        call_kw = client._arcadedb.find_similar_incidents.call_args
        self.assertEqual(call_kw.kwargs.get("top_k", call_kw.args[3] if len(call_kw.args) > 3 else None), 3)

    async def test_threshold_passed_to_arcadedb(self):
        client = await self._make_client()
        client._arcadedb.find_similar_incidents = AsyncMock(return_value=[])
        await client.get_past_incidents("crash", "ns1", threshold=0.9)
        call_kw = client._arcadedb.find_similar_incidents.call_args
        # threshold is the 5th keyword arg
        kw = call_kw.kwargs
        arg = kw.get("threshold")
        self.assertAlmostEqual(arg, 0.9)

    async def test_exclude_id_is_empty_string(self):
        """get_past_incidents uses content-based search, no existing ID to exclude."""
        client = await self._make_client()
        client._arcadedb.find_similar_incidents = AsyncMock(return_value=[])
        await client.get_past_incidents("crash", "ns1")
        call_kw = client._arcadedb.find_similar_incidents.call_args.kwargs
        self.assertEqual(call_kw.get("exclude_id"), "")

    async def test_requires_started(self):
        from engram.client import EngramClient
        client = EngramClient.__new__(EngramClient)
        client._started = False
        with self.assertRaises(RuntimeError):
            await client.get_past_incidents("crash", "ns1")


# ---------------------------------------------------------------------------
# PastIncidentSummary model
# ---------------------------------------------------------------------------

class TestPastIncidentSummary(unittest.TestCase):
    def test_defaults(self):
        from engram_api.routers.webhooks import PastIncidentSummary
        s = PastIncidentSummary(
            memory_id="m1",
            content="Redis OOM",
            severity="CRITICAL",
            similarity=0.92,
            created_at="2025-01-01T03:00:00",
        )
        self.assertEqual(s.resolution, "")
        self.assertEqual(s.memory_id, "m1")

    def test_resolution_can_be_set(self):
        from engram_api.routers.webhooks import PastIncidentSummary
        s = PastIncidentSummary(
            memory_id="m1",
            content="crash",
            severity="HIGH",
            similarity=0.80,
            created_at="2025-01-01T00:00:00",
            resolution="Resolved by restarting pod",
        )
        self.assertEqual(s.resolution, "Resolved by restarting pod")


# ---------------------------------------------------------------------------
# receive_incident webhook endpoint
# ---------------------------------------------------------------------------

class TestReceiveIncidentEnriched(unittest.IsolatedAsyncioTestCase):
    def _make_fastapi_client(self, mock_engram_client):
        sys.path.insert(0, _REPO_ROOT + "/packages/api")
        from fastapi.testclient import TestClient
        from engram_api.main import create_app
        from engram_api.auth import get_client
        app = create_app()
        app.dependency_overrides[get_client] = lambda: mock_engram_client
        return TestClient(app, raise_server_exceptions=True)

    def _make_engram_mock(self, similar_pairs=None, past_memories=None):
        from engram.models import MemoryEntry, MemoryType, MemoryStatus
        mem = MagicMock(spec=MemoryEntry)
        mem.id = "new-incident-id"
        mem.content = "Incident: Redis OOM\nSeverity: CRITICAL"
        mem.namespace = "org:default"
        mem.memory_type = MemoryType.incident
        mem.status = MemoryStatus.active
        mem.tags = ["incident"]
        mem.created_at = datetime(2025, 1, 1, 3, 0, 0)
        mem.metadata = {}
        mem.affects = []
        mem.author = ""
        mem.rationale = ""

        ec = MagicMock()
        ec.add = AsyncMock(return_value=mem)
        ec._embedder = AsyncMock()
        ec._embedder.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        ec._arcadedb = AsyncMock()
        ec._arcadedb.find_similar_incidents = AsyncMock(
            return_value=similar_pairs or []
        )
        ec._arcadedb.create_similar_to_edge = AsyncMock()
        if past_memories:
            ec._arcadedb.get_memory = AsyncMock(side_effect=past_memories)
        else:
            ec._arcadedb.get_memory = AsyncMock(return_value=None)
        return ec

    def test_past_incidents_empty_when_no_similar(self):
        mock_ec = self._make_engram_mock(similar_pairs=[])
        fc = self._make_fastapi_client(mock_ec)
        resp = fc.post("/api/v1/webhooks/incident", json={"title": "Redis OOM", "severity": "CRITICAL"})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["past_incidents"], [])

    def test_past_incidents_populated_with_content(self):
        past_mem = _make_memory("past-1", "Redis OOM last month", "CRITICAL")
        mock_ec = self._make_engram_mock(
            similar_pairs=[("past-1", 0.91)],
            past_memories=[past_mem],
        )
        fc = self._make_fastapi_client(mock_ec)
        resp = fc.post("/api/v1/webhooks/incident", json={"title": "Redis OOM again"})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(len(data["past_incidents"]), 1)
        pi = data["past_incidents"][0]
        self.assertEqual(pi["memory_id"], "past-1")
        self.assertEqual(pi["content"], "Redis OOM last month")
        self.assertAlmostEqual(pi["similarity"], 0.91, places=2)

    def test_similar_incidents_backward_compat_ids_still_present(self):
        past_mem = _make_memory("past-2", "Disk full on node", "HIGH")
        mock_ec = self._make_engram_mock(
            similar_pairs=[("past-2", 0.85)],
            past_memories=[past_mem],
        )
        fc = self._make_fastapi_client(mock_ec)
        resp = fc.post("/api/v1/webhooks/incident", json={"title": "Disk full"})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("past-2", data["similar_incidents"])

    def test_get_memory_none_skips_past_incident_entry(self):
        """If get_memory returns None for a similar id, that entry is silently skipped."""
        mock_ec = self._make_engram_mock(
            similar_pairs=[("missing-id", 0.88)],
            past_memories=[None],
        )
        fc = self._make_fastapi_client(mock_ec)
        resp = fc.post("/api/v1/webhooks/incident", json={"title": "Crash"})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("missing-id", data["similar_incidents"])  # ID still tracked
        self.assertEqual(data["past_incidents"], [])             # but no full record


# ---------------------------------------------------------------------------
# MCP incident_context tool
# ---------------------------------------------------------------------------

class TestMCPIncidentContext(unittest.IsolatedAsyncioTestCase):
    async def test_returns_no_similar_message_when_empty(self):
        from engram_mcp.server import _dispatch
        client = MagicMock()
        client.get_past_incidents = AsyncMock(return_value=[])
        result = await _dispatch(
            "incident_context",
            {"content": "disk full", "namespace": "ns1"},
            client,
            None,
        )
        self.assertEqual(len(result), 1)
        self.assertIn("No similar", result[0].text)

    async def test_returns_formatted_incidents(self):
        from engram_mcp.server import _dispatch
        mem = _make_memory("past-1", "Redis OOM on prod", "CRITICAL")
        client = MagicMock()
        client.get_past_incidents = AsyncMock(return_value=[(mem, 0.92)])
        result = await _dispatch(
            "incident_context",
            {"content": "Redis out of memory", "namespace": "ns1"},
            client,
            None,
        )
        self.assertEqual(len(result), 1)
        text = result[0].text
        self.assertIn("Redis OOM on prod", text)
        self.assertIn("0.92", text)

    async def test_top_k_and_threshold_passed_through(self):
        from engram_mcp.server import _dispatch
        client = MagicMock()
        client.get_past_incidents = AsyncMock(return_value=[])
        await _dispatch(
            "incident_context",
            {"content": "crash", "namespace": "ns1", "top_k": 3, "threshold": 0.85},
            client,
            None,
        )
        client.get_past_incidents.assert_awaited_once_with(
            content="crash",
            namespace="ns1",
            top_k=3,
            threshold=0.85,
        )

    async def test_multiple_incidents_all_included(self):
        from engram_mcp.server import _dispatch
        m1 = _make_memory("id-1", "Kafka lag spike", "HIGH")
        m2 = _make_memory("id-2", "Kafka consumer restart", "MEDIUM")
        client = MagicMock()
        client.get_past_incidents = AsyncMock(return_value=[(m1, 0.90), (m2, 0.78)])
        result = await _dispatch(
            "incident_context",
            {"content": "Kafka rebalance", "namespace": "ns1"},
            client,
            None,
        )
        text = result[0].text
        self.assertIn("Kafka lag spike", text)
        self.assertIn("Kafka consumer restart", text)
        self.assertIn("Found 2", text)

    async def test_severity_shown_in_output(self):
        from engram_mcp.server import _dispatch
        mem = _make_memory("id-1", "DB deadlock", "CRITICAL")
        client = MagicMock()
        client.get_past_incidents = AsyncMock(return_value=[(mem, 0.88)])
        result = await _dispatch(
            "incident_context",
            {"content": "database lock", "namespace": "ns1"},
            client,
            None,
        )
        self.assertIn("CRITICAL", result[0].text)

    async def test_default_top_k_is_5(self):
        from engram_mcp.server import _dispatch
        client = MagicMock()
        client.get_past_incidents = AsyncMock(return_value=[])
        await _dispatch(
            "incident_context",
            {"content": "crash", "namespace": "ns1"},
            client,
            None,
        )
        call_kw = client.get_past_incidents.call_args
        self.assertEqual(call_kw.kwargs["top_k"], 5)

    async def test_default_threshold_is_0_75(self):
        from engram_mcp.server import _dispatch
        client = MagicMock()
        client.get_past_incidents = AsyncMock(return_value=[])
        await _dispatch(
            "incident_context",
            {"content": "crash", "namespace": "ns1"},
            client,
            None,
        )
        call_kw = client.get_past_incidents.call_args
        self.assertAlmostEqual(call_kw.kwargs["threshold"], 0.75)


# ---------------------------------------------------------------------------
# incident_context tool declared in MCP tool list
# ---------------------------------------------------------------------------

class TestMCPToolList(unittest.TestCase):
    def test_incident_context_in_tool_list(self):
        from engram_mcp.server import TOOLS
        names = [t.name for t in TOOLS]
        self.assertIn("incident_context", names)

    def test_incident_context_has_required_schema_fields(self):
        from engram_mcp.server import TOOLS
        tool = next(t for t in TOOLS if t.name == "incident_context")
        props = tool.inputSchema["properties"]
        self.assertIn("content", props)
        self.assertIn("namespace", props)
        self.assertIn("top_k", props)
        self.assertIn("threshold", props)
        self.assertEqual(tool.inputSchema["required"], ["content", "namespace"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
