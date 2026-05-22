"""
engram_api.routers.viz — Graph visualization and statistics endpoints.

Endpoints
---------
GET  /graph/stats       — node/edge counts, tag distribution, recent activity
GET  /graph/visualize   — nodes + edges suitable for a force-directed graph UI
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from engram_api.auth import (
    get_client,
    require_api_key,
    require_api_key_entry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/graph", tags=["visualization"])

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NS_PARAM = Query("personal:default", description="Namespace prefix, or 'all' for every namespace")
_LIMIT_PARAM = Query(150, ge=1, le=500, description="Maximum graph rows to return (max 500)")


async def _cypher_scalar(client, cypher: str, ns: str, key: str, default: int = 0) -> int:
    """
    Execute a Cypher query and return a single integer scalar.

    Returns *default* if the query fails or the result is empty.
    """
    try:
        rows = await client.query_graph(cypher, ns, {"ns": ns})
        if rows:
            value = rows[0].get(key, default)
            return int(value) if value is not None else default
    except Exception as exc:
        logger.warning("Cypher scalar query failed (key=%r): %s", key, exc)
    return default


async def _cypher_rows(
    client, cypher: str, ns: str, params: dict[str, Any] | None = None
) -> list[dict]:
    """
    Execute a Cypher query and return all result rows.

    Returns an empty list if the query fails.
    """
    try:
        rows = await client.query_graph(cypher, ns, params or {"ns": ns})
        return rows if rows else []
    except Exception as exc:
        logger.warning("Cypher rows query failed: %s", exc)
    return []


# ---------------------------------------------------------------------------
# GET /graph/stats
# ---------------------------------------------------------------------------

@router.get("/stats")
async def graph_stats(
    namespace: str = _NS_PARAM,
    user_id: str = Depends(require_api_key),
    _key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """
    Return aggregate statistics for the knowledge graph.

    All Cypher queries degrade gracefully: a Neo4j outage returns zeroes rather
    than a 500 error.  The ``memory_count`` and ``recent_activity`` fields are
    derived from the vector-search index (capped at 100 results).
    """
    logger.debug("graph_stats | ns=%s user=%s", namespace, user_id)

    ns = namespace  # alias for query params

    # ------------------------------------------------------------------
    # Run all Cypher queries (failures return safe defaults independently)
    # ------------------------------------------------------------------

    node_count_cypher = (
        "MATCH (n) "
        "WHERE $ns = 'all' OR n.namespace STARTS WITH $ns "
        "RETURN count(n) as total"
    )

    edge_count_cypher = (
        "MATCH (n)-[r]->(m) "
        "WHERE $ns = 'all' OR n.namespace STARTS WITH $ns "
        "RETURN count(r) as total"
    )

    ns_dist_cypher = (
        "MATCH (n) "
        "WHERE n.namespace IS NOT NULL "
        "AND ($ns = 'all' OR n.namespace STARTS WITH $ns) "
        "RETURN n.namespace as namespace, count(n) as count "
        "ORDER BY count DESC LIMIT 20"
    )

    top_tags_cypher = (
        "MATCH (n) "
        "WHERE n.tags IS NOT NULL "
        "AND ($ns = 'all' OR n.namespace STARTS WITH $ns) "
        "UNWIND n.tags as tag "
        "RETURN tag, count(tag) as count "
        "ORDER BY count DESC LIMIT 20"
    )

    node_count = await _cypher_scalar(client, node_count_cypher, ns, "total")
    edge_count = await _cypher_scalar(client, edge_count_cypher, ns, "total")
    ns_dist_rows = await _cypher_rows(client, ns_dist_cypher, ns)
    top_tags_rows = await _cypher_rows(client, top_tags_cypher, ns)

    # ------------------------------------------------------------------
    # Memory count + recent activity from vector search
    # ------------------------------------------------------------------
    memory_count = 0
    recent_activity: list[dict] = []

    try:
        results = await client.search("", ns, top_k=100, mode="vector")
        if results:
            memory_count = len(results)
            recent_activity = _build_date_histogram(results)
    except Exception as exc:
        logger.warning("Vector search for stats failed: %s", exc)

    # ------------------------------------------------------------------
    # Shape the response
    # ------------------------------------------------------------------
    namespace_distribution = [
        {"namespace": str(row.get("namespace", "")), "count": int(row.get("count", 0))}
        for row in ns_dist_rows
        if row.get("namespace")
    ]

    top_tags = [
        {"tag": str(row.get("tag", "")), "count": int(row.get("count", 0))}
        for row in top_tags_rows
        if row.get("tag")
    ]

    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "memory_count": memory_count,
        "namespace_distribution": namespace_distribution,
        "top_tags": top_tags,
        "recent_activity": recent_activity,
    }


def _build_date_histogram(results: list) -> list[dict]:
    """
    Build a 30-day date histogram from search results.

    Each result is expected to expose a ``memory`` attribute whose
    ``created_at`` field is a timezone-aware ``datetime``.  Rows with
    missing or unparseable dates are silently skipped.
    """
    cutoff: date = datetime.now(timezone.utc).date() - timedelta(days=29)
    counts: dict[date, int] = defaultdict(int)

    for result in results:
        try:
            memory = getattr(result, "memory", None)
            if memory is None:
                continue
            created_at = getattr(memory, "created_at", None)
            if created_at is None:
                continue
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            entry_date: date = created_at.date() if isinstance(created_at, datetime) else created_at
            if entry_date >= cutoff:
                counts[entry_date] += 1
        except Exception:
            continue

    # Return as a sorted list of {"date": "YYYY-MM-DD", "count": N}
    return [
        {"date": d.isoformat(), "count": counts[d]}
        for d in sorted(counts)
    ]


# ---------------------------------------------------------------------------
# GET /graph/visualize
# ---------------------------------------------------------------------------

_VISUALIZE_CYPHER = """\
MATCH (n)
WHERE $ns = 'all' OR n.namespace STARTS WITH $ns
WITH n LIMIT $limit
OPTIONAL MATCH (n)-[r]->(m)
WHERE $ns = 'all' OR m.namespace STARTS WITH $ns
RETURN
  toString(id(n)) AS source_id,
  COALESCE(n.name, n.summary, LEFT(n.content, 80), '') AS source_label,
  COALESCE(n.namespace, '') AS source_ns,
  labels(n)[0] AS source_type,
  COALESCE(n.created_at, '') AS source_created_at,
  type(r) AS rel_type,
  toString(id(m)) AS target_id,
  COALESCE(m.name, m.summary, LEFT(m.content, 80), '') AS target_label,
  COALESCE(m.namespace, '') AS target_ns\
