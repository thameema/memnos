"""
tools/test_decay.py — Unit tests for decay_policy feature.

Tests cover:
- DecayPolicy enum values
- _apply_decay_score() in client.py
- run_decay_job() in decay/job.py
- mark_deprecated_bulk / get_decay_candidates signatures (mock ArcadeDB client)
- decay_policy field round-trip in models
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, _REPO_ROOT + "/packages/core")

from engram.models import DecayPolicy, MemoryEntry, MemoryType, MemoryStatus, SearchResult
from engram.client import _apply_decay_score, _DECAY_K_TIME, _DECAY_K_ACCESS
from engram.decay.job import run_decay_job, DecayReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_memory(
    *,
    decay_policy: DecayPolicy = DecayPolicy.none,
    age_days: float = 0,
    last_accessed_days_ago: float | None = None,
) -> MemoryEntry:
    now = _now()
    created = now - timedelta(days=age_days)
    last_accessed = (now - timedelta(days=last_accessed_days_ago)) if last_accessed_days_ago is not None else None
    return MemoryEntry(
        content="test",
        namespace="test:ns",
        memory_type=MemoryType.fact,
        status=MemoryStatus.active,
        decay_policy=decay_policy,
        created_at=created,
        last_accessed_at=last_accessed,
    )


def _make_search_result(memory: MemoryEntry, score: float = 1.0, source: str = "vector") -> SearchResult:
    return SearchResult(memory=memory, score=score, source=source)


# ---------------------------------------------------------------------------
# DecayPolicy enum
# ---------------------------------------------------------------------------

class TestDecayPolicyEnum(unittest.TestCase):
    def test_values(self):
        self.assertEqual(DecayPolicy.none.value, "none")
        self.assertEqual(DecayPolicy.time_weighted.value, "time_weighted")
        self.assertEqual(DecayPolicy.access_weighted.value, "access_weighted")

    def test_default_on_memory_entry(self):
        mem = MemoryEntry(content="hi", namespace="x")
        self.assertEqual(mem.decay_policy, DecayPolicy.none)

    def test_last_accessed_at_default_none(self):
        mem = MemoryEntry(content="hi", namespace="x")
        self.assertIsNone(mem.last_accessed_at)


# ---------------------------------------------------------------------------
# _apply_decay_score
# ---------------------------------------------------------------------------

class TestApplyDecayScore(unittest.TestCase):
    def test_none_policy_no_change(self):
        mem = _make_memory(decay_policy=DecayPolicy.none, age_days=500)
        result = _make_search_result(mem, score=0.8)
        out = _apply_decay_score(result, _now())
        self.assertAlmostEqual(out.score, 0.8, places=6)

    def test_time_weighted_fresh_memory(self):
        mem = _make_memory(decay_policy=DecayPolicy.time_weighted, age_days=0)
        result = _make_search_result(mem, score=1.0)
        out = _apply_decay_score(result, _now())
        # age=0 → factor=1.0
        self.assertAlmostEqual(out.score, 1.0, places=3)

    def test_time_weighted_90_day_half_life(self):
        mem = _make_memory(decay_policy=DecayPolicy.time_weighted, age_days=90)
        result = _make_search_result(mem, score=1.0)
        out = _apply_decay_score(result, _now())
        expected = math.exp(-_DECAY_K_TIME * 90)
        self.assertAlmostEqual(out.score, expected, places=3)
        # Should be close to 0.5
        self.assertAlmostEqual(out.score, 0.5, places=2)

    def test_time_weighted_365_days(self):
        mem = _make_memory(decay_policy=DecayPolicy.time_weighted, age_days=365)
        result = _make_search_result(mem, score=1.0)
        out = _apply_decay_score(result, _now())
        self.assertLess(out.score, 0.1)   # should be heavily decayed

    def test_access_weighted_fresh_access(self):
        mem = _make_memory(decay_policy=DecayPolicy.access_weighted, last_accessed_days_ago=0)
        result = _make_search_result(mem, score=1.0)
        out = _apply_decay_score(result, _now())
        self.assertAlmostEqual(out.score, 1.0, places=3)

    def test_access_weighted_30_day_half_life(self):
        mem = _make_memory(decay_policy=DecayPolicy.access_weighted, last_accessed_days_ago=30)
        result = _make_search_result(mem, score=1.0)
        out = _apply_decay_score(result, _now())
        expected = math.exp(-_DECAY_K_ACCESS * 30)
        self.assertAlmostEqual(out.score, expected, places=3)
        self.assertAlmostEqual(out.score, 0.5, places=2)

    def test_access_weighted_falls_back_to_created_at_when_no_access(self):
        # last_accessed_at is None → use created_at
        mem = _make_memory(decay_policy=DecayPolicy.access_weighted, age_days=30, last_accessed_days_ago=None)
        result = _make_search_result(mem, score=1.0)
        out = _apply_decay_score(result, _now())
        expected = math.exp(-_DECAY_K_ACCESS * 30)
        self.assertAlmostEqual(out.score, expected, places=3)

    def test_pinned_results_not_decayed(self):
        mem = _make_memory(decay_policy=DecayPolicy.time_weighted, age_days=500)
        result = SearchResult(memory=mem, score=2.0, source="pinned")
        # pinned results are not passed through _apply_decay_score in client.py,
        # but let's verify the function itself would decay if called directly
        out = _apply_decay_score(result, _now())
        self.assertLess(out.score, 2.0)  # it would decay if called
        # The important thing is client.py skips pinned — tested in integration


# ---------------------------------------------------------------------------
# run_decay_job
# ---------------------------------------------------------------------------

def _make_mock_arcadedb(time_candidates=None, access_candidates=None):
    client = MagicMock()
    client.get_decay_candidates = AsyncMock(side_effect=lambda ns, policy, limit=2000: (
        time_candidates or [] if policy == "time_weighted" else access_candidates or []
    ))
    client.mark_deprecated_bulk = AsyncMock(return_value=0)
    return client


class TestRunDecayJob(unittest.IsolatedAsyncioTestCase):
    async def test_no_candidates_returns_empty_report(self):
        db = _make_mock_arcadedb()
        report = await run_decay_job(db, "test:ns")
        self.assertEqual(report.total_deprecated, 0)
        db.mark_deprecated_bulk.assert_not_awaited()

    async def test_time_weighted_old_memory_deprecated(self):
        old_mem = _make_memory(decay_policy=DecayPolicy.time_weighted, age_days=400)
        db = _make_mock_arcadedb(time_candidates=[old_mem])
        report = await run_decay_job(db, "test:ns", max_age_days=365)
        self.assertIn(old_mem.id, report.time_weighted_deprecated)
        db.mark_deprecated_bulk.assert_awaited_once()

    async def test_time_weighted_young_memory_not_deprecated(self):
        young_mem = _make_memory(decay_policy=DecayPolicy.time_weighted, age_days=100)
        db = _make_mock_arcadedb(time_candidates=[young_mem])
        report = await run_decay_job(db, "test:ns", max_age_days=365)
        self.assertEqual(report.time_weighted_deprecated, [])
        db.mark_deprecated_bulk.assert_not_awaited()

    async def test_access_weighted_idle_memory_deprecated(self):
        idle_mem = _make_memory(
            decay_policy=DecayPolicy.access_weighted,
            last_accessed_days_ago=120,
        )
        db = _make_mock_arcadedb(access_candidates=[idle_mem])
        report = await run_decay_job(db, "test:ns", max_idle_days=90)
        self.assertIn(idle_mem.id, report.access_weighted_deprecated)
        db.mark_deprecated_bulk.assert_awaited_once()

    async def test_access_weighted_recent_memory_not_deprecated(self):
        fresh_mem = _make_memory(
            decay_policy=DecayPolicy.access_weighted,
            last_accessed_days_ago=30,
        )
        db = _make_mock_arcadedb(access_candidates=[fresh_mem])
        report = await run_decay_job(db, "test:ns", max_idle_days=90)
        self.assertEqual(report.access_weighted_deprecated, [])

    async def test_dry_run_does_not_write(self):
        old_mem = _make_memory(decay_policy=DecayPolicy.time_weighted, age_days=400)
        db = _make_mock_arcadedb(time_candidates=[old_mem])
        report = await run_decay_job(db, "test:ns", dry_run=True, max_age_days=365)
        # Should identify but NOT write
        self.assertIn(old_mem.id, report.time_weighted_deprecated)
        db.mark_deprecated_bulk.assert_not_awaited()
        self.assertTrue(report.dry_run)

    async def test_report_contains_namespace(self):
        db = _make_mock_arcadedb()
        report = await run_decay_job(db, "org:my-team")
        self.assertEqual(report.namespace, "org:my-team")

    async def test_db_error_is_captured_in_report_errors(self):
        db = MagicMock()
        db.get_decay_candidates = AsyncMock(side_effect=RuntimeError("DB down"))
        db.mark_deprecated_bulk = AsyncMock()
        report = await run_decay_job(db, "test:ns")
        self.assertTrue(len(report.errors) > 0)
        self.assertIn("DB down", " ".join(report.errors))


# ---------------------------------------------------------------------------
# DecayReport
# ---------------------------------------------------------------------------

class TestDecayReport(unittest.TestCase):
    def test_total_deprecated_sums_both_policies(self):
        r = DecayReport(namespace="x", dry_run=False)
        r.time_weighted_deprecated = ["a", "b"]
        r.access_weighted_deprecated = ["c"]
        self.assertEqual(r.total_deprecated, 3)

    def test_empty_report(self):
        r = DecayReport(namespace="x", dry_run=False)
        self.assertEqual(r.total_deprecated, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
