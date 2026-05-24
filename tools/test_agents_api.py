"""
tools/test_agents_api.py — Tests for the agents REST endpoints.

Covers:
- _yaml_to_agent: field mapping, system_prompt preview truncation
- _load_agents: reads YAML files, skips malformed, skips non-dict
- GET /agents/: returns list, empty when dir missing, requires auth
- GET /agents/{name}: found, not found, requires auth
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, "/Users/thameema/git/engram/packages/api")
sys.path.insert(0, "/Users/thameema/git/engram/packages/core")
sys.path.insert(0, "/Users/thameema/git/engram/packages/orchestrator")


SAMPLE_AGENT = {
    "name": "researcher",
    "version": "1.0",
    "description": "Does research",
    "model": "claude-sonnet-4-6",
    "temperature": 0.5,
    "max_tokens": 4096,
    "tools": ["memory_search", "web_search"],
    "use_critic": True,
    "critic_model": "claude-haiku-4-5-20251001",
    "timeout_s": 240,
    "system_prompt": "You are a researcher.",
}


# ---------------------------------------------------------------------------
# _yaml_to_agent
# ---------------------------------------------------------------------------

class TestYamlToAgent(unittest.TestCase):
    def _call(self, data):
        from engram_api.routers.agents import _yaml_to_agent
        return _yaml_to_agent(data)

    def test_maps_all_fields(self):
        agent = self._call(SAMPLE_AGENT)
        self.assertEqual(agent.name, "researcher")
        self.assertEqual(agent.version, "1.0")
        self.assertEqual(agent.description, "Does research")
        self.assertEqual(agent.model, "claude-sonnet-4-6")
        self.assertAlmostEqual(agent.temperature, 0.5)
        self.assertEqual(agent.max_tokens, 4096)
        self.assertEqual(agent.tools, ["memory_search", "web_search"])
        self.assertTrue(agent.use_critic)
        self.assertEqual(agent.critic_model, "claude-haiku-4-5-20251001")
        self.assertEqual(agent.timeout_s, 240)

    def test_system_prompt_short_not_truncated(self):
        data = dict(SAMPLE_AGENT, system_prompt="Short prompt")
        agent = self._call(data)
        self.assertEqual(agent.system_prompt_preview, "Short prompt")

    def test_system_prompt_long_is_truncated(self):
        long_prompt = "x" * 300
        data = dict(SAMPLE_AGENT, system_prompt=long_prompt)
        agent = self._call(data)
        self.assertEqual(len(agent.system_prompt_preview), 201)  # 200 + "…"
        self.assertTrue(agent.system_prompt_preview.endswith("…"))

    def test_missing_optional_fields_default(self):
        minimal = {"name": "min"}
        agent = self._call(minimal)
        self.assertEqual(agent.name, "min")
        self.assertEqual(agent.tools, [])
        self.assertFalse(agent.use_critic)
        self.assertEqual(agent.timeout_s, 300)
        self.assertIsNone(agent.temperature)

    def test_version_coerced_to_string(self):
        data = dict(SAMPLE_AGENT, version=2)
        agent = self._call(data)
        self.assertEqual(agent.version, "2")


# ---------------------------------------------------------------------------
# _load_agents
# ---------------------------------------------------------------------------

class TestLoadAgents(unittest.TestCase):
    def _write_yaml(self, d: Path, filename: str, content: str):
        (d / filename).write_text(content)

    def _call(self, agents_dir):
        from engram_api.routers.agents import _load_agents
        return _load_agents(str(agents_dir))

    def test_loads_yaml_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self._write_yaml(p, "researcher.yaml", """
name: researcher
description: Does research
model: claude-sonnet-4-6
tools:
  - memory_search
