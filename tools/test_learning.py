"""
test_learning.py — Comprehensive tests for engram's self-learning subsystem.

Tests cover all five mechanisms:
  1. Episodic memory store (save / retrieve / query)
  2. Heuristic store (add / search / confidence update / decay)
  3. Skill template store (add / find_match / increment_use)
  4. Reflection service (LLM-derived heuristics + ArcadeDB sync)
  5. Skill extraction (quality-gated extraction + ArcadeDB sync)
  6. Quality routing (per-agent quality records + best-agent selection)
  7. Feedback detection (correction regex + FeedbackService recording)
  8. Multi-namespace scheduler (discovery via EpisodeStore)
  9. MCP handler wiring (orchestrator_tools correct API usage)

Run with ArcadeDB optional — learning subsystem tests use SQLite only.
Reflection/extraction tests mock the Anthropic API.

Usage
-----
cd /path/to/engram
ENGRAM_CONFIG=engram.yaml .venv/bin/python -m pytest tools/test_learning.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root without install
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
for pkg in ["core", "mcp-server", "api", "learning", "orchestrator", "gateway"]:
    pkg_path = REPO_ROOT / "packages" / pkg
    if pkg_path.exists():
        sys.path.insert(0, str(pkg_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """Return a fresh SQLite DB path in a temp directory."""
    return tmp_path / "test_learning.db"


def _episode(namespace="personal:test", outcome="SUCCESS", quality=None, tags=None, agent=None):
    from engram_learning.models import EpisodicRecord, Outcome
    return EpisodicRecord(
        task_id="task-" + datetime.now().strftime("%f"),
        namespace=namespace,
        original_prompt="Summarise recent meetings",
        decomposition=["step 1", "step 2"],
        agent_used=agent or "api-worker",
        runtime="api",
        outcome=Outcome(outcome),
        quality_score=quality,
        duration_s=2.5,
        token_cost=100,
        tags=tags or ["meetings", "summary"],
    )


def _heuristic(namespace="personal:test", rule="Always cite sources", confidence=0.8):
    from engram_learning.models import Heuristic
    return Heuristic(
        namespace=namespace,
        rule=rule,
        rationale="Prevented wrong answers in past",
        applies_to_tags=["summary", "research"],
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# 1. Episodic store
# ---------------------------------------------------------------------------

class TestEpisodeStore:
    @pytest.mark.asyncio
    async def test_save_and_get(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore

        store = EpisodeStore(db_path=tmp_db)
        await store.init()
        ep = _episode()
        await store.save(ep)

        fetched = await store.get(ep.id)
        assert fetched is not None
        assert fetched.id == ep.id
        assert fetched.namespace == ep.namespace
        assert fetched.original_prompt == ep.original_prompt

    @pytest.mark.asyncio
    async def test_get_by_task_id(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore

        store = EpisodeStore(db_path=tmp_db)
        await store.init()
        ep = _episode()
        await store.save(ep)

        fetched = await store.get_by_task_id(ep.task_id)
        assert fetched is not None
        assert fetched.id == ep.id

    @pytest.mark.asyncio
    async def test_get_recent_filters_by_namespace(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore

        store = EpisodeStore(db_path=tmp_db)
        await store.init()
        ep_a = _episode(namespace="ns:a")
        ep_b = _episode(namespace="ns:b")
        await store.save(ep_a)
        await store.save(ep_b)

        results = await store.get_recent("ns:a", days=7)
        assert len(results) == 1
        assert results[0].namespace == "ns:a"

    @pytest.mark.asyncio
    async def test_update_outcome(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.models import Outcome

        store = EpisodeStore(db_path=tmp_db)
        await store.init()
        ep = _episode()
        await store.save(ep)
        await store.update_outcome(ep.id, Outcome.CORRECTED, feedback="Wrong answer", quality_score=0.1)

        fetched = await store.get(ep.id)
        assert fetched.outcome == Outcome.CORRECTED
        assert fetched.user_feedback == "Wrong answer"
        assert fetched.quality_score == pytest.approx(0.1)

    @pytest.mark.asyncio
    async def test_get_active_namespaces(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore

        store = EpisodeStore(db_path=tmp_db)
        await store.init()
        for ns in ["ns:a", "ns:b", "ns:a"]:
            await store.save(_episode(namespace=ns))

        namespaces = await store.get_active_namespaces(days=7)
        assert "ns:a" in namespaces
        assert "ns:b" in namespaces
        assert len(set(namespaces)) == len(namespaces), "Duplicates in namespace list"

    @pytest.mark.asyncio
    async def test_get_active_namespaces_empty(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore

        store = EpisodeStore(db_path=tmp_db)
        await store.init()

        namespaces = await store.get_active_namespaces(days=7)
        assert namespaces == []


# ---------------------------------------------------------------------------
# 2. Heuristic store
# ---------------------------------------------------------------------------

class TestHeuristicStore:
    @pytest.mark.asyncio
    async def test_add_and_get_all(self, tmp_db):
        from engram_learning.heuristic_store import HeuristicStore

        store = HeuristicStore(db_path=tmp_db)
        await store.init()
        h = _heuristic()
        await store.add(h)

        all_h = await store.get_all(h.namespace)
        assert len(all_h) == 1
        assert all_h[0].rule == h.rule

    @pytest.mark.asyncio
    async def test_search_by_tags(self, tmp_db):
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.models import Heuristic

        store = HeuristicStore(db_path=tmp_db)
        await store.init()

        h1 = Heuristic(namespace="ns", rule="Rule A", applies_to_tags=["auth", "jwt"])
        h2 = Heuristic(namespace="ns", rule="Rule B", applies_to_tags=["summary"])
        await store.add(h1)
        await store.add(h2)

        results = await store.search("ns", query_tags=["auth"])
        assert any(r.rule == "Rule A" for r in results)
        # Rule B has no overlap but no tags requirement either — may still appear
        # Verify Rule A is ranked above Rule B (or only Rule A is returned)
        if len(results) > 1:
            assert results[0].rule == "Rule A"

    @pytest.mark.asyncio
    async def test_update_confidence(self, tmp_db):
        from engram_learning.heuristic_store import HeuristicStore

        store = HeuristicStore(db_path=tmp_db)
        await store.init()
        h = _heuristic(confidence=0.8)
        await store.add(h)
        await store.update_confidence(h.id, 0.1)

        all_h = await store.get_all(h.namespace)
        assert all_h[0].confidence == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_confidence_clamped_to_one(self, tmp_db):
        from engram_learning.heuristic_store import HeuristicStore

        store = HeuristicStore(db_path=tmp_db)
        await store.init()
        h = _heuristic(confidence=0.95)
        await store.add(h)
        await store.update_confidence(h.id, 0.5)

        all_h = await store.get_all(h.namespace)
        assert all_h[0].confidence <= 1.0

    @pytest.mark.asyncio
    async def test_delete(self, tmp_db):
        from engram_learning.heuristic_store import HeuristicStore

        store = HeuristicStore(db_path=tmp_db)
        await store.init()
        h = _heuristic()
        await store.add(h)
        await store.delete(h.id)

        all_h = await store.get_all(h.namespace)
        assert len(all_h) == 0

    @pytest.mark.asyncio
    async def test_namespace_isolation(self, tmp_db):
        from engram_learning.heuristic_store import HeuristicStore

        store = HeuristicStore(db_path=tmp_db)
        await store.init()
        await store.add(_heuristic(namespace="ns:x", rule="Rule X"))
        await store.add(_heuristic(namespace="ns:y", rule="Rule Y"))

        x_results = await store.get_all("ns:x")
        y_results = await store.get_all("ns:y")
        assert len(x_results) == 1 and x_results[0].rule == "Rule X"
        assert len(y_results) == 1 and y_results[0].rule == "Rule Y"


# ---------------------------------------------------------------------------
# 3. Skill template store
# ---------------------------------------------------------------------------

class TestSkillStore:
    @pytest.mark.asyncio
    async def test_add_and_get_all(self, tmp_db):
        from engram_learning.skill_store import SkillStore
        from engram_learning.models import SkillTemplate

        store = SkillStore(db_path=tmp_db)
        await store.init()
        t = SkillTemplate(
            namespace="ns",
            name="meeting-summary",
            description="Summarise meeting notes",
            trigger_patterns=["summarise meeting", "meeting notes"],
            steps=["1. Collect notes", "2. Extract action items"],
        )
        await store.add(t)

        all_t = await store.get_all("ns")
        assert len(all_t) == 1
        assert all_t[0].name == "meeting-summary"

    @pytest.mark.asyncio
    async def test_find_match_above_threshold(self, tmp_db):
        from engram_learning.skill_store import SkillStore
        from engram_learning.models import SkillTemplate

        store = SkillStore(db_path=tmp_db)
        await store.init()
        t = SkillTemplate(
            namespace="ns",
            name="meeting-summary",
            description="Summarise meeting notes",
            trigger_patterns=["summarise meeting", "meeting notes"],
            steps=["step 1"],
        )
        await store.add(t)

        # Task text contains both trigger patterns → high match score
        match = await store.find_match("summarise meeting notes from today", "ns")
        assert match is not None
        assert match.name == "meeting-summary"

    @pytest.mark.asyncio
    async def test_find_match_below_threshold(self, tmp_db):
        from engram_learning.skill_store import SkillStore
        from engram_learning.models import SkillTemplate

        store = SkillStore(db_path=tmp_db)
        await store.init()
        t = SkillTemplate(
            namespace="ns",
            name="meeting-summary",
            description="Summarise meeting notes",
            trigger_patterns=["very specific trigger that won't match"],
            steps=["step 1"],
        )
        await store.add(t)

        match = await store.find_match("deploy kubernetes cluster", "ns")
        assert match is None

    @pytest.mark.asyncio
    async def test_increment_use_updates_count(self, tmp_db):
        from engram_learning.skill_store import SkillStore
        from engram_learning.models import SkillTemplate

        store = SkillStore(db_path=tmp_db)
        await store.init()
        t = SkillTemplate(namespace="ns", name="t1", description="d", trigger_patterns=["x"], steps=["s"])
        await store.add(t)
        await store.increment_use(t.id, success=True)

        all_t = await store.get_all("ns")
        assert all_t[0].use_count == 1


# ---------------------------------------------------------------------------
# 4. Reflection service (mocked LLM)
# ---------------------------------------------------------------------------

class TestReflectionService:
    def _make_mock_response(self, new_heuristics=None, updates=None, deletes=None):
        payload = {
            "new_heuristics": new_heuristics or [],
            "update_heuristics": updates or [],
            "delete_heuristic_ids": deletes or [],
        }
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=json.dumps(payload))]
        return mock_msg

    @pytest.mark.asyncio
    async def test_reflection_skipped_with_few_failures(self, tmp_db):
        """Reflection should not run when fewer than 2 failure/correction episodes."""
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.reflection import ReflectionService

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        h_store = HeuristicStore(db_path=tmp_db)
        await h_store.init()
        # One success episode only
        await ep_store.save(_episode(outcome="SUCCESS"))

        svc = ReflectionService("key", "model", ep_store, h_store, "personal:test")
        with patch.object(svc._client.messages, "create", new_callable=AsyncMock) as mock_create:
            await svc.run(lookback_days=7)
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflection_adds_new_heuristics(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.models import Outcome
        from engram_learning.reflection import ReflectionService

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        h_store = HeuristicStore(db_path=tmp_db)
        await h_store.init()

        # Two failure episodes to trigger reflection
        ep1 = _episode(outcome="FAILURE")
        ep2 = _episode(outcome="FAILURE")
        await ep_store.save(ep1)
        await ep_store.save(ep2)

        mock_response = self._make_mock_response(
            new_heuristics=[
                {"rule": "Always validate input", "rationale": "Caused failure X", "applies_to_tags": ["input"], "confidence": 0.9}
            ]
        )

        svc = ReflectionService("key", "model", ep_store, h_store, "personal:test")
        with patch.object(svc._client.messages, "create", new_callable=AsyncMock, return_value=mock_response):
            await svc.run(lookback_days=7)

        heuristics = await h_store.get_all("personal:test")
        assert len(heuristics) == 1
        assert heuristics[0].rule == "Always validate input"
        assert heuristics[0].confidence == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_reflection_syncs_to_arcadedb(self, tmp_db):
        """Reflection should write new heuristics to ArcadeDB when engram_client is provided."""
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.reflection import ReflectionService

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        h_store = HeuristicStore(db_path=tmp_db)
        await h_store.init()
        await ep_store.save(_episode(outcome="FAILURE"))
        await ep_store.save(_episode(outcome="FAILURE"))

        mock_response = self._make_mock_response(
            new_heuristics=[{"rule": "Test rule", "rationale": "reason", "applies_to_tags": [], "confidence": 0.8}]
        )
        mock_client = AsyncMock()

        svc = ReflectionService("key", "model", ep_store, h_store, "personal:test", engram_client=mock_client)
        with patch.object(svc._client.messages, "create", new_callable=AsyncMock, return_value=mock_response):
            await svc.run(lookback_days=7)

        mock_client.add.assert_called_once()
        call_kwargs = mock_client.add.call_args
        tags = call_kwargs.kwargs.get("tags", [])
        assert "heuristic" in tags

    @pytest.mark.asyncio
    async def test_reflection_updates_confidence(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.reflection import ReflectionService

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        h_store = HeuristicStore(db_path=tmp_db)
        await h_store.init()

        existing = _heuristic(namespace="personal:test", confidence=0.7)
        await h_store.add(existing)
        await ep_store.save(_episode(outcome="FAILURE"))
        await ep_store.save(_episode(outcome="FAILURE"))

        mock_response = self._make_mock_response(
            updates=[{"id": existing.id, "confidence_delta": 0.1, "reason": "pattern confirmed"}]
        )
        svc = ReflectionService("key", "model", ep_store, h_store, "personal:test")
        with patch.object(svc._client.messages, "create", new_callable=AsyncMock, return_value=mock_response):
            await svc.run(lookback_days=7)

        all_h = await h_store.get_all("personal:test")
        assert all_h[0].confidence == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_reflection_handles_invalid_llm_json(self, tmp_db):
        """Should not crash when LLM returns invalid JSON."""
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.reflection import ReflectionService

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        h_store = HeuristicStore(db_path=tmp_db)
        await h_store.init()
        await ep_store.save(_episode(outcome="FAILURE"))
        await ep_store.save(_episode(outcome="FAILURE"))

        bad_response = MagicMock()
        bad_response.content = [MagicMock(text="not valid json at all")]

        svc = ReflectionService("key", "model", ep_store, h_store, "personal:test")
        with patch.object(svc._client.messages, "create", new_callable=AsyncMock, return_value=bad_response):
            await svc.run(lookback_days=7)  # Must not raise


# ---------------------------------------------------------------------------
# 5. Skill extraction (mocked LLM)
# ---------------------------------------------------------------------------

class TestSkillExtractor:
    @pytest.mark.asyncio
    async def test_extraction_skipped_for_low_quality(self, tmp_db):
        from engram_learning.extractor import SkillExtractor
        from engram_learning.skill_store import SkillStore

        skill_store = SkillStore(db_path=tmp_db)
        await skill_store.init()
        extractor = SkillExtractor("key", "model", skill_store)

        ep = _episode(quality=0.5, outcome="SUCCESS")  # quality below threshold
        with patch.object(extractor._client.messages, "create", new_callable=AsyncMock) as mock_create:
            await extractor.maybe_extract(ep)
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_extraction_skipped_for_failures(self, tmp_db):
        from engram_learning.extractor import SkillExtractor
        from engram_learning.skill_store import SkillStore

        skill_store = SkillStore(db_path=tmp_db)
        await skill_store.init()
        extractor = SkillExtractor("key", "model", skill_store)

        ep = _episode(quality=0.95, outcome="FAILURE")
        with patch.object(extractor._client.messages, "create", new_callable=AsyncMock) as mock_create:
            await extractor.maybe_extract(ep)
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_extraction_saves_template(self, tmp_db):
        from engram_learning.extractor import SkillExtractor
        from engram_learning.skill_store import SkillStore

        skill_store = SkillStore(db_path=tmp_db)
        await skill_store.init()
        extractor = SkillExtractor("key", "model", skill_store)

        ep = _episode(quality=0.9, outcome="SUCCESS")
        mock_resp_data = {
            "extract": True,
            "description": "Summarise meeting notes efficiently",
            "trigger_patterns": ["summarise meeting", "meeting notes"],
            "steps": ["1. Read notes", "2. Extract key points"],
        }
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=json.dumps(mock_resp_data))]

        with patch.object(extractor._client.messages, "create", new_callable=AsyncMock, return_value=mock_resp):
            await extractor.maybe_extract(ep)

        all_templates = await skill_store.get_all(ep.namespace)
        assert len(all_templates) == 1
        assert "meeting" in all_templates[0].name

    @pytest.mark.asyncio
    async def test_extraction_syncs_to_arcadedb(self, tmp_db):
        from engram_learning.extractor import SkillExtractor
        from engram_learning.skill_store import SkillStore

        skill_store = SkillStore(db_path=tmp_db)
        await skill_store.init()
        mock_client = AsyncMock()
        extractor = SkillExtractor("key", "model", skill_store, engram_client=mock_client)

        ep = _episode(quality=0.9, outcome="SUCCESS")
        mock_resp_data = {
            "extract": True,
            "description": "Deploy service to k8s",
            "trigger_patterns": ["deploy", "kubernetes"],
            "steps": ["1. Build image", "2. Push to registry", "3. Apply manifests"],
        }
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=json.dumps(mock_resp_data))]

        with patch.object(extractor._client.messages, "create", new_callable=AsyncMock, return_value=mock_resp):
            await extractor.maybe_extract(ep)

        mock_client.add.assert_called_once()
        call_kwargs = mock_client.add.call_args
        tags = call_kwargs.kwargs.get("tags") or []
        assert "skill_template" in tags

    @pytest.mark.asyncio
    async def test_extraction_reuses_existing_template(self, tmp_db):
        """When a matching template already exists, should increment use count, not create new."""
        from engram_learning.extractor import SkillExtractor
        from engram_learning.skill_store import SkillStore
        from engram_learning.models import SkillTemplate

        skill_store = SkillStore(db_path=tmp_db)
        await skill_store.init()
        existing = SkillTemplate(
            namespace="personal:test",
            name="meeting-summary",
            description="Summarise meeting notes",
            trigger_patterns=["summarise", "meeting"],
            steps=["step 1"],
        )
        await skill_store.add(existing)

        extractor = SkillExtractor("key", "model", skill_store)
        ep = _episode(quality=0.95, outcome="SUCCESS")
        ep.original_prompt = "summarise meeting from today"

        with patch.object(extractor._client.messages, "create", new_callable=AsyncMock) as mock_create:
            await extractor.maybe_extract(ep)
            # LLM should NOT be called — existing template found
            mock_create.assert_not_called()

        all_t = await skill_store.get_all("personal:test")
        assert len(all_t) == 1
        assert all_t[0].use_count == 1


# ---------------------------------------------------------------------------
# 6. Quality routing
# ---------------------------------------------------------------------------

class TestQualityStore:
    @pytest.mark.asyncio
    async def test_update_and_retrieve(self, tmp_db):
        from engram_learning.quality_store import QualityStore

        store = QualityStore(db_path=tmp_db)
        await store.init()
        await store.update("agent-a", "summary", "ns", quality_score=0.9, duration_s=1.5, success=True)

        records = await store.get("summary", "ns")
        assert len(records) == 1
        assert records[0].agent_name == "agent-a"
        assert records[0].avg_quality_score == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_running_average(self, tmp_db):
        from engram_learning.quality_store import QualityStore

        store = QualityStore(db_path=tmp_db)
        await store.init()
        await store.update("agent-a", "summary", "ns", 1.0, 1.0, True)
        await store.update("agent-a", "summary", "ns", 0.8, 1.0, True)

        records = await store.get("summary", "ns")
        assert records[0].avg_quality_score == pytest.approx(0.9)
        assert records[0].sample_count == 2

    @pytest.mark.asyncio
    async def test_get_best_agent_insufficient_samples(self, tmp_db):
        from engram_learning.quality_store import QualityStore

        store = QualityStore(db_path=tmp_db)
        await store.init()
        # Only 1 sample, need min_samples=5
        await store.update("agent-a", "summary", "ns", 0.9, 1.0, True)

        best = await store.get_best_agent("summary", "ns", min_samples=5)
        assert best is None

    @pytest.mark.asyncio
    async def test_get_best_agent_picks_highest_quality(self, tmp_db):
        from engram_learning.quality_store import QualityStore

        store = QualityStore(db_path=tmp_db)
        await store.init()
        # Fill enough samples for both agents
        for _ in range(6):
            await store.update("agent-a", "summary", "ns", 0.7, 1.0, True)
            await store.update("agent-b", "summary", "ns", 0.9, 1.0, True)

        best = await store.get_best_agent("summary", "ns", min_samples=5)
        assert best == "agent-b"

    @pytest.mark.asyncio
    async def test_failure_rate_penalises_agent(self, tmp_db):
        from engram_learning.quality_store import QualityStore

        store = QualityStore(db_path=tmp_db)
        await store.init()
        for _ in range(6):
            await store.update("agent-a", "summary", "ns", 0.95, 1.0, False)  # high quality but always fails
            await store.update("agent-b", "summary", "ns", 0.75, 1.0, True)

        best = await store.get_best_agent("summary", "ns", min_samples=5)
        assert best == "agent-b"


# ---------------------------------------------------------------------------
# 7. Feedback detection and recording
# ---------------------------------------------------------------------------

class TestFeedbackDetection:
    def test_detects_explicit_correction(self):
        from engram_learning.feedback import detect_correction

        corrections = [
            "No, that's wrong",
            "Actually the correct answer is 42",
            "You missed the key point",
            "Wait, that's not right",
            "That's incorrect, let me clarify",
        ]
        for text in corrections:
            assert detect_correction(text), f"Expected correction: {text!r}"

    def test_does_not_flag_normal_messages(self):
        from engram_learning.feedback import detect_correction

        normal = [
            "Thanks, that was helpful",
            "Please summarise the meeting",
            "Deploy the service to production",
            "Search my memory for auth patterns",
        ]
        for text in normal:
            assert not detect_correction(text), f"False positive: {text!r}"

    @pytest.mark.asyncio
    async def test_record_correction_marks_episode_corrected(self, tmp_db):
        from engram_learning.feedback import FeedbackService
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.quality_store import QualityStore
        from engram_learning.models import Outcome

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        q_store = QualityStore(db_path=tmp_db)
        await q_store.init()
        ep = _episode()
        await ep_store.save(ep)

        svc = FeedbackService(ep_store, q_store)
        await svc.record_correction(ep.task_id, "Actually that was wrong")

        updated = await ep_store.get(ep.id)
        assert updated.outcome == Outcome.CORRECTED
        assert updated.quality_score == pytest.approx(0.1)

    @pytest.mark.asyncio
    async def test_record_explicit_positive_feedback(self, tmp_db):
        from engram_learning.feedback import FeedbackService
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.quality_store import QualityStore
        from engram_learning.models import Outcome

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        q_store = QualityStore(db_path=tmp_db)
        await q_store.init()
        ep = _episode(agent="agent-a")
        await ep_store.save(ep)

        svc = FeedbackService(ep_store, q_store)
        await svc.record_explicit(ep.task_id, "positive")

        updated = await ep_store.get(ep.id)
        assert updated.outcome == Outcome.SUCCESS
        assert updated.quality_score == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_record_negative_feedback_triggers_reflection(self, tmp_db):
        from engram_learning.feedback import FeedbackService
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.quality_store import QualityStore

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        q_store = QualityStore(db_path=tmp_db)
        await q_store.init()
        ep = _episode()
        await ep_store.save(ep)

        mock_reflection = AsyncMock()
        svc = FeedbackService(ep_store, q_store, reflection_service=mock_reflection)
        await svc.record_explicit(ep.task_id, "negative")

        mock_reflection.run.assert_called_once_with(lookback_days=1)


# ---------------------------------------------------------------------------
# 8. Multi-namespace scheduler
# ---------------------------------------------------------------------------

class TestMultiNamespaceScheduler:
    @pytest.mark.asyncio
    async def test_reflection_runs_for_all_active_namespaces(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.scheduler import LearningScheduler

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()

        # Episodes in 3 different namespaces
        for ns in ["ns:a", "ns:b", "ns:c"]:
            await ep_store.save(_episode(namespace=ns))

        mock_reflection = AsyncMock()
        mock_decay = AsyncMock()

        called_namespaces: list[str] = []

        async def reflection_run(lookback_days=7):
            called_namespaces.append(mock_reflection.namespace)

        mock_reflection.run = reflection_run
        mock_reflection.namespace = "ns:a"

        factory_calls: list[str] = []

        def mock_factory(ns: str):
            factory_calls.append(ns)
            svc = AsyncMock()
            svc.namespace = ns
            return svc

        config = MagicMock()
        config.learning = MagicMock()
        config.learning.reflection = MagicMock()
        config.learning.reflection.lookback_days = 7
        config.learning.heuristic_decay = MagicMock()

        scheduler = LearningScheduler(
            config=config,
            reflection_service=mock_reflection,
            decay_service=mock_decay,
            namespace="ns:a",
            episode_store=ep_store,
            reflection_factory=mock_factory,
        )
        await scheduler._run_reflection()

        # Factory should have been called for the non-default namespaces
        assert "ns:b" in factory_calls
        assert "ns:c" in factory_calls

    @pytest.mark.asyncio
    async def test_decay_runs_for_all_active_namespaces(self, tmp_db):
        from engram_learning.episode_store import EpisodeStore
        from engram_learning.scheduler import LearningScheduler

        ep_store = EpisodeStore(db_path=tmp_db)
        await ep_store.init()
        for ns in ["ns:a", "ns:b"]:
            await ep_store.save(_episode(namespace=ns))

        mock_reflection = AsyncMock()
        mock_decay = AsyncMock()
        config = MagicMock()
        config.learning = MagicMock()
        config.learning.reflection = MagicMock()
        config.learning.reflection.lookback_days = 7

        scheduler = LearningScheduler(
            config=config,
            reflection_service=mock_reflection,
            decay_service=mock_decay,
            namespace="ns:a",
            episode_store=ep_store,
        )
        await scheduler._run_decay()

        # Decay should have been called for both namespaces
        assert mock_decay.run.call_count >= 2
        called_ns = [c.args[0] for c in mock_decay.run.call_args_list]
        assert "ns:a" in called_ns
        assert "ns:b" in called_ns


# ---------------------------------------------------------------------------
# 9. MCP handler wiring
# ---------------------------------------------------------------------------

class TestMCPHandlers:
    """Test that MCP orchestrator_tools handlers call the correct APIs."""

    @pytest.mark.asyncio
    async def test_handle_get_heuristics_correct_api(self, tmp_db):
        """handle_get_heuristics must call HeuristicStore().get_all() or .search(), never .get()."""
        # Imports are local to the handler function, so we patch at source module level.
        mock_store = AsyncMock()
        mock_store.init = AsyncMock()
        mock_store.get_all = AsyncMock(return_value=[])
        mock_store.search = AsyncMock(return_value=[])

        with patch("engram_learning.heuristic_store.HeuristicStore", return_value=mock_store):
            from engram_mcp.tools.orchestrator_tools import handle_get_heuristics
            result = await handle_get_heuristics(namespace="ns:test")

        assert "heuristics" in result
        assert isinstance(result["total"], int)
        # Verify we did NOT call a non-existent .get() method
        assert not hasattr(mock_store, "_mock_called_get") or not mock_store.get.called

    @pytest.mark.asyncio
    async def test_handle_add_heuristic_creates_heuristic_object(self, tmp_db):
        """handle_add_heuristic must create a Heuristic dataclass and call store.add(heuristic)."""
        from engram_learning.heuristic_store import HeuristicStore
        from engram_learning.models import Heuristic

        real_store = HeuristicStore(db_path=tmp_db)
        await real_store.init()

        with patch("engram_learning.heuristic_store.HeuristicStore", return_value=real_store):
            real_store.init = AsyncMock()
            from engram_mcp.tools.orchestrator_tools import handle_add_heuristic
            result = await handle_add_heuristic(
                namespace="ns:test",
                rule="Always verify before deleting",
                rationale="Caused data loss",
                applies_to_tags=["database", "deletion"],
            )

        assert "id" in result
        assert result["rule"] == "Always verify before deleting"

        # Verify it was persisted to SQLite
        all_h = await real_store.get_all("ns:test")
        assert len(all_h) == 1
        assert all_h[0].rule == "Always verify before deleting"
        assert "database" in all_h[0].applies_to_tags

    @pytest.mark.asyncio
    async def test_handle_trigger_reflection_uses_reflection_service(self):
        """handle_trigger_reflection must import ReflectionService (not ReflectionAgent)."""
        # Patch at source module level — imports are local inside the function.
        mock_service = AsyncMock()
        mock_ep_store = AsyncMock()
        mock_ep_store.init = AsyncMock()
        mock_h_store = AsyncMock()
        mock_h_store.init = AsyncMock()

        with patch("engram_learning.episode_store.EpisodeStore", return_value=mock_ep_store), \
             patch("engram_learning.heuristic_store.HeuristicStore", return_value=mock_h_store), \
             patch("engram_learning.reflection.ReflectionService", return_value=mock_service), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            from engram_mcp.tools.orchestrator_tools import handle_trigger_reflection
            result = await handle_trigger_reflection(namespace="ns:test", lookback_days=3)

        assert result["triggered"] is True
        mock_service.run.assert_called_once_with(lookback_days=3)

    @pytest.mark.asyncio
    async def test_handle_trigger_reflection_returns_false_on_import_error(self):
        """handle_trigger_reflection must return triggered=False if engram_learning missing."""
        from engram_mcp.tools.orchestrator_tools import handle_trigger_reflection

        with patch.dict("sys.modules", {
            "engram_learning": None,
            "engram_learning.episode_store": None,
            "engram_learning.heuristic_store": None,
            "engram_learning.reflection": None,
        }):
            result = await handle_trigger_reflection(namespace="ns:test")

        assert result["triggered"] is False


# ---------------------------------------------------------------------------
# 10. Integration: orchestrator episode + skill extraction wiring
# ---------------------------------------------------------------------------

class TestOrchestratorLearningWiring:
    """Light integration test — verifies the orchestrator calls SkillExtractor after success."""

    @pytest.mark.asyncio
    async def test_skill_extractor_called_after_success(self, tmp_db):
        """After a successful task, SkillExtractor.maybe_extract() should be invoked."""
        from engram_learning.extractor import SkillExtractor
        from engram_learning.skill_store import SkillStore

        ep = _episode(quality=0.9, outcome="SUCCESS")
        skill_store = SkillStore(db_path=tmp_db)
        await skill_store.init()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps({"extract": False}))]
        extractor = SkillExtractor("key", "model", skill_store)

        with patch.object(extractor._client.messages, "create", new_callable=AsyncMock, return_value=mock_response):
            await extractor.maybe_extract(ep)
            # Should have called LLM since quality >= 0.8 and outcome is SUCCESS
            extractor._client.messages.create.assert_called_once()


# ---------------------------------------------------------------------------
# Pytest entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v", "--tb=short"]))
