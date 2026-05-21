"""
engram_orchestrator.workers.openrouter_worker — OpenRouter API tool-calling agent.

Uses httpx directly (not the openai SDK) to avoid version conflicts.
OpenRouter uses the same request format as OpenAI's chat completions API.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from .base import BaseWorker

logger = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_MAX_ITERATIONS = 20
_REQUEST_TIMEOUT = 120.0

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format, which OpenRouter accepts)
# ---------------------------------------------------------------------------

_MEMORY_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": (
            "Search persistent memory for relevant information. "
            "Use this to recall past task outcomes, heuristics, patterns, or facts."
        ),
        "parameters": {
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
    },
}

_MEMORY_WRITE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "memory_write",
        "description": (
            "Persist a piece of information to memory so it can be recalled in future sessions."
        ),
        "parameters": {
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
    },
}

_WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current information.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string.",
                },
            },
            "required": ["query"],
        },
    },
}

_DEFAULT_TOOLS = [_MEMORY_SEARCH_TOOL, _MEMORY_WRITE_TOOL, _WEB_SEARCH_TOOL]

_DEFAULT_SYSTEM = (
    "You are a capable AI assistant with access to persistent memory. "
    "Use memory_search to recall relevant context before answering. "
    "Use memory_write to persist important findings or outcomes. "
    "Complete the user's task thoroughly and return a clear, complete response."
)


class OpenRouterWorker(BaseWorker):
    """Worker that calls the OpenRouter API using httpx (no openai SDK dependency)."""

    def __init__(
        self,
        api_key: str,
        model: str,
        engram_client: Any,  # EngramClient
        namespace: str,
        tools: list[dict] | None = None,
        site_url: str = "https://github.com/engram",
        site_name: str = "engram",
    ) -> None:
        self.worker_id = str(uuid.uuid4())
        self._api_key = api_key
        self._model = model
        self._engram_client = engram_client
        self._namespace = namespace
        self._tools = tools if tools is not None else _DEFAULT_TOOLS
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": site_url,
            "X-Title": site_name,
        }

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _dispatch_tool(self, name: str, arguments_str: str) -> str:
        """Parse tool arguments and dispatch to the appropriate handler."""
        try:
            inputs: dict[str, Any] = json.loads(arguments_str)
        except json.JSONDecodeError:
            inputs = {}

        match name:
            case "memory_search":
                query = inputs.get("query", "")
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
                content = inputs.get("content", "")
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
    # HTTP helper
    # ------------------------------------------------------------------

    async def _chat_completion(self, messages: list[dict]) -> dict[str, Any]:
        """Call OpenRouter chat completions endpoint and return the response dict."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": self._tools,
            "tool_choice": "auto",
        }
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{_OPENROUTER_BASE_URL}/chat/completions",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, task_prompt: str, system_prompt: str | None = None) -> str:
        """Run the OpenRouter tool-calling agent loop until stop or max iterations."""
        system = system_prompt or _DEFAULT_SYSTEM
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task_prompt},
        ]

        for iteration in range(_MAX_ITERATIONS):
            logger.debug(
                "OpenRouterWorker[%s] iteration=%d model=%s",
                self.worker_id[:8],
                iteration,
                self._model,
            )

            response = await self._chat_completion(messages)
            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "stop")

            # Add the assistant message to history
            messages.append(message)

            if finish_reason == "tool_calls":
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    # Malformed response — treat as done
                    break

                # Execute each tool call and build tool result messages
                for tc in tool_calls:
                    tool_name = tc.get("function", {}).get("name", "")
                    arguments_str = tc.get("function", {}).get("arguments", "{}")
                    tool_call_id = tc.get("id", "")

                    result_str = await self._dispatch_tool(tool_name, arguments_str)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result_str,
                        }
                    )
                continue

            # stop / length / content_filter or any non-tool finish
            content = message.get("content") or ""
            return content

        # Max iterations reached
        logger.warning(
            "OpenRouterWorker[%s] reached max iterations (%d)",
            self.worker_id[:8],
            _MAX_ITERATIONS,
        )
        # Return any final text content from the last assistant message
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if content:
                    return content
        return "Task incomplete: max iterations reached."
