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
    handle_memory_delete,
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

logger = logging.getLogger(__name__)

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
        description="Write a new entry to engram persistent memory.",
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
    server = Server("engram")

    # ------------------------------------------------------------------ #
    # list_tools                                                           #
    # ------------------------------------------------------------------ #
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

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
        return await handle_memory_write(
            client,
            content=args["content"],
            namespace=args["namespace"],
            tags=args.get("tags"),
            source=str(args.get("source", "agent")),
            metadata=args.get("metadata"),
        )

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

    raise ValueError(f"Unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config(config_path: str):
    """
    Load EngramConfig from a YAML file.

    Falls back to a minimal in-process config object if the core package
    is not importable (e.g. during isolated MCP server testing).
    """
    try:
        from engram.config import EngramConfig  # type: ignore

        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        return EngramConfig(**raw)
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

    orchestrator = Orchestrator(client=client, config=config)
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
