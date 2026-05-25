"""
tools/test_learning_full.py — Unit tests for the learning package.

Covers:
- EpisodeStore: init, save, get, get_by_task_id, get_recent, update_outcome, get_active_namespaces
- HeuristicStore: init, add, get_all, search, update_confidence, increment_triggered, delete
- SkillStore: init, add, get_all, find_match, increment_use, delete
- QualityStore: init, update (new + existing), get, get_best_agent
- FeedbackService: detect_correction, record_explicit (positive/negative), record_correction
- SkillExtractor: maybe_extract (skip conditions, new template, extract=false, API error)
- ReflectionService: run (too few failures, LLM success, JSON error, API error, ArcadeDB sync)
- HeuristicDecayService: run (no heuristics, decays, deletes below threshold)
- LearningScheduler: start (apscheduler present/missing), stop, multi-namespace reflection
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, _REPO_ROOT + "/packages/learning")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> Path:
    """Return a temporary SQLite path inside a TemporaryDirectory (caller manages cleanup)."""
    d = tempfile.mkdtemp()
    return Path(d) / "test.db"


# ---------------------------------------------------------------------------
# EpisodeStore
# ---------------------------------------------------------------------------

class TestEpisodeStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.episode_store import EpisodeStore
        self.db_path = _tmp_db()
        self.store = EpisodeStore(self.db_path)
        await self.store.init()

    async def _ep(self, **kw):
        from engram_learning.models import EpisodicRecord, Outcome
        defaults = dict(
            task_id="t1", namespace="ns1", original_prompt="do the thing",
            decomposition=["step1"], agent_used="alpha", runtime="api",
            outcome=Outcome.SUCCESS, quality_score=0.9,
        )
        defaults.update(kw)
        return EpisodicRecord(**defaults)

    async def test_save_and_get(self):
        ep = await self._ep()
        await self.store.save(ep)
        fetched = await self.store.get(ep.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, ep.id)
        self.assertEqual(fetched.task_id, "t1")

    async def test_get_missing_returns_none(self):
        result = await self.store.get("nonexistent")
        self.assertIsNone(result)

    async def test_get_by_task_id(self):
        ep = await self._ep(task_id="task-xyz")
        await self.store.save(ep)
        found = await self.store.get_by_task_id("task-xyz")
        self.assertEqual(found.id, ep.id)

    async def test_get_by_task_id_missing(self):
        result = await self.store.get_by_task_id("no-such-task")
        self.assertIsNone(result)

    async def test_get_recent(self):
        from engram_learning.models import EpisodicRecord, Outcome
        ep = EpisodicRecord(namespace="ns1", original_prompt="recent task",
                            outcome=Outcome.SUCCESS, created_at=datetime.utcnow())
        await self.store.save(ep)
        results = await self.store.get_recent("ns1", days=7)
        self.assertTrue(any(r.id == ep.id for r in results))

    async def test_get_recent_excludes_old(self):
        from engram_learning.models import EpisodicRecord, Outcome
        ep = EpisodicRecord(namespace="ns1", original_prompt="old task",
                            outcome=Outcome.FAILURE,
                            created_at=datetime.utcnow() - timedelta(days=30))
        await self.store.save(ep)
        results = await self.store.get_recent("ns1", days=7)
        self.assertFalse(any(r.id == ep.id for r in results))

    async def test_update_outcome(self):
        from engram_learning.models import Outcome
        ep = await self._ep()
        await self.store.save(ep)
        await self.store.update_outcome(ep.id, Outcome.CORRECTED, "wrong answer", 0.2)
        fetched = await self.store.get(ep.id)
        self.assertEqual(fetched.outcome.value, "CORRECTED")
        self.assertAlmostEqual(fetched.quality_score, 0.2)
        self.assertEqual(fetched.user_feedback, "wrong answer")

    async def test_get_active_namespaces(self):
        ep = await self._ep(namespace="active_ns")
        await self.store.save(ep)
        ns_list = await self.store.get_active_namespaces(days=7)
        self.assertIn("active_ns", ns_list)

    async def test_get_active_namespaces_empty_when_old(self):
        from engram_learning.models import EpisodicRecord, Outcome
        ep = EpisodicRecord(namespace="stale_ns", original_prompt="old",
                            outcome=Outcome.SUCCESS,
                            created_at=datetime.utcnow() - timedelta(days=20))
        await self.store.save(ep)
        ns_list = await self.store.get_active_namespaces(days=7)
        self.assertNotIn("stale_ns", ns_list)


# ---------------------------------------------------------------------------
# HeuristicStore
# ---------------------------------------------------------------------------

class TestHeuristicStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.heuristic_store import HeuristicStore
        self.db_path = _tmp_db()
        self.store = HeuristicStore(self.db_path)
        await self.store.init()

    def _h(self, namespace="ns1", rule="always validate input", confidence=0.8, tags=None):
        from engram_learning.models import Heuristic
        return Heuristic(namespace=namespace, rule=rule, confidence=confidence,
                         applies_to_tags=tags or [])

    async def test_add_and_get_all(self):
        h = self._h()
        await self.store.add(h)
        all_h = await self.store.get_all("ns1")
        self.assertEqual(len(all_h), 1)
        self.assertEqual(all_h[0].rule, "always validate input")

    async def test_get_all_empty_namespace(self):
        result = await self.store.get_all("no_such_ns")
        self.assertEqual(result, [])

    async def test_search_with_matching_tags(self):
        h = self._h(tags=["memory", "search"])
        await self.store.add(h)
        results = await self.store.search("ns1", query_tags=["memory"])
        self.assertTrue(any(r.id == h.id for r in results))

    async def test_search_no_tags_returns_all(self):
        h1 = self._h(rule="rule A")
        h2 = self._h(rule="rule B")
        await self.store.add(h1)
        await self.store.add(h2)
        results = await self.store.search("ns1")
        self.assertEqual(len(results), 2)

    async def test_update_confidence_clamps_to_1(self):
        h = self._h(confidence=0.9)
        await self.store.add(h)
        await self.store.update_confidence(h.id, 0.5)
        all_h = await self.store.get_all("ns1")
        self.assertLessEqual(all_h[0].confidence, 1.0)

    async def test_update_confidence_clamps_to_0(self):
        h = self._h(confidence=0.05)
        await self.store.add(h)
        await self.store.update_confidence(h.id, -0.5)
        all_h = await self.store.get_all("ns1")
        self.assertGreaterEqual(all_h[0].confidence, 0.0)

    async def test_increment_triggered_updates_count(self):
        h = self._h()
        await self.store.add(h)
        await self.store.increment_triggered(h.id)
        all_h = await self.store.get_all("ns1")
        self.assertEqual(all_h[0].triggered_count, 1)
        self.assertIsNotNone(all_h[0].last_triggered_at)

    async def test_delete(self):
        h = self._h()
        await self.store.add(h)
        await self.store.delete(h.id)
        all_h = await self.store.get_all("ns1")
        self.assertEqual(all_h, [])

    async def test_get_by_tags(self):
        h = self._h(tags=["code"])
        await self.store.add(h)
        results = await self.store.get_by_tags("ns1", ["code"])
        self.assertTrue(any(r.id == h.id for r in results))


# ---------------------------------------------------------------------------
# SkillStore
# ---------------------------------------------------------------------------

class TestSkillStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.skill_store import SkillStore
        self.db_path = _tmp_db()
        self.store = SkillStore(self.db_path)
        await self.store.init()

    def _t(self, name="deploy-service", namespace="ns1", patterns=None, steps=None):
        from engram_learning.models import SkillTemplate
        return SkillTemplate(
            name=name, namespace=namespace,
            description="Deploy a service to k8s",
            trigger_patterns=patterns or ["deploy", "k8s", "kubernetes"],
            steps=steps or ["1. build image", "2. push", "3. apply manifest"],
        )

    async def test_add_and_get_all(self):
        t = self._t()
        await self.store.add(t)
        all_t = await self.store.get_all("ns1")
        self.assertEqual(len(all_t), 1)
        self.assertEqual(all_t[0].name, "deploy-service")

    async def test_get_all_empty(self):
        result = await self.store.get_all("empty_ns")
        self.assertEqual(result, [])

    async def test_find_match_success(self):
        t = self._t(patterns=["deploy", "kubernetes"])
        await self.store.add(t)
        result = await self.store.find_match("deploy to kubernetes cluster", "ns1")
        self.assertIsNotNone(result)
        self.assertEqual(result.id, t.id)

    async def test_find_match_below_threshold(self):
        t = self._t(patterns=["deploy", "kubernetes", "helm", "production"])
        await self.store.add(t)
        result = await self.store.find_match("random unrelated task", "ns1")
        self.assertIsNone(result)

    async def test_find_match_no_patterns(self):
        from engram_learning.models import SkillTemplate
        t = SkillTemplate(namespace="ns1", name="empty", trigger_patterns=[], steps=[])
        await self.store.add(t)
        result = await self.store.find_match("anything", "ns1")
        self.assertIsNone(result)

    async def test_increment_use_success(self):
        t = self._t()
        await self.store.add(t)
        await self.store.increment_use(t.id, success=True)
        all_t = await self.store.get_all("ns1")
        self.assertEqual(all_t[0].use_count, 1)

    async def test_increment_use_failure_lowers_success_rate(self):
        from engram_learning.models import SkillTemplate
        t = SkillTemplate(namespace="ns1", name="t", trigger_patterns=["x"], steps=[], success_rate=1.0)
        await self.store.add(t)
        # Prime use_count=1 with a success so the failure formula produces a non-zero result
        await self.store.increment_use(t.id, success=True)
        await self.store.increment_use(t.id, success=False)
        all_t = await self.store.get_all("ns1")
        self.assertLess(all_t[0].success_rate, 1.0)

    async def test_delete(self):
        t = self._t()
        await self.store.add(t)
        await self.store.delete(t.id)
        result = await self.store.get_all("ns1")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# QualityStore
# ---------------------------------------------------------------------------

class TestQualityStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.quality_store import QualityStore
        self.db_path = _tmp_db()
        self.store = QualityStore(self.db_path)
        await self.store.init()

    async def test_update_creates_new_record(self):
        await self.store.update("agent-a", "code", "ns1", 0.9, 1.5, True)
        records = await self.store.get("code", "ns1")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].agent_name, "agent-a")
        self.assertAlmostEqual(records[0].avg_quality_score, 0.9)
        self.assertEqual(records[0].sample_count, 1)

    async def test_update_averages_existing_record(self):
        await self.store.update("agent-a", "code", "ns1", 0.8, 2.0, True)
        await self.store.update("agent-a", "code", "ns1", 0.6, 1.0, True)
        records = await self.store.get("code", "ns1")
        self.assertAlmostEqual(records[0].avg_quality_score, 0.7)
        self.assertEqual(records[0].sample_count, 2)

    async def test_update_failure_increases_failure_rate(self):
        await self.store.update("agent-a", "code", "ns1", 0.0, 1.0, False)
        records = await self.store.get("code", "ns1")
        self.assertAlmostEqual(records[0].failure_rate, 1.0)

    async def test_update_mixed_failure_rate(self):
        await self.store.update("agent-a", "code", "ns1", 0.9, 1.0, True)
        await self.store.update("agent-a", "code", "ns1", 0.0, 1.0, False)
        records = await self.store.get("code", "ns1")
        self.assertAlmostEqual(records[0].failure_rate, 0.5)

    async def test_get_returns_empty_list_for_unknown_tag(self):
        result = await self.store.get("unknown_tag", "ns1")
        self.assertEqual(result, [])

    async def test_get_best_agent_returns_none_if_insufficient_samples(self):
        await self.store.update("agent-a", "code", "ns1", 0.9, 1.0, True)
        result = await self.store.get_best_agent("code", "ns1", min_samples=10)
        self.assertIsNone(result)

    async def test_get_best_agent_returns_best(self):
        for _ in range(10):
            await self.store.update("agent-good", "code", "ns1", 0.9, 1.0, True)
            await self.store.update("agent-bad", "code", "ns1", 0.3, 3.0, False)
        result = await self.store.get_best_agent("code", "ns1", min_samples=10)
        self.assertEqual(result, "agent-good")

    async def test_get_best_agent_none_if_score_too_low(self):
        for _ in range(10):
            await self.store.update("agent-a", "code", "ns1", 0.5, 1.0, False)
        result = await self.store.get_best_agent("code", "ns1", min_samples=10)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# FeedbackService
# ---------------------------------------------------------------------------

class TestDetectCorrection(unittest.TestCase):
    def _call(self, text):
        from engram_learning.feedback import detect_correction
        return detect_correction(text)

    def test_no_returns_true(self):
        self.assertTrue(self._call("No, that's wrong"))

    def test_actually_returns_true(self):
        self.assertTrue(self._call("Actually, it should be reversed"))

    def test_that_is_wrong_returns_true(self):
        self.assertTrue(self._call("that's incorrect"))

    def test_plain_positive_returns_false(self):
        self.assertFalse(self._call("Looks good, thanks"))

    def test_case_insensitive(self):
        self.assertTrue(self._call("NOPE that was bad"))


class TestFeedbackService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.quality_store import QualityStore
        from engram_learning.feedback import FeedbackService
        self.db_path = _tmp_db()
        self.episodes = EpisodeStore(self.db_path)
        self.quality = QualityStore(self.db_path)
        await self.episodes.init()
        await self.quality.init()
        self.svc = FeedbackService(self.episodes, self.quality)

    async def _save_episode(self, task_id="t1"):
        from engram_learning.models import EpisodicRecord, Outcome
        ep = EpisodicRecord(task_id=task_id, namespace="ns1",
                            original_prompt="do x", agent_used="agent-a",
                            runtime="api", outcome=Outcome.SUCCESS,
                            duration_s=1.0, tags=["code"])
        await self.episodes.save(ep)
        return ep

    async def test_record_explicit_positive(self):
        from engram_learning.models import Outcome
        ep = await self._save_episode()
        await self.svc.record_explicit(ep.task_id, "positive")
        fetched = await self.episodes.get(ep.id)
        self.assertEqual(fetched.outcome, Outcome.SUCCESS)
        self.assertAlmostEqual(fetched.quality_score, 1.0)

    async def test_record_explicit_negative_sets_failure(self):
        from engram_learning.models import Outcome
        ep = await self._save_episode()
        await self.svc.record_explicit(ep.task_id, "negative")
        fetched = await self.episodes.get(ep.id)
        self.assertEqual(fetched.outcome, Outcome.FAILURE)

    async def test_record_explicit_negative_with_comment_corrected(self):
        from engram_learning.models import Outcome
        ep = await self._save_episode()
        await self.svc.record_explicit(ep.task_id, "negative", comment="wrong approach")
        fetched = await self.episodes.get(ep.id)
        self.assertEqual(fetched.outcome, Outcome.CORRECTED)

    async def test_record_explicit_missing_episode_is_noop(self):
        await self.svc.record_explicit("no-such-task", "positive")

    async def test_record_correction(self):
        from engram_learning.models import Outcome
        ep = await self._save_episode()
        await self.svc.record_correction(ep.task_id, "you forgot step 3")
        fetched = await self.episodes.get(ep.id)
        self.assertEqual(fetched.outcome, Outcome.CORRECTED)
        self.assertEqual(fetched.user_feedback, "you forgot step 3")

    async def test_record_correction_missing_episode_is_noop(self):
        await self.svc.record_correction("no-such-task", "correction")

    async def test_record_explicit_triggers_reflection(self):
        ep = await self._save_episode()
        reflection = AsyncMock()
        from engram_learning.feedback import FeedbackService
        svc = FeedbackService(self.episodes, self.quality, reflection_service=reflection)
        await svc.record_explicit(ep.task_id, "negative")
        reflection.run.assert_awaited_once()

    async def test_record_correction_triggers_reflection(self):
        ep = await self._save_episode()
        reflection = AsyncMock()
        from engram_learning.feedback import FeedbackService
        svc = FeedbackService(self.episodes, self.quality, reflection_service=reflection)
        await svc.record_correction(ep.task_id, "bad")
        reflection.run.assert_awaited_once()


# ---------------------------------------------------------------------------
# SkillExtractor
# ---------------------------------------------------------------------------

class TestSkillExtractor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.skill_store import SkillStore
        from engram_learning.extractor import SkillExtractor
        self.db_path = _tmp_db()
        self.skill_store = SkillStore(self.db_path)
        await self.skill_store.init()
        self.extractor = SkillExtractor(
            api_key="test-key", model="claude-test",
            skill_store=self.skill_store,
        )

    async def _episode(self, quality=0.9, outcome=None):
        from engram_learning.models import EpisodicRecord, Outcome
        return EpisodicRecord(
            namespace="ns1",
            original_prompt="deploy service to kubernetes",
            decomposition=["build image", "push", "apply manifest"],
            outcome=outcome or Outcome.SUCCESS,
            quality_score=quality,
        )

    async def test_skip_low_quality(self):
        ep = await self._episode(quality=0.5)
        await self.extractor.maybe_extract(ep)
        templates = await self.skill_store.get_all("ns1")
        self.assertEqual(templates, [])

    async def test_skip_non_success_outcome(self):
        from engram_learning.models import Outcome
        ep = await self._episode(outcome=Outcome.FAILURE)
        await self.extractor.maybe_extract(ep)
        templates = await self.skill_store.get_all("ns1")
        self.assertEqual(templates, [])

    async def test_skip_when_existing_match(self):
        from engram_learning.models import SkillTemplate
        existing = SkillTemplate(
            namespace="ns1", name="deploy-k8s",
            trigger_patterns=["deploy", "kubernetes"],
            steps=["build", "push"],
        )
        await self.skill_store.add(existing)
        ep = await self._episode()
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            await self.extractor.maybe_extract(ep)
            mock_cls.return_value.messages.create.assert_not_called()
        all_t = await self.skill_store.get_all("ns1")
        self.assertEqual(len(all_t), 1)
        self.assertEqual(all_t[0].use_count, 1)

    async def test_extracts_new_template_on_success(self):
        ep = await self._episode()
        llm_response = MagicMock()
        llm_response.content = [MagicMock(text='{"extract": true, "description": "Deploy k8s service", "trigger_patterns": ["deploy", "k8s"], "steps": ["1. build", "2. push"]}')]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=llm_response)
            self.extractor._client = mock_cls.return_value
            await self.extractor.maybe_extract(ep)
        templates = await self.skill_store.get_all("ns1")
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0].description, "Deploy k8s service")

    async def test_no_template_when_extract_false(self):
        ep = await self._episode()
        llm_response = MagicMock()
        llm_response.content = [MagicMock(text='{"extract": false}')]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=llm_response)
            self.extractor._client = mock_cls.return_value
            await self.extractor.maybe_extract(ep)
        templates = await self.skill_store.get_all("ns1")
        self.assertEqual(templates, [])

    async def test_api_error_is_swallowed(self):
        ep = await self._episode()
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(side_effect=Exception("API down"))
            self.extractor._client = mock_cls.return_value
            await self.extractor.maybe_extract(ep)
        templates = await self.skill_store.get_all("ns1")
        self.assertEqual(templates, [])

    async def test_syncs_to_arcadedb_when_client_provided(self):
        ep = await self._episode()
        llm_response = MagicMock()
        llm_response.content = [MagicMock(text='{"extract": true, "description": "Do a thing", "trigger_patterns": ["do"], "steps": ["1. step"]}')]
        engram_client = AsyncMock()
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=llm_response)
            from engram_learning.skill_store import SkillStore
            from engram_learning.extractor import SkillExtractor
            extractor = SkillExtractor("k", "m", self.skill_store, engram_client)
            extractor._client = mock_cls.return_value
            await extractor.maybe_extract(ep)
        engram_client.add.assert_awaited_once()


# ---------------------------------------------------------------------------
# ReflectionService
# ---------------------------------------------------------------------------

class TestReflectionService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.reflection import ReflectionService
        self.db_path = _tmp_db()
        self.episodes = EpisodeStore(self.db_path)
        self.heuristics = HeuristicStore(self.db_path)
        await self.episodes.init()
        await self.heuristics.init()
        self.svc = ReflectionService(
            api_key="test-key", model="claude-test",
            episode_store=self.episodes,
            heuristic_store=self.heuristics,
            namespace="ns1",
        )

    async def _add_failures(self, n: int):
        from engram_learning.models import EpisodicRecord, Outcome
        for i in range(n):
            ep = EpisodicRecord(
                namespace="ns1",
                original_prompt=f"task {i}",
                outcome=Outcome.FAILURE,
            )
            await self.episodes.save(ep)

    async def test_skips_when_fewer_than_2_failures(self):
        await self._add_failures(1)
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            await self.svc.run()
            mock_cls.return_value.messages.create.assert_not_called()

    async def test_creates_heuristics_from_llm(self):
        await self._add_failures(3)
        llm_response = MagicMock()
        llm_response.content = [MagicMock(text="""
{
  "new_heuristics": [
    {"rule": "validate before acting", "rationale": "failure X", "applies_to_tags": ["code"], "confidence": 0.85}
  ],
  "update_heuristics": [],
  "delete_heuristic_ids": []
}""")]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=llm_response)
            self.svc._client = mock_cls.return_value
            await self.svc.run()
        all_h = await self.heuristics.get_all("ns1")
        self.assertEqual(len(all_h), 1)
        self.assertEqual(all_h[0].rule, "validate before acting")

    async def test_json_decode_error_is_handled(self):
        await self._add_failures(3)
        llm_response = MagicMock()
        llm_response.content = [MagicMock(text="not valid json")]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=llm_response)
            self.svc._client = mock_cls.return_value
            await self.svc.run()
        all_h = await self.heuristics.get_all("ns1")
        self.assertEqual(all_h, [])

    async def test_api_error_is_handled(self):
        await self._add_failures(3)
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(side_effect=Exception("network err"))
            self.svc._client = mock_cls.return_value
            await self.svc.run()
        all_h = await self.heuristics.get_all("ns1")
        self.assertEqual(all_h, [])

    async def test_deletes_heuristics(self):
        from engram_learning.models import Heuristic
        h = Heuristic(namespace="ns1", rule="old rule")
        await self.heuristics.add(h)
        await self._add_failures(3)
        llm_response = MagicMock()
        llm_response.content = [MagicMock(text=f'{{"new_heuristics": [], "update_heuristics": [], "delete_heuristic_ids": ["{h.id}"]}}')]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=llm_response)
            self.svc._client = mock_cls.return_value
            await self.svc.run()
        all_h = await self.heuristics.get_all("ns1")
        self.assertEqual(all_h, [])

    async def test_updates_confidence(self):
        from engram_learning.models import Heuristic
        h = Heuristic(namespace="ns1", rule="rule A", confidence=0.8)
        await self.heuristics.add(h)
        await self._add_failures(3)
        llm_response = MagicMock()
        llm_response.content = [MagicMock(text=f'{{"new_heuristics": [], "update_heuristics": [{{"id": "{h.id}", "confidence_delta": 0.1, "reason": "confirmed"}}], "delete_heuristic_ids": []}}')]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=llm_response)
            self.svc._client = mock_cls.return_value
            await self.svc.run()
        all_h = await self.heuristics.get_all("ns1")
        self.assertAlmostEqual(all_h[0].confidence, 0.9, places=5)

    async def test_syncs_to_arcadedb_when_client_provided(self):
        await self._add_failures(3)
        llm_response = MagicMock()
        llm_response.content = [MagicMock(text='{"new_heuristics": [{"rule": "be careful", "rationale": "x", "applies_to_tags": [], "confidence": 0.8}], "update_heuristics": [], "delete_heuristic_ids": []}')]
        engram_client = AsyncMock()
        from engram_learning.reflection import ReflectionService
        svc = ReflectionService("k", "m", self.episodes, self.heuristics, "ns1", engram_client=engram_client)
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=llm_response)
            svc._client = mock_cls.return_value
            await svc.run()
        engram_client.add.assert_awaited_once()


# ---------------------------------------------------------------------------
# HeuristicDecayService
# ---------------------------------------------------------------------------

class TestHeuristicDecayService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.decay import HeuristicDecayService
        self.db_path = _tmp_db()
        self.store = HeuristicStore(self.db_path)
        await self.store.init()
        self.svc = HeuristicDecayService(self.store, inactive_days=30, decay_rate=0.9)

    async def test_no_op_when_no_heuristics(self):
        await self.svc.run("ns1")
        all_h = await self.store.get_all("ns1")
        self.assertEqual(all_h, [])

    async def test_recent_heuristic_not_decayed(self):
        from engram_learning.models import Heuristic
        h = Heuristic(namespace="ns1", rule="fresh rule", confidence=0.8,
                      last_triggered_at=datetime.utcnow())
        await self.store.add(h)
        await self.svc.run("ns1")
        all_h = await self.store.get_all("ns1")
        self.assertAlmostEqual(all_h[0].confidence, 0.8)

    async def test_stale_heuristic_is_decayed(self):
        from engram_learning.models import Heuristic
        h = Heuristic(namespace="ns1", rule="stale rule", confidence=0.8,
                      last_triggered_at=datetime.utcnow() - timedelta(days=60))
        await self.store.add(h)
        await self.svc.run("ns1")
        all_h = await self.store.get_all("ns1")
        self.assertAlmostEqual(all_h[0].confidence, 0.72, places=4)

    async def test_very_stale_heuristic_is_deleted(self):
        from engram_learning.models import Heuristic
        h = Heuristic(namespace="ns1", rule="dying rule", confidence=0.09,
                      last_triggered_at=datetime.utcnow() - timedelta(days=60))
        await self.store.add(h)
        await self.svc.run("ns1")
        all_h = await self.store.get_all("ns1")
        self.assertEqual(all_h, [])


# ---------------------------------------------------------------------------
# LearningScheduler
# ---------------------------------------------------------------------------

class TestLearningScheduler(unittest.TestCase):
    def _make_svc(self, reflection=None, decay=None, episode_store=None):
        from engram_learning.scheduler import LearningScheduler
        cfg = MagicMock()
        cfg.learning.reflection.schedule = "0 2 * * *"
        cfg.learning.reflection.lookback_days = 7
        cfg.learning.heuristic_decay.schedule = "0 3 * * 0"
        return LearningScheduler(
            config=cfg,
            reflection_service=reflection or AsyncMock(),
            decay_service=decay or AsyncMock(),
            namespace="ns1",
            episode_store=episode_store,
        )

    def test_stop_with_no_scheduler_is_noop(self):
        svc = self._make_svc()
        svc.stop()

    def test_start_logs_warning_when_apscheduler_missing(self):
        svc = self._make_svc()
        with patch.dict("sys.modules", {"apscheduler": None, "apscheduler.schedulers.asyncio": None, "apscheduler.triggers.cron": None}):
            with patch("builtins.__import__", side_effect=ImportError("no apscheduler")):
                svc.start()
        self.assertIsNone(svc._scheduler)

    def test_start_with_apscheduler(self):
        mock_scheduler = MagicMock()
        mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
        mock_trigger = MagicMock()

        import types
        fake_apscheduler = types.ModuleType("apscheduler")
        fake_async_mod = types.ModuleType("apscheduler.schedulers.asyncio")
        fake_async_mod.AsyncIOScheduler = mock_scheduler_cls
        fake_cron_mod = types.ModuleType("apscheduler.triggers.cron")
        fake_cron_mod.CronTrigger = MagicMock(return_value=mock_trigger)

        svc = self._make_svc()
        with patch.dict("sys.modules", {
            "apscheduler": fake_apscheduler,
            "apscheduler.schedulers": types.ModuleType("apscheduler.schedulers"),
            "apscheduler.schedulers.asyncio": fake_async_mod,
            "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
            "apscheduler.triggers.cron": fake_cron_mod,
        }):
            svc.start()
        mock_scheduler.start.assert_called_once()
        self.assertEqual(mock_scheduler.add_job.call_count, 2)


class TestSchedulerRunReflection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.scheduler import LearningScheduler
        self.db_path = _tmp_db()
        self.episodes = EpisodeStore(self.db_path)
        await self.episodes.init()

        cfg = MagicMock()
        cfg.learning.reflection.schedule = "0 2 * * *"
        cfg.learning.reflection.lookback_days = 7
        cfg.learning.heuristic_decay.schedule = "0 3 * * 0"

        self.reflection = AsyncMock()
        self.reflection.namespace = "ns1"
        self.decay = AsyncMock()

        self.svc = LearningScheduler(
            config=cfg,
            reflection_service=self.reflection,
            decay_service=self.decay,
            namespace="ns1",
            episode_store=self.episodes,
        )

    async def test_run_reflection_calls_reflection_service(self):
        await self.svc._run_reflection()
        self.reflection.run.assert_awaited_once()

    async def test_run_reflection_additional_namespaces(self):
        from engram_learning.models import EpisodicRecord, Outcome
        ep = EpisodicRecord(namespace="ns2", original_prompt="x", outcome=Outcome.SUCCESS)
        await self.episodes.save(ep)

        factory_svc = AsyncMock()
        factory_svc.namespace = "ns2"
        factory = MagicMock(return_value=factory_svc)
        self.svc._reflection_factory = factory

        await self.svc._run_reflection()
        factory.assert_called_once_with("ns2")
        factory_svc.run.assert_awaited_once()

    async def test_run_decay_calls_decay_service(self):
        await self.svc._run_decay()
        self.decay.run.assert_awaited_once_with("ns1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
