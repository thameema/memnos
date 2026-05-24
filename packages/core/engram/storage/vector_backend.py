"""
engram.storage.vector_backend — Abstract vector backend interface and factory.

Engram supports two vector backends:
- ArcadeDB (default): embeddings stored in ArcadeDB, Python-layer cosine similarity
- Qdrant: embeddings stored in Qdrant, HNSW-accelerated ANN search

Select the backend via ENGRAM_VECTOR_BACKEND environment variable:
  ENGRAM_VECTOR_BACKEND=qdrant   → use QdrantVectorBackend
  (unset / anything else)       → use ArcadeDB built-in (no external backend)

When ENGRAM_VECTOR_BACKEND=qdrant, also set:
  ENGRAM_QDRANT_URL        default: http://localhost:6333
  ENGRAM_QDRANT_API_KEY    optional, for Qdrant Cloud
  ENGRAM_QDRANT_COLLECTION default: engram_memories
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class VectorBackend(ABC):
    """
    Abstract vector storage and search backend.

    The engram client calls these methods; concrete implementations
    handle the storage details.
    """

    @abstractmethod
    async def upsert(
        self,
        memory_id: str,
        embedding: list[float],
        namespace: str,
        memory_type: str = "fact",
    ) -> None:
        """Insert or update a vector point for *memory_id*."""

    @abstractmethod
    async def search(
        self,
        embedding: list[float],
        namespace: str,
        top_k: int = 10,
        include_superseded: bool = False,
    ) -> list[tuple[str, float]]:
        """
        Return up to *top_k* (memory_id, score) pairs for the nearest
        neighbours of *embedding* in *namespace*, sorted by score descending.

        *include_superseded* — when False (default), exclude superseded memories.
        """

    @abstractmethod
    async def delete(self, memory_id: str) -> None:
        """Remove the vector point for *memory_id* (best-effort)."""

    @abstractmethod
    async def mark_superseded(self, memory_id: str) -> None:
        """Flag a point as superseded so it is excluded from future searches."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying connections."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_vector_backend(vector_dim: int) -> "VectorBackend | None":
    """
    Return a VectorBackend based on ENGRAM_VECTOR_BACKEND, or None to
    use the built-in ArcadeDB Python-layer cosine similarity.
    """
    backend_type = os.environ.get("ENGRAM_VECTOR_BACKEND", "").lower().strip()
    if backend_type == "qdrant":
        from engram.storage.qdrant_backend import QdrantVectorBackend  # noqa: PLC0415
        url = os.environ.get("ENGRAM_QDRANT_URL", "http://localhost:6333")
        api_key = os.environ.get("ENGRAM_QDRANT_API_KEY") or None
        collection = os.environ.get("ENGRAM_QDRANT_COLLECTION", "engram_memories")
        logger.info(
            "Vector backend: Qdrant at %s (collection=%s, dim=%d)",
            url, collection, vector_dim,
        )
        return QdrantVectorBackend(
            url=url,
            api_key=api_key,
            collection=collection,
            vector_dim=vector_dim,
        )

    if backend_type and backend_type != "arcadedb":
        logger.warning(
            "Unknown ENGRAM_VECTOR_BACKEND=%r — falling back to ArcadeDB built-in",
            backend_type,
        )
    return None  # None → caller uses ArcadeDB built-in
