"""
tools/test_incident_webhook.py — Unit tests for incident webhook receiver.

Tests cover:
- normalise_webhook_payload() for PagerDuty, AlertManager, and generic formats
- create_similar_to_edge / create_resolved_by_edge (mock ArcadeDB)
- find_similar_incidents basic logic
- receive_incident endpoint (FastAPI test client with mocked EngramClient)
- resolve_incident endpoint
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/Users/thameema/git/engram/packages/api")
sys.path.insert(0, "/Users/thameema/git/engram/packages/core")

from engram_api.routers.webhooks import (
    _normalise_alertmanager,
    _normalise_generic,
    _normalise_pagerduty,
    _verify_webhook_secret,
    normalise_webhook_payload,
)
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Payload normalisation
# ---------------------------------------------------------------------------

class TestNormalisePagerDuty(unittest.TestCase):
    def _body(self, event_type="incident.trigger", severity="critical", title="DB outage"):
        return {
            "event": {
                "event_type": event_type,
                "id": "pd-001",
                "data": {
                    "id": "INC001",
                    "title": title,
                    "severity": severity,
                    "description": "The database is down.",
                },
            }
        }

    def test_trigger_maps_to_firing(self):
        norm = _normalise_pagerduty(self._body("incident.trigger"))
        self.assertEqual(norm["status"], "firing")

    def test_resolve_maps_to_resolved(self):
        norm = _normalise_pagerduty(self._body("incident.resolve"))
        self.assertEqual(norm["status"], "resolved")

    def test_title_extracted(self):
        norm = _normalise_pagerduty(self._body(title="Payment timeout"))
        self.assertEqual(norm["title"], "Payment timeout")

    def test_severity_uppercased(self):
        norm = _normalise_pagerduty(self._body(severity="high"))
        self.assertEqual(norm["severity"], "HIGH")

    def test_source_is_pagerduty(self):
        norm = _normalise_pagerduty(self._body())
        self.assertEqual(norm["source"], "pagerduty")


class TestNormaliseAlertManager(unittest.TestCase):
    def _body(self, status="firing", alertname="HighMemory", severity="warning"):
        return {
            "alerts": [{
                "status": status,
                "labels": {"alertname": alertname, "severity": severity},
                "annotations": {"summary": "Memory > 90%", "description": "Host is OOM."},
            }]
        }

    def test_firing_status(self):
        norm = _normalise_alertmanager(self._body("firing"))
        self.assertEqual(norm["status"], "firing")

    def test_resolved_status(self):
        norm = _normalise_alertmanager(self._body("resolved"))
        self.assertEqual(norm["status"], "resolved")

    def test_alertname_as_title(self):
        norm = _normalise_alertmanager(self._body(alertname="DiskFull"))
        self.assertEqual(norm["title"], "DiskFull")

    def test_empty_alerts_returns_none(self):
        self.assertIsNone(_normalise_alertmanager({"alerts": []}))

    def test_source_is_alertmanager(self):
        norm = _normalise_alertmanager(self._body())
        self.assertEqual(norm["source"], "alertmanager")


class TestNormaliseGeneric(unittest.TestCase):
    def test_basic_fields(self):
        body = {"title": "Service down", "severity": "P1", "status": "firing", "description": "srv crashed"}
        norm = _normalise_generic(body)
        self.assertEqual(norm["title"], "Service down")
        self.assertEqual(norm["severity"], "P1")

    def test_missing_title_uses_default(self):
        norm = _normalise_generic({})
        self.assertEqual(norm["title"], "Incident")

    def test_unknown_severity_becomes_empty_then_unknown(self):
        norm = _normalise_generic({})
        self.assertEqual(norm["severity"], "UNKNOWN")


class TestNormaliseWebhookPayload(unittest.TestCase):
    def test_pagerduty_detected(self):
        body = {"event": {"event_type": "incident.trigger", "data": {"title": "PD inc", "severity": "critical"}}}
        norm = normalise_webhook_payload(body)
        self.assertEqual(norm["source"], "pagerduty")

    def test_alertmanager_detected(self):
        body = {"alerts": [{"status": "firing", "labels": {"alertname": "test"}, "annotations": {}}]}
        norm = normalise_webhook_payload(body)
        self.assertEqual(norm["source"], "alertmanager")

    def test_generic_fallback(self):
        body = {"title": "Custom alert", "description": "custom desc"}
        norm = normalise_webhook_payload(body)
        self.assertEqual(norm["source"], "webhook")


# ---------------------------------------------------------------------------
# _verify_webhook_secret
# ---------------------------------------------------------------------------

class TestVerifyWebhookSecret(unittest.TestCase):
    def test_no_env_secret_passes_always(self):
        import engram_api.routers.webhooks as wh
        orig = wh._WEBHOOK_SECRET
        wh._WEBHOOK_SECRET = ""
        try:
            _verify_webhook_secret(None)   # should not raise
            _verify_webhook_secret("wrong")  # should not raise
        finally:
            wh._WEBHOOK_SECRET = orig

    def test_correct_secret_passes(self):
        import engram_api.routers.webhooks as wh
        orig = wh._WEBHOOK_SECRET
        wh._WEBHOOK_SECRET = "mysecret"
        try:
            _verify_webhook_secret("mysecret")  # should not raise
        finally:
            wh._WEBHOOK_SECRET = orig

    def test_missing_secret_raises_401(self):
        import engram_api.routers.webhooks as wh
        orig = wh._WEBHOOK_SECRET
        wh._WEBHOOK_SECRET = "mysecret"
        try:
            with self.assertRaises(HTTPException) as ctx:
                _verify_webhook_secret(None)
            self.assertEqual(ctx.exception.status_code, 401)
        finally:
            wh._WEBHOOK_SECRET = orig

    def test_wrong_secret_raises_403(self):
        import engram_api.routers.webhooks as wh
        orig = wh._WEBHOOK_SECRET
        wh._WEBHOOK_SECRET = "mysecret"
        try:
            with self.assertRaises(HTTPException) as ctx:
                _verify_webhook_secret("wrongsecret")
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            wh._WEBHOOK_SECRET = orig


# ---------------------------------------------------------------------------
# receive_incident endpoint (mock EngramClient)
# ---------------------------------------------------------------------------

def _make_mock_client(incident_id="inc-001"):
    from engram.models import MemoryEntry, MemoryType
    memory = MemoryEntry(
        id=incident_id,
        content="Incident: DB outage",
        namespace="test:ns",
        memory_type=MemoryType.incident,
    )
    client = MagicMock()
    client.add = AsyncMock(return_value=memory)
    client._embedder = MagicMock()
    client._embedder.embed = AsyncMock(return_value=[0.1] * 384)
    client._arcadedb = MagicMock()
    client._arcadedb.find_similar_incidents = AsyncMock(return_value=[])
    client._arcadedb.create_similar_to_edge = AsyncMock()
    client._arcadedb.create_resolved_by_edge = AsyncMock()
    client._arcadedb.get_memory = AsyncMock(return_value=memory)
    return client


class TestReceiveIncident(unittest.IsolatedAsyncioTestCase):
    async def test_pagerduty_payload_creates_incident_memory(self):
        from engram_api.routers.webhooks import receive_incident, IncidentWebhookResponse
        from unittest.mock import patch, AsyncMock as AM
        from starlette.requests import Request as StarletteRequest
        import json, io

        body = {"event": {"event_type": "incident.trigger", "data": {"title": "DB down", "severity": "critical", "id": "pd-1"}}}
        body_bytes = json.dumps(body).encode()

        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value=body)

        client = _make_mock_client()
        resp = await receive_incident(
            request=mock_request,
            namespace="test:ns",
            x_engram_webhook_secret=None,
            client=client,
        )

        client.add.assert_awaited_once()
        call_kwargs = client.add.call_args.kwargs
        self.assertEqual(call_kwargs["memory_type"].value, "incident")
        self.assertIn("DB down", call_kwargs["content"])
        self.assertIn("CRITICAL", call_kwargs["content"])
        self.assertIsInstance(resp, IncidentWebhookResponse)
        self.assertEqual(resp.memory_id, "inc-001")

    async def test_similar_incidents_linked_via_similar_to(self):
        from engram_api.routers.webhooks import receive_incident

        body = {"title": "DB outage again", "severity": "P1"}
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value=body)

        client = _make_mock_client("inc-new")
        client._arcadedb.find_similar_incidents = AsyncMock(return_value=[("inc-old", 0.92)])

        resp = await receive_incident(
            request=mock_request,
            namespace="test:ns",
            x_engram_webhook_secret=None,
            client=client,
        )
        client._arcadedb.create_similar_to_edge.assert_awaited_once()
        self.assertIn("inc-old", resp.similar_incidents)

    async def test_no_similar_incidents_returns_empty_list(self):
        from engram_api.routers.webhooks import receive_incident

        body = {"title": "Brand new alert type"}
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value=body)

        client = _make_mock_client()
        resp = await receive_incident(
            request=mock_request,
            namespace="test:ns",
            x_engram_webhook_secret=None,
            client=client,
        )
        self.assertEqual(resp.similar_incidents, [])


class TestResolveIncident(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_creates_resolved_by_edge(self):
        from engram_api.routers.webhooks import resolve_incident, ResolveRequest

        client = _make_mock_client("inc-001")
        req = ResolveRequest(
            resolution_notes="Fixed by restarting DB.",
            resolver="on-call-engineer",
            namespace="test:ns",
        )
        resp = await resolve_incident(
            incident_id="inc-001",
            req=req,
            x_engram_webhook_secret=None,
            client=client,
        )

        client._arcadedb.create_resolved_by_edge.assert_awaited_once()
        self.assertEqual(resp.incident_id, "inc-001")
        self.assertIsNotNone(resp.resolution_memory_id)

    async def test_resolve_incident_not_found_raises_404(self):
        from engram_api.routers.webhooks import resolve_incident, ResolveRequest

        client = _make_mock_client()
        client._arcadedb.get_memory = AsyncMock(return_value=None)

        req = ResolveRequest(namespace="test:ns")
        with self.assertRaises(HTTPException) as ctx:
            await resolve_incident(
                incident_id="nonexistent",
                req=req,
                x_engram_webhook_secret=None,
                client=client,
            )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
