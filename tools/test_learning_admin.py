"""
tools/test_learning_admin.py — Unit tests for learning admin API.

Tests cover:
- learning_stats: heuristic count, episode count, quality, success rate
- list_heuristics: returns sorted by confidence, respects limit
- delete_heuristic: calls store.delete
- recent_episodes: returns sorted by created_at desc
- trigger_reflection: 503 when API key missing
- graceful degradation when engram_learning not installed
- _require_learning raises 503 when store unavailable
"""
from __future__ import annotations

import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, _REPO_ROOT + "/packages/api")
sys.path.insert(0, _REPO_ROOT + "/packages/core")

from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Stubs for engram_learning models (not installed in test env)
# ---------------------------------------------------------------------------

class _Outcome:
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CORRECTED = "CORRECTED"

    def __init__(self, val="SUCCESS"):
        self.value = val

    def __str__(self):
        return f"Outcome.{self.value}"


class _Heuristic:
    def __init__(self, id, namespace, rule, rationale="", confidence=0.8,
                 triggered_count=0, overridden_count=0, applies_to_tags=None,
                 created_at=None, last_triggered_at=None, source_episode_id=""):
        self.id = id
        self.namespace = namespace
        self.rule = rule
        self.rationale = rationale
        self.confidence = confidence
        self.triggered_count = triggered_count
        self.overridden_count = overridden_count
        self.applies_to_tags = applies_to_tags or []
        self.created_at = created_at or datetime.now(timezone.utc)
        self.last_triggered_at = last_triggered_at
        self.source_episode_id = source_episode_id


class _Episode:
    def __init__(self, id, namespace, original_prompt, outcome="SUCCESS",
                 agent_used=None, quality_score=None, duration_s=1.0,
                 token_cost=100, created_at=None):
        self.id = id
        self.namespace = namespace
        self.original_prompt = original_prompt
        self.outcome = _Outcome(outcome)
        self.agent_used = agent_used
        self.quality_score = quality_score
        self.duration_s = duration_s
        self.token_cost = token_cost
        self.created_at = created_at or datetime.now(timezone.utc)


def _make_mock_heuristic_store(heuristics=None):
    store = MagicMock()
    store.init = AsyncMock()
    store.get_all = AsyncMock(return_value=heuristics or [])
    store.delete = AsyncMock()
    return store


def _make_mock_episode_store(episodes=None):
    store = MagicMock()
    store.init = AsyncMock()
    store.get_recent = AsyncMock(return_value=episodes or [])
    return store


# ---------------------------------------------------------------------------
# _require_learning
# ---------------------------------------------------------------------------

