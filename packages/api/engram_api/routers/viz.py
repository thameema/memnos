"""
engram_api.routers.viz — Graph visualization and statistics endpoints.

Endpoints
---------
GET  /graph/stats       — node/edge counts, namespace distribution, recent activity
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

_NS_PARAM = Query("all", description="Namespace prefix, or 'all' for every namespace")
_LIMIT_PARAM = Query(150, ge=1, le=500, description="Maximum graph rows to return (max 500)")


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
    """Return aggregate statistics for the knowledge graph.

    All queries degrade gracefully — an ArcadeDB hiccup returns zeroes
    rather than a 500 error.
    """
    logger.debug("graph_stats | ns=%s user=%s", namespace, user_id)

    try:
        stat_data = await client.stats(namespace)
    except Exception as exc:
        logger.warning("client.stats() failed: %s", exc)
        stat_data = {"memories": 0, "edges": 0, "namespace_distribution": {}}

    memory_count = stat_data.get("memories", 0)
    edge_count = stat_data.get("edges", 0)
    ns_dist_raw = stat_data.get("namespace_distribution", {})

    namespace_distribution = [
        {"namespace": k, "count": v}
        for k, v in sorted(ns_dist_raw.items(), key=lambda x: -x[1])
    ]

    # Recent activity — sample search for date histogram
    recent_activity: list[dict] = []
    try:
        sample = await client.search("recent activity", namespace, top_k=200) or []
        if sample:
            recent_activity = _build_date_histogram(sample)
    except Exception as exc:
        logger.warning("Search for recent_activity failed: %s", exc)

    # Top tags — query ArcadeDB directly
    top_tags: list[dict] = []
    try:
        tag_rows = await client.query_graph(
            "SELECT tags, count(*) AS cnt FROM Memory "
            "WHERE (namespace = :namespace OR namespace LIKE :ns_prefix) "
            "AND tags IS NOT NULL "
            "GROUP BY tags ORDER BY cnt DESC LIMIT 20",
            namespace,
            {"ns_prefix": f"{namespace}:%"},
        )
        tag_counts: dict[str, int] = defaultdict(int)
        for row in tag_rows:
            tags = row.get("tags") or []
            if isinstance(tags, list):
                for tag in tags:
                    tag_counts[str(tag)] += int(row.get("cnt", 1))
        top_tags = [
            {"tag": t, "count": c}
            for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])[:20]
        ]
    except Exception as exc:
        logger.debug("Tag query failed (non-fatal): %s", exc)

    return {
        "node_count": memory_count,
        "edge_count": edge_count,
        "memory_count": memory_count,
        "namespace_distribution": namespace_distribution,
        "top_tags": top_tags,
        "recent_activity": recent_activity,
    }


def _build_date_histogram(results: list) -> list[dict]:
    """Build a 30-day date histogram from search results."""
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

    return [
        {"date": d.isoformat(), "count": counts[d]}
        for d in sorted(counts)
    ]


# ---------------------------------------------------------------------------
# GET /graph/visualize
# ---------------------------------------------------------------------------

@router.get("/visualize")
async def graph_visualize(
    namespace: str = _NS_PARAM,
    limit: int = _LIMIT_PARAM,
    user_id: str = Depends(require_api_key),
    _key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Return a node-and-edge payload suitable for client-side graph rendering.

    Nodes include Memory vertices; edges are MENTIONS connections to Entity
    vertices.  The ``truncated`` flag is set when the result was capped at
    ``limit``.
    """
    logger.debug("graph_visualize | ns=%s limit=%d user=%s", namespace, limit, user_id)

    try:
        data = await client.visualize(namespace=namespace, limit=limit)
        return data
    except Exception as exc:
        logger.warning("visualize failed: %s", exc)
        return {"nodes": [], "edges": [], "truncated": False}
