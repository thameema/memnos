"""
engram_mcp.tools.graph — MCP tool handlers for knowledge-graph operations.

Handlers
--------
handle_graph_query  : run a read-only Cypher query
handle_get_entity   : fetch an entity and its immediate relations
handle_get_related  : return only the adjacency-list for an entity
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _dt_to_iso(value: Any) -> Any:
    """Recursively convert datetime objects to ISO-8601 strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _dt_to_iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dt_to_iso(v) for v in value]
    return value


def _serialise_entity(entity) -> dict:
    """Convert an Entity model (or any object with the right attrs) to a dict."""
    if entity is None:
        return {}
    if isinstance(entity, dict):
        return _dt_to_iso(entity)
    return _dt_to_iso(
        {
            "id": str(getattr(entity, "id", "")),
            "name": str(getattr(entity, "name", "")),
            "entity_type": str(getattr(entity, "entity_type", "")),
            "namespace": str(getattr(entity, "namespace", "")),
            "attributes": dict(getattr(entity, "attributes", {})),
            "created_at": getattr(entity, "created_at", None),
            "valid_until": getattr(entity, "valid_until", None),
        }
    )


def _serialise_relation(relation) -> dict:
    """Convert a Relation model to a dict."""
    if relation is None:
        return {}
    if isinstance(relation, dict):
        return _dt_to_iso(relation)
    return _dt_to_iso(
        {
            "id": str(getattr(relation, "id", "")),
            "source_entity_id": str(getattr(relation, "source_entity_id", "")),
            "target_entity_id": str(getattr(relation, "target_entity_id", "")),
            "relation_type": str(getattr(relation, "relation_type", "")),
            "namespace": str(getattr(relation, "namespace", "")),
            "weight": float(getattr(relation, "weight", 1.0)),
            "created_at": getattr(relation, "created_at", None),
            "valid_until": getattr(relation, "valid_until", None),
            "attributes": dict(getattr(relation, "attributes", {})),
        }
    )


# ---------------------------------------------------------------------------
# Graph query (Cypher)
# ---------------------------------------------------------------------------

async def handle_graph_query(
    client,
    cypher: str,
    namespace: str,
    params: dict | None = None,
) -> dict:
    """
    Execute a read-only Cypher query against the knowledge graph.

    Returns
    -------
    {"rows": [...], "count": N}
    """
    logger.debug("graph_query | ns=%s cypher=%r params=%s", namespace, cypher[:120], params)

    results = await client.query_graph(cypher, namespace, params or {})

    if results is None:
        results = []

    serialised = [_dt_to_iso(r) if isinstance(r, dict) else r for r in results]

    return {"rows": serialised, "count": len(serialised)}


# ---------------------------------------------------------------------------
# Get entity (entity + relations)
# ---------------------------------------------------------------------------

async def handle_get_entity(
    client,
    name: str,
    namespace: str,
    depth: int = 2,
) -> dict:
    """
    Fetch a named entity and its relationships up to *depth* hops.

    Returns
    -------
    {"entity": {...}, "relations": [...]}
    """
    logger.debug("get_entity | ns=%s name=%r depth=%d", namespace, name, depth)

    entity = await client.get_entity(name, namespace)
    related = await client.get_related(name, namespace, depth)

    relations: list = []
    if related is not None:
        # get_related may return a Graph object or a list of Relation objects
        if hasattr(related, "relations"):
            relations = [_serialise_relation(r) for r in (related.relations or [])]
        elif isinstance(related, list):
            relations = [_serialise_relation(r) for r in related]

    return {
        "entity": _serialise_entity(entity),
        "relations": relations,
    }


# ---------------------------------------------------------------------------
# Get related (adjacency list only)
# ---------------------------------------------------------------------------

async def handle_get_related(
    client,
    entity_name: str,
    namespace: str,
    depth: int = 2,
) -> dict:
    """
    Return the adjacency list for *entity_name* up to *depth* hops.

    Returns
    -------
    {"entity_name": str, "adjacency": [{"target", "relation_type", "weight"}], "total": N}
    """
    logger.debug("get_related | ns=%s entity=%r depth=%d", namespace, entity_name, depth)

    related = await client.get_related(entity_name, namespace, depth)

    relations: list = []
    if related is not None:
        if hasattr(related, "relations"):
            raw_relations = related.relations or []
        elif isinstance(related, list):
            raw_relations = related
        else:
            raw_relations = []

        for r in raw_relations:
            relations.append(
                {
                    "source_entity_id": str(getattr(r, "source_entity_id", "")),
                    "target_entity_id": str(getattr(r, "target_entity_id", "")),
                    "relation_type": str(getattr(r, "relation_type", "")),
                    "weight": float(getattr(r, "weight", 1.0)),
                }
            )

    return {
        "entity_name": entity_name,
        "adjacency": relations,
        "total": len(relations),
    }
