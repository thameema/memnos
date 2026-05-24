"""
Test suite for Feature 3.5 — Skill Coach v2.

Coverage:
- TOOL_CAPABILITY_CATALOGS registry (gh, docker, kubectl, claude-code)
- seed_tool_capabilities() — generic seeder, idempotency, unknown tool error
- suggest_skills() — multi-namespace, tool_filter, include_team_skills, dedup, top_k
- MCP server tool definitions — skill_suggest v2, skill_discover v2, skill_author
- MCP handlers — tool_filter routing, seed all, seed specific, author flow
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

@dataclass
class FakeMemory:
    id: str
    content: str
    metadata: dict = field(default_factory=dict)
    namespace: str = ""


@dataclass
class FakeResult:
    memory: FakeMemory
    score: float = 0.8


def make_client(search_results: dict[str, list[FakeResult]] | None = None):
    """Build a mock engram client with configurable per-namespace search results."""
    client = MagicMock()
    search_results = search_results or {}

    async def _search(query, namespace, top_k=3, mode="hybrid"):
        return search_results.get(namespace, [])

    async def _add(**kwargs):
        mem = FakeMemory(
            id="mem-123",
            content=kwargs.get("content", ""),
            metadata=kwargs.get("metadata", {}),
            namespace=kwargs.get("namespace", ""),
        )
        return mem

    async def _supersede(memory_id, namespace):
        pass

    client.search = AsyncMock(side_effect=_search)
    client.add = AsyncMock(side_effect=_add)
    client.supersede = AsyncMock(side_effect=_supersede)
    return client


# ===========================================================================
# 1. TOOL_CAPABILITY_CATALOGS
# ===========================================================================

class TestToolCapabilityCatalogs:
    def test_all_tools_present(self):
        from engram.skill_coach.capabilities import TOOL_CAPABILITY_CATALOGS
        assert "claude-code" in TOOL_CAPABILITY_CATALOGS
        assert "gh" in TOOL_CAPABILITY_CATALOGS
        assert "docker" in TOOL_CAPABILITY_CATALOGS
        assert "kubectl" in TOOL_CAPABILITY_CATALOGS

    def test_gh_capabilities_count(self):
        from engram.skill_coach.capabilities import GH_CAPABILITIES
        assert len(GH_CAPABILITIES) >= 5

    def test_docker_capabilities_count(self):
        from engram.skill_coach.capabilities import DOCKER_CAPABILITIES
        assert len(DOCKER_CAPABILITIES) >= 5

    def test_kubectl_capabilities_count(self):
        from engram.skill_coach.capabilities import KUBECTL_CAPABILITIES
        assert len(KUBECTL_CAPABILITIES) >= 5

    def test_each_entry_has_required_fields(self):
        from engram.skill_coach.capabilities import TOOL_CAPABILITY_CATALOGS
        required = {"id", "title", "category", "when_to_use", "example", "content"}
        for tool, caps in TOOL_CAPABILITY_CATALOGS.items():
            for cap in caps:
                missing = required - cap.keys()
                assert not missing, f"{tool}/{cap.get('id')}: missing {missing}"

    def test_all_ids_unique_per_catalog(self):
        from engram.skill_coach.capabilities import TOOL_CAPABILITY_CATALOGS
        for tool, caps in TOOL_CAPABILITY_CATALOGS.items():
            ids = [c["id"] for c in caps]
            assert len(ids) == len(set(ids)), f"Duplicate IDs in {tool}: {ids}"

    def test_gh_has_pr_and_issue_skills(self):
        from engram.skill_coach.capabilities import GH_CAPABILITIES
        ids = {c["id"] for c in GH_CAPABILITIES}
        # At least one PR-related and one issue-related
        assert any("pr" in i or "pull" in i for i in ids)

    def test_docker_has_build_and_run_skills(self):
        from engram.skill_coach.capabilities import DOCKER_CAPABILITIES
        ids = {c["id"] for c in DOCKER_CAPABILITIES}
        assert any("build" in i or "run" in i for i in ids)

    def test_kubectl_has_apply_and_logs_skills(self):
        from engram.skill_coach.capabilities import KUBECTL_CAPABILITIES
        ids = {c["id"] for c in KUBECTL_CAPABILITIES}
        assert any("log" in i or "apply" in i or "get" in i for i in ids)

    def test_catalogs_reference_correct_lists(self):
        from engram.skill_coach.capabilities import (
            TOOL_CAPABILITY_CATALOGS,
            CLAUDE_CODE_CAPABILITIES,
            GH_CAPABILITIES,
            DOCKER_CAPABILITIES,
            KUBECTL_CAPABILITIES,
        )
        assert TOOL_CAPABILITY_CATALOGS["claude-code"] is CLAUDE_CODE_CAPABILITIES
        assert TOOL_CAPABILITY_CATALOGS["gh"] is GH_CAPABILITIES
        assert TOOL_CAPABILITY_CATALOGS["docker"] is DOCKER_CAPABILITIES
        assert TOOL_CAPABILITY_CATALOGS["kubectl"] is KUBECTL_CAPABILITIES


# ===========================================================================
# 2. seed_tool_capabilities()
# ===========================================================================

class TestSeedToolCapabilities:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_seeds_known_tool(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        client = make_client()
        result = self._run(seed_tool_capabilities(client, "gh"))
        assert "added" in result
        assert "updated" in result
        assert "skipped" in result
        assert result["added"] + result["updated"] + result["skipped"] > 0

    def test_seeds_all_four_tools(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        from engram.skill_coach.capabilities import TOOL_CAPABILITY_CATALOGS
        client = make_client()
        for tool in TOOL_CAPABILITY_CATALOGS:
            result = self._run(seed_tool_capabilities(client, tool))
            assert result["added"] >= 0

    def test_uses_correct_namespace(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        client = make_client()
        self._run(seed_tool_capabilities(client, "docker"))
        # All client.add calls should use tool:docker:capabilities namespace
        for c in client.add.call_args_list:
            assert c.kwargs["namespace"] == "tool:docker:capabilities"

    def test_custom_capabilities(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        custom = [{
            "id": "custom-001",
            "title": "Custom Skill",
            "category": "custom",
            "when_to_use": "custom workflow",
            "example": "do something custom",
            "content": "SKILL: custom\nDo something custom.",
            "tags": ["custom"],
        }]
        client = make_client()
        result = self._run(seed_tool_capabilities(client, "my-tool", capabilities=custom))
        assert result["added"] == 1
        call_kwargs = client.add.call_args.kwargs
        assert call_kwargs["namespace"] == "tool:my-tool:capabilities"
        assert call_kwargs["metadata"]["tool"] == "my-tool"

    def test_unknown_tool_raises_value_error(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        client = make_client()
        with pytest.raises(ValueError, match="No built-in catalog"):
            self._run(seed_tool_capabilities(client, "unknown-tool-xyz"))

    def test_error_message_lists_available_tools(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        client = make_client()
        with pytest.raises(ValueError) as exc_info:
            self._run(seed_tool_capabilities(client, "bad-tool"))
        assert "claude-code" in str(exc_info.value)
        assert "gh" in str(exc_info.value)

    def test_idempotent_skip_on_same_content(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        from engram.skill_coach.capabilities import GH_CAPABILITIES
        cap = GH_CAPABILITIES[0]
        content_h = hashlib.sha256(cap["content"].encode()).hexdigest()[:16]
        existing_mem = FakeMemory(
            id="existing-1",
            content=cap["content"],
            metadata={"skill_id": cap["id"], "content_hash": content_h},
        )
        client = make_client(
            search_results={"tool:gh:capabilities": [FakeResult(memory=existing_mem)]}
        )
        result = self._run(seed_tool_capabilities(client, "gh"))
        assert result["skipped"] >= 1

    def test_update_on_changed_content(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        from engram.skill_coach.capabilities import GH_CAPABILITIES
        cap = GH_CAPABILITIES[0]
        existing_mem = FakeMemory(
            id="existing-1",
            content=cap["content"],
            metadata={"skill_id": cap["id"], "content_hash": "old-hash-xxxx"},
        )
        client = make_client(
            search_results={"tool:gh:capabilities": [FakeResult(memory=existing_mem)]}
        )
        result = self._run(seed_tool_capabilities(client, "gh"))
        assert result["updated"] >= 1
        client.supersede.assert_called()

    def test_metadata_includes_tool_field(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        client = make_client()
        self._run(seed_tool_capabilities(client, "kubectl"))
        for c in client.add.call_args_list:
            assert c.kwargs["metadata"]["tool"] == "kubectl"

    def test_tags_include_tool_name(self):
        from engram.skill_coach.seeder import seed_tool_capabilities
        client = make_client()
        self._run(seed_tool_capabilities(client, "docker"))
        for c in client.add.call_args_list:
            assert "docker" in c.kwargs["tags"]

    def test_seed_claude_code_delegates(self):
        from engram.skill_coach.seeder import seed_claude_code_capabilities
        client = make_client()
        result = asyncio.run(seed_claude_code_capabilities(client))
        assert "added" in result


# ===========================================================================
# 3. suggest_skills() multi-namespace
# ===========================================================================

class TestSuggestSkillsV2:
    def _run(self, coro):
        return asyncio.run(coro)

    def _make_result(self, skill_id, title, tool="claude-code", score=0.8):
        mem = FakeMemory(
            id=skill_id,
            content=f"SKILL_ID:{skill_id}\nTITLE: {title}\n\nContent here.",
            metadata={"skill_id": skill_id, "title": title, "category": "test", "tool": tool},
        )
        return FakeResult(memory=mem, score=score)

    def test_searches_all_catalogs_by_default(self):
        from engram.skill_coach.suggester import suggest_skills
        from engram.skill_coach.capabilities import TOOL_CAPABILITY_CATALOGS
        search_results = {
            f"tool:{t}:capabilities": [self._make_result(f"{t}-001", f"{t} skill", tool=t)]
            for t in TOOL_CAPABILITY_CATALOGS
        }
        client = make_client(search_results=search_results)
        suggestions = self._run(suggest_skills(client, "deploy my app"))
        assert len(suggestions) > 0
        # Should have searched all catalog namespaces
        namespaces_searched = {c.kwargs["namespace"] for c in client.search.call_args_list}
        for t in TOOL_CAPABILITY_CATALOGS:
            assert f"tool:{t}:capabilities" in namespaces_searched

    def test_tool_filter_restricts_namespace(self):
        from engram.skill_coach.suggester import suggest_skills
        client = make_client(search_results={
            "tool:gh:capabilities": [self._make_result("gh-001", "gh pr", tool="gh")],
            "tool:kubectl:capabilities": [self._make_result("k-001", "kubectl apply", tool="kubectl")],
        })
        suggestions = self._run(suggest_skills(client, "open pull request", tool_filter="gh"))
        # Only searched gh namespace
        namespaces = {c.kwargs["namespace"] for c in client.search.call_args_list}
        assert namespaces == {"tool:gh:capabilities"}

    def test_explicit_namespaces_param(self):
        from engram.skill_coach.suggester import suggest_skills
        ns1 = "tool:docker:capabilities"
        ns2 = "tool:kubectl:capabilities"
        client = make_client(search_results={
            ns1: [self._make_result("d-001", "docker build", tool="docker")],
            ns2: [self._make_result("k-001", "kubectl logs", tool="kubectl")],
        })
        suggestions = self._run(suggest_skills(client, "container ops", namespaces=[ns1, ns2]))
        namespaces = {c.kwargs["namespace"] for c in client.search.call_args_list}
        assert namespaces == {ns1, ns2}

    def test_include_team_skills_adds_org_namespace(self):
        from engram.skill_coach.suggester import suggest_skills
        org_ns = "org:myteam:skills"
        client = make_client(search_results={
            org_ns: [self._make_result("team-001", "deploy runbook", tool="team", score=0.9)],
        })
        suggestions = self._run(suggest_skills(
            client, "deploy", tool_filter="gh",
            include_team_skills=True, org_namespace=org_ns,
        ))
        namespaces = {c.kwargs["namespace"] for c in client.search.call_args_list}
        assert org_ns in namespaces

    def test_include_team_skills_false_skips_org(self):
        from engram.skill_coach.suggester import suggest_skills
        org_ns = "org:myteam:skills"
        client = make_client()
        self._run(suggest_skills(client, "deploy", tool_filter="gh", include_team_skills=False, org_namespace=org_ns))
        namespaces = {c.kwargs["namespace"] for c in client.search.call_args_list}
        assert org_ns not in namespaces

    def test_deduplication_across_namespaces(self):
        from engram.skill_coach.suggester import suggest_skills
        same_result = self._make_result("dup-001", "same skill", tool="claude-code")
        client = make_client(search_results={
            "tool:claude-code:capabilities": [same_result],
            "tool:gh:capabilities": [same_result],
        })
        suggestions = self._run(suggest_skills(
            client, "task", namespaces=["tool:claude-code:capabilities", "tool:gh:capabilities"]
        ))
        ids = [s["skill_id"] for s in suggestions]
        assert len(ids) == len(set(ids)), "Duplicate skill_ids returned"

    def test_top_k_limits_results(self):
        from engram.skill_coach.suggester import suggest_skills
        results = [self._make_result(f"s-{i}", f"skill {i}", score=1.0 - i * 0.1) for i in range(5)]
        client = make_client(search_results={"tool:claude-code:capabilities": results})
        suggestions = self._run(suggest_skills(client, "anything", namespaces=["tool:claude-code:capabilities"], top_k=2))
        assert len(suggestions) <= 2

    def test_sorted_by_relevance(self):
        from engram.skill_coach.suggester import suggest_skills
        results = [
            self._make_result("low", "low score", score=0.3),
            self._make_result("high", "high score", score=0.95),
            self._make_result("mid", "mid score", score=0.6),
        ]
        client = make_client(search_results={"tool:claude-code:capabilities": results})
        suggestions = self._run(suggest_skills(client, "task", namespaces=["tool:claude-code:capabilities"]))
        scores = [s["relevance_score"] for s in suggestions]
        assert scores == sorted(scores, reverse=True)

    def test_returns_empty_when_no_results(self):
        from engram.skill_coach.suggester import suggest_skills
        client = make_client()
        suggestions = self._run(suggest_skills(client, "task", namespaces=["tool:unknown:capabilities"]))
        assert suggestions == []

    def test_tool_field_in_suggestion(self):
        from engram.skill_coach.suggester import suggest_skills
        client = make_client(search_results={
            "tool:docker:capabilities": [self._make_result("d-001", "docker run", tool="docker")],
        })
        suggestions = self._run(suggest_skills(client, "run container", namespaces=["tool:docker:capabilities"]))
        assert suggestions[0]["tool"] == "docker"

    def test_missing_namespace_does_not_raise(self):
        from engram.skill_coach.suggester import suggest_skills

        async def _search_raises(query, namespace, **kwargs):
            raise Exception("namespace not found")

        client = MagicMock()
        client.search = AsyncMock(side_effect=_search_raises)
        # Should return empty gracefully
        suggestions = self._run(suggest_skills(client, "task", namespaces=["tool:missing:capabilities"]))
        assert suggestions == []


# ===========================================================================
# 4. MCP server — skill tool definitions
# ===========================================================================

class TestMCPSkillToolDefinitions:
    def _get_tools(self):
        from engram_mcp.server import TOOLS
        return {t.name: t for t in TOOLS}

    def test_skill_suggest_tool_exists(self):
        tools = self._get_tools()
        assert "skill_suggest" in tools

    def test_skill_discover_tool_exists(self):
        tools = self._get_tools()
        assert "skill_discover" in tools

    def test_skill_author_tool_exists(self):
        tools = self._get_tools()
        assert "skill_author" in tools

    def test_skill_suggest_has_tool_filter_param(self):
        tools = self._get_tools()
        schema = tools["skill_suggest"].inputSchema
        assert "tool_filter" in schema["properties"]

    def test_skill_suggest_has_include_team_skills_param(self):
        tools = self._get_tools()
        schema = tools["skill_suggest"].inputSchema
        assert "include_team_skills" in schema["properties"]

    def test_skill_suggest_has_org_namespace_param(self):
        tools = self._get_tools()
        schema = tools["skill_suggest"].inputSchema
        assert "org_namespace" in schema["properties"]

    def test_skill_discover_has_tool_param(self):
        tools = self._get_tools()
        schema = tools["skill_discover"].inputSchema
        assert "tool" in schema["properties"]

    def test_skill_discover_tool_not_required(self):
        tools = self._get_tools()
        schema = tools["skill_discover"].inputSchema
        assert "tool" not in schema.get("required", [])

    def test_skill_author_required_fields(self):
        tools = self._get_tools()
        schema = tools["skill_author"].inputSchema
        required = schema.get("required", [])
        assert "title" in required
        assert "content" in required
        assert "when_to_use" in required
        assert "namespace" in required

    def test_skill_author_has_tags_param(self):
        tools = self._get_tools()
        schema = tools["skill_author"].inputSchema
        assert "tags" in schema["properties"]
        assert schema["properties"]["tags"]["type"] == "array"


# ===========================================================================
# 5. MCP handlers
# ===========================================================================

class TestMCPSkillHandlers:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_skill_suggest_with_tool_filter(self):
        from engram_mcp.server import _dispatch

        async def _test():
            client = make_client(search_results={
                "tool:gh:capabilities": [
                    FakeResult(
                        memory=FakeMemory(
                            id="gh-001", content="SKILL_ID:gh-001\nTITLE: gh pr\nCATEGORY: pr\nEXAMPLE: gh pr create\n\nFull content.",
                            metadata={"skill_id": "gh-001", "title": "gh pr", "category": "pr", "tool": "gh"},
                        ),
                        score=0.9,
                    )
                ]
            })
            result = await _dispatch("skill_suggest", {"task": "open PR", "tool_filter": "gh"}, client, None)
            text = result[0].text
            assert "gh" in text.lower()
            namespaces = {c.kwargs["namespace"] for c in client.search.call_args_list}
            assert "tool:gh:capabilities" in namespaces
            assert "tool:kubectl:capabilities" not in namespaces

        self._run(_test())

    def test_skill_suggest_no_results_message(self):
        from engram_mcp.server import _dispatch

        async def _test():
            client = make_client()
            result = await _dispatch("skill_suggest", {"task": "unknown task xyz"}, client, None)
            assert "skill_discover" in result[0].text.lower() or "no skills" in result[0].text.lower()

        self._run(_test())

    def test_skill_discover_seeds_specific_tool(self):
        from engram_mcp.server import _dispatch

        async def _test():
            client = make_client()
            result = await _dispatch("skill_discover", {"tool": "gh"}, client, None)
            text = result[0].text
            assert "gh" in text
            namespaces = {c.kwargs["namespace"] for c in client.add.call_args_list}
            assert all(ns == "tool:gh:capabilities" for ns in namespaces)

        self._run(_test())

    def test_skill_discover_seeds_all_when_no_tool(self):
        from engram_mcp.server import _dispatch
        from engram.skill_coach.capabilities import TOOL_CAPABILITY_CATALOGS

        async def _test():
            client = make_client()
            result = await _dispatch("skill_discover", {}, client, None)
            text = result[0].text
            for t in TOOL_CAPABILITY_CATALOGS:
                assert t in text
            namespaces = {c.kwargs["namespace"] for c in client.add.call_args_list}
            for t in TOOL_CAPABILITY_CATALOGS:
                assert f"tool:{t}:capabilities" in namespaces

        self._run(_test())

    def test_skill_author_creates_memory(self):
        from engram_mcp.server import _dispatch

        async def _test():
            client = make_client()
            result = await _dispatch("skill_author", {
                "title": "Deploy to staging",
                "content": "Run make deploy-staging, then verify with curl.",
                "when_to_use": "deploying to staging environment",
                "example": "make deploy-staging",
                "category": "deployment",
                "namespace": "org:myteam:skills",
                "tags": ["deploy", "staging"],
            }, client, None)
            text = result[0].text
            assert "Deploy to staging" in text
            assert "org:myteam:skills" in text
            client.add.assert_called_once()
            add_kwargs = client.add.call_args.kwargs
            assert add_kwargs["namespace"] == "org:myteam:skills"
            assert "team-skill" in add_kwargs["tags"]
            assert "deploy" in add_kwargs["tags"]

        self._run(_test())

    def test_skill_author_generates_stable_skill_id(self):
        from engram_mcp.server import _dispatch

        async def _test():
            client = make_client()
            await _dispatch("skill_author", {
                "title": "My Skill",
                "content": "Do something.",
                "when_to_use": "always",
                "namespace": "org:test:skills",
            }, client, None)
            call_kwargs = client.add.call_args.kwargs
            meta = call_kwargs["metadata"]
            assert meta["skill_id"].startswith("team-")
            expected = f"team-{hashlib.sha256('My Skill'.encode()).hexdigest()[:12]}"
            assert meta["skill_id"] == expected

        self._run(_test())

    def test_skill_author_content_includes_when_to_use(self):
        from engram_mcp.server import _dispatch

        async def _test():
            client = make_client()
            await _dispatch("skill_author", {
                "title": "Staging Deploy",
                "content": "Content here.",
                "when_to_use": "staging deployment workflow",
                "namespace": "org:team:skills",
            }, client, None)
            content = client.add.call_args.kwargs["content"]
            assert "staging deployment workflow" in content

        self._run(_test())

    def test_skill_suggest_include_team_skills(self):
        from engram_mcp.server import _dispatch

        async def _test():
            org_ns = "org:eng:skills"
            client = make_client(search_results={
                org_ns: [
                    FakeResult(
                        memory=FakeMemory(
                            id="team-001",
                            content="SKILL_ID:team-001\nTITLE: Our Deploy Process\nCATEGORY: deployment\nEXAMPLE: make deploy\n\nDetails.",
                            metadata={"skill_id": "team-001", "title": "Our Deploy Process", "category": "deployment", "tool": "team"},
                        ),
                        score=0.95,
                    )
                ]
            })
            result = await _dispatch("skill_suggest", {
                "task": "deploy my service",
                "include_team_skills": True,
                "org_namespace": org_ns,
                "tool_filter": "gh",
            }, client, None)
            namespaces = {c.kwargs["namespace"] for c in client.search.call_args_list}
            assert org_ns in namespaces

        self._run(_test())
