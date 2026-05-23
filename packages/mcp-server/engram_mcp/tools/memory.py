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
_MAX_CONTENT = 4000  # chars per result — full document for small vaults; truncates very large ones


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
    Formatted text results with CONSTRAINT memories always prepended.
    """
    logger.debug(
        "memory_search | ns=%s mode=%s top_k=%d query=%r",
        namespace,
        mode,
        top_k,
        query[:_CONTENT_PREVIEW_LEN],
    )

    # Inject active CONSTRAINT memories first — they bypass score competition
    constraint_memories: list = []
    try:
        constraint_memories = await client.get_constraints(namespace)
    except Exception as exc:
        logger.debug("get_constraints skipped: %s", exc)

    try:
        raw_results = await client.search(
            query=query, namespace=namespace, top_k=top_k, mode=mode
        )
    except Exception as exc:
        logger.warning("memory_search failed: %s", exc)
        return f"No memories found for query: {query!r} in namespace {namespace!r} (search error: {exc})"

    if not raw_results and not constraint_memories:
        return f"No memories found for query: {query!r} in namespace {namespace!r}"

    lines: list[str] = []

    # Prepend constraints as a prominent governance block
    if constraint_memories:
        lines.append(f"⚠ ACTIVE CONSTRAINTS for namespace {namespace!r} ({len(constraint_memories)} rules — always enforced):\n")
        for c in constraint_memories:
            content = str(c.content or "")
            snippet = content[:_MAX_CONTENT] + ("…" if len(content) > _MAX_CONTENT else "")
            author_str = f"  author: {c.author}" if c.author else ""
            lines.append(f"  CONSTRAINT: {snippet}{author_str}")
            lines.append(f"  id: {c.id}\n")
        lines.append("")

    if raw_results:
        lines.append(f"Found {len(raw_results)} memories for {query!r}:\n")
        for i, r in enumerate(raw_results, 1):
            memory = r.memory if hasattr(r, "memory") else r
            score = float(getattr(r, "score", 0.0))
            tags = list(memory.tags) if memory.tags else []
            tag_str = f"  tags: {', '.join(tags)}" if tags else ""
            mem_type = getattr(memory.memory_type, "value", str(memory.memory_type)) if hasattr(memory, "memory_type") else "fact"
            type_str = f"  type: {mem_type}" if mem_type != "fact" else ""
            author_str = f"  author: {memory.author}" if getattr(memory, "author", "") else ""
            created = memory.created_at.isoformat() if isinstance(memory.created_at, datetime) else str(memory.created_at)
            content = str(memory.content or "")
            snippet = content[:_MAX_CONTENT] + ("…" if len(content) > _MAX_CONTENT else "")
            lines.append(f"{i}. [score: {score:.2f}]{type_str}{tag_str}{author_str}")
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
    memory_type: str = "fact",
    status: str = "active",
    author: str = "",
    affects: list[str] | None = None,
    rationale: str = "",
) -> dict:
    """
    Store a new memory entry.

    Parameters
    ----------
    memory_type : "fact" | "decision" | "constraint" | "incident" | "adr" | "skill"
    status      : "active" | "proposed" | "superseded" | "deprecated"
    author      : who is recording this (user_id, team name, or tool)
    affects     : list of entity names this decision/constraint governs
    rationale   : WHY — the reasoning behind a decision or constraint

    Returns
    -------
    {"id": str, "namespace": str, "created_at": str, "memory_type": str}
    """
    from engram.models import MemoryType, MemoryStatus
    logger.debug(
        "memory_write | ns=%s type=%s source=%s tags=%s content=%r",
        namespace,
        memory_type,
        source,
        tags,
        content[:_CONTENT_PREVIEW_LEN],
    )

    try:
        mem_type = MemoryType(memory_type)
    except ValueError:
        mem_type = MemoryType.fact
    try:
        mem_status = MemoryStatus(status)
    except ValueError:
        mem_status = MemoryStatus.active

    memory = await client.add(
        content=content,
        namespace=namespace,
        tags=tags or [],
        source=source,
        metadata=metadata or {},
        memory_type=mem_type,
        status=mem_status,
        author=author,
        affects=affects or [],
        rationale=rationale,
    )

    return {
        "id": str(memory.id),
        "namespace": namespace,
        "memory_type": mem_type.value,
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