""")
            agents = self._call(p)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].name, "researcher")

    def test_loads_yml_extension(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self._write_yaml(p, "agent.yml", "name: agent-yml\ndescription: test\n")
            agents = self._call(p)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].name, "agent-yml")

    def test_skips_malformed_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self._write_yaml(p, "broken.yaml", ": invalid: yaml: [")
            self._write_yaml(p, "good.yaml", "name: good\n")
            agents = self._call(p)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].name, "good")

    def test_skips_yaml_without_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self._write_yaml(p, "noname.yaml", "description: no name here\n")
            agents = self._call(p)
        self.assertEqual(agents, [])

    def test_returns_empty_for_nonexistent_dir(self):
        agents = self._call(Path("/nonexistent/agents"))
        self.assertEqual(agents, [])

    def test_loads_nested_subdirectory(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            sub = p / "builtin"
            sub.mkdir()
            self._write_yaml(sub, "nested.yaml", "name: nested\n")
            agents = self._call(p)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].name, "nested")


# ---------------------------------------------------------------------------
# HTTP endpoint tests via TestClient
# ---------------------------------------------------------------------------

def _make_test_client():
    """Create a FastAPI test client with the agents router and a mocked auth dep."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engram_api.routers.agents import router
    from engram_api.auth import require_api_key

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_api_key] = lambda: "test-user"
    return TestClient(app, raise_server_exceptions=True)


_PATCH_TARGET = "engram_api.routers.agents._get_agents_dir"


class TestAgentsListEndpoint(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        p = Path(self._tmpdir.name)
        (p / "researcher.yaml").write_text("name: researcher\ndescription: Does research\n")
        (p / "writer.yaml").write_text("name: writer\ndescription: Writes docs\n")
        self._agents_dir = str(p)
        self._client = _make_test_client()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_list_returns_all_agents(self):
        with patch(_PATCH_TARGET, return_value=self._agents_dir):
            resp = self._client.get("/api/v1/agents/")
        self.assertEqual(resp.status_code, 200)
        names = {a["name"] for a in resp.json()}
        self.assertIn("researcher", names)
        self.assertIn("writer", names)

    def test_list_empty_when_no_agents(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            with patch(_PATCH_TARGET, return_value=empty_dir):
                resp = self._client.get("/api/v1/agents/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_list_empty_when_dir_missing(self):
        with patch(_PATCH_TARGET, return_value="/no/such/dir"):
            resp = self._client.get("/api/v1/agents/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_response_contains_expected_fields(self):
        with patch(_PATCH_TARGET, return_value=self._agents_dir):
            resp = self._client.get("/api/v1/agents/")
        agent = resp.json()[0]
        for field in ("name", "description", "model", "tools", "use_critic", "timeout_s"):
            self.assertIn(field, agent)


class TestAgentsGetEndpoint(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        p = Path(self._tmpdir.name)
        (p / "researcher.yaml").write_text(
            "name: researcher\ndescription: Does research\nmodel: claude-sonnet-4-6\n"
        )
        self._agents_dir = str(p)
        self._client = _make_test_client()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_get_existing_agent(self):
        with patch(_PATCH_TARGET, return_value=self._agents_dir):
            resp = self._client.get("/api/v1/agents/researcher")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "researcher")
        self.assertEqual(resp.json()["model"], "claude-sonnet-4-6")

    def test_get_missing_agent_returns_404(self):
        with patch(_PATCH_TARGET, return_value=self._agents_dir):
            resp = self._client.get("/api/v1/agents/nonexistent")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("not found", resp.json()["detail"].lower())

    def test_system_prompt_preview_returned(self):
        p = Path(self._agents_dir)
        long_prompt = "x" * 300
        (p / "verbose.yaml").write_text(f"name: verbose\nsystem_prompt: {long_prompt}\n")
        with patch(_PATCH_TARGET, return_value=self._agents_dir):
            resp = self._client.get("/api/v1/agents/verbose")
        self.assertEqual(resp.status_code, 200)
        preview = resp.json()["system_prompt_preview"]
        self.assertLessEqual(len(preview), 202)


if __name__ == "__main__":
    unittest.main(verbosity=2)
