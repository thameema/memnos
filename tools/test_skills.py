"""
tools/test_skills.py — Unit tests for the skills system and skill_coach.

Covers:
- SkillDefinition: to_anthropic_tool, to_mcp_tool_schema
- @skill decorator: attaches _engram_skill, required defaults to all params
- load_skills: discovers @skill functions, skips _private.py, handles import errors
- load_all_skills: builtin skills loaded, deduplicated
- Memory skills: memory_search, memory_write, memory_delete, memory_get
- Graph skills: graph_query, get_entity, get_related, add_fact
- Web skills: web_search (no key, brave, serper), fetch_url
- Orchestrator skills: spawn_task, get_task_result
- SkillCoach seeder: seed_claude_code_capabilities (add, skip, update)
- SkillCoach suggester: suggest_skills
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/thameema/git/engram/packages/core")


# ---------------------------------------------------------------------------
# SkillDefinition + @skill decorator
# ---------------------------------------------------------------------------

class TestSkillDefinition(unittest.TestCase):
    def _make(self, params=None, required=None):
        from engram.skills.decorator import SkillDefinition
        return SkillDefinition(
            name="test_skill",
            description="Does the thing",
            parameters=params or {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            fn=lambda: None,
            required=required,
        )

    def test_to_anthropic_tool_structure(self):
        sd = self._make()
        tool = sd.to_anthropic_tool()
        self.assertEqual(tool["name"], "test_skill")
        self.assertEqual(tool["description"], "Does the thing")
        self.assertIn("input_schema", tool)
        self.assertEqual(tool["input_schema"]["type"], "object")
        self.assertIn("query", tool["input_schema"]["properties"])

    def test_to_anthropic_tool_uses_all_params_as_required_when_none(self):
        sd = self._make(required=None)
        tool = sd.to_anthropic_tool()
        self.assertIn("query", tool["input_schema"]["required"])
        self.assertIn("top_k", tool["input_schema"]["required"])

    def test_to_anthropic_tool_respects_explicit_required(self):
        sd = self._make(required=["query"])
        tool = sd.to_anthropic_tool()
        self.assertEqual(tool["input_schema"]["required"], ["query"])
        self.assertNotIn("top_k", tool["input_schema"]["required"])

    def test_to_mcp_tool_schema_structure(self):
        sd = self._make()
        schema = sd.to_mcp_tool_schema()
        self.assertEqual(schema["type"], "object")
        self.assertIn("query", schema["properties"])


class TestSkillDecorator(unittest.TestCase):
    def test_decorator_attaches_skill(self):
        from engram.skills.decorator import skill
        @skill(name="my_skill", description="desc", parameters={"x": {"type": "string"}})
        async def fn(x: str):
            pass
        self.assertTrue(hasattr(fn, "_engram_skill"))
        self.assertEqual(fn._engram_skill.name, "my_skill")
        self.assertEqual(fn._engram_skill.description, "desc")

    def test_decorator_preserves_function(self):
        from engram.skills.decorator import skill
        @skill(name="s", description="d", parameters={})
        def fn(): return 42
        self.assertEqual(fn(), 42)

    def test_decorator_explicit_required(self):
        from engram.skills.decorator import skill
        @skill(name="s", description="d", parameters={"a": {}, "b": {}}, required=["a"])
        def fn(): pass
        self.assertEqual(fn._engram_skill.required, ["a"])


# ---------------------------------------------------------------------------
# load_skills / load_all_skills
# ---------------------------------------------------------------------------

class TestLoadSkills(unittest.TestCase):
    def _write_skill(self, d: Path, filename: str, content: str):
        path = d / filename
        path.write_text(content)

    def test_discovers_skill_functions(self):
        from engram.skills.loader import load_skills
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self._write_skill(p, "myplugin.py", """
import sys
sys.path.insert(0, "/Users/thameema/git/engram/packages/core")
from engram.skills.decorator import skill

@skill(name="my_custom", description="custom", parameters={"x": {"type": "string"}})
def my_custom(x): pass
""")
            skills = load_skills(p)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "my_custom")

    def test_skips_private_files(self):
        from engram.skills.loader import load_skills
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self._write_skill(p, "_internal.py", """
import sys
sys.path.insert(0, "/Users/thameema/git/engram/packages/core")
from engram.skills.decorator import skill

