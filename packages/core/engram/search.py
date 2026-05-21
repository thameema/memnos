"""
engram.search — Hybrid search combining Qdrant (vector) and Graphiti (graph).

Three modes:
  "vector"  — Qdrant only
  "graph"   — Graphiti episode search only
  "hybrid"  — both; scores averaged per memory_id, top_k returned
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from engram.models import MemoryEntry, SearchResult

if TYPE_CHECKING:
    from engram.graph.graphiti_client import EngramGraphitiClient
    from engram.vector.embedder import Embedder
    from engram.vector.qdrant_client import EngramQdrantClient

logger = logging.getLogger(__name__)


class HybridSearch:
    """Orchestrate vector and/or graph searches and merge results."""

    def __init__(
        self,
        qdrant: "EngramQdrantClient",
        graphiti: "EngramGraphitiClient",
        embedder: "Embedder",
    ) -> None:
        self._qdrant = qdrant
        self._graphiti = graphiti
        self._embedder = embedder

    async def search(
        self,
        query: str,
        namespace: str,
        top_k: int = 10,
        mode: str = "hybrid",
    ) -> list[SearchResult]:
        """Search for memories matching *query* within *namespace*.

        Parameters
        ----------
        query:
            Natural-language search string.
        namespace:
            Restrict results to this namespace.
        top_k:
            Maximum number of results to return.
        mode:
            ``"vector"``, ``"graph"``, or ``"hybrid"`` (default).

        Returns
        -------
        list[SearchResult]
            Results ordered by descending score.
        """
        mode = mode.lower()
        if mode == "vector":
            return await self._vector_search(query, namespace, top_k)
        if mode == "graph":
            return await self._graph_search(query, namespace, top_k)
        if mode == "hybrid":
            return await self._hybrid_search(query, namespace, top_k)
        raise ValueError(
            f"Unknown search mode {mode!r}. Supported: 'vector', 'graph', 'hybrid'."
        )

    # ------------------------------------------------------------------
    # Vector search
    # ------------------------------------------------------------------

    async def _vector_search(
        self, query: str, namespace: str, top_k: int
    ) -> list[SearchResult]:
        logger.debug("Vector search: query=%r namespace=%s top_k=%d", query, namespace, top_k)
        query_vec = await self._embedder.embed(query)
        raw = await self._qdrant.search(query_vec, namespace=namespace, top_k=top_k)
        results: list[SearchResult] = []
        for point_id, score, payload in raw:
            memory = _payload_to_memory(point_id, payload, namespace)
            results.append(SearchResult(memory=memory, score=score, source="vector"))
        return results

    # ------------------------------------------------------------------
    # Graph search
    # ------------------------------------------------------------------

    async def _graph_search(
        self, query: str, namespace: str, top_k: int
    ) -> list[SearchResult]:
        logger.debug("Graph search: query=%r namespace=%s top_k=%d", query, namespace, top_k)
        memories = await self._graphiti.search_episodes(query, namespace=namespace, top_k=top_k)
        # Graphiti doesn't return scores; assign uniform score of 1.0
        return [
            SearchResult(memory=m, score=1.0, source="graph")
            for m in memories
        ]

    # ------------------------------------------------------------------
    # Hybrid search — merge vector + graph results
    # ------------------------------------------------------------------

    async def _hybrid_search(
        self, query: str, namespace: str, top_k: int
    ) -> list[SearchResult]:
        logger.debug("Hybrid search: query=%r namespace=%s top_k=%d", query, namespace, top_k)

        # Run both searches (fetch more than top_k so merging is meaningful)
        fetch_k = max(top_k * 2, 20)
        query_vec = await self._embedder.embed(query)

        vector_raw = await self._qdrant.search(
            query_vec, namespace=namespace, top_k=fetch_k
        )
        graph_memories = await self._graphiti.search_episodes(
            query, namespace=namespace, top_k=fetch_k
        )

        # Build a score accumulator keyed by memory_id
        # { memory_id: {"scores": [...], "memory": MemoryEntry, "sources": set} }
        accumulator: dict[str, dict] = {}

        for point_id, score, payload in vector_raw:
            memory = _payload_to_memory(point_id, payload, namespace)
            mid = memory.id
            if mid not in accumulator:
                accumulator[mid] = {"scores": [], "memory": memory, "sources": set()}
            accumulator[mid]["scores"].append(score)
            accumulator[mid]["sources"].add("vector")

        for memory in graph_memories:
            mid = memory.id
            if mid not in accumulator:
                accumulator[mid] = {"scores": [], "memory": memory, "sources": set()}
            # Assign a baseline graph score of 1.0 (Graphiti doesn't expose numeric scores)
            accumulator[mid]["scores"].append(1.0)
            accumulator[mid]["sources"].add("graph")

        # Average the scores and determine the source label
        merged: list[SearchResult] = []
        for mid, data in accumulator.items():
            avg_score = sum(data["scores"]) / len(data["scores"])
            sources = data["sources"]
            if len(sources) > 1:
                source_label = "hybrid"
            else:
                source_label = next(iter(sources))
            merged.append(
                SearchResult(memory=data["memory"], score=avg_score, source=source_label)
            )

        merged.sort(key=lambda r: r.score, reverse=True)
        return merged[:top_k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload_to_memory(point_id: str, payload: dict, default_namespace: str) -> MemoryEntry:
    """Reconstruct a MemoryEntry from a Qdrant point payload."""
    from datetime import datetime, timezone

    def _parse_dt(val) -> datetime:
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    return MemoryEntry(
        id=payload.get("memory_id", point_id),
        content=payload.get("content", ""),
        namespace=payload.get("namespace", default_namespace),
        created_at=_parse_dt(payload.get("created_at")),
        updated_at=_parse_dt(payload.get("updated_at")),
        tags=payload.get("tags", []),
        source=payload.get("source", "agent"),
        embedding_id=point_id,
        graph_node_id=payload.get("graph_node_id"),
        metadata=payload.get("metadata", {}),
    )
