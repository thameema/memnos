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

    raw_results = await client.search(query, namespace, top_k, mode)

    if raw_results is None:
        raw_results = []

    serialised = []
    for r in raw_results:
        memory = r.memory if hasattr(r, "memory") else r
        serialised.append(
            {
                "id": str(memory.id),
                "content": memory.content,
                "score": float(getattr(r, "score", 0.0)),
                "source": str(getattr(r, "source", getattr(memory, "source", "unknown"))),
                "created_at": memory.created_at.isoformat() if isinstance(memory.created_at, datetime) else str(memory.created_at),
                "tags": list(memory.tags) if memory.tags else [],
            }
        )

    return {"results": serialised, "total": len(serialised)}


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
