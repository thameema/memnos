"""
tools/test_orchestrator.py — Unit tests for the orchestrator package.

Tests cover:
- Planner: _extract_json_array, decompose (success, API error, malformed JSON, empty list)
- Synthesizer: synthesize (success, API error, empty results, multiple workers)
- CriticWorker: evaluate (LGTM, corrections, API error, critic_prompt override)
- WorkerPool: run_parallel (all succeed, one fails, semaphore concurrency, teardown called)
- AgentRouter: _cosine_similarity, _agent_description_text, load_agent, match
- tag_extractor: extract_tags keyword matching and fallback
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/thameema/git/engram/packages/orchestrator")
sys.path.insert(0, "/Users/thameema/git/engram/packages/core")


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class TestExtractJsonArray(unittest.TestCase):
    def _call(self, text):
        from engram_orchestrator.planner import _extract_json_array
        return _extract_json_array(text)

    def test_clean_json_array(self):
        result = self._call('[{"id": "1", "prompt": "do thing", "agent": null}]')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "1")

    def test_fenced_json(self):
        text = '```json\n[{"id": "1", "prompt": "a", "agent": null}]\n```'
        result = self._call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["prompt"], "a")

    def test_fenced_no_language(self):
        text = '```\n[{"id": "2", "prompt": "b", "agent": null}]\n```'
        result = self._call(text)
        self.assertIsNotNone(result)

    def test_json_embedded_in_text(self):
        text = 'Here is the plan:\n[{"id": "1", "prompt": "do it", "agent": null}]\nDone.'
        result = self._call(text)
        self.assertIsNotNone(result)

    def test_invalid_json_returns_none(self):
        result = self._call("not json at all")
        self.assertIsNone(result)

    def test_non_array_json_returns_none(self):
        result = self._call('{"id": "1"}')
        self.assertIsNone(result)

    def test_multiple_subtasks(self):
        result = self._call('[{"id":"1","prompt":"a","agent":null},{"id":"2","prompt":"b","agent":"x"}]')
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["agent"], "x")


class TestPlanner(unittest.IsolatedAsyncioTestCase):
    def _make_planner(self):
        from engram_orchestrator.planner import Planner
        with patch("anthropic.AsyncAnthropic"):
            p = Planner(api_key="test-key", model="claude-haiku-4-5-20251001")
        return p

    def _make_response(self, text):
        block = MagicMock()
        block.type = "text"
        block.text = text
        resp = MagicMock()
        resp.content = [block]
        return resp

    async def test_decompose_single_subtask(self):
        p = self._make_planner()
        p._client.messages.create = AsyncMock(
            return_value=self._make_response('[{"id":"1","prompt":"do it","agent":null}]')
        )
        result = await p.decompose("do it")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "1")

    async def test_decompose_multiple_subtasks(self):
        p = self._make_planner()
        p._client.messages.create = AsyncMock(
            return_value=self._make_response(
                '[{"id":"1","prompt":"part A","agent":null},{"id":"2","prompt":"part B","agent":null}]'
            )
        )
        result = await p.decompose("complex task")
        self.assertEqual(len(result), 2)

    async def test_decompose_api_error_falls_back(self):
        p = self._make_planner()
        p._client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))
        result = await p.decompose("my task")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["prompt"], "my task")

    async def test_decompose_malformed_json_falls_back(self):
        p = self._make_planner()
        p._client.messages.create = AsyncMock(
            return_value=self._make_response("sorry, cannot decompose")
        )
        result = await p.decompose("my task")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["prompt"], "my task")

    async def test_decompose_empty_list_falls_back(self):
        p = self._make_planner()
        p._client.messages.create = AsyncMock(
            return_value=self._make_response("[]")
        )
        result = await p.decompose("my task")
        self.assertEqual(len(result), 1)

    async def test_decompose_with_context_sections(self):
        p = self._make_planner()
        captured = {}

        async def _capture(**kwargs):
            captured["messages"] = kwargs["messages"]
            return self._make_response('[{"id":"1","prompt":"t","agent":null}]')

        p._client.messages.create = _capture
        await p.decompose("task", past_context="past", heuristics="rule 1", template="step 1")
        user_msg = captured["messages"][0]["content"]
        self.assertIn("Past successful tasks", user_msg)
        self.assertIn("Heuristic rules", user_msg)
        self.assertIn("Skill template", user_msg)

    async def test_decompose_normalises_entries(self):
        p = self._make_planner()
        p._client.messages.create = AsyncMock(
            return_value=self._make_response('[{"id":1,"prompt":"go","agent":"bot"}]')
        )
        result = await p.decompose("go")
        self.assertEqual(result[0]["id"], "1")  # cast to str
        self.assertEqual(result[0]["agent"], "bot")


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------

class TestSynthesizer(unittest.IsolatedAsyncioTestCase):
    def _make_synth(self):
        from engram_orchestrator.synthesizer import Synthesizer
        with patch("anthropic.AsyncAnthropic"):
            s = Synthesizer(api_key="test-key", model="claude-haiku-4-5-20251001")
        return s

    def _make_response(self, text):
        block = MagicMock()
        block.type = "text"
        block.text = text
        resp = MagicMock()
        resp.content = [block]
        return resp

    async def test_synthesize_single_result(self):
        s = self._make_synth()
        s._client.messages.create = AsyncMock(
            return_value=self._make_response("Combined answer.")
        )
        result = await s.synthesize("original task", [("subtask 1", "result 1")])
        self.assertEqual(result, "Combined answer.")

    async def test_synthesize_empty_results(self):
        s = self._make_synth()
        result = await s.synthesize("task", [])
        self.assertIn("No subtask", result)

    async def test_synthesize_api_error_concatenates(self):
        s = self._make_synth()
        s._client.messages.create = AsyncMock(side_effect=RuntimeError("down"))
        result = await s.synthesize("task", [("p1", "r1"), ("p2", "r2")])
        self.assertIn("r1", result)
        self.assertIn("r2", result)

    async def test_synthesize_multiple_workers(self):
        s = self._make_synth()
        captured = {}

        async def _capture(**kwargs):
            captured["content"] = kwargs["messages"][0]["content"]
            return self._make_response("merged")

        s._client.messages.create = _capture
        await s.synthesize("task", [("A", "res A"), ("B", "res B")])
        self.assertIn("Worker 1", captured["content"])
        self.assertIn("Worker 2", captured["content"])


# ---------------------------------------------------------------------------
# CriticWorker
# ---------------------------------------------------------------------------

class TestCriticWorker(unittest.IsolatedAsyncioTestCase):
    def _make_critic(self):
        from engram_orchestrator.critic import CriticWorker
        with patch("anthropic.AsyncAnthropic"):
            c = CriticWorker(api_key="test-key", model="claude-haiku-4-5-20251001")
        return c

    def _make_response(self, text):
        block = MagicMock()
        block.type = "text"
        block.text = text
        resp = MagicMock()
        resp.content = [block]
        return resp

    async def test_evaluate_lgtm_passes(self):
        c = self._make_critic()
        c._client.messages.create = AsyncMock(return_value=self._make_response("LGTM"))
        passed, corrections = await c.evaluate("task", "draft")
        self.assertTrue(passed)
        self.assertIsNone(corrections)

    async def test_evaluate_lgtm_case_insensitive(self):
        c = self._make_critic()
        c._client.messages.create = AsyncMock(return_value=self._make_response("lgtm - looks good"))
        passed, _ = await c.evaluate("task", "draft")
        self.assertTrue(passed)

    async def test_evaluate_corrections_returned(self):
        c = self._make_critic()
        c._client.messages.create = AsyncMock(
            return_value=self._make_response("Missing the error handling section.")
        )
        passed, corrections = await c.evaluate("task", "draft")
        self.assertFalse(passed)
        self.assertIn("error handling", corrections)

    async def test_evaluate_api_error_passes_draft(self):
        c = self._make_critic()
        c._client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))
        passed, corrections = await c.evaluate("task", "draft")
        self.assertTrue(passed)
        self.assertIsNone(corrections)

    async def test_evaluate_critic_prompt_appended(self):
        c = self._make_critic()
        captured = {}

        async def _capture(**kwargs):
            captured["system"] = kwargs["system"]
            return self._make_response("LGTM")

        c._client.messages.create = _capture
        await c.evaluate("task", "draft", critic_prompt="Check for PII.")
        self.assertIn("Check for PII", captured["system"])

    async def test_evaluate_agent_system_prompt_in_user_message(self):
        c = self._make_critic()
        captured = {}

        async def _capture(**kwargs):
            captured["messages"] = kwargs["messages"]
            return self._make_response("LGTM")

        c._client.messages.create = _capture
        await c.evaluate("task", "draft", agent_system_prompt="Be precise.")
        user_content = captured["messages"][0]["content"]
        self.assertIn("Be precise", user_content)


# ---------------------------------------------------------------------------
# WorkerPool
# ---------------------------------------------------------------------------

class TestWorkerPool(unittest.IsolatedAsyncioTestCase):
    def _make_subtasks(self, n):
        from engram_orchestrator.models import SubTask
        return [SubTask(prompt=f"task {i}") for i in range(n)]

    def _make_worker(self, result="done", raises=None):
        w = MagicMock()
        if raises:
            w.run = AsyncMock(side_effect=raises)
        else:
            w.run = AsyncMock(return_value=result)
        w.teardown = AsyncMock()
        return w

    async def test_all_succeed(self):
        from engram_orchestrator.pool import WorkerPool
        from engram_orchestrator.models import TaskStatus
        pool = WorkerPool(max_concurrent=3)
        subtasks = self._make_subtasks(3)
        results = await pool.run_parallel(subtasks, lambda st: self._make_worker("ok"))
        self.assertTrue(all(r.status == TaskStatus.COMPLETE for r in results))
        self.assertTrue(all(r.result == "ok" for r in results))

    async def test_one_fails_others_complete(self):
        from engram_orchestrator.pool import WorkerPool
        from engram_orchestrator.models import TaskStatus

        pool = WorkerPool(max_concurrent=5)
        subtasks = self._make_subtasks(3)

        def factory(st):
            if st.prompt == "task 1":
                return self._make_worker(raises=RuntimeError("boom"))
            return self._make_worker("ok")

        results = await pool.run_parallel(subtasks, factory)
        statuses = {r.prompt: r.status for r in results}
        self.assertEqual(statuses["task 0"], TaskStatus.COMPLETE)
        self.assertEqual(statuses["task 1"], TaskStatus.FAILED)
        self.assertEqual(statuses["task 2"], TaskStatus.COMPLETE)

    async def test_failed_subtask_has_error_message(self):
        from engram_orchestrator.pool import WorkerPool
        pool = WorkerPool()
        subtasks = self._make_subtasks(1)
        results = await pool.run_parallel(
            subtasks, lambda st: self._make_worker(raises=ValueError("bad input"))
        )
        self.assertIn("bad input", results[0].error)

    async def test_teardown_called_on_success(self):
        from engram_orchestrator.pool import WorkerPool
        pool = WorkerPool()
        subtasks = self._make_subtasks(1)
        workers = []

        def factory(st):
            w = self._make_worker()
            workers.append(w)
            return w

        await pool.run_parallel(subtasks, factory)
        workers[0].teardown.assert_awaited_once()

    async def test_teardown_called_on_failure(self):
        from engram_orchestrator.pool import WorkerPool
        pool = WorkerPool()
        subtasks = self._make_subtasks(1)
        workers = []

        def factory(st):
            w = self._make_worker(raises=RuntimeError("err"))
            workers.append(w)
            return w

        await pool.run_parallel(subtasks, factory)
        workers[0].teardown.assert_awaited_once()

    async def test_timestamps_set(self):
        from engram_orchestrator.pool import WorkerPool
        pool = WorkerPool()
        subtasks = self._make_subtasks(1)
        results = await pool.run_parallel(subtasks, lambda st: self._make_worker())
        self.assertIsNotNone(results[0].started_at)
        self.assertIsNotNone(results[0].completed_at)

    async def test_semaphore_respected(self):
        from engram_orchestrator.pool import WorkerPool
        pool = WorkerPool(max_concurrent=2)
        subtasks = self._make_subtasks(6)
        results = await pool.run_parallel(subtasks, lambda st: self._make_worker())
        self.assertEqual(len(results), 6)


# ---------------------------------------------------------------------------
# AgentRouter
# ---------------------------------------------------------------------------

class TestCosineSimiliarity(unittest.TestCase):
    def test_identical_vectors(self):
        from engram_orchestrator.router import _cosine_similarity
        v = [1.0, 0.0, 0.0]
        self.assertAlmostEqual(_cosine_similarity(v, v), 1.0)

    def test_orthogonal_vectors(self):
        from engram_orchestrator.router import _cosine_similarity
        a, b = [1.0, 0.0], [0.0, 1.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), 0.0)

    def test_zero_vector_returns_zero(self):
        from engram_orchestrator.router import _cosine_similarity
        self.assertEqual(_cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_antiparallel_vectors(self):
        from engram_orchestrator.router import _cosine_similarity
        a, b = [1.0, 0.0], [-1.0, 0.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), -1.0)


class TestAgentDescriptionText(unittest.TestCase):
    def _call(self, agent):
        from engram_orchestrator.router import _agent_description_text
        return _agent_description_text(agent)

    def test_combines_name_and_description(self):
        result = self._call({"name": "researcher", "description": "does research"})
        self.assertIn("researcher", result)
        self.assertIn("does research", result)

    def test_list_field_joined(self):
        result = self._call({"name": "coder", "skills": ["python", "java"]})
        self.assertIn("python", result)
        self.assertIn("java", result)

    def test_empty_agent_returns_string(self):
        result = self._call({})
        self.assertIsInstance(result, str)


class TestAgentRouterLoadAgent(unittest.TestCase):
    def test_load_agent_from_file(self):
        from engram_orchestrator.router import AgentRouter
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_file = Path(tmpdir) / "test-agent.yaml"
            agent_file.write_text("name: test-agent\ndescription: does testing\n")
            router = AgentRouter(agents_dir=tmpdir, engram_client=MagicMock())
            agent = router.load_agent("test-agent")
        self.assertIsNotNone(agent)
        self.assertEqual(agent["name"], "test-agent")

    def test_load_missing_agent_returns_none(self):
        from engram_orchestrator.router import AgentRouter
        with tempfile.TemporaryDirectory() as tmpdir:
            router = AgentRouter(agents_dir=tmpdir, engram_client=MagicMock())
            result = router.load_agent("nonexistent")
        self.assertIsNone(result)

    def test_load_agent_nonexistent_dir_returns_none(self):
        from engram_orchestrator.router import AgentRouter
        router = AgentRouter(agents_dir="/nonexistent/path", engram_client=MagicMock())
        self.assertIsNone(router.load_agent("any"))

    def test_cached_agent_returned_directly(self):
        from engram_orchestrator.router import AgentRouter
        router = AgentRouter(agents_dir="/nonexistent", engram_client=MagicMock())
        router._agents_by_name["cached"] = {"name": "cached"}
        result = router.load_agent("cached")
        self.assertEqual(result["name"], "cached")


class TestAgentRouterInit(unittest.IsolatedAsyncioTestCase):
    async def test_init_nonexistent_dir_logs_warning(self):
        from engram_orchestrator.router import AgentRouter
        router = AgentRouter(agents_dir="/nonexistent", engram_client=MagicMock())
        await router.init()
        self.assertEqual(len(router._agent_embeddings), 0)

    async def test_init_loads_yaml_files(self):
        from engram_orchestrator.router import AgentRouter
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "agent-a.yaml").write_text("name: agent-a\ndescription: does A\n")
            Path(tmpdir, "agent-b.yaml").write_text("name: agent-b\ndescription: does B\n")
            mock_client = MagicMock()
            mock_client.embedder = MagicMock()
            mock_client.embedder.embed_batch = AsyncMock(return_value=[[0.1, 0.2], [0.3, 0.4]])
            router = AgentRouter(agents_dir=tmpdir, engram_client=mock_client)
            await router.init()
        self.assertEqual(len(router._agent_embeddings), 2)
        self.assertIn("agent-a", router._agents_by_name)

    async def test_init_skips_malformed_yaml(self):
        from engram_orchestrator.router import AgentRouter
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "bad.yaml").write_text("this: {unclosed bracket: [")
            Path(tmpdir, "good.yaml").write_text("name: good-agent\ndescription: works\n")
            mock_client = MagicMock()
            mock_client.embedder = MagicMock()
            mock_client.embedder.embed_batch = AsyncMock(return_value=[[0.1, 0.2]])
            router = AgentRouter(agents_dir=tmpdir, engram_client=mock_client)
            await router.init()
        self.assertEqual(len(router._agent_embeddings), 1)


class TestAgentRouterMatch(unittest.IsolatedAsyncioTestCase):
    async def test_match_returns_best_agent_above_threshold(self):
        from engram_orchestrator.router import AgentRouter
        router = AgentRouter(agents_dir="/nonexistent", engram_client=MagicMock())
        agent = {"name": "coder", "description": "writes code"}
        # Set embedding to a unit vector along axis 0
        router._agent_embeddings = [(agent, [1.0, 0.0, 0.0])]

        mock_client = MagicMock()
        mock_client.embedder.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])  # exact match → score 1.0
        router._engram_client = mock_client

        result = await router.match("write some code", "ns")
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "coder")

    async def test_match_returns_none_below_threshold(self):
        from engram_orchestrator.router import AgentRouter
        router = AgentRouter(agents_dir="/nonexistent", engram_client=MagicMock())
        agent = {"name": "coder"}
        router._agent_embeddings = [(agent, [1.0, 0.0, 0.0])]

        mock_client = MagicMock()
        mock_client.embedder.embed = AsyncMock(return_value=[0.0, 1.0, 0.0])  # orthogonal → score 0.0
        router._engram_client = mock_client

        result = await router.match("unrelated task", "ns")
        self.assertIsNone(result)

    async def test_match_no_agents_returns_none(self):
        from engram_orchestrator.router import AgentRouter
        router = AgentRouter(agents_dir="/nonexistent", engram_client=MagicMock())
        result = await router.match("any task", "ns")
        self.assertIsNone(result)

    async def test_match_embedding_failure_returns_none(self):
        from engram_orchestrator.router import AgentRouter
        router = AgentRouter(agents_dir="/nonexistent", engram_client=MagicMock())
        router._agent_embeddings = [({"name": "x"}, [1.0])]
        mock_client = MagicMock()
        mock_client.embedder.embed = AsyncMock(side_effect=RuntimeError("embed failed"))
        router._engram_client = mock_client
        result = await router.match("task", "ns")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# tag_extractor
# ---------------------------------------------------------------------------

class TestTagExtractor(unittest.TestCase):
    def _call(self, text):
        from engram_orchestrator.tag_extractor import extract_tags
        return extract_tags(text)

    def test_code_tag(self):
        self.assertIn("code", self._call("write a function to parse JSON"))

    def test_research_tag(self):
        self.assertIn("research", self._call("research the best database options"))

    def test_writing_tag(self):
        self.assertIn("writing", self._call("write documentation for the API"))

    def test_data_tag(self):
        self.assertIn("data", self._call("analyze the CSV metrics"))

    def test_planning_tag(self):
        self.assertIn("planning", self._call("design the architecture for this service"))

    def test_memory_tag(self):
        self.assertIn("memory", self._call("remember this for later"))

    def test_multiple_tags(self):
        tags = self._call("research and document the code structure")
        self.assertIn("research", tags)
        self.assertIn("writing", tags)

    def test_no_match_returns_general(self):
        self.assertEqual(self._call("hello world"), ["general"])

    def test_case_insensitive(self):
        self.assertIn("code", self._call("IMPLEMENT this feature"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