@skill(name="should_skip", description="x", parameters={})
def fn(): pass
""")
            skills = load_skills(p)
        self.assertEqual(skills, [])

    def test_skips_non_decorated_functions(self):
        from engram.skills.loader import load_skills
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self._write_skill(p, "plain.py", "def plain_fn(): pass")
            skills = load_skills(p)
        self.assertEqual(skills, [])

    def test_returns_empty_for_nonexistent_dir(self):
        from engram.skills.loader import load_skills
        skills = load_skills(Path("/nonexistent/path"))
        self.assertEqual(skills, [])

    def test_swallows_import_error_and_continues(self):
        from engram.skills.loader import load_skills
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self._write_skill(p, "broken.py", "import nonexistent_module_xyz")
            self._write_skill(p, "good.py", """
import sys
sys.path.insert(0, "/Users/thameema/git/engram/packages/core")
from engram.skills.decorator import skill

@skill(name="good_skill", description="ok", parameters={})
def good(): pass
""")
            skills = load_skills(p)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "good_skill")


class TestLoadAllSkills(unittest.TestCase):
    def test_loads_builtin_skills(self):
        from engram.skills.loader import load_all_skills
        from pathlib import Path
        skills = load_all_skills(repo_root=Path("/tmp/no_user_skills"))
        names = {s.name for s in skills}
        self.assertIn("memory_search", names)
        self.assertIn("memory_write", names)
        self.assertIn("memory_delete", names)
        self.assertIn("graph_query", names)
        self.assertIn("spawn_task", names)
        self.assertIn("web_search", names)

    def test_deduplicates_by_name(self):
        from engram.skills.loader import load_all_skills
        skills = load_all_skills(repo_root=Path("/tmp/no_user_skills"))
        names = [s.name for s in skills]
        self.assertEqual(len(names), len(set(names)))


# ---------------------------------------------------------------------------
# Memory builtin skills
# ---------------------------------------------------------------------------

class TestMemorySearchSkill(unittest.IsolatedAsyncioTestCase):
    async def _call(self, client=None, **kw):
        from engram.skills.builtin.memory import memory_search
        return await memory_search(query="test query", engram_client=client, **kw)

    async def test_returns_error_when_no_client(self):
        result = await self._call()
        self.assertIn("error", result)
        self.assertEqual(result["results"], [])

    async def test_returns_results_from_client(self):
        hit = MagicMock()
        hit.memory.content = "some content"
        hit.memory.id = "abc"
        hit.memory.tags = ["tag1"]
        hit.score = 0.95
        client = AsyncMock()
        client.search = AsyncMock(return_value=[hit])
        result = await self._call(client=client)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["content"], "some content")
        self.assertAlmostEqual(result["results"][0]["score"], 0.95)

    async def test_returns_error_on_exception(self):
        client = AsyncMock()
        client.search = AsyncMock(side_effect=Exception("boom"))
        result = await self._call(client=client)
        self.assertIn("error", result)


class TestMemoryWriteSkill(unittest.IsolatedAsyncioTestCase):
    async def _call(self, client=None, **kw):
        from engram.skills.builtin.memory import memory_write
        return await memory_write(content="store this", engram_client=client, **kw)

    async def test_returns_error_when_no_client(self):
        result = await self._call()
        self.assertIn("error", result)

    async def test_returns_entry_from_client(self):
        entry = MagicMock(id="id1", content="store this", namespace="ns1", tags=[])
        client = AsyncMock()
        client.add = AsyncMock(return_value=entry)
        result = await self._call(client=client)
        self.assertEqual(result["id"], "id1")

    async def test_returns_error_on_exception(self):
        client = AsyncMock()
        client.add = AsyncMock(side_effect=Exception("db down"))
        result = await self._call(client=client)
        self.assertIn("error", result)


class TestMemoryDeleteSkill(unittest.IsolatedAsyncioTestCase):
    async def _call(self, client=None):
        from engram.skills.builtin.memory import memory_delete
        return await memory_delete(memory_id="mem-123", engram_client=client)

    async def test_no_client_returns_error(self):
        result = await self._call()
        self.assertIn("error", result)
        self.assertFalse(result["deleted"])

    async def test_successful_delete(self):
        client = AsyncMock()
        client.delete = AsyncMock(return_value=True)
        result = await self._call(client=client)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["id"], "mem-123")

    async def test_exception_returns_error(self):
        client = AsyncMock()
        client.delete = AsyncMock(side_effect=Exception("err"))
        result = await self._call(client=client)
        self.assertIn("error", result)


class TestMemoryGetSkill(unittest.IsolatedAsyncioTestCase):
    async def _call(self, client=None):
        from engram.skills.builtin.memory import memory_get
        return await memory_get(memory_id="mem-1", engram_client=client)

    async def test_no_client_returns_error(self):
        result = await self._call()
        self.assertIn("error", result)
        self.assertFalse(result["found"])

    async def test_found(self):
        entry = MagicMock(id="mem-1", content="c", namespace="ns1", tags=[], source="agent")
        client = AsyncMock()
        client.get_memory = AsyncMock(return_value=entry)
        result = await self._call(client=client)
        self.assertTrue(result["found"])
        self.assertEqual(result["id"], "mem-1")

    async def test_not_found_returns_found_false(self):
        client = AsyncMock()
        client.get_memory = AsyncMock(return_value=None)
        result = await self._call(client=client)
        self.assertFalse(result["found"])


# ---------------------------------------------------------------------------
# Graph builtin skills
# ---------------------------------------------------------------------------

class TestGraphQuerySkill(unittest.IsolatedAsyncioTestCase):
    async def _call(self, client=None):
        from engram.skills.builtin.graph import graph_query
        return await graph_query(cypher="MATCH (n) RETURN n", engram_client=client)

    async def test_no_client_returns_error(self):
        result = await self._call()
        self.assertIn("error", result)
        self.assertEqual(result["rows"], [])

    async def test_success(self):
        client = AsyncMock()
        client.query_graph = AsyncMock(return_value=[{"n": "x"}])
        result = await self._call(client=client)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rows"][0]["n"], "x")

    async def test_exception_returns_error(self):
        client = AsyncMock()
        client.query_graph = AsyncMock(side_effect=Exception("graph err"))
        result = await self._call(client=client)
        self.assertIn("error", result)


class TestGetEntitySkill(unittest.IsolatedAsyncioTestCase):
    async def test_no_client_returns_error(self):
        from engram.skills.builtin.graph import get_entity
        result = await get_entity(name="Alice", engram_client=None)
        self.assertIn("error", result)
        self.assertFalse(result["found"])

    async def test_entity_found(self):
        from engram.skills.builtin.graph import get_entity
        entity = MagicMock()
        entity.id = "e1"
        entity.name = "Alice"
        entity.entity_type = "Person"
        entity.attributes = {}
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=entity)
        result = await get_entity(name="Alice", engram_client=client)
        self.assertTrue(result["found"])
        self.assertEqual(result["name"], "Alice")

    async def test_entity_not_found(self):
        from engram.skills.builtin.graph import get_entity
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=None)
        result = await get_entity(name="Ghost", engram_client=client)
        self.assertFalse(result["found"])


class TestAddFactSkill(unittest.IsolatedAsyncioTestCase):
    async def test_no_client_returns_error(self):
        from engram.skills.builtin.graph import add_fact
        result = await add_fact(subject="A", predicate="is", object="B", engram_client=None)
        self.assertIn("error", result)

    async def test_success(self):
        from engram.skills.builtin.graph import add_fact
        fact = MagicMock(id="f1", subject="A", predicate="is", object="B")
        client = AsyncMock()
        client.add_fact = AsyncMock(return_value=fact)
        result = await add_fact(subject="A", predicate="is", object="B", engram_client=client)
        self.assertEqual(result["id"], "f1")
        self.assertEqual(result["subject"], "A")


class TestGetRelatedSkill(unittest.IsolatedAsyncioTestCase):
    async def test_no_client_returns_error(self):
        from engram.skills.builtin.graph import get_related
        result = await get_related(entity_name="Alice", engram_client=None)
        self.assertIn("error", result)
        self.assertEqual(result["entities"], [])

    async def test_success(self):
        from engram.skills.builtin.graph import get_related
        graph = MagicMock()
        graph.entities = [MagicMock(id="e1", name="Bob", entity_type="Person")]
        graph.relations = []
        client = AsyncMock()
        client.get_related = AsyncMock(return_value=graph)
        result = await get_related(entity_name="Alice", engram_client=client)
        self.assertEqual(result["entity_count"], 1)
        self.assertEqual(result["root"], "Alice")


# ---------------------------------------------------------------------------
# Web builtin skills
# ---------------------------------------------------------------------------

class TestWebSearchSkill(unittest.IsolatedAsyncioTestCase):
    async def test_no_key_returns_error(self):
        from engram.skills.builtin.web import web_search
        with patch.dict("os.environ", {}, clear=True):
            result = await web_search(query="test")
        self.assertIn("error", result)
        self.assertEqual(result["results"], [])

    async def test_brave_key_used(self):
        from engram.skills.builtin.web import web_search
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"web": {"results": [{"title": "T", "url": "http://x.com", "description": "desc"}]}}
        mock_resp.raise_for_status = MagicMock()
        async_client = AsyncMock()
        async_client.__aenter__ = AsyncMock(return_value=async_client)
        async_client.__aexit__ = AsyncMock(return_value=False)
        async_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=async_client):
            with patch.dict("os.environ", {"BRAVE_API_KEY": "bk", "SERPER_API_KEY": ""}):
                result = await web_search(query="test")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["title"], "T")

    async def test_serper_key_used_when_no_brave(self):
        from engram.skills.builtin.web import web_search
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"organic": [{"title": "S", "link": "http://y.com", "snippet": "s"}]}
        mock_resp.raise_for_status = MagicMock()
        async_client = AsyncMock()
        async_client.__aenter__ = AsyncMock(return_value=async_client)
        async_client.__aexit__ = AsyncMock(return_value=False)
        async_client.post = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=async_client):
            with patch.dict("os.environ", {"SERPER_API_KEY": "sk", "BRAVE_API_KEY": ""}):
                result = await web_search(query="test")
        self.assertEqual(len(result["results"]), 1)


class TestFetchUrlSkill(unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        from engram.skills.builtin.web import fetch_url
        mock_resp = MagicMock()
        mock_resp.text = "Hello world content"
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        async_client = AsyncMock()
        async_client.__aenter__ = AsyncMock(return_value=async_client)
        async_client.__aexit__ = AsyncMock(return_value=False)
        async_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=async_client):
            result = await fetch_url(url="http://example.com")
        self.assertEqual(result["status_code"], 200)
        self.assertIn("Hello world", result["content"])

    async def test_exception_returns_error(self):
        from engram.skills.builtin.web import fetch_url
        async_client = AsyncMock()
        async_client.__aenter__ = AsyncMock(return_value=async_client)
        async_client.__aexit__ = AsyncMock(return_value=False)
        async_client.get = AsyncMock(side_effect=Exception("connection refused"))
        with patch("httpx.AsyncClient", return_value=async_client):
            result = await fetch_url(url="http://bad.host")
        self.assertIn("error", result)
        self.assertEqual(result["content"], "")


# ---------------------------------------------------------------------------
# Orchestrator builtin skills
# ---------------------------------------------------------------------------

class TestSpawnTaskSkill(unittest.IsolatedAsyncioTestCase):
    async def _call(self, orchestrator=None, **kw):
        from engram.skills.builtin.orchestrator import spawn_task
        return await spawn_task(prompt="do something", orchestrator=orchestrator, **kw)

    async def test_no_orchestrator_returns_error(self):
        result = await self._call()
        self.assertIn("error", result)

    async def test_success(self):
        orch = AsyncMock()
        orch.spawn = AsyncMock(return_value="task-123")
        result = await self._call(orchestrator=orch)
        self.assertEqual(result["task_id"], "task-123")
        self.assertEqual(result["status"], "queued")

    async def test_exception_returns_error(self):
        orch = AsyncMock()
        orch.spawn = AsyncMock(side_effect=Exception("overloaded"))
        result = await self._call(orchestrator=orch)
        self.assertIn("error", result)


class TestGetTaskResultSkill(unittest.IsolatedAsyncioTestCase):
    async def _call(self, orchestrator=None):
        from engram.skills.builtin.orchestrator import get_task_result
        return await get_task_result(task_id="task-abc", orchestrator=orchestrator)

    async def test_no_orchestrator_returns_error(self):
        result = await self._call()
        self.assertIn("error", result)
        self.assertFalse(result["found"])

    async def test_task_not_found(self):
        orch = AsyncMock()
        orch.get_result = AsyncMock(return_value=None)
        result = await self._call(orchestrator=orch)
        self.assertFalse(result["found"])
        self.assertEqual(result["status"], "pending")

    async def test_task_found(self):
        orch = AsyncMock()
        orch.get_result = AsyncMock(return_value={"status": "COMPLETE", "result": "done"})
        result = await self._call(orchestrator=orch)
        self.assertTrue(result["found"])
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["result"], "done")

    async def test_exception_returns_error(self):
        orch = AsyncMock()
        orch.get_result = AsyncMock(side_effect=Exception("timeout"))
        result = await self._call(orchestrator=orch)
        self.assertIn("error", result)


# ---------------------------------------------------------------------------
# SkillCoach — Seeder
# ---------------------------------------------------------------------------

class TestSkillCoachSeeder(unittest.IsolatedAsyncioTestCase):
    async def test_adds_new_skills(self):
        from engram.skill_coach.seeder import seed_claude_code_capabilities
        client = AsyncMock()
        client.search = AsyncMock(return_value=[])
        client.add = AsyncMock(return_value=MagicMock())
        client.supersede = AsyncMock()
        result = await seed_claude_code_capabilities(client)
        self.assertGreater(result["added"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 0)

    async def test_skips_unchanged_skills(self):
        from engram.skill_coach.seeder import seed_claude_code_capabilities, _content_hash
        from engram.skill_coach.capabilities import CLAUDE_CODE_CAPABILITIES
        cap = CLAUDE_CODE_CAPABILITIES[0]
        # Seeder hashes only cap["content"], not the full formatted string
        content_h = _content_hash(cap["content"])
        existing_mem = MagicMock()
        existing_mem.memory.metadata = {"skill_id": cap["id"], "content_hash": content_h}

        async def _search_side_effect(**kw):
            if cap["id"] in kw.get("query", ""):
                return [existing_mem]
            return []

        client = AsyncMock()
        client.search = AsyncMock(side_effect=_search_side_effect)
        client.add = AsyncMock()
        client.supersede = AsyncMock()
        result = await seed_claude_code_capabilities(client)
        self.assertGreater(result["skipped"], 0)

    async def test_updates_changed_skills(self):
        from engram.skill_coach.seeder import seed_claude_code_capabilities
        from engram.skill_coach.capabilities import CLAUDE_CODE_CAPABILITIES
        cap = CLAUDE_CODE_CAPABILITIES[0]
        existing_mem = MagicMock()
        existing_mem.memory.id = "old-id"
        existing_mem.memory.metadata = {"skill_id": cap["id"], "content_hash": "stale_hash"}
        client = AsyncMock()
        client.search = AsyncMock(side_effect=lambda **kw: [existing_mem] if cap["id"] in kw.get("query", "") else [])
        client.add = AsyncMock(return_value=MagicMock())
        client.supersede = AsyncMock()
        result = await seed_claude_code_capabilities(client)
        self.assertGreater(result["updated"], 0)
        client.supersede.assert_awaited_once_with("old-id", "tool:claude-code:capabilities")


# ---------------------------------------------------------------------------
# SkillCoach — Suggester
# ---------------------------------------------------------------------------

class TestSkillCoachSuggester(unittest.IsolatedAsyncioTestCase):
    async def test_empty_results_returns_empty_list(self):
        from engram.skill_coach.suggester import suggest_skills
        client = AsyncMock()
        client.search = AsyncMock(return_value=[])
        result = await suggest_skills(client, "how do I run tests in parallel")
        self.assertEqual(result, [])

    async def test_returns_suggestions_from_results(self):
        from engram.skill_coach.suggester import suggest_skills
        hit = MagicMock()
        hit.score = 0.87
        hit.memory.metadata = {"skill_id": "cc-loop", "title": "/loop — Repeat", "category": "slash-commands"}
        hit.memory.content = (
            "SKILL_ID:cc-loop\nTITLE: /loop\nCATEGORY: slash-commands\n"
            "WHEN TO USE: polling\nEXAMPLE: /loop check status\n\nFull content here."
        )
        client = AsyncMock()
        client.search = AsyncMock(return_value=[hit])
        result = await suggest_skills(client, "poll deploy status")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["skill_id"], "cc-loop")
        self.assertAlmostEqual(result[0]["relevance_score"], 0.87)
        self.assertEqual(result[0]["example"], "/loop check status")

    async def test_title_extracted_from_content_when_not_in_metadata(self):
        from engram.skill_coach.suggester import suggest_skills
        hit = MagicMock()
        hit.score = 0.75
        hit.memory.metadata = {"skill_id": "cc-test"}
        hit.memory.content = "SKILL_ID:cc-test\nTITLE: /test skill\n\nBody text"
        client = AsyncMock()
        client.search = AsyncMock(return_value=[hit])
        result = await suggest_skills(client, "something")
        self.assertEqual(result[0]["title"], "/test skill")


if __name__ == "__main__":
    unittest.main(verbosity=2)
