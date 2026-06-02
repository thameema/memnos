"""
memnos.storage.vector_backend — Abstract vector backend interface and factory.

Memnos supports two vector backends:
- ArcadeDB (default): embeddings stored in ArcadeDB, Python-layer cosine similarity
- Qdrant: embeddings stored in Qdrant, HNSW-accelerated ANN search

Select the backend via MEMNOS_VECTOR_BACKEND environment variable:
  MEMNOS_VECTOR_BACKEND=qdrant   → use QdrantVectorBackend
  (unset / anything else)       → use ArcadeDB built-in (no external backend)

When MEMNOS_VECTOR_BACKEND=qdrant, also set:
  QDRANT_URL               default: http://localhost:6333   (checked first)
  MEMNOS_QDRANT_URL        fallback alias for QDRANT_URL
  QDRANT_API_KEY           optional, for Qdrant Cloud       (checked first)
  MEMNOS_QDRANT_API_KEY    fallback alias for QDRANT_API_KEY
  MEMNOS_QDRANT_COLLECTION default: memnos_memories
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

    The memnos client calls these methods; concrete implementations
    handle the storage details.
    """

    @abstractmethod
    async def upsert(
        self,
        memory_id: str,
        embedding: list[float],
        namespace: str,
        memory_type: str = "fact",
        text: str | None = None,
    ) -> None:
        """Insert or update a vector point for *memory_id*.

        *text* — original content string used to generate a BM25 sparse vector
        alongside the dense embedding.  Implementations that do not support
        sparse vectors should ignore this parameter.
        """

    @abstractmethod
    async def search(
        self,
        embedding: list[float],
        namespace: str,
        top_k: int = 10,
        include_superseded: bool = False,
        text: str | None = None,
    ) -> list[tuple[str, float]]:
        """
        Return up to *top_k* (memory_id, score) pairs for the nearest
        neighbours of *embedding* in *namespace*, sorted by score descending.

        *include_superseded* — when False (default), exclude superseded memories.
        *text* — original query string used to generate a BM25 sparse vector for
        hybrid RRF search.  Implementations that do not support sparse vectors
        should fall back to dense-only search.
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
    Return a VectorBackend based on MEMNOS_VECTOR_BACKEND, or None to
    use the built-in ArcadeDB Python-layer cosine similarity.
    """
    backend_type = os.environ.get("MEMNOS_VECTOR_BACKEND", "").lower().strip()
    if backend_type == "qdrant":
        from memnos.storage.qdrant_backend import QdrantVectorBackend  # noqa: PLC0415
        url = (
            os.environ.get("QDRANT_URL")
            or os.environ.get("MEMNOS_QDRANT_URL")
            or "http://localhost:6333"
        )
        api_key = (
            os.environ.get("QDRANT_API_KEY")
            or os.environ.get("MEMNOS_QDRANT_API_KEY")
            or None
        )
        collection = os.environ.get("MEMNOS_QDRANT_COLLECTION", "memnos_memories")
        logger.info(
            "Vector backend: Qdrant at %s (collection=%s, dim=%d)",
            url, collection, vector_dim,
        )
        backend = QdrantVectorBackend(
            url=url,
            api_key=api_key,
            collection=collection,
            vector_dim=vector_dim,
        )
        # Strict mode: probe Qdrant reachability synchronously at construction
        # time and refuse to start if it fails. Without strict mode, the
        # backend silently degrades (the client catches upsert/search errors).
        if os.environ.get("MEMNOS_VECTOR_BACKEND_STRICT", "").lower() in ("1", "true", "yes"):
            import httpx
            try:
                probe = httpx.get(
                    f"{url.rstrip('/')}/collections",
                    headers={"api-key": api_key} if api_key else {},
                    timeout=3.0,
                )
                if probe.status_code >= 400:
                    raise RuntimeError(
                        f"Qdrant probe failed: HTTP {probe.status_code} at {url}"
                    )
            except Exception as exc:
                raise RuntimeError(
                    f"MEMNOS_VECTOR_BACKEND_STRICT=true and Qdrant unreachable "
                    f"at {url}: {exc}"
                ) from exc
        return backend

    if backend_type and backend_type != "arcadedb":
        msg = (
            f"Unknown MEMNOS_VECTOR_BACKEND={backend_type!r} — "
            f"falling back to ArcadeDB built-in"
        )
        if os.environ.get("MEMNOS_VECTOR_BACKEND_STRICT", "").lower() in ("1", "true", "yes"):
            # Strict mode: caller wants a hard fail rather than silent fallback.
            raise RuntimeError(msg)
        logger.warning(msg)
    return None  # None → caller uses ArcadeDB built-in
