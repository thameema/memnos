"""
engram.search — Search facade delegating to ArcadeDB hybrid search.

ArcadeDB handles both vector similarity (HNSW) and graph traversal in a
single SQL query, so there is no separate merging step needed. This module
provides a thin compatibility shim so callers that use HybridSearch directly
continue to work unchanged.

Search modes supported:
  "vector"   — HNSW vector similarity only (pure semantic)
  "graph"    — keyword/entity match via MENTIONS edges
  "hybrid"   — 0.7 * semantic + 0.3 * recency (default, recommended)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from engram.models import SearchResult

if TYPE_CHECKING:
    from engram.storage.arcadedb_client import ArcadeDBClient
    from engram.vector.embedder import Embedder

logger = logging.getLogger(__name__)


class HybridSearch:
    """Thin wrapper around ArcadeDBClient search for backward compatibility."""

    def __init__(
        self,
        arcadedb: "ArcadeDBClient",
        embedder: "Embedder",
    ) -> None:
        self._arcadedb = arcadedb
        self._embedder = embedder

    async def search(
        self,
        query: str,
        namespace: str,
        top_k: int = 10,
        mode: str = "hybrid",
        include_historical: bool = False,
    ) -> list[SearchResult]:
        """Search for memories matching *query* within *namespace*.

        All three modes embed the query and call ArcadeDB.
        - "vector" / "hybrid": uses HNSW @vectorNeighbors with recency weighting.
        - "graph": uses entity-based graph traversal via MENTIONS edges.
        """
        mode = mode.lower()
        if mode not in ("vector", "graph", "hybrid"):
            raise ValueError(
                f"Unknown search mode {mode!r}. Supported: 'vector', 'graph', 'hybrid'."
            )

        logger.debug(
            "search: mode=%s query=%r namespace=%s top_k=%d", mode, query, namespace, top_k
        )

        vector = await self._embedder.embed(query)

        if mode == "graph":
            return await self._arcadedb.graph_search(
                query=query, namespace=namespace, top_k=top_k,
                include_superseded=include_historical,
            )

        # Both "vector" and "hybrid" use the same HNSW + recency path;
        # "hybrid" is the default and preferred mode.
        return await self._arcadedb.vector_search(
            embedding=vector,
            namespace=namespace,
            top_k=top_k,
            include_superseded=include_historical,
        )
