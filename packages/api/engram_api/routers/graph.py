"""
engram_api.routers.graph — Knowledge-graph query and entity endpoints.

Endpoints
---------
POST  /graph/query          — execute a read-only Cypher query
GET   /graph/entity/{name}  — fetch a named entity + its relations
POST  /graph/fact           — add a temporal subject-predicate-object fact
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from engram_api.auth import (
    check_namespace_access,
    get_client,
    require_api_key,
    require_api_key_entry,
)
from engram_api.schemas import FactRequest, GraphQueryRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])


def _dt_to_iso(value: Any) -> Any:
    """Recursively convert datetime objects to ISO strings for JSON serialisation."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _dt_to_iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dt_to_iso(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Cypher query
# ---------------------------------------------------------------------------

@router.post("/query", response_model=list[dict])
async def graph_query(
    req: GraphQueryRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> list[dict]:
    """
    Execute a read-only Cypher query against the Neo4j knowledge graph.

    Only MATCH statements are permitted; the client-side should enforce this,
    but callers are expected to pass safe, read-only queries.
    """
    await check_namespace_access(key_entry, req.namespace)
    logger.debug(
        "graph_query | ns=%s user=%s cypher=%r params=%s",
        req.namespace,
        user_id,
        req.cypher[:120],
        req.params,
    )
    try:
        results = await client.query_graph(req.cypher, req.namespace, req.params)
    except Exception as exc:
        logger.exception("Graph query failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if results is None:
        return []

    return [_dt_to_iso(r) if isinstance(r, dict) else r for r in results]


# ---------------------------------------------------------------------------
# Entity lookup
# ---------------------------------------------------------------------------

@router.get("/entity/{name}")
async def get_entity(
    name: str,
    ns: str = Query(..., description="Namespace to search in"),
    depth: int = Query(2, ge=1, le=5, description="Relation traversal depth"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Fetch a named entity and its relationships up to *depth* hops."""
    await check_namespace_access(key_entry, ns)
    logger.debug(
        "get_entity | name=%r ns=%s depth=%d user=%s", name, ns, depth, user_id
    )
    try:
        entity = await client.get_entity(name, ns)
        related = await client.get_related(name, ns, depth)
    except Exception as exc:
        logger.exception("get_entity failed for %r: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if entity is None:
        raise HTTPException(status_code=404, detail=f"Entity {name!r} not found in namespace {ns!r}")

    def _serialise_entity(e) -> dict:
        if isinstance(e, dict):
            return _dt_to_iso(e)
        return _dt_to_iso({
            "id": str(getattr(e, "id", "")),
            "name": str(getattr(e, "name", "")),
            "entity_type": str(getattr(e, "entity_type", "")),
            "namespace": str(getattr(e, "namespace", ns)),
            "attributes": dict(getattr(e, "attributes", {})),
            "created_at": getattr(e, "created_at", None),
            "valid_until": getattr(e, "valid_until", None),
        })

    def _serialise_relation(r) -> dict:
        if isinstance(r, dict):
            return _dt_to_iso(r)
        return _dt_to_iso({
            "id": str(getattr(r, "id", "")),
            "source_entity_id": str(getattr(r, "source_entity_id", "")),
            "target_entity_id": str(getattr(r, "target_entity_id", "")),
            "relation_type": str(getattr(r, "relation_type", "")),
            "namespace": str(getattr(r, "namespace", ns)),
            "weight": float(getattr(r, "weight", 1.0)),
            "created_at": getattr(r, "created_at", None),
            "valid_until": getattr(r, "valid_until", None),
            "attributes": dict(getattr(r, "attributes", {})),
        })

    relations: list[dict] = []
    if related is not None:
        raw_relations = []
        if hasattr(related, "relations"):
            raw_relations = related.relations or []
        elif isinstance(related, list):
            raw_relations = related
        relations = [_serialise_relation(r) for r in raw_relations]

    return {
        "entity": _serialise_entity(entity),
        "relations": relations,
    }


# ---------------------------------------------------------------------------
# Add fact
# ---------------------------------------------------------------------------

@router.post("/fact")
async def add_fact(
    req: FactRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Store a temporal subject-predicate-object triple in the knowledge graph."""
    await check_namespace_access(key_entry, req.namespace)
    logger.debug(
        "add_fact | ns=%s user=%s %r -[%r]-> %r valid_until=%s",
        req.namespace,
        user_id,
        req.subject,
        req.predicate,
        req.object,
        req.valid_until,
    )
    try:
        fact = await client.add_fact(
            subject=req.subject,
            predicate=req.predicate,
            object=req.object,
            namespace=req.namespace,
            valid_until=req.valid_until,
        )
    except Exception as exc:
        logger.exception("add_fact failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if isinstance(fact, dict):
        return _dt_to_iso(fact)

    return _dt_to_iso({
        "id": str(getattr(fact, "id", "")),
        "subject": str(getattr(fact, "subject", req.subject)),
        "predicate": str(getattr(fact, "predicate", req.predicate)),
        "object": str(getattr(fact, "object", req.object)),
        "namespace": str(getattr(fact, "namespace", req.namespace)),
        "valid_from": getattr(fact, "valid_from", datetime.now(timezone.utc)),
        "valid_until": getattr(fact, "valid_until", req.valid_until),
        "source_memory_id": getattr(fact, "source_memory_id", None),
    })