class TestRequireLearning(unittest.TestCase):
    def test_raises_503_when_import_fails(self):
        import engram_api.routers.learning_admin as la
        with patch.object(la, "_get_heuristic_store", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                la._require_learning()
            self.assertEqual(ctx.exception.status_code, 503)


# ---------------------------------------------------------------------------
# learning_stats
# ---------------------------------------------------------------------------

class TestLearningStats(unittest.IsolatedAsyncioTestCase):
    async def _call(self, ns="test:ns", h_store=None, e_store=None):
        import engram_api.routers.learning_admin as la
        from unittest.mock import patch as _patch

        h = h_store or _make_mock_heuristic_store()
        e = e_store or _make_mock_episode_store()

        key_entry = MagicMock()
        with _patch.object(la, "_get_heuristic_store", return_value=h), \
             _patch.object(la, "_get_episode_store", return_value=e), \
             _patch("engram_api.routers.learning_admin.check_namespace_access", new=AsyncMock()):
            return await la.learning_stats(ns=ns, key_entry=key_entry)

    async def test_heuristic_count(self):
        h_store = _make_mock_heuristic_store([
            _Heuristic("h1", "test:ns", "Rule A"),
            _Heuristic("h2", "test:ns", "Rule B"),
        ])
        result = await self._call(h_store=h_store)
        self.assertEqual(result.heuristic_count, 2)

    async def test_empty_heuristics(self):
        result = await self._call()
        self.assertEqual(result.heuristic_count, 0)

    async def test_episode_count_7d(self):
        e_store = _make_mock_episode_store([
            _Episode("e1", "test:ns", "prompt 1"),
            _Episode("e2", "test:ns", "prompt 2"),
            _Episode("e3", "test:ns", "prompt 3"),
        ])
        result = await self._call(e_store=e_store)
        self.assertEqual(result.episode_count_7d, 3)

    async def test_avg_quality(self):
        e_store = _make_mock_episode_store([
            _Episode("e1", "ns", "p", quality_score=0.8),
            _Episode("e2", "ns", "p", quality_score=0.6),
        ])
        result = await self._call(e_store=e_store)
        self.assertAlmostEqual(result.avg_quality_7d, 0.7, places=2)

    async def test_no_quality_scores_returns_none(self):
        e_store = _make_mock_episode_store([_Episode("e1", "ns", "p")])
        result = await self._call(e_store=e_store)
        self.assertIsNone(result.avg_quality_7d)

    async def test_success_rate(self):
        e_store = _make_mock_episode_store([
            _Episode("e1", "ns", "p", outcome="SUCCESS"),
            _Episode("e2", "ns", "p", outcome="SUCCESS"),
            _Episode("e3", "ns", "p", outcome="FAILURE"),
            _Episode("e4", "ns", "p", outcome="FAILURE"),
        ])
        result = await self._call(e_store=e_store)
        self.assertAlmostEqual(result.success_rate_7d, 0.5, places=2)

    async def test_top_agents_populated(self):
        e_store = _make_mock_episode_store([
            _Episode("e1", "ns", "p", agent_used="agent-a"),
            _Episode("e2", "ns", "p", agent_used="agent-a"),
            _Episode("e3", "ns", "p", agent_used="agent-b"),
        ])
        result = await self._call(e_store=e_store)
        agents = [a["agent"] for a in result.top_agents]
        self.assertIn("agent-a", agents)
        self.assertEqual(result.top_agents[0]["agent"], "agent-a")

    async def test_namespace_matches_input(self):
        result = await self._call(ns="my:special:ns")
        self.assertEqual(result.namespace, "my:special:ns")


# ---------------------------------------------------------------------------
# list_heuristics
# ---------------------------------------------------------------------------

class TestListHeuristics(unittest.IsolatedAsyncioTestCase):
    async def _call(self, heuristics, ns="test:ns", limit=50):
        import engram_api.routers.learning_admin as la
        from unittest.mock import patch as _patch

        store = _make_mock_heuristic_store(heuristics)
        key_entry = MagicMock()
        with _patch.object(la, "_require_learning", return_value=store), \
             _patch("engram_api.routers.learning_admin.check_namespace_access", new=AsyncMock()):
            return await la.list_heuristics(ns=ns, limit=limit, key_entry=key_entry)

    async def test_returns_heuristics(self):
        heuristics = [_Heuristic("h1", "ns", "Always use HTTPS")]
        result = await self._call(heuristics)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].rule, "Always use HTTPS")

    async def test_sorted_by_confidence_desc(self):
        heuristics = [
            _Heuristic("h1", "ns", "Low conf", confidence=0.5),
            _Heuristic("h2", "ns", "High conf", confidence=0.95),
            _Heuristic("h3", "ns", "Mid conf", confidence=0.75),
        ]
        result = await self._call(heuristics)
        confidences = [r.confidence for r in result]
        self.assertEqual(confidences, sorted(confidences, reverse=True))

    async def test_limit_respected(self):
        heuristics = [_Heuristic(f"h{i}", "ns", f"Rule {i}") for i in range(10)]
        result = await self._call(heuristics, limit=3)
        self.assertEqual(len(result), 3)

    async def test_empty_returns_empty_list(self):
        result = await self._call([])
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# delete_heuristic
# ---------------------------------------------------------------------------

class TestDeleteHeuristic(unittest.IsolatedAsyncioTestCase):
    async def test_calls_store_delete(self):
        import engram_api.routers.learning_admin as la
        from unittest.mock import patch as _patch

        store = _make_mock_heuristic_store()
        key_entry = MagicMock()
        with _patch.object(la, "_require_learning", return_value=store), \
             _patch("engram_api.routers.learning_admin.check_namespace_access", new=AsyncMock()):
            result = await la.delete_heuristic("heuristic-123", ns="test:ns", key_entry=key_entry)

        store.delete.assert_awaited_once_with("heuristic-123")
        self.assertEqual(result.get("deleted"), "heuristic-123")


# ---------------------------------------------------------------------------
# recent_episodes
# ---------------------------------------------------------------------------