"""


@router.get("/visualize")
async def graph_visualize(
    namespace: str = _NS_PARAM,
    limit: int = _LIMIT_PARAM,
    user_id: str = Depends(require_api_key),
    _key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """
    Return a node-and-edge payload suitable for client-side graph rendering.

    Nodes are collected from both ends of every returned relationship, so the
    result is self-consistent.  The ``truncated`` flag is set when the query
    reached the requested ``limit``, indicating that the caller may want to
    reduce scope via a more specific namespace.

    A Cypher failure returns an empty graph rather than a 500 error.
    """
    logger.debug("graph_visualize | ns=%s limit=%d user=%s", namespace, limit, user_id)

    ns_param = namespace  # pass through verbatim ("all" is valid)
    params: dict[str, Any] = {"ns": ns_param, "limit": limit}

    try:
        rows = await client.query_graph(_VISUALIZE_CYPHER, namespace, params)
    except Exception as exc:
        logger.warning("Visualize Cypher query failed: %s", exc)
        return {"nodes": [], "edges": [], "truncated": False}

    if not rows:
        return {"nodes": [], "edges": [], "truncated": False}

    # ------------------------------------------------------------------
    # Assemble nodes and edges; deduplicate by id
    # ------------------------------------------------------------------
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for row in rows:
        source_id = row.get("source_id")
        if not source_id:
            continue

        # Source node
        if source_id not in nodes:
            created_raw = row.get("source_created_at", "")
            nodes[source_id] = {
                "id": source_id,
                "label": str(row.get("source_label", "") or ""),
                "namespace": str(row.get("source_ns", "") or ""),
                "type": str(row.get("source_type", "") or ""),
                "created_at": _normalise_datetime(created_raw),
            }

        # Target node (only present when a relationship exists)
        rel_type = row.get("rel_type")
        target_id = row.get("target_id")

        if rel_type is not None and target_id:
            if target_id not in nodes:
                nodes[target_id] = {
                    "id": target_id,
                    "label": str(row.get("target_label", "") or ""),
                    "namespace": str(row.get("target_ns", "") or ""),
                    "type": "",
                    "created_at": "",
                }

            edges.append(
                {
                    "source": source_id,
                    "target": target_id,
                    "type": str(rel_type),
                    "weight": 1.0,
                }
            )

    truncated = len(rows) >= limit

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "truncated": truncated,
    }


def _normalise_datetime(value: Any) -> str:
    """
    Coerce a Neo4j datetime value to an ISO-8601 string.

    Handles ``datetime`` objects, ISO strings, and empty / None values
    without raising.
    """
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    # Neo4j driver may return neo4j.time.DateTime — convert via str()
    try:
        return str(value)
    except Exception:
        return ""
