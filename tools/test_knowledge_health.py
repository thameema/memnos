"""
tools/test_knowledge_health.py — Unit tests for knowledge health metrics API.

Tests cover:
- _compute_health_score() scoring logic
- knowledge_health endpoint with mocked ArcadeDB client
- HealthIssue generation for each metric category
- Perfect-health (score=100) path
- Edge cases: negative total, all metrics zero
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/Users/thameema/git/engram/packages/api")
sys.path.insert(0, "/Users/thameema/git/engram/packages/core")

from engram_api.routers.knowledge import (
    _compute_health_score,
    KnowledgeHealthReport,
    HealthIssue,
)


# ---------------------------------------------------------------------------
# _compute_health_score
# ---------------------------------------------------------------------------

class TestComputeHealthScore(unittest.TestCase):
    def test_perfect_score(self):
        self.assertEqual(_compute_health_score(0, 0, 0, 0), 100)

    def test_single_unused_constraint(self):
        # 1 unused constraint → -3
        self.assertEqual(_compute_health_score(1, 0, 0, 0), 97)

    def test_unused_constraints_cap(self):
        # 5 unused constraints → -15 (capped at 15)
        self.assertEqual(_compute_health_score(5, 0, 0, 0), 85)
        # 10 unused constraints → still capped at -15
        self.assertEqual(_compute_health_score(10, 0, 0, 0), 85)

    def test_single_stale_namespace(self):
        # 1 stale namespace → -5
        self.assertEqual(_compute_health_score(0, 1, 0, 0), 95)

    def test_stale_namespaces_cap(self):
        # 4 stale namespaces → -20 (capped at 20)
        self.assertEqual(_compute_health_score(0, 4, 0, 0), 80)
        # 10 stale namespaces → still capped at -20
        self.assertEqual(_compute_health_score(0, 10, 0, 0), 80)

    def test_single_overdue_review(self):
        # 1 overdue review → -2
        self.assertEqual(_compute_health_score(0, 0, 1, 0), 98)

    def test_overdue_reviews_cap(self):
        # 10 overdue reviews → -20 (capped at 20)
        self.assertEqual(_compute_health_score(0, 0, 10, 0), 80)
        # 20 overdue reviews → still capped at -20
        self.assertEqual(_compute_health_score(0, 0, 20, 0), 80)

    def test_single_approaching_expiry(self):
        # 1 approaching expiry → -1
        self.assertEqual(_compute_health_score(0, 0, 0, 1), 99)

    def test_approaching_expiry_cap(self):
        # 10 approaching expiry → -10 (capped at 10)
        self.assertEqual(_compute_health_score(0, 0, 0, 10), 90)
        # 20 approaching expiry → still capped at -10
        self.assertEqual(_compute_health_score(0, 0, 0, 20), 90)

    def test_all_maxed_out(self):
        # All categories at cap: -15 -20 -20 -10 = -65 → 35
        self.assertEqual(_compute_health_score(10, 10, 20, 20), 35)

    def test_floor_is_zero(self):
        # Even with extreme values, score never goes below 0
        result = _compute_health_score(100, 100, 100, 100)
        self.assertGreaterEqual(result, 0)

    def test_combined_partial(self):
        # 2 unused (−6) + 1 stale (−5) + 3 overdue (−6) + 2 expiry (−2) = −19 → 81
        self.assertEqual(_compute_health_score(2, 1, 3, 2), 81)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_memory(mem_id: str, namespace: str = "test:ns") -> MagicMock:
    m = MagicMock()
    m.id = mem_id
    m.namespace = namespace
    m.content = f"Memory {mem_id}"
    return m


def _make_mock_client(
    total: int = 10,
    unused_constraints: list | None = None,
    last_writes: dict | None = None,
    overdue: list | None = None,
    approaching_expiry: int = 0,
) -> MagicMock:
    client = MagicMock()
    client._arcadedb = MagicMock()
    client._arcadedb.count_memories = AsyncMock(return_value=total)
    client._arcadedb.get_unused_constraints = AsyncMock(
        return_value=unused_constraints or []
    )
    client._arcadedb.get_namespace_last_writes = AsyncMock(
        return_value=last_writes or {}
    )
    client._arcadedb.get_review_due = AsyncMock(return_value=overdue or [])
    client._arcadedb.count_approaching_expiry = AsyncMock(return_value=approaching_expiry)
    return client


# ---------------------------------------------------------------------------
# knowledge_health endpoint
# ---------------------------------------------------------------------------

class TestKnowledgeHealthEndpoint(unittest.IsolatedAsyncioTestCase):

    async def _call(self, client, ns="test:ns", stale_days=30):
        from engram_api.routers.knowledge import knowledge_health
        # Bypass auth dependency — pass key_entry as a simple mock
        key_entry = MagicMock()
        key_entry.namespaces = [ns]
        key_entry.access = "full"
        # Patch check_namespace_access to be a no-op
        import engram_api.routers.knowledge as kmod
        from unittest.mock import patch, AsyncMock as AM
        with patch.object(kmod, "check_namespace_access", new=AM(return_value=None)):
            return await knowledge_health(
                ns=ns,
                stale_days=stale_days,
                key_entry=key_entry,
                client=client,
            )

    async def test_perfect_health_returns_100(self):
        client = _make_mock_client(total=5)
        report = await self._call(client)
        self.assertEqual(report.health_score, 100)
        self.assertEqual(report.total_memories, 5)

    async def test_perfect_health_has_healthy_issue(self):
        client = _make_mock_client(total=5)
        report = await self._call(client)
        levels = [i.level for i in report.issues]
        self.assertIn("info", levels)
        messages = [i.message for i in report.issues]
        self.assertTrue(any("healthy" in m.lower() for m in messages))

    async def test_unused_constraints_deduct_score(self):
        constraints = [_make_mock_memory("c1"), _make_mock_memory("c2")]
        client = _make_mock_client(unused_constraints=constraints)
        report = await self._call(client)
        # 2 unused constraints → -6
        self.assertEqual(report.health_score, 94)

    async def test_unused_constraints_issue_added(self):
        constraints = [_make_mock_memory("c1")]
        client = _make_mock_client(unused_constraints=constraints)
        report = await self._call(client)
        issues = [i for i in report.issues if "constraint" in i.message.lower()]
        self.assertTrue(len(issues) > 0)
        self.assertEqual(issues[0].level, "warning")
        self.assertIn("c1", issues[0].affected_ids)

    async def test_stale_namespace_deducts_score(self):
        # Return a last_write > stale_days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        client = _make_mock_client(last_writes={"test:ns:child": old_ts})
        report = await self._call(client, stale_days=30)
        # 1 stale namespace → -5
        self.assertEqual(report.health_score, 95)

    async def test_recent_namespace_not_stale(self):
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        client = _make_mock_client(last_writes={"test:ns:child": recent_ts})
        report = await self._call(client, stale_days=30)
        self.assertEqual(report.health_score, 100)

    async def test_stale_namespace_issue_added(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        client = _make_mock_client(last_writes={"test:ns:old": old_ts})
        report = await self._call(client, stale_days=30)
        issues = [i for i in report.issues if "namespace" in i.message.lower()]
        self.assertTrue(len(issues) > 0)
        self.assertEqual(issues[0].level, "info")

    async def test_overdue_reviews_deduct_score(self):
        overdue = [_make_mock_memory("m1"), _make_mock_memory("m2"), _make_mock_memory("m3")]
        client = _make_mock_client(overdue=overdue)
        report = await self._call(client)
        # 3 overdue reviews → -6
        self.assertEqual(report.health_score, 94)

    async def test_overdue_reviews_issue_added(self):
        overdue = [_make_mock_memory("m1")]
        client = _make_mock_client(overdue=overdue)
        report = await self._call(client)
        issues = [i for i in report.issues if "review" in i.message.lower()]
        self.assertTrue(len(issues) > 0)
        self.assertEqual(issues[0].level, "warning")

    async def test_approaching_expiry_deducts_score(self):
        client = _make_mock_client(approaching_expiry=4)
        report = await self._call(client)
        # 4 approaching expiry → -4
        self.assertEqual(report.health_score, 96)

    async def test_approaching_expiry_issue_added(self):
        client = _make_mock_client(approaching_expiry=2)
        report = await self._call(client)
        issues = [i for i in report.issues if "expir" in i.message.lower()]
        self.assertTrue(len(issues) > 0)
        self.assertEqual(issues[0].level, "warning")

    async def test_combined_all_issues(self):
        constraints = [_make_mock_memory("c1")]
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        overdue = [_make_mock_memory("m1")]
        client = _make_mock_client(
            total=20,
            unused_constraints=constraints,
            last_writes={"test:ns:old": old_ts},
            overdue=overdue,
            approaching_expiry=3,
        )
        report = await self._call(client, stale_days=30)
        # -3 (1 constraint) -5 (1 stale) -2 (1 overdue) -3 (3 expiry) = -13 → 87
        self.assertEqual(report.health_score, 87)
        self.assertEqual(report.total_memories, 20)

    async def test_report_namespace_matches_input(self):
        client = _make_mock_client()
        report = await self._call(client, ns="my:custom:ns")
        self.assertEqual(report.namespace, "my:custom:ns")

    async def test_report_generated_at_is_recent(self):
        client = _make_mock_client()
        report = await self._call(client)
        age = datetime.now(timezone.utc) - report.generated_at
        self.assertLess(age.total_seconds(), 5)

    async def test_metrics_dict_populated(self):
        client = _make_mock_client(total=7, approaching_expiry=1)
        report = await self._call(client)
        self.assertIn("total_memories", report.metrics)
        self.assertIn("approaching_expiry_7d", report.metrics)
        self.assertIn("overdue_reviews", report.metrics)

    async def test_count_memories_failure_returns_minus_one(self):
        client = _make_mock_client()
        client._arcadedb.count_memories = AsyncMock(side_effect=Exception("db error"))
        report = await self._call(client)
        self.assertEqual(report.total_memories, -1)

    async def test_get_unused_constraints_failure_graceful(self):
        client = _make_mock_client()
        client._arcadedb.get_unused_constraints = AsyncMock(side_effect=Exception("db error"))
        # Should still return a report without crashing
        report = await self._call(client)
        self.assertIsInstance(report, KnowledgeHealthReport)

    async def test_stale_namespace_malformed_timestamp_skipped(self):
        client = _make_mock_client(last_writes={"test:ns:broken": "not-a-date"})
        # Should not crash — malformed timestamps are silently skipped
        report = await self._call(client)
        self.assertIsInstance(report, KnowledgeHealthReport)


if __name__ == "__main__":
    unittest.main(verbosity=2)
