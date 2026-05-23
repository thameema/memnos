"""
engram_orchestrator.workers.api_worker — Anthropic API tool-calling agent loop.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import anthropic

from .base import BaseWorker

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 20

# ---------------------------------------------------------------------------
# Tool schemas (Anthropic tool format)
# ---------------------------------------------------------------------------

_MEMORY_SEARCH_TOOL: dict[str, Any] = {
    "name": "memory_search",
    "description": (
        "Search the user's private knowledge base (imported notes, documents, past sessions). "
        "This is the ONLY source of truth — always call this before answering any question. "
        "Results are the user's own content, not general knowledge."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query.",
            },
            "namespace": {
                "type": "string",
                "description": "Engram namespace to search within.",
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of results to return (default 10).",
                "default": 10,
            },
        },
        "required": ["query", "namespace"],
    },
}

_MEMORY_WRITE_TOOL: dict[str, Any] = {
    "name": "memory_write",
    "description": (
        "Persist a piece of information to memory so it can be recalled in future sessions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The content to store in memory.",
            },
            "namespace": {
                "type": "string",
                "description": "Engram namespace to write into.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of tags to attach to this memory.",
            },
        },
        "required": ["content", "namespace"],
    },
}

_WEB_SEARCH_TOOL: dict[str, Any] = {
    "name": "web_search",
    "description": "Search the web for current information.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string.",
            },
        },
        "required": ["query"],
    },
}

_DEFAULT_TOOLS = [_MEMORY_SEARCH_TOOL, _MEMORY_WRITE_TOOL, _WEB_SEARCH_TOOL]

_DEFAULT_SYSTEM = (
    "You are a personal AI assistant backed by a private knowledge base. "
    "CRITICAL RULE: You MUST call memory_search FIRST on every request, before writing any response. "
    "Your answers MUST be grounded in what memory_search returns — do NOT answer from your training data. "
    "If memory_search returns no relevant results, say exactly: "
    "'I don't have anything about that in your knowledge base.' "
    "Never substitute general knowledge for missing memory results. "
    "Use memory_write to persist important new information the user tells you."
)


class ApiWorker(BaseWorker):
    """Worker that runs an Anthropic API tool-calling agent loop."""

    def __init__(
        self,
        api_key: str,
        model: str,
        engram_client: Any,  # EngramClient
        namespace: str,
        tools: list[dict] | None = None,
    ) -> None:
        self.worker_id = str(uuid.uuid4())
        self._api_key = api_key
        self._model = model
        self._engram_client = engram_client
        self._namespace = namespace
        self._tools = tools if tools is not None else _DEFAULT_TOOLS
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _dispatch_tool(self, name: str, inputs: dict[str, Any]) -> str:
        """Call the appropriate tool handler and return a string result."""
        match name:
            case "memory_search":
                query = inputs["query"]
                namespace = inputs.get("namespace", self._namespace)
                top_k = int(inputs.get("top_k", 10))
                try:
                    results = await self._engram_client.search(
                        query, namespace, top_k, "vector"
                    )
                    if not results:
                        return "No matching memories found."
                    lines = []
                    for r in results:
                        memory = r.memory if hasattr(r, "memory") else r
                        score = float(getattr(r, "score", 0.0))
                        lines.append(f"[score={score:.3f}] {memory.content}")
                    return "\n".join(lines)
                except Exception as exc:
                    logger.warning("memory_search failed: %s", exc)
                    return f"Memory search error: {exc}"

            case "memory_write":
                content = inputs["content"]
                namespace = inputs.get("namespace", self._namespace)
                tags = inputs.get("tags") or []
                try:
                    memory = await self._engram_client.add(
                        content=content,
                        namespace=namespace,
                        tags=tags,
                        source="agent",
                    )
                    return f"Stored memory with id={memory.id}"
                except Exception as exc:
                    logger.warning("memory_write failed: %s", exc)
                    return f"Memory write error: {exc}"

            case "web_search":
                return "Web search not available in this configuration."

            case _:
                return f"Unknown tool: {name}"

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, task_prompt: str, system_prompt: str | None = None) -> str:
        """Run the tool-calling agent loop until end_turn or max iterations."""
        system = system_prompt or _DEFAULT_SYSTEM
        messages: list[dict[str, Any]] = [{"role": "user", "content": task_prompt}]

        for iteration in range(_MAX_ITERATIONS):
            logger.debug(
                "ApiWorker[%s] iteration=%d model=%s",
                self.worker_id[:8],
                iteration,
                self._model,
            )

            response = await self._client.messages.create(
                model=self._model,
                max_tokens=8096,
                system=system,
                tools=self._tools,
                messages=messages,
            )

            # Collect the assistant turn
            assistant_content: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                # Extract the final text response
                for block in response.content:
                    if block.type == "text":
                        return block.text
                # No text block — return empty string
                return ""

            if response.stop_reason == "tool_use":
                # Execute all tool calls and build tool_result turn
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if block.type == "tool_use":
                        result_str = await self._dispatch_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_str,
                            }
                        )
                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason — break and return whatever text we have
            logger.warning(
                "ApiWorker unexpected stop_reason=%s", response.stop_reason
            )
            for block in response.content:
                if block.type == "text":
                    return block.text
            break

        # Forced stop after max iterations
        logger.warning(
            "ApiWorker[%s] reached max iterations (%d), forcing stop",
            self.worker_id[:8],
            _MAX_ITERATIONS,
        )
        # Return the last text content we have, if any
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                content = msg["content"]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block["text"]
                elif isinstance(content, str):
                    return content
        return "Task incomplete: max iterations reached."
