"""
engram_api.routers.episodes — Episode CRUD endpoints (Feature 2).

Episodes are named containers that group related memories — e.g. a work session,
a conversation thread, or a project sprint.

Endpoints
---------
POST   /episodes/                          — create a new episode
GET    /episodes/                          — list episodes in a namespace
GET    /episodes/{id}                      — get an episode + its memories
PATCH  /episodes/{id}                      — update title / summary / tags
POST   /episodes/{id}/close               — mark episode closed
POST   /episodes/{id}/memories/{mem_id}   — link an existing memory to an episode
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from engram.models import Episode
from engram_api.auth import (
    check_namespace_access,
    get_client,
    require_api_key,
    require_api_key_entry,
)
from engram_api.schemas import (
    EpisodeCreateRequest,
    EpisodeResponse,
    EpisodeUpdateRequest,
    EpisodeWithMemoriesResponse,
    MemoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/episodes", tags=["episodes"])


def _episode_to_response(ep: Episode) -> EpisodeResponse:
    return EpisodeResponse(
        id=ep.id,
        title=ep.title,
        namespace=ep.namespace,
        summary=ep.summary,
        tags=list(ep.tags or []),
        created_at=ep.created_at,
        closed_at=ep.closed_at,
        is_open=ep.is_open,
    )


def _memory_to_response(mem) -> MemoryResponse:
    prov = mem.provenance.model_dump() if mem.provenance and hasattr(mem.provenance, "model_dump") else {}
    return MemoryResponse(
        id=str(mem.id),
        content=mem.content,
        namespace=mem.namespace,
        created_at=mem.created_at,
        tags=list(mem.tags or []),
        memory_type=mem.memory_type.value if hasattr(mem.memory_type, "value") else str(mem.memory_type),
        author=getattr(mem, "author", ""),
        affects=list(getattr(mem, "affects", None) or []),
        rationale=getattr(mem, "rationale", "") or "",
        provenance=prov,
        episode_ids=list(getattr(mem, "episode_ids", None) or []),
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@router.post("/", response_model=EpisodeResponse, status_code=201)
async def create_episode(
    req: EpisodeCreateRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> EpisodeResponse:
    """Create a new episode in the given namespace."""
    await check_namespace_access(key_entry, req.namespace, operation="write")
    episode = Episode(title=req.title, namespace=req.namespace, summary=req.summary, tags=req.tags)
    try:
        await client.create_episode(episode)
    except Exception as exc:
        logger.exception("Failed to create episode: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _episode_to_response(episode)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[EpisodeResponse])
async def list_episodes(
    ns: str = Query(..., description="Namespace to list episodes for"),
    limit: int = Query(50, ge=1, le=200),
    include_closed: bool = Query(True),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> list[EpisodeResponse]:
    """List episodes in a namespace, newest first."""
    await check_namespace_access(key_entry, ns)
    try:
        episodes = await client.list_episodes(ns, limit=limit, include_closed=include_closed)
    except Exception as exc:
        logger.exception("Failed to list episodes: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [_episode_to_response(ep) for ep in episodes]


# ---------------------------------------------------------------------------
# Get with memories
# ---------------------------------------------------------------------------

@router.get("/{episode_id}", response_model=EpisodeWithMemoriesResponse)
async def get_episode(
    episode_id: str,
    ns: str = Query(..., description="Namespace the episode belongs to"),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> EpisodeWithMemoriesResponse:
    """Fetch an episode and all memories linked to it."""
    await check_namespace_access(key_entry, ns)
    try:
        episode = await client.get_episode(episode_id, ns)
    except Exception as exc:
        logger.exception("Failed to get episode: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if episode is None:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id!r} not found")

    try:
        memories = await client.get_episode_memories(episode_id, ns, limit=limit)
    except Exception as exc:
        logger.warning("Failed to fetch episode memories (non-fatal): %s", exc)
        memories = []

    return EpisodeWithMemoriesResponse(
        **_episode_to_response(episode).model_dump(),
        memories=[_memory_to_response(m) for m in memories],
    )


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

@router.patch("/{episode_id}", response_model=EpisodeResponse)
async def update_episode(
    episode_id: str,
    req: EpisodeUpdateRequest,
    ns: str = Query(..., description="Namespace the episode belongs to"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> EpisodeResponse:
    """Update an episode's title, summary, or tags."""
    await check_namespace_access(key_entry, ns, operation="write")
    try:
        ok = await client.update_episode(
            episode_id, ns,
            title=req.title,
            summary=req.summary,
            tags=req.tags,
        )
    except Exception as exc:
        logger.exception("Failed to update episode: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id!r} not found")

    episode = await client.get_episode(episode_id, ns)
    if episode is None:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id!r} not found")
    return _episode_to_response(episode)


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

@router.post("/{episode_id}/close", response_model=EpisodeResponse)
async def close_episode(
    episode_id: str,
    ns: str = Query(..., description="Namespace the episode belongs to"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> EpisodeResponse:
    """Mark an episode as closed (sets closed_at to now)."""
    await check_namespace_access(key_entry, ns, operation="write")
    try:
        ok = await client.close_episode(episode_id, ns)
    except Exception as exc:
        logger.exception("Failed to close episode: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id!r} not found")

    episode = await client.get_episode(episode_id, ns)
    if episode is None:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id!r} not found")
    return _episode_to_response(episode)


# ---------------------------------------------------------------------------
# Link memory to episode
# ---------------------------------------------------------------------------

@router.post("/{episode_id}/memories/{memory_id}", status_code=204, response_model=None)
async def link_memory(
    episode_id: str,
    memory_id: str,
    ns: str = Query(..., description="Namespace shared by both episode and memory"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> None:
    """Link an existing memory to an episode."""
    await check_namespace_access(key_entry, ns, operation="write")
    try:
        ok = await client.link_memory_to_episode(memory_id, ns, episode_id)
    except Exception as exc:
        logger.exception("Failed to link memory to episode: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id!r} not found in namespace {ns!r}")
