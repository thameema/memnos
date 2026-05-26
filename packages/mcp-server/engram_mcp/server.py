"""
engram_mcp.server — MCP server definition and entry point.

Creates an mcp.server.Server instance, registers all 10 tools, and
routes incoming tool calls to the appropriate handler functions.

Entry points
------------
create_mcp_server(client, orchestrator, config) -> Server
    Instantiate and register tools; call this from transport modules.

main()
    CLI entry point: load config, start services, run stdio or SSE.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Sequence

import yaml
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)

from engram_mcp.tools.graph import (
    handle_get_entity,
    handle_get_related,
    handle_graph_query,
)
from engram_mcp.tools.memory import (
    _dt_to_iso,
    handle_memory_delete,
    handle_memory_review_due,
    handle_memory_search,
    handle_memory_write,
)
from engram_mcp.tools.orchestrator_tools import (
    handle_add_heuristic,
    handle_get_heuristics,
    handle_get_task_result,
    handle_list_agents,
    handle_list_tasks,
    handle_spawn_task,
    handle_trigger_reflection,
)
from engram_mcp.tools.vault import (
    handle_secret_set,
    handle_secret_get,
    handle_secret_list,
    handle_secret_rotate,
    handle_vault_audit,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill pack registry (populated by create_mcp_server at startup)
# ---------------------------------------------------------------------------

_SKILL_PACK_HANDLERS: dict[str, Any] = {}  # tool name → WebhookHandler
_EXTERNAL_TOOLS: list[Tool] = []           # Tool objects from loaded packs


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="memory_search",
        description=(
            "Search engram persistent memory using vector similarity, knowledge-graph "
            "traversal, or a hybrid of both. Returns ranked memory entries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query"},
                "namespace": {"type": "string", "description": "Engram namespace to search"},
                "top_k": {"type": "integer", "default": 10, "description": "Max results to return"},
                "mode": {
                    "type": "string",
                    "enum": ["hybrid", "vector", "graph"],
                    "default": "hybrid",
                    "description": "Search mode",
                },
            },
            "required": ["query", "namespace"],
        },
    ),
    Tool(
        name="memory_write",
        description=(
            "Write a new entry to engram persistent memory. "
            "Use memory_type='decision' for architectural decisions with rationale, "
            "'constraint' for rules AI agents must always follow (injected into every search), "
            "'incident' for production issues and RCAs, 'skill' for technique tips."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Memory content to store"},
                "namespace": {"type": "string", "description": "Target namespace"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional classification tags",
                },
                "source": {
                    "type": "string",
                    "default": "agent",
                    "description": "Source identifier (e.g. 'agent', 'user', 'system')",
                },
                "metadata": {
                    "type": "object",
                    "description": "Arbitrary metadata key-value pairs",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "decision", "constraint", "incident", "adr", "skill"],
                    "default": "fact",
                    "description": "Semantic type: 'constraint' memories are always injected into search results",
                },
                "status": {
                    "type": "string",
                    "enum": ["active", "proposed", "superseded", "deprecated"],
                    "default": "active",
                    "description": "Lifecycle status of this memory",
                },
                "author": {
                    "type": "string",
                    "default": "",
                    "description": "Who recorded this (user_id, team name, or tool identifier)",
                },
                "affects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Entity names this decision/constraint governs (creates AFFECTS graph edges)",
                },
                "rationale": {
                    "type": "string",
                    "default": "",
                    "description": "WHY — the reasoning behind a decision or constraint",
                },
                "provenance": {
                    "type": "object",
                    "description": "Chain of custody: who/what/where this memory originated",
                    "properties": {
                        "agent_id": {"type": "string", "default": ""},
                        "user_id": {"type": "string", "default": ""},
                        "tool": {"type": "string", "default": ""},
                        "git_commit": {"type": "string", "default": ""},
                        "jira_ticket": {"type": "string", "default": ""},
                        "team": {"type": "string", "default": ""},
                    },
                },
            },
            "required": ["content", "namespace"],
        },
    ),
    Tool(
        name="memory_delete",
        description="Delete a specific memory entry by its ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "UUID of the memory to delete"},
                "namespace": {"type": "string", "description": "Namespace that owns the memory"},
            },
            "required": ["memory_id", "namespace"],
        },
    ),
    Tool(
        name="graph_query",
        description=(
            "Execute a read-only Cypher query against engram's knowledge graph (Neo4j). "
            "Use MATCH/RETURN patterns only — mutations are rejected."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cypher": {"type": "string", "description": "Read-only Cypher query"},
                "namespace": {"type": "string", "description": "Graph namespace scope"},
                "params": {
                    "type": "object",
                    "description": "Optional Cypher query parameters",
                },
            },
            "required": ["cypher", "namespace"],
        },
    ),
    Tool(
        name="get_entity",
        description=(
            "Retrieve a named entity from the knowledge graph together with its "
            "related entities and relationships up to a configurable depth."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Entity name to look up"},
                "namespace": {"type": "string", "description": "Graph namespace scope"},
                "depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "Traversal depth for related entities",
                },
            },
            "required": ["name", "namespace"],
        },
    ),
    Tool(
        name="get_related",
        description=(
            "Return the adjacency list of relationships for a named entity in the "
            "knowledge graph without the full entity record."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Entity name"},
                "namespace": {"type": "string", "description": "Graph namespace scope"},
                "depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "Traversal depth",
                },
            },
            "required": ["entity_name", "namespace"],
        },
    ),
    Tool(
        name="spawn_task",
        description=(
            "Fork a background worker task via the engram orchestrator. "
            "The task runs asynchronously; use get_task_result to collect the output."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task instruction / prompt"},
                "namespace": {"type": "string", "description": "Namespace context for the task"},
                "runtime": {
                    "type": "string",
                    "default": "api",
                    "description": "Runtime mode: 'api' (Anthropic API), 'openrouter', or 'claudecode'",
                },
                "agent": {
                    "type": "string",
                    "description": "Optional agent definition name to bind this task to",
                },
                "timeout_s": {
                    "type": "integer",
                    "default": 300,
                    "description": "Task timeout in seconds",
                },
            },
            "required": ["prompt", "namespace"],
        },
    ),
    Tool(
        name="get_task_result",
        description="Retrieve the result (or current status) of a spawned background task.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID returned by spawn_task"},
                "wait": {
                    "type": "boolean",
                    "default": False,
                    "description": "Block up to 30 s waiting for the task to finish",
                },
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="list_tasks",
        description="List tasks in a namespace, optionally filtered by status.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to query"},
                "status": {
                    "type": "string",
                    "default": "ALL",
                    "description": "Filter by status: ALL | PENDING | RUNNING | COMPLETED | FAILED",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of tasks to return",
                },
            },
            "required": ["namespace"],
        },
    ),
    Tool(
        name="get_heuristics",
        description=(
            "Retrieve learned heuristic rules for a namespace. "
            "Heuristics are distilled from past agent behaviour via the reflection pipeline."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to query"},
                "query": {
                    "type": "string",
                    "description": "Optional keyword filter for heuristic rules",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum rules to return",
                },
            },
            "required": ["namespace"],
        },
    ),
    Tool(
        name="add_heuristic",
        description="Manually add a heuristic rule to the engram learning store.",
        inputSchema={
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The heuristic rule text"},
                "namespace": {"type": "string", "description": "Target namespace"},
                "rationale": {
                    "type": "string",
                    "default": "",
                    "description": "Why this rule was added",
                },
                "applies_to_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags that scope this rule's applicability",
                },
            },
            "required": ["rule", "namespace"],
        },
    ),
    Tool(
        name="trigger_reflection",
        description=(
            "Trigger the engram reflection agent to distil heuristics and skills "
            "from recent episodic memory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to reflect on"},
                "lookback_days": {
                    "type": "integer",
                    "default": 7,
                    "description": "Number of days of history to analyse",
                },
            },
            "required": ["namespace"],
        },
    ),
    Tool(
        name="list_agents",
        description=(
            "List available agent definitions from the agents directory "
            "(ENGRAM_AGENTS_DIR env var, default ./agents). "
            "Each entry includes name, description, model, and tool list."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": "Optional substring filter on agent name or description",
                },
            },
            "required": [],
        },
    ),
    # ---- vault tools ----
    Tool(
        name="vault_secret_set",
        description=(
            "Store (or replace) an encrypted secret in the engram vault. "
            "The value is envelope-encrypted with AES-256-GCM and never stored in plaintext. "
            "Use this for API keys, tokens, passwords, and other credentials."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key_name": {"type": "string", "description": "Unique name for the secret (e.g. 'openai_api_key')"},
                "value": {"type": "string", "description": "Secret plaintext value to encrypt and store"},
                "namespace": {"type": "string", "description": "Vault namespace (same hierarchy as memory namespaces)"},
                "secret_type": {
                    "type": "string",
                    "enum": ["api_key", "token", "password", "certificate", "webhook", "other"],
                    "default": "api_key",
                    "description": "Category of secret",
                },
                "note": {"type": "string", "default": "", "description": "Human-readable note about this secret"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional classification tags",
                },
            },
            "required": ["key_name", "value", "namespace"],
        },
    ),
    Tool(
        name="vault_secret_get",
        description=(
            "Retrieve and decrypt a secret from the engram vault. "
            "Returns the plaintext value. Access is audit-logged."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key_name": {"type": "string", "description": "Name of the secret to retrieve"},
                "namespace": {"type": "string", "description": "Vault namespace that owns the secret"},
            },
            "required": ["key_name", "namespace"],
        },
    ),
    Tool(
        name="vault_secret_list",
        description=(
            "List secrets in a vault namespace. "
            "Returns metadata only (key_name, type, description, tags) — never plaintext values."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Vault namespace to list"},
            },
            "required": ["namespace"],
        },
    ),
    Tool(
        name="vault_secret_rotate",
        description=(
            "Rotate a secret by replacing its value. "
            "The old ciphertext is superseded and a new DEK is generated. "
            "History is preserved in the audit log."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key_name": {"type": "string", "description": "Name of the secret to rotate"},
                "new_value": {"type": "string", "description": "New plaintext value"},
                "namespace": {"type": "string", "description": "Vault namespace that owns the secret"},
            },
            "required": ["key_name", "new_value", "namespace"],
        },
    ),
    Tool(
        name="vault_audit",
        description=(
            "Retrieve the immutable audit log for a vault namespace. "
            "Shows who accessed or modified secrets and when. Requires vault_admin permission."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Vault namespace to audit"},
                "limit": {"type": "integer", "default": 100, "description": "Maximum entries to return"},
            },
            "required": ["namespace"],
        },
    ),
    # Skill Coach tools (Tier 1 + 3.5 v2)
    Tool(
        name="skill_suggest",
        description=(
            "Find relevant tool capabilities for what you are trying to do. "
            "Searches across all seeded tool catalogs (claude-code, gh, docker, kubectl) "
            "or a specific tool via tool_filter. Surfaces techniques you may not know about — "
            "based on your task description. Call this when starting a task."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What you are trying to accomplish (natural language)",
                },
                "top_k": {
                    "type": "integer",
                    "default": 3,
                    "description": "Number of skill suggestions to return",
                },
                "tool_filter": {
                    "type": "string",
                    "description": "Restrict to a specific tool: claude-code, gh, docker, kubectl",
                },
                "include_team_skills": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also search org_namespace for team-authored skills",
                },
                "org_namespace": {
                    "type": "string",
                    "description": "Namespace for team-authored skills (required when include_team_skills=true)",
                },
            },
            "required": ["task"],
        },
    ),
    Tool(
        name="skill_discover",
        description=(
            "Seed or refresh tool capability catalogs in engram. "
            "Pass tool= to seed a specific tool (claude-code, gh, docker, kubectl), "
            "or omit to seed all available catalogs at once. "
            "Populates tool:{name}:capabilities namespaces with searchable skill memories."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "description": "Tool to seed (claude-code, gh, docker, kubectl). Omit to seed all.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="skill_author",
        description=(
            "Author and publish a custom team skill into a shared namespace. "
            "Team skills are searchable by skill_suggest (set include_team_skills=true). "
            "Use this to capture org-specific workflows, conventions, or runbooks."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short skill title"},
                "content": {"type": "string", "description": "Full skill content / runbook"},
                "when_to_use": {"type": "string", "description": "Natural language description of when this skill applies (used for semantic search)"},
                "example": {"type": "string", "description": "Concrete usage example"},
                "category": {"type": "string", "description": "Category label, e.g. 'workflow', 'deployment', 'debugging'"},
                "namespace": {"type": "string", "description": "Namespace to publish into (e.g. org:myteam:skills)"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for filtering",
                },
            },
            "required": ["title", "content", "when_to_use", "namespace"],
        },
    ),
    # ---- Feature 2.4: memory review due ----
    Tool(
        name="memory_review_due",
        description="List memories past their review_by date that need human confirmation or deprecation. Use this at session start to surface stale decisions.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to check"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["namespace"],
        },
    ),
    # ---- Feature 2.1: namespace subscriptions ----
    Tool(
        name="namespace_subscribe",
        description="Subscribe to receive new memories from a namespace. After subscribing, use namespace_feed to poll for updates. Set delivery_namespace for push fan-out, or delivery_mode=webhook with webhook_url to receive HTTP POST notifications on every new memory.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Source namespace to watch"},
                "subscriber_id": {"type": "string", "description": "Your user or agent ID"},
                "filter_types": {"type": "array", "items": {"type": "string"}, "description": "Memory types to filter to (empty = all)"},
                "delivery_namespace": {"type": "string", "description": "If set, new memories are auto-copied here (push fan-out)"},
                "delivery_mode": {"type": "string", "description": "Delivery mode: 'cursor' (poll via namespace_feed), 'webhook' (HTTP POST to webhook_url), or 'immediate' (reserved). Default: cursor."},
                "webhook_url": {"type": "string", "description": "HTTPS endpoint to POST new memories to (required when delivery_mode=webhook)"},
            },
            "required": ["namespace", "subscriber_id"],
        },
    ),
    Tool(
        name="namespace_feed",
        description="Poll for new memories in a subscribed namespace since your last check. Returns new memories and advances your read cursor automatically.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "subscriber_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["namespace", "subscriber_id"],
        },
    ),
    # ---- Feature 3.4: community search ----
    Tool(
        name="community_search",
        description=(
            "Find the entity cluster (community) that a concept belongs to, and see what other "
            "entities are in the same community. Useful for broad 'what else is related to X?' "
            "questions without knowing the exact relationship."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name to look up"},
                "namespace": {"type": "string"},
            },
            "required": ["entity", "namespace"],
        },
    ),
    # ---- Feature: automatic past-incident retrieval ----
    Tool(
        name="incident_context",
        description=(
            "Retrieve past incidents similar to a given alert description. "
            "Use this when a new alert fires to answer 'have we seen this before?' — "
            "returns the most similar past incidents with content and metadata, "
            "ranked by semantic similarity. Ideal for oncall triage context injection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Alert description or symptom text to match against past incidents",
                },
                "namespace": {"type": "string", "description": "Namespace to search"},
                "top_k": {"type": "integer", "default": 5, "description": "Max incidents to return"},
                "threshold": {
                    "type": "number",
                    "default": 0.75,
                    "description": "Minimum similarity score (0.0–1.0)",
                },
            },
            "required": ["content", "namespace"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

def create_mcp_server(client, orchestrator, config) -> Server:
    """
    Build and return an mcp.server.Server with all tools registered.

    Parameters
    ----------
    client       : EngramClient (already started)
    orchestrator : Orchestrator (already started)
    config       : EngramConfig (for metadata / future auth)
    """
    global _EXTERNAL_TOOLS

    # Load external skill packs (non-fatal; bad packs are logged and skipped)
    try:
        from engram_mcp.skill_packs import load_skill_packs  # noqa: PLC0415

        known = {t.name for t in TOOLS}
        entries = load_skill_packs(known_names=known)
        _EXTERNAL_TOOLS = []
        for entry in entries:
            _SKILL_PACK_HANDLERS[entry.tool.name] = entry.handler
            _EXTERNAL_TOOLS.append(entry.tool)
    except Exception as exc:
        logger.warning("Skill pack loading failed (non-fatal): %s", exc)

    server = Server("engram")

    # ------------------------------------------------------------------ #
    # list_tools                                                           #
    # ------------------------------------------------------------------ #
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS + _EXTERNAL_TOOLS

    # ------------------------------------------------------------------ #
    # call_tool                                                            #
    # ------------------------------------------------------------------ #
    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
        logger.debug("call_tool | name=%s args_keys=%s", name, list(arguments.keys()))

        result = await _dispatch(name, arguments, client, orchestrator)

        import json
        text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
        return [TextContent(type="text", text=text)]

    return server


async def _dispatch(
    name: str,
    args: dict[str, Any],
    client,
    orchestrator,
) -> Any:
    """Route a tool call to the correct handler function."""

    # ---- memory tools ----
    if name == "memory_search":
        return await handle_memory_search(
            client,
            query=args["query"],
            namespace=args["namespace"],
            top_k=int(args.get("top_k", 10)),
            mode=str(args.get("mode", "hybrid")),
        )

    if name == "memory_write":
        prov_raw = args.get("provenance")
        prov_dict = prov_raw if isinstance(prov_raw, dict) else {}
        result = await handle_memory_write(
            client,
            content=args["content"],
            namespace=args["namespace"],
            tags=args.get("tags"),
            source=str(args.get("source", "agent")),
            metadata=args.get("metadata"),
            memory_type=str(args.get("memory_type", "fact")),
            status=str(args.get("status", "active")),
            author=str(args.get("author", "")),
            affects=args.get("affects"),
            rationale=str(args.get("rationale", "")),
            provenance=prov_dict,
        )
        return _dt_to_iso(result)

    if name == "memory_delete":
        return await handle_memory_delete(
            client,
            memory_id=args["memory_id"],
            namespace=args["namespace"],
        )

    # ---- graph tools ----
    if name == "graph_query":
        return await handle_graph_query(
            client,
            cypher=args["cypher"],
            namespace=args["namespace"],
            params=args.get("params"),
        )

    if name == "get_entity":
        return await handle_get_entity(
            client,
            name=args["name"],
            namespace=args["namespace"],
            depth=int(args.get("depth", 2)),
        )

    if name == "get_related":
        return await handle_get_related(
            client,
            entity_name=args["entity_name"],
            namespace=args["namespace"],
            depth=int(args.get("depth", 2)),
        )

    # ---- orchestrator tools ----
    if name == "spawn_task":
        return await handle_spawn_task(
            orchestrator,
            prompt=args["prompt"],
            namespace=args["namespace"],
            runtime=str(args.get("runtime", "api")),
            agent=args.get("agent"),
            timeout_s=int(args.get("timeout_s", 300)),
        )

    if name == "get_task_result":
        return await handle_get_task_result(
            orchestrator,
            task_id=args["task_id"],
            wait=bool(args.get("wait", False)),
        )

    if name == "list_tasks":
        return await handle_list_tasks(
            orchestrator,
            namespace=args["namespace"],
            status=str(args.get("status", "ALL")),
            limit=int(args.get("limit", 20)),
        )

    # ---- learning / heuristics tools ----
    if name == "get_heuristics":
        return await handle_get_heuristics(
            namespace=args["namespace"],
            query=args.get("query"),
            limit=int(args.get("limit", 20)),
        )

    if name == "add_heuristic":
        return await handle_add_heuristic(
            namespace=args["namespace"],
            rule=args["rule"],
            rationale=str(args.get("rationale", "")),
            applies_to_tags=args.get("applies_to_tags"),
        )

    if name == "trigger_reflection":
        return await handle_trigger_reflection(
            namespace=args["namespace"],
            lookback_days=int(args.get("lookback_days", 7)),
        )

    # ---- agent discovery ----
    if name == "list_agents":
        return await handle_list_agents(filter=args.get("filter"))

    # ---- vault tools ----
    if name == "vault_secret_set":
        return await handle_secret_set(
            client,
            key_name=args["key_name"],
            value=args["value"],
            namespace=args["namespace"],
            secret_type=str(args.get("secret_type", "api_key")),
            note=str(args.get("note", "")),
            tags=args.get("tags"),
        )

    if name == "vault_secret_get":
        return await handle_secret_get(
            client,
            key_name=args["key_name"],
            namespace=args["namespace"],
        )

    if name == "vault_secret_list":
        return await handle_secret_list(
            client,
            namespace=args["namespace"],
        )

    if name == "vault_secret_rotate":
        return await handle_secret_rotate(
            client,
            key_name=args["key_name"],
            new_value=args["new_value"],
            namespace=args["namespace"],
        )

    if name == "vault_audit":
        return await handle_vault_audit(
            client,
            namespace=args["namespace"],
            limit=int(args.get("limit", 100)),
        )

    # ---- skill coach tools ----
    if name == "skill_suggest":
        from engram.skill_coach.suggester import suggest_skills
        suggestions = await suggest_skills(
            client,
            task_description=args["task"],
            top_k=int(args.get("top_k", 3)),
            tool_filter=args.get("tool_filter") or None,
            include_team_skills=bool(args.get("include_team_skills", False)),
            org_namespace=args.get("org_namespace") or None,
        )
        if not suggestions:
            text = (
                "No skills found. Run skill_discover first to seed the capability catalog.\n"
                "Tip: use skill_discover with no arguments to populate all tool catalogs."
            )
        else:
            tool_label = args.get("tool_filter") or "all tools"
            lines = [f"Found {len(suggestions)} relevant skill(s) for your task [{tool_label}]:\n"]
            for i, s in enumerate(suggestions, 1):
                tool_tag = f" ({s['tool']})" if s.get("tool") else ""
                lines.append(f"{i}. {s['title']} [{s['category']}]{tool_tag}  (relevance: {s['relevance_score']})")
                if s.get("example"):
                    lines.append(f"   Example: {s['example']}")
                if s.get("tip"):
                    lines.append(f"   {s['tip']}")
                lines.append("")
            text = "\n".join(lines)
        return [TextContent(type="text", text=text)]

    if name == "skill_discover":
        from engram.skill_coach.seeder import seed_tool_capabilities, seed_claude_code_capabilities
        from engram.skill_coach.capabilities import TOOL_CAPABILITY_CATALOGS
        tool_arg = args.get("tool") or None
        if tool_arg:
            result = await seed_tool_capabilities(client, tool_arg)
            text = (
                f"Tool '{tool_arg}' catalog seeded: {result['added']} added, "
                f"{result['updated']} updated, {result['skipped']} unchanged.\n"
                f"Use skill_suggest with tool_filter='{tool_arg}' to find relevant techniques."
            )
        else:
            totals = {"added": 0, "updated": 0, "skipped": 0}
            for t in TOOL_CAPABILITY_CATALOGS:
                r = await seed_tool_capabilities(client, t)
                for k in totals:
                    totals[k] += r[k]
            text = (
                f"All tool catalogs seeded ({', '.join(TOOL_CAPABILITY_CATALOGS)}): "
                f"{totals['added']} added, {totals['updated']} updated, {totals['skipped']} unchanged.\n"
                f"Use skill_suggest to surface relevant techniques for your tasks."
            )
        return [TextContent(type="text", text=text)]

    if name == "skill_author":
        import hashlib as _hashlib
        from engram.models import MemoryType, MemoryStatus
        title = args["title"]
        content = args["content"]
        when_to_use = args["when_to_use"]
        example = args.get("example", "")
        category = args.get("category", "workflow")
        namespace = args["namespace"]
        tags = args.get("tags") or []
        skill_id = f"team-{_hashlib.sha256(title.encode()).hexdigest()[:12]}"
        full_content = (
            f"SKILL_ID:{skill_id}\n"
            f"TITLE: {title}\n"
            f"CATEGORY: {category}\n"
            f"WHEN TO USE: {when_to_use}\n"
            f"EXAMPLE: {example}\n\n"
            f"{content}"
        )
        content_h = _hashlib.sha256(content.encode()).hexdigest()[:16]
        memory = await client.add(
            content=full_content,
            namespace=namespace,
            tags=["skill-coach", "team-skill"] + tags,
            source="skill-author",
            metadata={
                "skill_id": skill_id,
                "title": title,
                "category": category,
                "content_hash": content_h,
                "tool": "team",
            },
            memory_type=MemoryType.skill,
            status=MemoryStatus.active,
            author="skill-author",
            rationale=when_to_use,
        )
        text = (
            f"Team skill authored: '{title}' (id: {skill_id})\n"
            f"Namespace: {namespace}\n"
            f"Memory id: {memory.id}\n"
            f"Searchable via skill_suggest with include_team_skills=true, org_namespace='{namespace}'"
        )
        return [TextContent(type="text", text=text)]

    # ---- Feature 2.4: memory review due ----
    if name == "memory_review_due":
        text = await handle_memory_review_due(
            client,
            namespace=args["namespace"],
            limit=int(args.get("limit", 20)),
        )
        return [TextContent(type="text", text=text)]

    # ---- Feature 2.1: namespace subscriptions ----
    if name == "namespace_subscribe":
        delivery_mode = args.get("delivery_mode") or "cursor"
        webhook_url = args.get("webhook_url") or ""
        sub_id = await client.subscribe(
            subscriber_id=args["subscriber_id"],
            namespace=args["namespace"],
            filter_types=args.get("filter_types") or [],
            delivery_namespace=args.get("delivery_namespace") or "",
            delivery_mode=delivery_mode,
            webhook_url=webhook_url,
        )
        import json as _json
        result_obj = {
            "subscribed": True,
            "namespace": args["namespace"],
            "subscriber_id": args["subscriber_id"],
            "delivery_mode": delivery_mode,
        }
        if args.get("delivery_namespace"):
            result_obj["delivery_namespace"] = args["delivery_namespace"]
            result_obj["fan_out"] = True
        if webhook_url:
            result_obj["webhook_url"] = webhook_url
        return [TextContent(type="text", text=_json.dumps(result_obj))]

    if name == "namespace_feed":
        memories, cursor = await client.get_feed(
            subscriber_id=args["subscriber_id"],
            namespace=args["namespace"],
            limit=int(args.get("limit", 20)),
        )
        import json as _json
        items = [
            {
                "id": str(m.id),
                "content": m.content,
                "namespace": m.namespace,
                "memory_type": m.memory_type.value if hasattr(m.memory_type, "value") else str(m.memory_type),
                "author": m.author,
                "created_at": m.created_at.isoformat() if hasattr(m.created_at, "isoformat") else str(m.created_at),
                "tags": list(m.tags or []),
            }
            for m in memories
        ]
        return [TextContent(type="text", text=_json.dumps({
            "items": items,
            "cursor": cursor,
            "count": len(items),
        }, indent=2))]

    # ---- past-incident retrieval ----
    if name == "incident_context":
        import json as _json
        pairs = await client.get_past_incidents(
            content=args["content"],
            namespace=args["namespace"],
            top_k=int(args.get("top_k", 5)),
            threshold=float(args.get("threshold", 0.75)),
        )
        if not pairs:
            return [TextContent(type="text", text="No similar past incidents found.")]
        lines = [f"Found {len(pairs)} similar past incident(s):\n"]
        for i, (mem, score) in enumerate(pairs, 1):
            created = mem.created_at.isoformat() if hasattr(mem.created_at, "isoformat") else str(mem.created_at)
            severity = (mem.metadata or {}).get("severity", "UNKNOWN")
            lines.append(f"--- {i}. Similarity: {score:.2f}  Severity: {severity}  Created: {created} ---")
            lines.append(mem.content)
            lines.append("")
        return [TextContent(type="text", text="\n".join(lines))]

    # ---- Feature 3.4: community search ----
    if name == "community_search":
        community = await client._arcadedb.get_entity_community(
            args["entity"].lower(), args["namespace"]
        )
        if not community:
            return [TextContent(
                type="text",
                text=(
                    f"No community found for '{args['entity']}'. "
                    "Run engram-community detect to build communities."
                ),
            )]
        lines = [
            f"Community: {community['label']}",
            f"Members ({community['member_count']}): {', '.join(community['member_names'][:10])}",
        ]
        return [TextContent(type="text", text="\n".join(lines))]

    # ---- external skill pack tools ----
    if name in _SKILL_PACK_HANDLERS:
        from engram_mcp.skill_packs import call_webhook_handler  # noqa: PLC0415

        result_text = await call_webhook_handler(_SKILL_PACK_HANDLERS[name], name, args)
        return [TextContent(type="text", text=result_text)]

    raise ValueError(f"Unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config(config_path: str):
    """
    Load EngramConfig from a YAML file with ${VAR} env-var expansion.

    Falls back to a minimal in-process config object if the core package
    is not importable (e.g. during isolated MCP server testing).
    """
    try:
        from engram.config import EngramConfig  # type: ignore

        return EngramConfig.from_yaml(config_path)
    except ImportError:
        # Minimal stand-in so the server still starts
        logger.warning("engram.config not importable; using raw YAML dict")
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        return _DictConfig(raw)


class _DictConfig:
    """Minimal dot-access wrapper around a plain dict for when engram-core is absent."""

    def __init__(self, data: dict) -> None:
        self._data = data or {}
        for k, v in self._data.items():
            if isinstance(v, dict):
                setattr(self, k, _DictConfig(v))
            elif isinstance(v, list):
                setattr(self, k, [_DictConfig(i) if isinstance(i, dict) else i for i in v])
            else:
                setattr(self, k, v)


async def _start_services(config):
    """Instantiate and start EngramClient + Orchestrator."""
    from engram.client import EngramClient  # type: ignore
    from engram_orchestrator.orchestrator import Orchestrator  # type: ignore

    client = EngramClient(config)
    await client.start()

    from engram_orchestrator.task_store import TaskStore  # type: ignore
    orchestrator = Orchestrator(config=config, engram_client=client, task_store=TaskStore())
    await orchestrator.start()

    return client, orchestrator


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Main entry point for the ``engram-mcp`` CLI command.

    Transport selection (in priority order):
      1. ``--transport sse`` / ``--transport stdio`` CLI flag
      2. ``ENGRAM_TRANSPORT`` environment variable
      3. Default: stdio
    """
    parser = argparse.ArgumentParser(description="engram MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=None,
        help="Transport to use (stdio or sse). Overrides ENGRAM_TRANSPORT env var.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to engram YAML config file. Overrides ENGRAM_CONFIG env var.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Host to bind SSE server (overrides config.server.host)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for SSE server (overrides config.server.mcp_port)",
    )
    args = parser.parse_args()

    config_path = args.config or os.environ.get("ENGRAM_CONFIG", "engram.yaml")
    transport = args.transport or os.environ.get("ENGRAM_TRANSPORT", "stdio")

    # Resolve logging level early
    log_level = os.environ.get("ENGRAM_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    if transport == "sse":
        from engram_mcp.transports.sse import run_sse_server  # noqa: PLC0415

        host = args.host or os.environ.get("ENGRAM_HOST", None)
        port = args.port or int(os.environ.get("ENGRAM_PORT", "0")) or None
        asyncio.run(run_sse_server(config_path=config_path, host=host, port=port))
    else:
        from engram_mcp.transports.stdio import run  # noqa: PLC0415

        asyncio.run(run(config_path=config_path))