class TestRecentEpisodes(unittest.IsolatedAsyncioTestCase):
    async def _call(self, episodes, ns="test:ns", days=7, limit=50):
        import engram_api.routers.learning_admin as la
        from unittest.mock import patch as _patch

        store = _make_mock_episode_store(episodes)
        key_entry = MagicMock()
        with _patch.object(la, "_get_episode_store", return_value=store), \
             _patch("engram_api.routers.learning_admin.check_namespace_access", new=AsyncMock()):
            return await la.recent_episodes(ns=ns, days=days, limit=limit, key_entry=key_entry)

    async def test_returns_episodes(self):
        episodes = [_Episode("e1", "ns", "do something")]
        result = await self._call(episodes)
        self.assertEqual(len(result), 1)
        self.assertIn("do something", result[0].original_prompt)

    async def test_sorted_newest_first(self):
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        episodes = [
            _Episode("e1", "ns", "old", created_at=now - timedelta(hours=5)),
            _Episode("e2", "ns", "new", created_at=now - timedelta(hours=1)),
            _Episode("e3", "ns", "mid", created_at=now - timedelta(hours=3)),
        ]
        result = await self._call(episodes)
        ids = [r.id for r in result]
        self.assertEqual(ids, ["e2", "e3", "e1"])

    async def test_long_prompt_truncated(self):
        long_prompt = "x" * 500
        result = await self._call([_Episode("e1", "ns", long_prompt)])
        self.assertLessEqual(len(result[0].original_prompt), 320)

    async def test_limit_respected(self):
        episodes = [_Episode(f"e{i}", "ns", f"prompt {i}") for i in range(20)]
        result = await self._call(episodes, limit=5)
        self.assertEqual(len(result), 5)

    async def test_503_when_learning_not_installed(self):
        import engram_api.routers.learning_admin as la
        from unittest.mock import patch as _patch

        key_entry = MagicMock()
        with _patch.object(la, "_get_episode_store", return_value=None), \
             _patch("engram_api.routers.learning_admin.check_namespace_access", new=AsyncMock()):
            with self.assertRaises(HTTPException) as ctx:
                await la.recent_episodes(ns="ns", days=7, limit=10, key_entry=key_entry)
            self.assertEqual(ctx.exception.status_code, 503)


# ---------------------------------------------------------------------------
# trigger_reflection
# ---------------------------------------------------------------------------

class TestTriggerReflection(unittest.IsolatedAsyncioTestCase):
    async def test_503_when_no_api_key(self):
        import engram_api.routers.learning_admin as la
        from unittest.mock import patch as _patch
        import os

        req = la.ReflectRequest(namespace="test:ns", lookback_days=7)
        key_entry = MagicMock()

        with _patch("engram_api.routers.learning_admin.check_namespace_access", new=AsyncMock()), \
             _patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaises(HTTPException) as ctx:
                await la.trigger_reflection(req=req, key_entry=key_entry)
            self.assertEqual(ctx.exception.status_code, 503)

    async def test_503_when_placeholder_api_key(self):
        import engram_api.routers.learning_admin as la
        from unittest.mock import patch as _patch
        import os

        req = la.ReflectRequest(namespace="test:ns")
        key_entry = MagicMock()

        with _patch("engram_api.routers.learning_admin.check_namespace_access", new=AsyncMock()), \
             _patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-placeholder-xxx"}):
            with self.assertRaises(HTTPException) as ctx:
                await la.trigger_reflection(req=req, key_entry=key_entry)
            self.assertEqual(ctx.exception.status_code, 503)

    async def test_503_when_learning_not_installed(self):
        import engram_api.routers.learning_admin as la
        from unittest.mock import patch as _patch
        import os

        req = la.ReflectRequest(namespace="test:ns")
        key_entry = MagicMock()

        # Remove engram_learning from sys.modules so the imports in trigger_reflection
        # raise ImportError (setting to None makes Python raise ImportError on from-import)
        hidden = {
            "engram_learning.episode_store": None,
            "engram_learning.heuristic_store": None,
            "engram_learning.reflection": None,
        }
        with _patch("engram_api.routers.learning_admin.check_namespace_access", new=AsyncMock()), \
             _patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-real-key-here"}), \
             _patch.dict(sys.modules, hidden):
            with self.assertRaises(HTTPException) as ctx:
                await la.trigger_reflection(req=req, key_entry=key_entry)
            self.assertIn(ctx.exception.status_code, (503, 500))


# ---------------------------------------------------------------------------
# HTML dashboard route
# ---------------------------------------------------------------------------

class TestLearningDashboard(unittest.IsolatedAsyncioTestCase):
    async def test_returns_html(self):
        import engram_api.routers.learning_admin as la
        resp = await la.learning_dashboard()
        self.assertIn("text/html", resp.media_type)
        self.assertIn("engram", resp.body.decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
