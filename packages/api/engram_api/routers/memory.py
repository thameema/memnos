"""
engram_api.routers.memory — CRUD and search endpoints for persistent memory.

Endpoints
---------
POST   /memory/          — write a memory entry
GET    /memory/search    — full-text / vector / hybrid search
GET    /memory/{id}      — fetch a single memory by ID
DELETE /memory/{id}      — delete a memory entry
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from engram.models import MemoryStatus, MemoryType, Provenance
from engram_api.auth import (
    check_namespace_access,
    get_accessible_namespaces,
    get_client,
    require_api_key,
    require_api_key_entry,
)
from engram_api.schemas import MemoryResponse, MemoryWriteRequest, ReviewDueItem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


def _to_response(memory, score: float | None = None) -> MemoryResponse:
    """Convert a MemoryEntry model to a MemoryResponse."""
    prov_dict = {}
    if memory.provenance:
        prov_dict = memory.provenance.model_dump() if hasattr(memory.provenance, "model_dump") else {}
    return MemoryResponse(
        id=str(memory.id),
        content=memory.content,
        namespace=memory.namespace,
        created_at=memory.created_at,
        tags=list(memory.tags or []),
        score=score,
        memory_type=memory.memory_type.value if hasattr(memory.memory_type, "value") else str(memory.memory_type),
        author=getattr(memory, "author", ""),
        provenance=prov_dict,
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

@router.post("/", response_model=MemoryResponse, status_code=201)
async def write_memory(
    req: MemoryWriteRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> MemoryResponse:
    """Persist a new memory entry to both the vector store and knowledge graph."""
    await check_namespace_access(key_entry, req.namespace, operation="write")
    logger.debug(
        "write_memory | ns=%s user=%s content=%r",
        req.namespace,
        user_id,
        req.content[:120],
    )
    try:
        try:
            mem_type = MemoryType(req.memory_type)
        except ValueError:
            mem_type = MemoryType.fact
        try:
            mem_status = MemoryStatus(req.status)
        except ValueError:
            mem_status = MemoryStatus.active
        memory = await client.add(
            content=req.content,
            namespace=req.namespace,
            tags=req.tags,
            source=req.source,
            metadata=req.metadata,
            memory_type=mem_type,
            status=mem_status,
            author=req.author,
            affects=req.affects,
            rationale=req.rationale,
            provenance=Provenance(**req.provenance.model_dump()),
        )
    except Exception as exc:
        logger.exception("Failed to write memory: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _to_response(memory)


# ---------------------------------------------------------------------------
# Review due (Feature 2.4)
# ---------------------------------------------------------------------------

@router.get("/review-due", response_model=list[ReviewDueItem])
async def review_due(
    ns: str = Query(..., description="Namespace to check"),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> list[ReviewDueItem]:
    """Return memories whose review_by date has passed — needs human review."""
    await check_namespace_access(key_entry, ns)
    memories = await client.get_review_due(ns, limit)
    return [
        ReviewDueItem(
            id=str(m.id),
            content=m.content,
            namespace=m.namespace,
            memory_type=m.memory_type.value if hasattr(m.memory_type, "value") else str(m.memory_type),
            author=m.author,
            review_by=m.review_by,
            created_at=m.created_at,
            tags=list(m.tags or []),
            rationale=m.rationale,
        )
        for m in memories
    ]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@router.get("/search", response_model=list[MemoryResponse])
async def search_memory(
    q: str = Query(..., description="Natural-language search query"),
    ns: str = Query(
        default="all",
        description=(
            "Namespace to search within. "
            "Pass 'all' (or omit) to search all namespaces the API key can access."
        ),
    ),
    top_k: int = Query(10, ge=1, le=100),
    mode: str = Query("hybrid", description="hybrid | vector | graph"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> list[MemoryResponse]:
    """Search memories using vector similarity, graph traversal, or a hybrid of both.

    When ns='all' (the default), the search fans out across every namespace the
    calling API key has read access to. Results are merged and re-ranked by score
    descending before the top_k slice is returned. Each result includes a
    ``namespace`` field so callers know where the memory came from.
    """
    # Normalise: treat missing/empty/whitespace-only/literal "all" as multi-ns search
    _ns_raw = (ns or "").strip()
    is_all = _ns_raw in ("", "all", "*")

    if is_all:
        # Resolve all namespaces from DB, then ACL-filter to what this key can see
        try:
            db_namespaces = await client._arcadedb.list_namespaces()
        except Exception as exc:
            logger.exception("list_namespaces failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        accessible = get_accessible_namespaces(key_entry, db_namespaces)
        accessible_set = set(accessible)
        logger.debug(
            "search_memory | ns=all user=%s mode=%s top_k=%d "
            "db_ns=%d accessible=%d q=%r",
            user_id, mode, top_k,
            len(db_namespaces), len(accessible),
            q[:120],
        )
        if not accessible:
            return []

        # Fix #3 (revised): search once with ns="all" at the ArcadeDB layer —
        # a single vector search across all memories is faster than N separate
        # per-namespace searches. The ACL filter is applied post-search on the
        # result set. We ask for top_k * 4 to ensure enough results survive the
        # namespace filter, then trim back to top_k.
        try:
            results = await client.search(q, "all", top_k * 4, mode=mode)
        except Exception as exc:
            logger.exception("Multi-namespace search failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # ACL post-filter: keep only results whose namespace is accessible
        results = [r for r in results if r.memory.namespace in accessible_set]
        # Re-rank and trim
        results.sort(key=lambda r: r.score, reverse=True)
        results = results[:top_k]
    else:
        # Single-namespace path — existing behaviour
        await check_namespace_access(key_entry, _ns_raw)
        logger.debug(
            "search_memory | ns=%s mode=%s top_k=%d user=%s q=%r",
            _ns_raw, mode, top_k, user_id, q[:120],
        )
        try:
            results = await client.search(q, _ns_raw, top_k, mode=mode)
        except Exception as exc:
            logger.exception("Memory search failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    if results is None:
        return []

    # Score threshold early exit (fix #5): drop results below 0.45 to
    # avoid injecting noise. Pinned memories (score=2.0) always pass through.
    _SCORE_FLOOR = 0.45
    results = [r for r in results if r.score >= _SCORE_FLOOR]

    return [_to_response(r.memory, score=r.score) for r in results]


# ---------------------------------------------------------------------------
# Get by ID
# ---------------------------------------------------------------------------

@router.get("/{memory_id}", response_model=MemoryResponse)
async def get_memory(
    memory_id: str,
    ns: str = Query(..., description="Namespace the memory belongs to"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> MemoryResponse:
    """Fetch a single memory entry by its UUID."""
    await check_namespace_access(key_entry, ns)
    logger.debug("get_memory | id=%s ns=%s user=%s", memory_id, ns, user_id)
    try:
        # EngramClient exposes get_memory(); fall back to get() for compatibility
        get_fn = getattr(client, "get_memory", None) or getattr(client, "get", None)
        if get_fn is None:
            raise HTTPException(status_code=501, detail="Memory get not supported by this client")
        memory = await get_fn(memory_id, ns)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch memory %s: %s", memory_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if memory is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id!r} not found")

    return _to_response(memory)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@router.delete("/{memory_id}", status_code=204, response_model=None)
async def delete_memory(
    memory_id: str,
    ns: str = Query(..., description="Namespace the memory belongs to"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> None:
    """Permanently delete a memory entry from both vector and graph stores."""
    await check_namespace_access(key_entry, ns, operation="write")
    logger.debug("delete_memory | id=%s ns=%s user=%s", memory_id, ns, user_id)
    try:
        deleted = await client.delete(memory_id, ns)
    except Exception as exc:
        logger.exception("Failed to delete memory %s: %s", memory_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id!r} not found")
