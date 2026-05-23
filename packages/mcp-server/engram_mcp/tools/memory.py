"""
engram_mcp.tools.memory — MCP tool handlers for persistent memory operations.

Handlers
--------
handle_memory_search  : vector/graph/hybrid similarity search
handle_memory_write   : store a new memory entry
handle_memory_delete  : remove a memory entry by id
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_CONTENT_PREVIEW_LEN = 120


def _dt_to_iso(value: Any) -> Any:
    """Recursively convert datetime objects to ISO-8601 strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _dt_to_iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dt_to_iso(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def handle_memory_search(
    client,
    query: str,
    namespace: str,
    top_k: int = 10,
    mode: str = "hybrid",
) -> dict:
    """
    Search persistent memory.

    Parameters
    ----------
    client    : EngramClient instance
    query     : natural-language query string
    namespace : engram namespace to search within
    top_k     : maximum number of results to return (default 10)
    mode      : "hybrid" | "vector" | "graph"

    Returns
    -------
    {"results": [...], "total": N}
    """
    logger.debug(
        "memory_search | ns=%s mode=%s top_k=%d query=%r",
        namespace,
        mode,
        top_k,
        query[:_CONTENT_PREVIEW_LEN],
    )

    try:
        raw_results = await client.search(
            query=query, namespace=namespace, top_k=top_k, mode=mode
        )
    except Exception as exc:
        logger.warning("memory_search failed: %s", exc)
        return f"No memories found for query: {query!r} in namespace {namespace!r} (search error: {exc})"

    if not raw_results:
        return f"No memories found for query: {query!r} in namespace {namespace!r}"

    _MAX_CONTENT = 400  # chars per result — keeps total response under ~6KB for top_k=10

    lines: list[str] = [f"Found {len(raw_results)} memories for {query!r}:\n"]
    for i, r in enumerate(raw_results, 1):
        memory = r.memory if hasattr(r, "memory") else r
        score = float(getattr(r, "score", 0.0))
        tags = list(memory.tags) if memory.tags else []
        tag_str = f"  tags: {', '.join(tags)}" if tags else ""
        created = memory.created_at.isoformat() if isinstance(memory.created_at, datetime) else str(memory.created_at)
        content = str(memory.content or "")
        snippet = content[:_MAX_CONTENT] + ("…" if len(content) > _MAX_CONTENT else "")
        lines.append(f"{i}. [score: {score:.2f}]{tag_str}")
        lines.append(f"   {snippet}")
        lines.append(f"   id: {memory.id}  created: {created}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

async def handle_memory_write(
    client,
    content: str,
    namespace: str,
    tags: list[str] | None = None,
    source: str = "agent",
    metadata: dict | None = None,
) -> dict:
    """
    Store a new memory entry.

    Returns
    -------
    {"id": str, "namespace": str, "created_at": str}
    """
    logger.debug(
        "memory_write | ns=%s source=%s tags=%s content=%r",
        namespace,
        source,
        tags,
        content[:_CONTENT_PREVIEW_LEN],
    )

    memory = await client.add(
        content=content,
        namespace=namespace,
        tags=tags or [],
        source=source,
        metadata=metadata or {},
    )

    return {
        "id": str(memory.id),
        "namespace": namespace,
        "created_at": memory.created_at.isoformat()
        if isinstance(memory.created_at, datetime)
        else str(memory.created_at),
    }


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

async def handle_memory_delete(
    client,
    memory_id: str,
    namespace: str,
) -> dict:
    """
    Delete a memory entry by ID.

    Returns
    -------
    {"deleted": bool, "memory_id": str}
    """
    logger.debug("memory_delete | ns=%s id=%s", namespace, memory_id)

    deleted = await client.delete(memory_id, namespace)

    return {
        "deleted": bool(deleted) if deleted is not None else False,
        "memory_id": memory_id,
    }
