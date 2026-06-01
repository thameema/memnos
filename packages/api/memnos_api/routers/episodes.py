"""
memnos_api.routers.episodes — Immutable raw-input layer (Feature 2).

An Episode is a write-once, verbatim record of what was submitted to memnos.
Every Memory that memnos derives from that input carries a `source_episode_ids`
list pointing back to the Episode(s) it came from. This gives us:

  • A reproducible audit trail (CMS, SOC 2, HITRUST, FDA) — every derived
    fact can be traced to the raw input that produced it.
  • A re-derivation path — if a better extractor / LLM is released later,
    we can re-process the Episodes without having to re-collect the data.
  • PHI / PII review — the original input is always available for redaction
    review against current rules.

Endpoints
---------
POST   /episodes/          — write a new Episode (verbatim input)
GET    /episodes/          — list Episodes in a namespace
GET    /episodes/{id}      — fetch one Episode by ID
GET    /episodes/{id}/memories — list Memories derived from this Episode

There is intentionally NO PUT, PATCH, or DELETE on /episodes/{id}.
Episodes are append-only by design — that is the whole point of the feature.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from memnos.models import Episode, Provenance
from memnos_api.auth import (
    check_namespace_access,
    get_client,
    require_api_key,
    require_api_key_entry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/episodes", tags=["episodes"])


class EpisodeWriteRequest(BaseModel):
    """POST body for creating an Episode."""
    content: str = Field(..., min_length=1, description="Verbatim text of the input")
    namespace: str = Field(..., min_length=1)
    source: str = Field("api", description="api | file | voice | webhook | mcp")
    metadata: dict[str, Any] = Field(default_factory=dict)


class EpisodeResponse(BaseModel):
    """Single Episode envelope."""
    id: str
    content: str
    namespace: str
    created_at: str
    source: str
    author: str
    metadata: dict[str, Any]
    provenance: dict[str, Any]


def _episode_to_response(ep: Episode) -> EpisodeResponse:
    return EpisodeResponse(
        id=ep.id,
        content=ep.content,
        namespace=ep.namespace,
        created_at=ep.created_at.isoformat(),
        source=ep.source,
        author=ep.author,
        metadata=ep.metadata or {},
        provenance=ep.provenance.model_dump() if ep.provenance else {},
    )


# ---------------------------------------------------------------------------
# POST /episodes/  — create a new Episode (write-only; no updates ever)
# ---------------------------------------------------------------------------
@router.post("/", response_model=EpisodeResponse, status_code=201)
async def create_episode(
    payload: EpisodeWriteRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
):
    await check_namespace_access(key_entry, payload.namespace, operation="write")
    ep = Episode(
        content=payload.content,
        namespace=payload.namespace,
        source=payload.source,
        author=user_id,
        metadata=payload.metadata or {},
        provenance=Provenance(user_id=user_id, tool="api"),
    )
    await client._arcadedb.insert_episode(ep)
    logger.info("Episode created: id=%s namespace=%s by=%s", ep.id, ep.namespace, user_id)
    return _episode_to_response(ep)


# ---------------------------------------------------------------------------
# GET /episodes/  — list episodes in a namespace
# ---------------------------------------------------------------------------
@router.get("/", response_model=list[EpisodeResponse])
async def list_episodes(
    ns: str = Query(..., description="Namespace to list episodes for"),
    limit: int = Query(50, ge=1, le=500),
    skip: int = Query(0, ge=0),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
):
    await check_namespace_access(key_entry, ns, operation="read")
    episodes = await client._arcadedb.list_episodes(ns, limit=limit, skip=skip)
    return [_episode_to_response(e) for e in episodes]


# ---------------------------------------------------------------------------
# GET /episodes/{id} — fetch a single Episode
# ---------------------------------------------------------------------------
@router.get("/{episode_id}", response_model=EpisodeResponse)
async def get_episode(
    episode_id: str,
    ns: str = Query(..., description="Namespace the episode lives in"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
):
    await check_namespace_access(key_entry, ns, operation="read")
    ep = await client._arcadedb.get_episode(episode_id, ns)
    if ep is None:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found in {ns}")
    return _episode_to_response(ep)


# ---------------------------------------------------------------------------
# GET /episodes/{id}/memories — list derived Memories
# ---------------------------------------------------------------------------
@router.get("/{episode_id}/memories")
async def list_episode_memories(
    episode_id: str,
    ns: str = Query(..., description="Namespace"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
):
    await check_namespace_access(key_entry, ns, operation="read")
    memories = await client._arcadedb.list_memories_for_episode(episode_id, ns)
    return [
        {
            "id": m.id,
            "content": m.content,
            "memory_type": m.memory_type.value if hasattr(m.memory_type, "value") else str(m.memory_type),
            "namespace": m.namespace,
            "created_at": m.created_at.isoformat(),
            "source_episode_ids": list(getattr(m, "source_episode_ids", []) or []),
        }
        for m in memories
    ]
